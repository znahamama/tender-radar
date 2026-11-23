"""Lambda entry point for batch work (image target `worker`, handler tenders.worker.handler).

Events:
    {"task": "migrate"}  apply migrations over Neon's DIRECT url (SSM database_url_direct)
    {"task": "daily"}    ingest --save, keyword baseline then LLM on Open tenders (LLM caps
                         apply), reassess, then fetch new CanadaBuys attachment PDFs (last,
                         so it can't hold up or stop the steps the site depends on)
    {"task": "assess"}   assess_tenders only: recompute eligibility from the stored
                         extractions with today's rules and profile. No ingest, no API calls
    {"task": "extract_one", "reference": "<ref>"}
                         extraction (v5 when the tender has documents, else v3) and its
                         assessment for that one tender, under the usual per-run and daily
                         LLM caps. Sent asynchronously by the API after a reviewer uploads
                         or deletes a document (tenders.documents.upload). An unknown
                         reference is rejected before anything runs
    {"task": "create_admin", "username": "<name>"}
                         create or update that superuser with the password in the one-time
                         SecureString <SSM_PREFIX>admin_bootstrap_password (infra/README.md)

Each command's own output goes to the log as usual; the last line is a one-line JSON summary.
A failed step stops the run and fails the invocation (no retries are configured anywhere,
so a failure never repeats LLM calls).
"""

import json
import os
import time
from contextlib import contextmanager, nullcontext

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()  # at import: once per cold start, including the SSM fetch in settings

import dj_database_url
from config.ssm import fetch_admin_bootstrap_password, fetch_direct_database_url
from django.conf import settings
from django.core.management import call_command
from django.db import connections

# The image is read-only except /tmp (1024 MB ephemeral storage on the worker).
DOWNLOAD_DIR = "/tmp/canadabuys"

TASKS: dict[str, list[tuple[str, list[str]]]] = {
    "migrate": [("migrate", ["--noinput"])],
    "daily": [
        ("ingest_canadabuys", ["--url-defaults", "--save", "--download-dir", DOWNLOAD_DIR]),
        # The keyword baseline first: free (no API) and deterministic, so the detail page's
        # baseline column is filled even on a day the LLM step stops at a cap or fails.
        ("extract_tenders", ["--status", "Open", "--baseline"]),
        ("extract_tenders", ["--status", "Open"]),
        ("assess_tenders", []),
        # Reads the CSVs ingest left in DOWNLOAD_DIR, for the redaction hints only.
        ("fetch_documents", ["--csv-dir", DOWNLOAD_DIR]),
    ],
    # After a rules or profile change ships: the site shows the new verdicts without
    # waiting for (or spending on) the next daily run.
    "assess": [("assess_tenders", [])],
    "create_admin": [("create_admin", [])],  # a Python step, not a command: STEPS below
    "extract_one": [("extract_one", [])],  # a Python step: STEPS below
}
MAX_REFERENCE_CHARS = 100  # Tender.reference_number's max_length


@contextmanager
def _direct_database():
    """Point the default connection at the direct (unpooled) URL for one step, then put
    the pooled settings back. Settings load once per sandbox, and a warm sandbox may run
    "daily" next, so the direct URL is never left in place."""
    url = fetch_direct_database_url(settings.SSM_PREFIX)["DATABASE_URL_DIRECT"]
    conn = connections["default"]
    saved = dict(conn.settings_dict)
    conn.close()
    # Keys the URL doesn't set (DISABLE_SERVER_SIDE_CURSORS, TEST, ...) are kept.
    conn.settings_dict.update(dj_database_url.parse(url, conn_max_age=0))
    try:
        yield
    finally:
        conn.close()
        conn.settings_dict.clear()
        conn.settings_dict.update(saved)


def _database_for(task: str):
    # Locally (no SSM_PREFIX) settings already prefer DATABASE_URL_DIRECT from .env.
    if task == "migrate" and settings.SSM_PREFIX:
        return _direct_database()
    return nullcontext()


def _create_admin(event: dict) -> None:
    """Never logs, prints or returns the password; only the username and the outcome."""
    from tenders.bootstrap import bootstrap_admin

    username = event.get("username")
    if not isinstance(username, str) or not username.strip():
        raise ValueError('create_admin needs "username", e.g. {"task": "create_admin", '
                         '"username": "admin"}')
    if not settings.SSM_PREFIX:
        raise RuntimeError("create_admin reads its password from SSM and runs on Lambda only; "
                           "locally use manage.py createsuperuser")
    outcome = bootstrap_admin(username, fetch_admin_bootstrap_password(settings.SSM_PREFIX))
    print(f"admin {outcome}: {username.strip()}", flush=True)


def _reference(event: dict) -> str:
    """extract_one's reference: a stored tender's, else ValueError (nothing has run)."""
    from tenders.models import Tender

    ref = event.get("reference")
    if (not isinstance(ref, str) or not ref.strip() or len(ref) > MAX_REFERENCE_CHARS
            or not Tender.objects.filter(reference_number=ref).exists()):
        shown = repr(ref)[:120] if isinstance(ref, str) else type(ref).__name__
        raise ValueError(f'extract_one needs the "reference" of a stored tender; got {shown}')
    return ref


def _extract_one(event: dict) -> None:
    # extract_tenders picks v5 or v3 for the tender, applies the per-run and daily caps
    # (a cap is a clean exit), and assesses the extraction it made or found in its cache.
    call_command("extract_tenders", "--reference", _reference(event))


# Steps that are Python functions of the event rather than management commands.
STEPS = {"create_admin": _create_admin, "extract_one": _extract_one}


def _step_label(name: str, args: list[str]) -> str:
    """The step's name in the summary; the two extract steps differ only by --baseline."""
    return f"{name} --baseline" if "--baseline" in args else name


def _error_line(e: BaseException) -> str:
    first = (str(e).splitlines() or [""])[0]
    return f"{type(e).__name__}: {first[:200]}"


def handler(event, context=None):
    task = event.get("task") if isinstance(event, dict) else None
    if task not in TASKS:
        raise ValueError(f"unknown task {task!r}; expected one of {sorted(TASKS)}")

    started = time.monotonic()
    steps: list[dict] = []
    try:
        if task == "extract_one":
            _reference(event)  # before any step: a bad event runs nothing
        for name, args in TASKS[task]:
            t0 = time.monotonic()
            step = {"step": _step_label(name, args), "ok": False}
            steps.append(step)
            try:
                with _database_for(task):
                    if name in STEPS:
                        STEPS[name](event)
                    else:
                        call_command(name, *args)
            except Exception as e:
                step["error"] = _error_line(e)
                raise
            finally:
                step["seconds"] = round(time.monotonic() - t0, 1)
            step["ok"] = True
    finally:
        # Don't hold a Neon connection open while the sandbox is frozen.
        connections.close_all()
        summary = {
            "task": task,
            "ok": len(steps) == len(TASKS[task]) and all(s["ok"] for s in steps),
            "seconds": round(time.monotonic() - started, 1),
            "version": settings.APP_VERSION,
            "steps": steps,
        }
        if task == "extract_one":
            ref = event.get("reference")
            summary["reference"] = ref[:MAX_REFERENCE_CHARS] if isinstance(ref, str) else None
        print(json.dumps(summary, separators=(",", ":")), flush=True)
    return summary
