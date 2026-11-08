"""Download CanadaBuys open-data CSVs politely. Fetching only; parsing lives elsewhere."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

OPEN_TENDERS_URL = (
    "https://canadabuys.canada.ca/opendata/pub/openTenderNotice-ouvertAvisAppelOffres.csv"
)
FISCAL_YEAR_TENDERS_URL = (
    "https://canadabuys.canada.ca/opendata/pub/2026-2027-TenderNotice-AvisAppelOffres.csv"
)
DEFAULT_URLS = (OPEN_TENDERS_URL, FISCAL_YEAR_TENDERS_URL)

USER_AGENT = "tender-radar portfolio project"
TIMEOUT_S = 60
MAX_RETRIES = 3
MAX_BYTES = 300 * 1024 * 1024
CHUNK = 1024 * 1024


class DownloadTooLarge(Exception):
    pass


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or exc.code >= 500
    return isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError))


def _download_once(url: str, dest: Path, max_bytes: int) -> int:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        declared = resp.headers.get("Content-Length")
        if declared and int(declared) > max_bytes:
            raise DownloadTooLarge(f"{url}: Content-Length {declared} > {max_bytes}")
        fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".part-", suffix=".csv")
        written = 0
        try:
            with os.fdopen(fd, "wb") as tmp:
                while chunk := resp.read(CHUNK):
                    written += len(chunk)
                    if written > max_bytes:
                        raise DownloadTooLarge(f"{url}: exceeded {max_bytes} bytes")
                    tmp.write(chunk)
            shutil.move(tmp_name, dest)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
    return written


def download(
    url: str,
    dest_dir: Path,
    *,
    max_bytes: int = MAX_BYTES,
    retries: int = MAX_RETRIES,
    backoff_s: float = 2.0,
) -> Path:
    """Stream ``url`` to ``dest_dir/<basename>`` via a temp file. Returns the final path."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / url.rsplit("/", 1)[-1]
    attempt = 0
    while True:
        try:
            size = _download_once(url, dest, max_bytes)
            log.info("downloaded %s (%d bytes)", dest.name, size)
            return dest
        except DownloadTooLarge:
            raise
        except Exception as exc:
            if attempt >= retries or not _retryable(exc):
                raise
            attempt += 1
            wait = backoff_s * 2 ** (attempt - 1)
            log.warning("fetch %s failed (%s); retry %d/%d in %.0fs", url, exc, attempt, retries, wait)
            time.sleep(wait)
