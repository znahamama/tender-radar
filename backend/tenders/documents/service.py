"""Fetch the attachment PDFs of open, kept tenders and store their redacted text.

Run by the worker's daily task after ingest (management command fetch_documents), which
passes the CSVs that ingest just downloaded. Their contact columns give each notice's
redaction hints (the contracting officer's name, email, phone), which redact() needs to
catch names: without them it caught none of the 5 names in 3 real CanadaBuys PDFs
(2026-11-18). The hints are read here, used and dropped; they are never stored. A tender
with no row in those CSVs is not fetched this run.

Per tender, every attachment URL gets one TenderDocument row once looked at (fetched,
skipped or failed). A tender whose URL set equals its stored rows' URLs, with nothing left
to retry or re-evaluate, is not touched. Rows for URLs that left the set are deleted.

Order and per-tender limit: allowed URLs are ranked main solicitation first (CanadaBuys'
"...EBSU000.PDF", or a file named as a solicitation), then amendments by number
("...EBSU001.PDF", ...), then other PDFs, then other file types, each group in CSV order.
The first 8 are downloaded; the rest are recorded as over_tender_limit, which is not final:
a later run downloads any of them that rank within the limit then (e.g. once it is raised).

Re-cleaning: each row records the redact() version that cleaned it. Every ok row cleaned by
an older version (REDACT_VERSION was bumped) is downloaded and parsed again and re-cleaned,
since the raw text is never kept: first those of the open, kept tenders above (as part of
their plan), then those of any other tender, whatever its status. They count toward the run
caps like any download. If that download now fails, the failure replaces the row and drops
its old text (an open, kept tender then retries it normally). A stale row whose tender has
no row in the CSVs is never re-cleaned without the CSV's contact hints (that could expose
names the old cleaning caught); see below.

Leaving the CSVs: every run deletes all document rows, and so all stored text, of tenders
that are in none of the CSVs any more (remove_documents_not_in_csvs), whatever their
status. If the CSVs list no reference at all (a broken download), nothing is deleted.

Retries: a transient failure (RETRYABLE: download timeout, HTTP or network error, parse
timeout) is tried again on later runs, one attempt per run, up to MAX_ATTEMPTS in all
(TenderDocument.attempts). Everything else is final for that URL: an OK document, a
skipped host or the per-tender limit, and the permanent rejections (too big, too many
pages, not a PDF, a redirect, a parse error, which give the same answer every time).

Limits: 10 MB and 60 pages per file, 20 s per download, 15 s per parse, 8 documents per
tender, 40 downloads and 300 s per run. A download cut off by a run limit is not recorded,
so the next run picks it up.
"""

from __future__ import annotations

import csv
import hashlib
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path
from urllib.parse import unquote, urlsplit

from django.db.models import Count, Sum
from django.db.models.functions import Length
from django.utils import timezone

from tenders.ingest import filters
from tenders.ingest.injection import scan_text
from tenders.ingest.pipeline import REF_HEADER
from tenders.ingest.redact import REDACT_VERSION, ContactHints, redact
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


_EBSU_RE = re.compile(r"\.EBSU(\d{3})\.PDF$", re.IGNORECASE)
_SOLICITATION_WORDS = frozenset(
    {"solicitation", "sollicitation", "demande", "invitation", "rfp", "rfq", "rfsa", "rfso",
     "itq", "itt", "dp", "ddp"})
_AMENDMENT_RE = re.compile(r"(?:amendment|amend|modification|modif|addendum)[\W_]*(\d+)",
                           re.IGNORECASE)


def _filename(url: str) -> str:
    return unquote(urlsplit(url).path.rsplit("/", 1)[-1])


def _rank(url: str) -> tuple[int, int]:
    """(group, number): 0 main solicitation, 1 amendment n, 2 other PDF, 3 other file type."""
    name = _filename(url)
    if m := _EBSU_RE.search(name):
        n = int(m.group(1))
        return (0, 0) if n == 0 else (1, n)
    stem, _, ext = name.rpartition(".")
    if m := _AMENDMENT_RE.search(stem):
        return (1, int(m.group(1)))
    if _SOLICITATION_WORDS & set(re.split(r"[\W_]+", stem.lower())):
        return (0, 0)
    return (2, 0) if ext.lower() == "pdf" else (3, 0)


def order_urls(urls: list[str]) -> list[str]:
    """Main solicitation first, then amendments in order, then the rest (stable)."""
    return sorted(urls, key=_rank)


@dataclass(frozen=True)
class Limits:
    max_bytes: int = 10 * 1024 * 1024
    max_pages: int = 60
    download_timeout_s: float = 20.0
    parse_timeout_s: float = 15.0
    per_tender: int = 8
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
    reevaluated: int = 0  # over_tender_limit rows downloaded because they now rank within it
    recleaned: int = 0  # ok rows downloaded again because an older redact() cleaned them
    deleted: int = 0
    stopped: str = ""  # why the run ended early, if it did
    # Over all stored documents after the run (not just this run's):
    ok_tenders: int = 0
    ok_documents: int = 0
    ok_chars: int = 0
    ok_stale: int = 0  # ok documents still cleaned by an older redact()
    stale_no_csv_row: int = 0  # stale ok documents left as they are: tender not in the CSVs
    # Rows deleted (their stored text with them) because the tender is in no CSV any more:
    removed_documents: int = 0
    removed_with_text: int = 0
    removed_tenders: int = 0
    removal_skipped: str = ""  # why nothing was removed although it might have been


@dataclass(frozen=True)
class Step:
    url: str
    action: str  # "download", or a status recorded without downloading
    attempts: int = 1
    why: str = ""  # for a download: "new", "retry", "reevaluate" or "reclean"


@dataclass(frozen=True)
class Plan:
    """What a run would do for one tender. Shared by the "anything to do?" check and the run,
    so a tender counted as unchanged is exactly one with nothing planned."""
    gone: frozenset[str]
    steps: tuple[Step, ...]

    def __bool__(self) -> bool:
        return bool(self.gone or self.steps)


def plan(tender: Tender, limits: Limits) -> Plan:
    urls = list(dict.fromkeys(tender.attachment_urls or []))
    stored = {d.url: d for d in tender.documents.all()}
    allowed = order_urls([u for u in urls if is_allowed_url(u)])
    within = set(allowed[: limits.per_tender])
    steps = []
    for url in [*allowed, *(u for u in urls if u not in set(allowed))]:  # best first
        doc = stored.get(url)
        if doc is None:
            if url in within:
                steps.append(Step(url, "download", 1, "new"))
            else:
                steps.append(Step(url, Status.SKIPPED_HOST if url not in allowed
                                  else Status.OVER_TENDER_LIMIT))
        elif doc.status == Status.OK and doc.redact_version < REDACT_VERSION:
            steps.append(Step(url, "download", 1, "reclean"))  # wherever it ranks: it's stored
        elif url not in within:
            continue  # stored and outside the limit: kept as it is
        elif needs_retry(doc):
            steps.append(Step(url, "download", doc.attempts + 1, "retry"))
        elif doc.status == Status.OVER_TENDER_LIMIT:
            steps.append(Step(url, "download", 1, "reevaluate"))  # its first real download
    return Plan(frozenset(set(stored) - set(urls)), tuple(steps))


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


def csv_references(csv_paths: Iterable[Path]) -> set[str]:
    """Every reference number with a row in the CSVs."""
    refs: set[str] = set()
    for path in csv_paths:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            refs.update(ref for row in csv.DictReader(fh)
                        if (ref := (row.get(REF_HEADER) or "").strip()))
    return refs


def remove_documents_not_in_csvs(csv_paths: Iterable[Path], summary: FetchSummary) -> None:
    """Delete the document rows, and so the stored text, of every tender that is in none
    of the CSVs any more. No reference at all in the CSVs means a broken download, not an
    empty source: then nothing is deleted."""
    refs = csv_references(csv_paths)
    if not refs:
        summary.removal_skipped = "the CSVs list no reference numbers"
        return
    stored = dict(TenderDocument.objects.values_list("tender_id", "tender__reference_number")
                  .distinct())
    gone = [pk for pk, ref in stored.items() if ref not in refs]
    if not gone:
        return
    rows = TenderDocument.objects.filter(tender_id__in=gone)
    summary.removed_with_text = rows.exclude(text="").count()
    summary.removed_tenders = len(gone)
    summary.removed_documents, _ = rows.delete()


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
                  "redact_version": REDACT_VERSION, "text": "", "pages": None, "sha256": "",
                  "injection_patterns": [], **fields}
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

    def tender(self, tender: Tender, todo: Plan, hints: list[ContactHints]) -> bool:
        """Carry out one tender's plan. False when a run limit stopped it."""
        if todo.gone:
            self.summary.deleted += TenderDocument.objects.filter(
                tender=tender, url__in=todo.gone).delete()[0]
        for step in todo.steps:
            if step.action != "download":
                self.record(tender, step.url, step.action)
                continue
            if reason := self.limit_reached():
                self.summary.stopped = reason
                return False
            if step.why == "retry":
                self.summary.retries += 1
            elif step.why == "reevaluate":
                self.summary.reevaluated += 1
            elif step.why == "reclean":
                self.summary.recleaned += 1
            self.fetch_one(tender, step.url, hints, attempts=step.attempts)
        return True


def fetch_documents(csv_paths: Iterable[Path], *, limits: Limits | None = None, opener=None,
                    clock: Callable[[], float] = time.monotonic,
                    sleep: Callable[[float], None] = time.sleep,
                    now: datetime | None = None) -> FetchSummary:
    summary = FetchSummary()
    limits = limits or Limits()
    run = _Run(limits, opener or default_opener(), clock, sleep, now or timezone.now(),
               clock(), summary)
    pending = []
    for t in candidate_tenders():
        summary.tenders_checked += 1
        if todo := plan(t, limits):
            pending.append((t, todo))
        else:
            summary.unchanged += 1
    stopped = False
    if pending:
        hints = contact_hints(csv_paths, {t.reference_number for t, _ in pending})
        for t, todo in pending:
            if t.reference_number not in hints:
                summary.no_csv_row += 1
                continue
            if not run.tender(t, todo, hints[t.reference_number]):
                stopped = True
                break
    # Not a download: runs even when the run stopped at a cap.
    remove_documents_not_in_csvs(csv_paths, summary)
    if not stopped:
        _reclean_other_tenders(run, csv_paths, exclude={t.pk for t, _ in pending})
    _stored_totals(summary)
    return summary


def _reclean_other_tenders(run: _Run, csv_paths: Iterable[Path], exclude: set[int]) -> None:
    """Stale ok documents of tenders the plans above didn't cover (closed, cancelled, no
    longer kept, or open with nothing else to do), oldest tender first."""
    stale = list(TenderDocument.objects.filter(status=Status.OK,
                                               redact_version__lt=REDACT_VERSION)
                 .exclude(tender_id__in=exclude).select_related("tender")
                 .order_by("tender_id", "id"))
    if not stale:
        return
    hints = contact_hints(csv_paths, {d.tender.reference_number for d in stale})
    for d in stale:
        if d.tender.reference_number not in hints:
            run.summary.stale_no_csv_row += 1
            continue
        if reason := run.limit_reached():
            run.summary.stopped = reason
            return
        run.summary.recleaned += 1
        run.fetch_one(d.tender, d.url, hints[d.tender.reference_number], attempts=1)


def _stored_totals(summary: FetchSummary) -> None:
    totals = TenderDocument.objects.filter(status=Status.OK).aggregate(
        docs=Count("id"), tenders=Count("tender", distinct=True), chars=Sum(Length("text")))
    summary.ok_documents = totals["docs"]
    summary.ok_tenders = totals["tenders"]
    summary.ok_chars = totals["chars"] or 0
    summary.ok_stale = TenderDocument.objects.filter(
        status=Status.OK, redact_version__lt=REDACT_VERSION).count()
