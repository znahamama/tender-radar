"""Fetch the attachment PDFs of open, kept tenders and store their redacted text.

Run by the worker's daily task after ingest (management command fetch_documents), which
passes the CSVs that ingest just downloaded. Their contact columns give each notice's
redaction hints (the contracting officer's name, email, phone), which redact() needs to
catch names: without them it caught none of the 5 names in 3 real CanadaBuys PDFs
(2026-11-18). The hints are read here, used and dropped; they are never stored. A tender
with no row in those CSVs is not fetched this run.

Per tender, every attachment URL gets one TenderDocument row once looked at (fetched,
skipped or failed). Only a .pdf URL (path ending in ".pdf", any case) on an allowlisted
CanadaBuys attachment path is downloaded (tenders.documents.fetch); another URL on such a
path is recorded as skipped_type, and one anywhere else as skipped_host, without a request.
A tender whose URL set equals its stored rows' URLs, with nothing left to retry or
re-evaluate, is not touched. Rows for URLs that left the set are deleted.

Order and per-tender limit: allowed URLs are ranked main solicitation first (CanadaBuys'
"...EBSU000.PDF", or a file named as a solicitation), then amendments by number
("...EBSU001.PDF", ...), then other PDFs, then other file types, then any file named as
standard instructions ("Standard Instructions", "Instructions uniformisées": boilerplate),
each group in CSV order.
Only .pdf URLs are ranked and count toward the limit. The first 8 are downloaded; the rest
are recorded as over_tender_limit, which is not final: a later run downloads any of them
that rank within the limit then (e.g. once it is raised). Two kinds of row from before
2026-11-21 are re-evaluated the same way, once: a skipped_host row whose URL the widened
allowlist now takes, and a too_many_pages row (long PDFs are now read in part). Each is
downloaded if it ranks within the limit, else recorded as over_tender_limit or
skipped_type; either way its old status is gone, so it is never re-evaluated again.

Uploaded documents (source "upload", tenders.documents.upload) are not the CSV's: plan()
never touches them, and an upload cleaned by an older redact() is re-cleaned from its stored
text (reclean_uploads; there is nothing to download). They are deleted with the tender's
other rows when the tender leaves the CSVs (below).

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
skipped host (while the allowlist doesn't take it), a skipped type or the per-tender limit,
and the permanent rejections (too big, not a PDF, a redirect, a parse error, which give the
same answer every time).

Limits: 10 MB per file, 20 s per download, 15 s per parse, 8 documents per tender, 40
downloads and 300 s per run. A longer PDF is not rejected: the first 150 pages are read,
whole pages only, and at most 400,000 characters are stored (after cleaning too); the row is
ok with pages_read < pages. A download cut off by a run limit is not recorded, so the next
run picks it up.
"""

from __future__ import annotations

import csv
import hashlib
import re
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path

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

from .fetch import (
    FetchFailed,
    default_opener,
    download_pdf,
    is_allowed_url,
    is_pdf_url,
    url_filename,
)
from .parse import ParseFailed, parse_pdf

Status = TenderDocument.Status
Source = TenderDocument.Source
MAX_ATTEMPTS = 3
RETRYABLE = frozenset({Status.TIMEOUT, Status.HTTP_ERROR, Status.NETWORK_ERROR,
                       Status.PARSE_TIMEOUT})


# Statuses from before 2026-11-21 that today's rules would not give: see plan().
REEVALUATED_ONCE = frozenset({Status.SKIPPED_HOST, Status.TOO_MANY_PAGES})


def needs_retry(doc: TenderDocument) -> bool:
    return doc.status in RETRYABLE and doc.attempts < MAX_ATTEMPTS


_EBSU_RE = re.compile(r"\.EBSU(\d{3})\.PDF$", re.IGNORECASE)
_SOLICITATION_WORDS = frozenset(
    {"solicitation", "sollicitation", "demande", "invitation", "rfp", "rfq", "rfsa", "rfso",
     "itq", "itt", "dp", "ddp"})
# An amendment's number follows its keyword, directly ("amd2", "amendment_002"), after a
# separator ("amd_001", "modif.-012"), or after up to four words
# ("amendment-to-the-bid-solicitation-002"). The keyword must stand alone: "amd" inside a
# longer word is not one. Checked before the solicitation words, so "rfp_amd_001" and
# "...-amendment-to-the-bid-solicitation-002" are amendments, not main documents.
_AMENDMENT_RE = re.compile(
    r"(?<![a-z])(?:amendments?|amend|amd|modifications?|modif|addendum)(?![a-z])"
    r"(?:[\W_]+[a-z]+){0,4}?[\W_]*(\d+)",
    re.IGNORECASE)


# PSPC's standard instructions (SACC 2003 "Standard Instructions - Goods or Services -
# Competitive Requirements" and the like, in French "Instructions uniformisées"): long
# boilerplate, the same for every notice, so read last and left out first at the cap.
STANDARD_INSTRUCTIONS = 4  # the rank group, after every other document
_STANDARD_INSTRUCTIONS_RE = re.compile(
    r"standard[\W_]*instructions?|instructions?[\W_]*uniformis[eé]es?", re.IGNORECASE)

_filename = url_filename  # the name the ranking reads


def _rank(url: str) -> tuple[int, int]:
    """(group, number): 0 main solicitation, 1 amendment n, 2 other PDF, 3 other file type,
    4 standard instructions (whatever else the name says)."""
    return _rank_name(_filename(url))


def upload_rank(name: str) -> tuple[int, int]:
    """An uploaded document: an amendment, or standard instructions (last), when its file
    name says so (the same rules as a fetched one's), otherwise the main solicitation,
    whatever else the name says."""
    rank = _rank_name(name)
    return rank if rank[0] in (1, STANDARD_INSTRUCTIONS) else (0, 0)


def document_rank(doc) -> tuple[int, int]:
    """A TenderDocument's rank: _rank of its URL, or upload_rank of an upload's name."""
    if getattr(doc, "source", TenderDocument.Source.FETCHED) == TenderDocument.Source.UPLOAD:
        return upload_rank(doc.original_filename)
    return _rank(doc.url)


def _rank_name(name: str) -> tuple[int, int]:
    if _STANDARD_INSTRUCTIONS_RE.search(unicodedata.normalize("NFC", name)):
        return (STANDARD_INSTRUCTIONS, 0)
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
    """Main solicitation first, then amendments in order, then the rest, standard
    instructions last (stable)."""
    return sorted(urls, key=_rank)


@dataclass(frozen=True)
class Limits:
    max_bytes: int = 10 * 1024 * 1024
    max_pages: int = 150  # read; a longer PDF is read in part, not rejected
    max_chars: int = 400_000  # stored text per document, after cleaning
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
    # over_tender_limit rows downloaded because they now rank within it, skipped_host rows
    # whose URL the allowlist now takes, and too_many_pages rows (now read in part)
    reevaluated: int = 0
    partial: int = 0  # ok downloads of this run read in part (pages_read < pages)
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
    # Uploaded documents re-cleaned from their stored text (they can't be downloaded again)
    uploads_recleaned: int = 0


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


def _not_downloaded(url: str) -> str:
    """The status of a URL that won't be downloaded: off the allowlist, not a .pdf, or (an
    allowed .pdf) outside the per-tender limit."""
    if not is_allowed_url(url):
        return Status.SKIPPED_HOST
    return Status.SKIPPED_TYPE if not is_pdf_url(url) else Status.OVER_TENDER_LIMIT


def plan(tender: Tender, limits: Limits) -> Plan:
    urls = list(dict.fromkeys(tender.attachment_urls or []))
    # Uploaded rows are not the CSV's: never planned, retried or deleted per URL here.
    stored = {d.url: d for d in tender.documents.all() if not d.is_upload}
    allowed = order_urls([u for u in urls if is_allowed_url(u) and is_pdf_url(u)])
    within = set(allowed[: limits.per_tender])
    steps = []
    for url in [*allowed, *(u for u in urls if u not in set(allowed))]:  # best first
        doc = stored.get(url)
        if doc is None:
            if url in within:
                steps.append(Step(url, "download", 1, "new"))
            else:
                steps.append(Step(url, _not_downloaded(url)))
        elif doc.status in REEVALUATED_ONCE and is_allowed_url(url):
            # Rejected under rules since relaxed (the allowlist now takes its URL, or a long
            # PDF is now read in part): as if new, except that the row exists.
            if url in within:
                steps.append(Step(url, "download", 1, "reevaluate"))
            else:
                steps.append(Step(url, _not_downloaded(url)))
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


def cut_at_whitespace(text: str, limit: int) -> str:
    """At most ``limit`` characters, ending before the last whitespace in them ("" if none),
    so no word, address or number is split."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    last = max((m.start() for m in re.finditer(r"\s", head)), default=None)
    return head[:last] if last is not None else ""


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
                  "redact_version": REDACT_VERSION, "text": "", "pages": None,
                  "pages_read": None, "sha256": "", "injection_patterns": [], **fields}
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
            parsed = parse_pdf(data, max_pages=lim.max_pages, max_chars=lim.max_chars,
                               timeout_s=lim.parse_timeout_s)
        except ParseFailed as e:
            record(e.status, pages=e.pages, sha256=digest)
            return
        del data  # never stored
        clean, patterns = clean_text(parsed.text, hints)
        pages_read = parsed.pages_read
        if len(clean) > lim.max_chars:  # cleaning can lengthen text ("Li" -> "[NAME]")
            # Cut after redaction, so it can't expose anything; the last page is now cut.
            clean, pages_read = cut_at_whitespace(clean, lim.max_chars), max(0, pages_read - 1)
            patterns = scan_text(clean)
        if pages_read < parsed.pages:
            self.summary.partial += 1
        record(Status.OK, text=clean, pages=parsed.pages, pages_read=pages_read, sha256=digest,
               injection_patterns=patterns)

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
    from .upload import reclean_uploads

    summary.uploads_recleaned = reclean_uploads(limits)  # no downloads: first, always
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
    stale = list(TenderDocument.objects.filter(status=Status.OK, source=Source.FETCHED,
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
