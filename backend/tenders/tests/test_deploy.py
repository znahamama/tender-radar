"""Deployment plumbing: health endpoints, SSM secret loading, the worker Lambda handler,
and what the API loads at cold start."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from config.ssm import SECRET_NAMES, fetch_direct_database_url, fetch_secrets
from django.core.exceptions import ImproperlyConfigured

# --- health ---------------------------------------------------------------------------

def test_health_needs_no_database(client, settings):
    # No django_db mark: pytest-django raises if the view touches the database.
    settings.APP_VERSION = "abc123"
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": "abc123"}


def test_health_rejects_post(client):
    assert client.post("/api/health").status_code == 405


@pytest.mark.django_db
def test_health_db_runs_select_1(client):
    r = client.get("/api/health/db")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": "ok"}


def test_health_db_hides_error_details(client, monkeypatch):
    import tenders.views

    class Broken:
        def cursor(self):
            raise RuntimeError("password=hunter2 host=secret")

    monkeypatch.setattr(tenders.views, "connection", Broken())
    r = client.get("/api/health/db")
    assert r.status_code == 503
    assert "hunter2" not in r.content.decode()


# --- API cold start -------------------------------------------------------------------

# Worker-only: the LLM SDK and its HTTP stack, and the extraction/ingest pipelines.
WORKER_ONLY = ("anthropic", "httpx", "httpx2", "tenders.extraction.providers",
               "tenders.extraction.service", "tenders.ingest.pipeline")


def test_api_urlconf_does_not_import_worker_only_modules():
    """Everything the API Lambda's gunicorn worker imports before its first request:
    settings, django.setup(), the WSGI app and the URLconf. The Anthropic SDK alone is
    ~0.6 s of a cold start (~2 s at 512 MB's CPU share). A fresh interpreter, because
    this test process has imported anthropic already."""
    code = (
        "import sys, django; django.setup()\n"
        "import config.wsgi, config.urls\n"
        f"print(sorted(m for m in {WORKER_ONLY!r} if m in sys.modules))\n"
    )
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings",
           "DJANGO_SECRET_KEY": "test-only", "DATABASE_URL": "sqlite:///:memory:",
           "DATABASE_URL_DIRECT": ""}  # set but empty: settings fall back to DATABASE_URL
    env.pop("SSM_PREFIX", None)
    backend = Path(__file__).resolve().parents[2]
    out = subprocess.run([sys.executable, "-c", code], cwd=backend, env=env, check=False,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip() == "[]"


# --- SSM ------------------------------------------------------------------------------

class FakeSSM:
    """ssm.get_parameters as AWS behaves: unknown names come back under InvalidParameters
    (no exception); ``error`` makes the whole call fail, like AccessDeniedException."""

    def __init__(self, values, error=None):
        self.values, self.error, self.calls = values, error, []

    def get_parameters(self, Names, WithDecryption):
        self.calls.append((tuple(Names), WithDecryption))
        if self.error:
            err = Exception("boom")
            err.response = {"Error": {"Code": self.error}}
            raise err
        return {"Parameters": [{"Name": n, "Value": self.values[n]}
                               for n in Names if n in self.values],
                "InvalidParameters": [n for n in Names if n not in self.values]}


def _values(prefix):
    return {f"{prefix}{n}": f"value-of-{n}" for n in SECRET_NAMES}


def test_fetch_secrets_is_one_decrypted_call():
    fake = FakeSSM(_values("/t/prod/"))
    got = fetch_secrets("/t/prod/", client=fake)
    assert got == {n: f"value-of-{n}" for n in SECRET_NAMES}
    # One GetParameters round trip at cold start, not one per secret.
    assert fake.calls == [(tuple(f"/t/prod/{n}" for n in SECRET_NAMES), True)]
    fetch_secrets("/t/prod/", client=fake)  # cached for the process
    assert len(fake.calls) == 1
    # The direct URL is never part of the cold-start load (API and daily stay pooled).
    assert all("database_url_direct" not in n for names, _ in fake.calls for n in names)


def test_fetch_direct_database_url():
    fake = FakeSSM({"/t/direct/database_url_direct": "postgres://direct"})
    assert fetch_direct_database_url("/t/direct/", client=fake) == {
        "DATABASE_URL_DIRECT": "postgres://direct"}
    assert fake.calls == [(("/t/direct/database_url_direct",), True)]


def test_missing_direct_url_names_it():
    with pytest.raises(ImproperlyConfigured, match="/t/nodirect/database_url_direct"):
        fetch_direct_database_url("/t/nodirect/", client=FakeSSM({}))


def test_missing_parameter_names_it_without_values():
    vals = _values("/t/miss/")
    del vals["/t/miss/ANTHROPIC_API_KEY"]
    with pytest.raises(ImproperlyConfigured) as exc:
        fetch_secrets("/t/miss/", client=FakeSSM(vals))
    msg = str(exc.value)
    assert "/t/miss/ANTHROPIC_API_KEY" in msg and "ParameterNotFound" in msg
    assert "value-of" not in msg


def test_failed_call_names_parameters_and_code_only():
    with pytest.raises(ImproperlyConfigured) as exc:
        fetch_secrets("/t/denied/", client=FakeSSM(_values("/t/denied/"),
                                                   error="AccessDeniedException"))
    msg = str(exc.value)
    assert "/t/denied/DATABASE_URL" in msg and "AccessDeniedException" in msg
    assert "value-of" not in msg


def test_empty_parameter_fails():
    vals = _values("/t/empty/")
    vals["/t/empty/DJANGO_SECRET_KEY"] = "  "
    with pytest.raises(ImproperlyConfigured, match="DJANGO_SECRET_KEY is empty"):
        fetch_secrets("/t/empty/", client=FakeSSM(vals))


@pytest.mark.parametrize("fetch", [fetch_secrets, fetch_direct_database_url])
@pytest.mark.parametrize("prefix", ["tender-radar/prod/", "/tender-radar/prod"])
def test_prefix_must_be_slash_delimited(fetch, prefix):
    with pytest.raises(ImproperlyConfigured, match="SSM_PREFIX"):
        fetch(prefix, client=FakeSSM({}))


# --- worker ---------------------------------------------------------------------------

@pytest.fixture
def calls(monkeypatch):
    import tenders.worker

    made = []
    monkeypatch.setattr(tenders.worker, "call_command", lambda name, *a: made.append((name, a)))
    return made


def _summary(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_daily_runs_ingest_baseline_extract_assess_in_order(calls, capsys):
    from tenders.worker import handler

    out = handler({"task": "daily"})
    assert [c[0] for c in calls] == ["ingest_canadabuys", "extract_tenders", "extract_tenders",
                                     "assess_tenders", "fetch_documents"]
    ingest_args = calls[0][1]
    assert "--save" in ingest_args and "--url-defaults" in ingest_args
    assert ingest_args[ingest_args.index("--download-dir") + 1].startswith("/tmp/")
    # The free keyword baseline first, so a capped or failed LLM step can't skip it.
    assert calls[1][1] == ("--status", "Open", "--baseline")
    assert calls[2][1] == ("--status", "Open")
    # Documents last, reading the CSVs ingest downloaded (for redaction hints only).
    assert calls[4][1] == ("--csv-dir", ingest_args[ingest_args.index("--download-dir") + 1])
    assert out["ok"] is True
    assert [s["step"] for s in out["steps"]] == [
        "ingest_canadabuys", "extract_tenders --baseline", "extract_tenders", "assess_tenders",
        "fetch_documents"]
    assert _summary(capsys) == out


def test_migrate(calls, capsys):
    from tenders.worker import handler

    assert handler({"task": "migrate"})["ok"]
    assert calls == [("migrate", ("--noinput",))]


@pytest.mark.parametrize("event", [{}, {"task": "drop"}, None, "daily"])
def test_unknown_task_is_rejected(calls, event):
    from tenders.worker import handler

    with pytest.raises(ValueError, match="unknown task"):
        handler(event)
    assert calls == []


def test_failed_step_stops_and_logs(monkeypatch, capsys):
    from django.core.management.base import CommandError

    import tenders.worker

    made = []

    def fake(name, *a):
        made.append((name, "--baseline" in a))
        if name == "extract_tenders" and "--baseline" not in a:  # the LLM step
            raise CommandError("No tenders match.")

    monkeypatch.setattr(tenders.worker, "call_command", fake)
    with pytest.raises(CommandError):
        tenders.worker.handler({"task": "daily"})
    assert made == [("ingest_canadabuys", False), ("extract_tenders", True),
                    ("extract_tenders", False)]  # stopped: no assess
    s = _summary(capsys)
    assert s["ok"] is False
    assert s["steps"][-1] == {"step": "extract_tenders", "ok": False,
                              "error": "CommandError: No tenders match.",
                              "seconds": s["steps"][-1]["seconds"]}


# --- worker: direct URL for migrate only ----------------------------------------------

DIRECT = "postgres://direct-user:pw@ep-direct.example.neon.tech:5432/prod?sslmode=require"


@pytest.fixture
def direct_url(monkeypatch, settings):
    """SSM_PREFIX set as on Lambda; records each fetch of the direct URL."""
    import tenders.worker

    settings.SSM_PREFIX = "/tender-radar/prod/"
    fetched = []

    def fake_fetch(prefix):
        fetched.append(prefix)
        return {"DATABASE_URL_DIRECT": DIRECT}

    monkeypatch.setattr(tenders.worker, "fetch_direct_database_url", fake_fetch)
    return fetched


def _record_db(monkeypatch, fail_on=None):
    from django.db import connections

    import tenders.worker

    seen = []

    def fake(name, *a):
        seen.append((name, connections["default"].settings_dict["HOST"]))
        if name == fail_on:
            raise RuntimeError("migration failed")

    monkeypatch.setattr(tenders.worker, "call_command", fake)
    return seen


def test_migrate_uses_direct_url_then_restores_pooled(direct_url, monkeypatch):
    from django.db import connections

    from tenders.worker import handler

    before = dict(connections["default"].settings_dict)
    seen = _record_db(monkeypatch)
    assert handler({"task": "migrate"})["ok"]
    assert direct_url == ["/tender-radar/prod/"]
    assert seen == [("migrate", "ep-direct.example.neon.tech")]
    after = connections["default"].settings_dict
    assert after == before
    assert after["DISABLE_SERVER_SIDE_CURSORS"] is True


def test_failed_migrate_still_restores_pooled(direct_url, monkeypatch, capsys):
    from django.db import connections

    from tenders.worker import handler

    before = dict(connections["default"].settings_dict)
    _record_db(monkeypatch, fail_on="migrate")
    with pytest.raises(RuntimeError):
        handler({"task": "migrate"})
    assert connections["default"].settings_dict == before
    assert _summary(capsys)["steps"][0]["error"] == "RuntimeError: migration failed"


def test_daily_keeps_pooled_url(direct_url, monkeypatch):
    from django.db import connections

    from tenders.worker import handler

    pooled_host = connections["default"].settings_dict["HOST"]
    seen = _record_db(monkeypatch)
    assert handler({"task": "daily"})["ok"]
    assert direct_url == []
    assert {host for _, host in seen} == {pooled_host}


def test_migrate_locally_does_not_touch_ssm(monkeypatch, settings):
    import tenders.worker

    settings.SSM_PREFIX = ""
    monkeypatch.setattr(tenders.worker, "fetch_direct_database_url",
                        lambda prefix: pytest.fail("SSM fetched without SSM_PREFIX"))
    _record_db(monkeypatch)
    assert tenders.worker.handler({"task": "migrate"})["ok"]
