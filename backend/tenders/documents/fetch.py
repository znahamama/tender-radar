"""Download one CanadaBuys attachment PDF, with every guard applied before parsing.

Only https://canadabuys.canada.ca under one of ALLOWED_PATH_PREFIXES is fetched, and only
a URL whose path ends in ".pdf" (any case). Redirects are never followed (one could leave
the allowlisted host), and nothing here tries to get past a login or a captcha: an SSC,
Ariba or other URL is not requested at all.
"""

from __future__ import annotations

import http.client
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from urllib.parse import unquote, urlsplit

from tenders.ingest.fetch import USER_AGENT

ALLOWED_HOST = "canadabuys.canada.ca"
# Both are public CanadaBuys attachment paths under the same terms (checked 2026-11-21: the
# notice pages of a /documents/pub/att/ notice and a /sites/default/files/ one carry the
# same "Eligibility and terms and conditions" text, and 5 PDFs from the second path came
# back 200 application/pdf with no redirect and no login). Newer notices use the second.
ALLOWED_PATH_PREFIXES = ("/documents/pub/att/", "/sites/default/files/webform/tender_notice/")
# CanadaBuys serves its attachment PDFs as application/octet-stream (checked 2026-11-18),
# so that counts too; the %PDF- magic bytes are always required.
PDF_CONTENT_TYPES = frozenset({"application/pdf", "application/octet-stream"})
PDF_MAGIC = b"%PDF-"
CHUNK = 64 * 1024


class FetchFailed(Exception):
    def __init__(self, status: str):
        super().__init__(status)
        self.status = status  # a TenderDocument.Status value


def is_allowed_url(url: str) -> bool:
    """https, exactly canadabuys.canada.ca (no port, no user info), under an attachment
    path, and no "." or ".." segment (even percent-encoded) that could climb out of it."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https" or parts.netloc != ALLOWED_HOST:
        return False
    if not parts.path.startswith(ALLOWED_PATH_PREFIXES):
        return False
    return not any(seg in (".", "..") for seg in unquote(parts.path).split("/"))


def url_filename(url: str) -> str:
    """The last path segment, percent-decoded: a fetched document's name."""
    return unquote(urlsplit(url).path.rsplit("/", 1)[-1])


def is_pdf_url(url: str) -> bool:
    """The URL's path (percent-decoded) ends in ".pdf", any case. Zip, xlsx and docx
    attachments are never downloaded: they would only use up the run's download cap."""
    try:
        path = urlsplit(url).path
    except ValueError:
        return False
    return unquote(path).lower().endswith(".pdf")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises HTTPError(3xx): reported as "redirected"


def default_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoRedirect)


def download_pdf(url: str, *, opener, max_bytes: int, timeout_s: float,
                 clock: Callable[[], float] = time.monotonic) -> bytes:
    """The file's bytes, or FetchFailed with the reason. ``timeout_s`` bounds the whole
    download (a slow trickle too), not only each socket read."""
    if not is_allowed_url(url):
        raise FetchFailed("skipped_host")
    if not is_pdf_url(url):
        raise FetchFailed("skipped_type")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    deadline = clock() + timeout_s
    body = bytearray()
    try:
        with opener.open(request, timeout=timeout_s) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype not in PDF_CONTENT_TYPES:
                raise FetchFailed("not_pdf")
            declared = (resp.headers.get("Content-Length") or "").strip()
            if declared.isdigit() and int(declared) > max_bytes:
                raise FetchFailed("too_big")
            while chunk := resp.read(CHUNK):
                body += chunk
                if len(body) > max_bytes:
                    raise FetchFailed("too_big")
                if clock() > deadline:
                    raise FetchFailed("timeout")
    except FetchFailed:
        raise
    except urllib.error.HTTPError as e:
        raise FetchFailed("redirected" if 300 <= e.code < 400 else "http_error") from None
    except urllib.error.URLError as e:
        status = "timeout" if isinstance(e.reason, TimeoutError) else "network_error"
        raise FetchFailed(status) from None
    except TimeoutError:
        raise FetchFailed("timeout") from None
    except (ConnectionError, http.client.HTTPException, OSError):
        raise FetchFailed("network_error") from None
    if not body.startswith(PDF_MAGIC):
        raise FetchFailed("not_pdf")
    return bytes(body)
