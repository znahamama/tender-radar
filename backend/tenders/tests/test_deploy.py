"""Deployment plumbing: health endpoints, SSM secret loading, the worker Lambda handler."""

import json

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


# --- SSM ------------------------------------------------------------------------------

class FakeSSM:
    def __init__(self, values, fail=None):
        self.values, self.fail, self.calls = values, fail, []

    def get_parameter(self, Name, WithDecryption):
        self.calls.append((Name, WithDecryption))
        if Name == self.fail:
            err = Exception("boom")
            err.response = {"Error": {"Code": "ParameterNotFound"}}
            raise err
        return {"Parameter": {"Value": self.values[Name]}}


def _values(prefix):
    return {f"{prefix}{n}": f"value-of-{n}" for n in SECRET_NAMES}


def test_fetch_secrets_decrypts_each_name_once():
    fake = FakeSSM(_values("/t/prod/"))
    got = fetch_secrets("/t/prod/", client=fake)
    assert got == {n: f"value-of-{n}" for n in SECRET_NAMES}
    assert fake.calls == [(f"/t/prod/{n}", True) for n in SECRET_NAMES]
    fetch_secrets("/t/prod/", client=fake)  # cached for the process
    assert len(fake.calls) == len(SECRET_NAMES)
    # The direct URL is never part of the cold-start load (API and daily stay pooled).
    assert all("database_url_direct" not in name for name, _ in fake.calls)


def test_fetch_direct_database_url():
    fake = FakeSSM({"/t/direct/database_url_direct": "postgres://direct"})
    assert fetch_direct_database_url("/t/direct/", client=fake) == {
        "DATABASE_URL_DIRECT": "postgres://direct"}
    assert fake.calls == [("/t/direct/database_url_direct", True)]


def test_missing_direct_url_names_it():
    fake = FakeSSM({}, fail="/t/nodirect/database_url_direct")
    with pytest.raises(ImproperlyConfigured, match="/t/nodirect/database_url_direct"):
        fetch_direct_database_url("/t/nodirect/", client=fake)


def test_missing_parameter_names_it_without_values():
    fake = FakeSSM(_values("/t/miss/"), fail="/t/miss/ANTHROPIC_API_KEY")
    with pytest.raises(ImproperlyConfigured) as exc:
        fetch_secrets("/t/miss/", client=fake)
    msg = str(exc.value)
    assert "/t/miss/ANTHROPIC_API_KEY" in msg and "ParameterNotFound" in msg
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


def test_daily_runs_ingest_extract_assess_in_order(calls, capsys):
    from tenders.worker import handler

    out = handler({"task": "daily"})
    assert [c[0] for c in calls] == ["ingest_canadabuys", "extract_tenders", "assess_tenders"]
    ingest_args = calls[0][1]
    assert "--save" in ingest_args and "--url-defaults" in ingest_args
    assert ingest_args[ingest_args.index("--download-dir") + 1].startswith("/tmp/")
    assert calls[1][1] == ("--status", "Open")
    assert out["ok"] is True
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
        made.append(name)
        if name == "extract_tenders":
            raise CommandError("No tenders match.")

    monkeypatch.setattr(tenders.worker, "call_command", fake)
    with pytest.raises(CommandError):
        tenders.worker.handler({"task": "daily"})
    assert made == ["ingest_canadabuys", "extract_tenders"]
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
