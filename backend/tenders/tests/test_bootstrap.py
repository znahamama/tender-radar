"""The worker's create_admin task: a one-time SSM password, Django's validators, no leaks."""

import json

import pytest
from axes.models import AccessAttempt
from config.ssm import fetch_admin_bootstrap_password
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

import tenders.worker
from tenders.bootstrap import BootstrapError
from tenders.tests.test_deploy import FakeSSM

PREFIX = "/tender-radar/prod/"
PARAM = f"{PREFIX}admin_bootstrap_password"
STRONG = "Fjord-lantern-41-quietly-ballast"  # test-only
WEAK = "short1"


@pytest.fixture
def ssm(monkeypatch, settings):
    """SSM_PREFIX set as on Lambda; the parameter store is a FakeSSM."""
    settings.SSM_PREFIX = PREFIX
    store = {}
    fake = FakeSSM(store)
    monkeypatch.setattr(tenders.worker, "fetch_admin_bootstrap_password",
                        lambda prefix: fetch_admin_bootstrap_password(prefix, client=fake))
    # The handler closes connections at the end; inside a test transaction that would
    # break the assertions that follow.
    monkeypatch.setattr(tenders.worker.connections, "close_all", lambda: None)
    fake.store = store
    return fake


def run(username="alice"):
    return tenders.worker.handler({"task": "create_admin", "username": username})


def output(capsys):
    out = capsys.readouterr()
    return out.out + out.err


@pytest.mark.django_db
def test_strong_password_creates_a_superuser(ssm, capsys):
    ssm.store[PARAM] = STRONG
    summary = run()
    u = User.objects.get(username="alice")
    assert u.is_superuser and u.is_staff and u.is_active and u.check_password(STRONG)
    logged = output(capsys)
    assert "admin created: alice" in logged
    assert STRONG not in logged and STRONG not in json.dumps(summary)
    assert summary["ok"] is True and summary["steps"][0]["step"] == "create_admin"
    assert ssm.calls == [((PARAM,), True)]  # one decrypted GetParameters call


@pytest.mark.django_db
def test_rerun_updates_the_password_and_clears_the_lockout(ssm, capsys):
    User.objects.create_user("alice", password="Old-password-that-is-long-9")
    AccessAttempt.objects.create(username="alice", ip_address="203.0.113.9", user_agent="",
                                 get_data="", post_data="", http_accept="", path_info="",
                                 failures_since_start=5, attempt_time=timezone.now())
    ssm.store[PARAM] = STRONG
    run()
    u = User.objects.get(username="alice")
    assert u.check_password(STRONG) and u.is_superuser and u.is_staff
    assert "admin updated: alice" in output(capsys)
    assert not AccessAttempt.objects.filter(username="alice").exists()
    run()
    assert ssm.calls == [((PARAM,), True)] * 2  # never cached: read fresh each run


@pytest.mark.django_db
@pytest.mark.parametrize("username, weak, reason", [
    ("alice", WEAK, "too short"),
    ("alice", "password1234", "too common"),
    ("alice", "381947265019384", "entirely numeric"),
    ("alicesmith", "alicesmith-admin", "too similar"),
])
def test_weak_password_fails_clearly_and_writes_nothing(ssm, capsys, username, weak, reason):
    ssm.store[PARAM] = weak
    with pytest.raises(BootstrapError, match=reason):
        run(username)
    assert not User.objects.filter(username=username).exists()
    logged = output(capsys)
    summary = json.loads(logged.strip().splitlines()[-1])
    assert summary["ok"] is False and "password validators" in summary["steps"][0]["error"]
    assert weak not in logged


@pytest.mark.django_db
def test_weak_password_leaves_an_existing_admin_unchanged(ssm):
    User.objects.create_superuser("alice", password=STRONG)
    ssm.store[PARAM] = WEAK
    with pytest.raises(BootstrapError):
        run()
    assert User.objects.get(username="alice").check_password(STRONG)


@pytest.mark.django_db
def test_missing_parameter_names_it(ssm, capsys):
    with pytest.raises(ImproperlyConfigured, match=f"{PARAM}: ParameterNotFound"):
        run()
    assert not User.objects.exists()
    summary = json.loads(output(capsys).strip().splitlines()[-1])
    assert "ParameterNotFound" in summary["steps"][0]["error"]


@pytest.mark.django_db
@pytest.mark.parametrize("username", [None, "", "   ", 42])
def test_username_is_required(ssm, username):
    ssm.store[PARAM] = STRONG
    event = {"task": "create_admin"} if username is None else {"task": "create_admin",
                                                               "username": username}
    with pytest.raises(ValueError, match="needs \"username\""):
        tenders.worker.handler(event)
    assert ssm.calls == []  # refused before reading the password


@pytest.mark.django_db
def test_invalid_username_is_refused(ssm):
    ssm.store[PARAM] = STRONG
    with pytest.raises(BootstrapError, match="not valid"):
        run("bad name!")
    assert not User.objects.exists()


def test_runs_on_lambda_only(settings, monkeypatch):
    settings.SSM_PREFIX = ""
    monkeypatch.setattr(tenders.worker.connections, "close_all", lambda: None)
    with pytest.raises(RuntimeError, match="createsuperuser"):
        run()


def test_create_admin_is_a_known_task():
    assert "create_admin" in tenders.worker.TASKS and "create_admin" in tenders.worker.STEPS
