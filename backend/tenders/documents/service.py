"""Fetch the attachment PDFs of open, kept tenders and store their redacted text.

Run by the worker's daily task after ingest (management command fetch_documents), which
passes the CSVs that ingest just downloaded. Their contact columns give each notice's
redaction hints (the contracting officer's name, email, phone), which redact() needs to
catch names: without them it caught none of the 5 names in 3 real CanadaBuys PDFs
(2026-11-18). The hints are read here, used and dropped; they are never stored. A tender
with no row in those CSVs is not fetched this run.

Per tender, every attachment URL gets one TenderDocument row once looked at (fetched,
skipped or failed). A tender whose URL set equals its stored rows' URLs, with nothing left
to retry, is not touched. Rows for URLs that left the set are deleted.

Retries: a transient failure (RETRYABLE: download timeout, HTTP or network error, parse
timeout) is tried again on later runs, one attempt per run, up to MAX_ATTEMPTS in all
(TenderDocument.attempts). Everything else is final for that URL: an OK document, a
skipped host or the per-tender limit, and the permanent rejections (too big, too many
pages, not a PDF, a redirect, a parse error, which give the same answer every time).

Limits: 10 MB and 60 pages per file, 20 s per download, 15 s per parse, 5 downloads per
tender, 40 downloads and 300 s per run. A download cut off by a run limit is not recorded,
so the next run picks it up.
"""

from __future__ import annotations

import csv
import hashlib
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path

from django.utils import timezone

from tenders.ingest import filters
from tenders.ingest.injection import scan_text
from tenders.ingest.pipeline import REF_HEADER
from tenders.ingest.redact import ContactHints, redact
from tenders.ingest.schema import normalize_text
from tenders.models import Tender, TenderDocument
from tenders.pages import is_open

from .fetch import FetchFailed, default_opener, download_pdf, is_allowed_url
from .parse import ParseFailed, parse_pdf

Status = TenderDocument.Status
MAX_ATTEMPTS = 3
RETRYABLE = frozenset({Status.TIMEOUT, Status.HTTP_ERROR, Status.NETWORK_ERROR,
                       Status.PARSE_TIMEOUT})


def needs_retry(doc: TenderDocument) -> bool:
    return doc.status in RETRYABLE and doc.attempts < MAX_ATTEMPTS


def has_work(tender: Tender) -> bool:
    """New or removed URLs, or a stored document due for another attempt."""
    docs = list(tender.documents.all())
    return (set(tender.attachment_urls or []) != {d.url for d in docs}
            or any(needs_retry(d) for d in docs))


@dataclass(frozen=True)
class Limits:
    max_bytes: int = 10 * 1024 * 1024
    max_pages: int = 60
    download_timeout_s: float = 20.0
    parse_timeout_s: float = 15.0
    per_tender: int = 5
    per_run: int = 40
    run_budget_s: float = 300.0  # the worker's timeout is 900 s; the daily run takes ~40 s
    pause_s: float = 0.5  # between downloads, to be polite to CanadaBuys


@dataclass
class FetchSummary:
    statuses: Counter = field(default_factory=Counter)
    tenders_checked: int = 0
    unchanged: int = 0
    no_csv_row: int = 0
    downloads: int = 0  # every attempt, retries included (the per-run cap counts these)
    retries: int = 0
    deleted: int = 0
    stopped: str = ""  # why the run ended early, if it did


def candidate_tenders() -> list[Tender]:
    """Open now (as on the site) and kept by today's ingest filter."""
    qs = (Tender.objects.filter(is_open()).order_by("closing_date", "reference_number")
          .prefetch_related("documents"))
    return [t for t in qs if filters.is_kept(t)]


def contact_hints(csv_paths: Iterable[Path], refs: set[str]) -> dict[str, list[ContactHints]]:
    """Each wanted reference's distinct contact hints, from every row it has in the CSVs."""
    found: dict[str, dict[ContactHints, None]] = {}
    for path in csv_paths:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                ref = (row.get(REF_HEADER) or "").strip()
                if ref in refs:
                    found.setdefault(ref, {})[ContactHints.from_row(row)] = None
    return {ref: list(hints) for ref, hints in found.items()}


def clean_text(text: str, hints: list[ContactHints]) -> tuple[str, list[str]]:
    """Redacted with each of the notice's hint sets in turn, then scanned for injection."""
    clean = normalize_text(text)
    for h in hints or [ContactHints()]:
        clean, _ = redact(clean, h)
    return clean, scan_text(clean)


@dataclass
class _Run:
    limits: Limits
    opener: object
    clock: Callable[[], float]
    sleep: Callable[[float], None]
    now: datetime
    started: float
    summary: FetchSummary

    def limit_reached(self) -> str:
        if self.summary.downloads >= self.limits.per_run:
            return f"per-run download cap ({self.limits.per_run})"
        if self.clock() - self.started >= self.limits.run_budget_s:
            return f"per-run time budget ({self.limits.run_budget_s:.0f} s)"
        return ""

    def record(self, tender: Tender, url: str, status: str, *, attempts: int = 1,
               **fields) -> None:
        """Create the URL's row, or overwrite it on a retry (every field is set again)."""
        values = {"status": status, "attempts": attempts, "fetched_at": self.now,
                  "text": "", "pages": None, "sha256": "", "injection_patterns": [], **fields}
        TenderDocument.objects.update_or_create(tender=tender, url=url, defaults=values)
        self.summary.statuses[status] += 1

    def fetch_one(self, tender: Tender, url: str, hints: list[ContactHints],
                  attempts: int = 1) -> None:
        record = partial(self.record, tender, url, attempts=attempts)
        self.summary.downloads += 1
        lim = self.limits
        try:
            data = download_pdf(url, opener=self.opener, max_bytes=lim.max_bytes,
                                timeout_s=lim.download_timeout_s, clock=self.clock)
        except FetchFailed as e:
            record(e.status)
            return
        finally:
            self.sleep(lim.pause_s)
        digest = hashlib.sha256(data).hexdigest()
        try:
            pages, text = parse_pdf(data, max_pages=lim.max_pages,
                                    timeout_s=lim.parse_timeout_s)
        except ParseFailed as e:
            record(e.status, pages=e.pages, sha256=digest)
            return
        del data  # never stored
        clean, patterns = clean_text(text, hints)
        record(Status.OK, text=clean, pages=pages, sha256=digest, injection_patterns=patterns)

    def tender(self, tender: Tender, hints: list[ContactHints]) -> bool:
        """Process one tender's new URLs. False when a run limit stopped it."""
        urls = list(dict.fromkeys(tender.attachment_urls or []))
        stored = {d.url: d for d in tender.documents.all()}
        gone = set(stored) - set(urls)
        if gone:
            self.summary.deleted += TenderDocument.objects.filter(
                tender=tender, url__in=gone).delete()[0]
        allowed = [u for u in urls if is_allowed_url(u)]
        to_download = set(allowed[: self.limits.per_tender])
        for url in urls:
            doc = stored.get(url)
            if doc is not None:
                if not needs_retry(doc):
                    continue
                if reason := self.limit_reached():
                    self.summary.stopped = reason
                    return False
                self.summary.retries += 1
                self.fetch_one(tender, url, hints, attempts=doc.attempts + 1)
            elif url not in allowed:
                self.record(tender, url, Status.SKIPPED_HOST)
            elif url not in to_download:
                self.record(tender, url, Status.OVER_TENDER_LIMIT)
            elif reason := self.limit_reached():
                self.summary.stopped = reason
                return False
            else:
                self.fetch_one(tender, url, hints)
        return True


def fetch_documents(csv_paths: Iterable[Path], *, limits: Limits | None = None, opener=None,
                    clock: Callable[[], float] = time.monotonic,
                    sleep: Callable[[float], None] = time.sleep,
                    now: datetime | None = None) -> FetchSummary:
    summary = FetchSummary()
    run = _Run(limits or Limits(), opener or default_opener(), clock, sleep, now or timezone.now(),
               clock(), summary)
    pending = []
    for t in candidate_tenders():
        summary.tenders_checked += 1
        if has_work(t):
            pending.append(t)
        else:
            summary.unchanged += 1
    if not pending:
        return summary
    hints = contact_hints(csv_paths, {t.reference_number for t in pending})
    for t in pending:
        if t.reference_number not in hints:
            summary.no_csv_row += 1
            continue
        if not run.tender(t, hints[t.reference_number]):
            break
    return summary
