"""Lambda entry point for batch work (image target `worker`, handler tenders.worker.handler).

Events:
    {"task": "migrate"}  apply migrations over Neon's DIRECT url (SSM database_url_direct)
    {"task": "daily"}    ingest --save, extract Open tenders (LLM caps apply), reassess

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
from config.ssm import fetch_direct_database_url
from django.conf import settings
from django.core.management import call_command
from django.db import connections

# The image is read-only except /tmp (1024 MB ephemeral storage on the worker).
DOWNLOAD_DIR = "/tmp/canadabuys"

TASKS: dict[str, list[tuple[str, list[str]]]] = {
    "migrate": [("migrate", ["--noinput"])],
    "daily": [
        ("ingest_canadabuys", ["--url-defaults", "--save", "--download-dir", DOWNLOAD_DIR]),
        ("extract_tenders", ["--status", "Open"]),
        ("assess_tenders", []),
    ],
}


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
        for name, args in TASKS[task]:
            t0 = time.monotonic()
            step = {"step": name, "ok": False}
            steps.append(step)
            try:
                with _database_for(task):
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
        print(json.dumps(summary, separators=(",", ":")), flush=True)
    return summary
