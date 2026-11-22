"""Parse an untrusted PDF in a child process that can't hang or bloat the worker.

pypdf is pure Python: a hostile file can loop or allocate without bound, and a timer in
the same process can't reliably stop that. So pdftext.py runs as a child with
``python -I`` (no app code, no PYTHON* variables, no script-dir imports), a page limit, a
wall-clock budget after which it is killed, a minimal environment (no secrets), and on
Linux (Lambda) an address-space limit.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

SCRIPT = Path(__file__).with_name("pdftext.py")
MEMORY_LIMIT_BYTES = 512 * 1024 * 1024  # the worker has 1024 MB in all
# Only what the interpreter needs to start (Lambda's python finds libpython via
# LD_LIBRARY_PATH). Never the database URL, API key or other secrets.
ENV_KEEP = ("PATH", "LD_LIBRARY_PATH", "LANG", "LC_ALL", "TZ")


class ParseFailed(Exception):
    def __init__(self, status: str, pages: int | None = None):
        super().__init__(status)
        self.status = status  # a TenderDocument.Status value
        self.pages = pages


class Parsed(NamedTuple):
    pages: int  # in the whole document
    pages_read: int  # whose text is all in ``text``; < pages means read in part
    text: str


def _limit_memory() -> None:  # runs in the child, before exec
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))


def _count(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def parse_pdf(data: bytes, *, max_pages: int, max_chars: int, timeout_s: float) -> Parsed:
    """The text of the first ``max_pages`` pages, whole pages only, at most ``max_chars``
    characters (see pdftext.py), or ParseFailed("parse_timeout" | "parse_error"). A longer
    document is not an error: ``pages_read`` < ``pages`` says it was read in part."""
    try:
        proc = subprocess.run(
            [sys.executable, "-I", str(SCRIPT), str(max_pages), str(max_chars)],
            input=data, capture_output=True, timeout=timeout_s, check=False,
            env={k: os.environ[k] for k in ENV_KEEP if k in os.environ},
            preexec_fn=_limit_memory if sys.platform.startswith("linux") else None,
        )
    except subprocess.TimeoutExpired:  # subprocess.run has already killed the child
        raise ParseFailed("parse_timeout") from None
    if proc.returncode != 0:  # e.g. killed by the memory limit; stderr is not kept
        raise ParseFailed("parse_error")
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        raise ParseFailed("parse_error") from None
    if not isinstance(out, dict):
        raise ParseFailed("parse_error")
    pages, read, text = _count(out.get("pages")), _count(out.get("pages_read")), out.get("text")
    if ("error" in out or pages is None or read is None or read > pages
            or read > max_pages or not isinstance(text, str) or len(text) > max_chars):
        raise ParseFailed("parse_error")
    return Parsed(pages, read, text)
