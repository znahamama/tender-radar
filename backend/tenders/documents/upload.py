"""Reviewer uploads: solicitation PDFs a reviewer adds on the admin review page.

For a tender whose documents the fetcher can't get (behind a login, off the allowlist), a
reviewer with the documents can upload them. Each file is handled inside the API request
(tenders.admin.NeedsReviewAdmin.upload_view):

1. Limits (checked before any parsing): at most MAX_FILE_BYTES per file, MAX_PER_TENDER
   uploaded documents per tender, MAX_PER_USER_PER_DAY uploads per user per local day
   (counted from the admin log, so a deleted upload still counts).
2. Name ends in ".pdf" (any case) and the bytes start with %PDF-.
3. The fetcher's sandboxed parse (tenders.documents.parse: a child process with a time and,
   on Linux, a memory limit; the first 150 pages, at most 400,000 characters), within what
   is left of the request's PARSE_BUDGET_S.
4. redact() without CSV hints (there is no CSV row to give the contact names), then the
   injection scan, as for a fetched document. The file name is redacted too: it is shown
   on the public page.
5. A TenderDocument, source "upload", status ok (``partial`` when read in part), with the
   redacted text, sha256, uploader and file name. The PDF itself is never stored: it is
   held in memory for the request only (settings.FILE_UPLOAD_MAX_MEMORY_SIZE).
   The same bytes (sha256) already stored for the tender, fetched or uploaded, are
   rejected.

Then the API asks the worker to re-read the tender (start_extract_one; worker task
"extract_one"). Deleting an upload does the same.

Imports stay light (no ingest pipeline): the admin imports this module on the API.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from datetime import time as dtime

from django.conf import settings
from django.contrib.admin.models import ADDITION, LogEntry
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.utils import timezone

from tenders.ingest.injection import scan_text
from tenders.ingest.redact import REDACT_VERSION, redact
from tenders.ingest.schema import normalize_text
from tenders.models import Tender, TenderDocument

from .fetch import PDF_MAGIC
from .parse import ParseFailed, parse_pdf

log = logging.getLogger(__name__)

# Lambda takes at most 6 MB per request, and API Gateway base64-encodes a multipart body
# (4/3 larger): 4 MB of PDF is about 5.3 MB on the wire.
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_PER_TENDER = 5
MAX_PER_USER_PER_DAY = 10
# Parsing for one request, all files together: the API function and API Gateway both stop
# at 30 s. A file is parsed with min(the fetcher's 15 s, what is left of this).
PARSE_BUDGET_S = 20.0
MIN_PARSE_S = 2.0  # less left than this: the file is not tried (upload it again)
NO_TIME_LEFT = "not read: no time left in this request; upload it again on its own"
MAX_NAME_CHARS = 200
UPLOAD_URL_PREFIX = "upload:"

Source = TenderDocument.Source


class Rejected(Exception):
    """A file (or the whole request) refused, with a message for the reviewer."""


def upload_url(sha256: str) -> str:
    """The ``url`` of an uploaded row: unique per content within a tender, never a link."""
    return f"{UPLOAD_URL_PREFIX}{sha256}"


_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def clean_filename(name: str) -> str:
    """The name as sent, without any path, control characters or contact details, at most
    MAX_NAME_CHARS characters (the ".pdf" kept)."""
    base = re.split(r"[\\/]", name or "")[-1]
    base = " ".join(_CONTROL.sub(" ", normalize_text(base)).split())
    base, _ = redact(base)
    if len(base) > MAX_NAME_CHARS:
        stem, dot, ext = base.rpartition(".")
        keep = MAX_NAME_CHARS - len(dot + ext)
        base = (stem[:keep].rstrip() + dot + ext) if dot and keep > 0 else base[:MAX_NAME_CHARS]
    return base


def day_start(now: datetime | None = None) -> datetime:
    """Midnight of today, local time (America/St_Johns), as the LLM daily cap counts days."""
    local_day = timezone.localtime(now or timezone.now()).date()
    return timezone.make_aware(datetime.combine(local_day, dtime.min))


def uploads_today(user, now: datetime | None = None) -> int:
    """Uploads this user stored today, deleted ones included (the admin log keeps them)."""
    return LogEntry.objects.filter(
        user_id=user.pk, action_flag=ADDITION, action_time__gte=day_start(now),
        content_type=ContentType.objects.get_for_model(TenderDocument)).count()


def uploaded(tender: Tender):
    return tender.documents.filter(source=Source.UPLOAD)


def check_request(tender: Tender, user, n_files: int) -> None:
    """The count limits, for the whole request before any file is read."""
    if n_files < 1:
        raise Rejected("Choose at least one PDF.")
    have = uploaded(tender).count()
    if have + n_files > MAX_PER_TENDER:
        raise Rejected(f"At most {MAX_PER_TENDER} uploaded documents per notice: this one "
                       f"has {have}, so {max(0, MAX_PER_TENDER - have)} more can be added. "
                       "Nothing was uploaded.")
    done = uploads_today(user)
    if done + n_files > MAX_PER_USER_PER_DAY:
        raise Rejected(f"At most {MAX_PER_USER_PER_DAY} uploads per person per day: you have "
                       f"made {done} today. Nothing was uploaded.")


@dataclass(frozen=True)
class Cleaned:
    text: str
    pages: int
    pages_read: int
    patterns: list[str]


def clean(text: str, pages: int, pages_read: int, max_chars: int, name: str = "") -> Cleaned:
    """redact() (no hints), then the character cap again (cleaning can lengthen text, and a
    cut after it can't expose anything, as the fetcher does), then the injection scan of the
    text and the file name (the prompt names the document by it)."""
    from .service import cut_at_whitespace

    out, _ = redact(normalize_text(text))
    if len(out) > max_chars:
        out, pages_read = cut_at_whitespace(out, max_chars), max(0, pages_read - 1)
    return Cleaned(out, pages, pages_read, scan_text(f"{name}\n{out}" if name else out))


def read_pdf(sent_name: str, data: bytes, timeout_s: float) -> Cleaned:
    """The cleaned text of one upload, or Rejected with the reason. ``sent_name``: the
    file name as sent (cleaning could drop its extension)."""
    from .service import Limits

    lim = Limits()
    if not (sent_name or "").strip().lower().endswith(".pdf"):
        raise Rejected("not a .pdf file")
    if len(data) > MAX_FILE_BYTES:
        raise Rejected(f"over {MAX_FILE_BYTES // (1024 * 1024)} MB")
    if not data.startswith(PDF_MAGIC):
        raise Rejected("not a PDF (the file doesn't start with %PDF-)")
    try:
        parsed = parse_pdf(data, max_pages=lim.max_pages, max_chars=lim.max_chars,
                           timeout_s=min(lim.parse_timeout_s, timeout_s))
    except ParseFailed as e:
        raise Rejected("reading the PDF took too long" if e.status == "parse_timeout"
                       else "the PDF could not be read (damaged or encrypted)") from None
    cleaned = clean(parsed.text, parsed.pages, parsed.pages_read, lim.max_chars,
                    clean_filename(sent_name))
    if not cleaned.text.strip():
        raise Rejected("no text in the PDF (a scan without a text layer?)")
    return cleaned


def store(tender: Tender, user, name: str, data: bytes, cleaned: Cleaned) -> TenderDocument:
    """One upload's row; Rejected if the tender already has these exact bytes."""
    digest = hashlib.sha256(data).hexdigest()
    if tender.documents.filter(sha256=digest).exists():
        raise Rejected("this notice already has this exact file")
    try:
        with transaction.atomic():
            doc = TenderDocument.objects.create(
                tender=tender, url=upload_url(digest), source=Source.UPLOAD, status="ok",
                text=cleaned.text, pages=cleaned.pages, pages_read=cleaned.pages_read,
                sha256=digest, injection_patterns=cleaned.patterns, attempts=1,
                redact_version=REDACT_VERSION, fetched_at=timezone.now(), uploaded_by=user,
                original_filename=name)
            # The per-day count reads these (uploads_today), deleted uploads included.
            LogEntry.objects.log_actions(
                user.pk, [doc], ADDITION, single_object=True,
                change_message=f"Uploaded {name} to {tender.reference_number}")
            return doc
    except IntegrityError:  # the same file in one request, or a concurrent upload
        raise Rejected("this notice already has this exact file") from None


@dataclass
class Result:
    stored: list[TenderDocument]
    rejected: list[tuple[str, str]]  # (file name, reason)


def handle_files(tender: Tender, user, files: list, *,
                 clock: Callable[[], float] = time.monotonic) -> Result:
    """Each file of one request in turn; ``files``: Django UploadedFile objects. Call
    check_request first. A file that fails is reported and the others still go ahead."""
    deadline = clock() + PARSE_BUDGET_S
    result = Result([], [])
    for f in files:
        name = clean_filename(f.name) or "unnamed.pdf"
        left = deadline - clock()
        if left < MIN_PARSE_S:
            result.rejected.append((name, NO_TIME_LEFT))
            continue
        if f.size > MAX_FILE_BYTES:  # before reading it
            result.rejected.append((name, f"over {MAX_FILE_BYTES // (1024 * 1024)} MB"))
            continue
        data = f.read()
        try:
            result.stored.append(store(tender, user, name, data,
                                       read_pdf(f.name, data, timeout_s=left)))
        except Rejected as e:
            result.rejected.append((name, str(e)))
        finally:
            del data  # never kept beyond this file
    return result


# --- the worker ----------------------------------------------------------------------

def start_extract_one(reference: str, invoke: Callable[[str, bytes], None] | None = None
                      ) -> str:
    """Ask the worker (asynchronously) to re-read one tender: {"task": "extract_one"}.
    Returns "" when started, else why not (no worker configured, or the call failed). The
    daily run would read the documents anyway."""
    function = settings.WORKER_FUNCTION_NAME
    if not function:
        return ("no worker is configured here: run manage.py extract_tenders --reference "
                f"{reference}")
    payload = json.dumps({"task": "extract_one", "reference": reference}).encode()
    try:
        (invoke or _invoke_async)(function, payload)
    except Exception as e:  # botocore errors name the code, never a secret
        log.warning("extract_one not started for %s", reference, exc_info=True)
        return f"the worker could not be started ({type(e).__name__})"
    return ""


def _invoke_async(function: str, payload: bytes) -> None:
    import boto3  # Lambda only; the API role may invoke the worker and nothing else

    boto3.client("lambda").invoke(FunctionName=function, InvocationType="Event",
                                  Payload=payload)


# --- reading status, for the review page -------------------------------------------------

def reading_status(doc: TenderDocument) -> str:
    """"reading…" until an extraction of the tender (configured model) was produced or
    re-used after the upload (Extraction.last_used_at); then how the current one used it:
    read whole, read in part, or left out."""
    from tenders.models import Extraction

    newest = (Extraction.objects.filter(tender_id=doc.tender_id, model_id=settings.LLM_MODEL,
                                        last_used_at__gt=doc.fetched_at)
              .order_by("-last_used_at", "-id").first())
    if newest is None:
        return "reading…"
    rec = next((d for d in newest.documents or [] if d.get("url") == doc.url), None)
    if rec is None:
        return f"not in the current extraction (#{newest.pk}, prompt {newest.prompt_version})"
    said = {"full": "read in full", "partial": "read in part (the input cap)",
            "not_read": "left out (the input cap)"}.get(rec.get("status"), rec.get("status"))
    return f"{said} in extraction #{newest.pk}"


# --- REDACT_VERSION bumps ------------------------------------------------------------------

def reclean_uploads(limits=None) -> int:
    """Re-run redact() on every uploaded document cleaned by an older version. The PDF is
    gone, so this cleans the stored (already redacted) text again; text the older version
    left in place is caught as far as the new rules catch it without CSV hints. Returns how
    many rows changed version."""
    from .service import Limits

    lim = limits or Limits()
    n = 0
    stale = TenderDocument.objects.filter(source=Source.UPLOAD,
                                          redact_version__lt=REDACT_VERSION)
    for doc in stale.iterator():
        c = clean(doc.text, doc.pages or 0, doc.pages_read or 0, lim.max_chars,
                  doc.original_filename)
        doc.text, doc.pages_read, doc.injection_patterns = c.text, c.pages_read, c.patterns
        doc.redact_version = REDACT_VERSION
        doc.save(update_fields=["text", "pages_read", "injection_patterns", "redact_version"])
        n += 1
    return n
