"""Admin hardening: login lockout (django-axes), the client IP it keys on, sessions."""

import ast
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from axes.models import AccessAttempt
from django.contrib.auth.models import User

from tenders.security import client_ip

LOGIN = "/admin/login/"
PASSWORD = "correct horse battery staple"  # test-only


@pytest.fixture(autouse=True)
def behind_api_gateway(settings):
    """The deployed config (DEBUG off), whatever a local .env says."""
    settings.TRUST_X_FORWARDED_FOR = True


def req(xff=None, remote="127.0.0.1"):
    meta = {"REMOTE_ADDR": remote}
    if xff is not None:
        meta["HTTP_X_FORWARDED_FOR"] = xff
    return SimpleNamespace(META=meta)


# --- which address ------------------------------------------------------------------

def test_client_ip_is_the_hop_api_gateway_appended():
    # No client header: API Gateway's own entry is the only one.
    assert client_ip(req("203.0.113.9")) == "203.0.113.9"
    # The client sent its own X-Forwarded-For: API Gateway appends the real address last.
    assert client_ip(req("6.6.6.6, 10.0.0.1, 203.0.113.9")) == "203.0.113.9"
    assert client_ip(req("2001:db8::1")) == "2001:db8::1"


def test_spoofed_left_hops_never_change_the_address():
    real = "203.0.113.9"
    seen = {client_ip(req(f"198.51.100.{i}, {real}")) for i in range(1, 20)}
    assert seen == {real}


def test_bad_or_missing_hop_falls_back_to_remote_addr():
    # On Lambda REMOTE_ADDR is the Web Adapter (127.0.0.1): shared, so it locks more, not less.
    assert client_ip(req(None)) == "127.0.0.1"
    assert client_ip(req("203.0.113.9, not-an-ip")) == "127.0.0.1"
    assert client_ip(req("")) == "127.0.0.1"


def test_header_ignored_without_a_trusted_proxy(settings):
    settings.TRUST_X_FORWARDED_FOR = False  # local, no proxy in front
    assert client_ip(req("203.0.113.9", remote="192.0.2.7")) == "192.0.2.7"


def test_axes_uses_this_callable(settings):
    assert settings.AXES_CLIENT_IP_CALLABLE == "tenders.security.client_ip"


# --- lockout ------------------------------------------------------------------------

@pytest.fixture
def admin_user(db):
    return User.objects.create_superuser("boss", password=PASSWORD)


def login(client, password, ip="203.0.113.9", spoof=None, username="boss"):
    xff = f"{spoof}, {ip}" if spoof else ip
    return client.post(LOGIN, {"username": username, "password": password, "next": "/admin/"},
                       HTTP_X_FORWARDED_FOR=xff)


def test_settings_lock_5_failures_for_an_hour_by_username_and_ip(settings):
    assert settings.AXES_FAILURE_LIMIT == 5
    assert settings.AXES_COOLOFF_TIME == timedelta(hours=1)
    assert settings.AXES_LOCKOUT_PARAMETERS == [["username", "ip_address"]]
    assert settings.AUTHENTICATION_BACKENDS[0] == "axes.backends.AxesStandaloneBackend"
    assert settings.MIDDLEWARE[-1] == "axes.middleware.AxesMiddleware"


def test_lockout_after_5_failures(client, admin_user):
    for i in range(4):
        r = login(client, f"wrong-{i}")
        assert r.status_code == 200 and b"Please enter the correct" in r.content
    r = login(client, "wrong-4")  # the 5th failure locks
    assert r.status_code == 429
    assert b"Account temporarily locked" in r.content
    # Locked: even the right password is refused, and no session is made.
    r = login(client, PASSWORD)
    assert r.status_code == 429 and "_auth_user_id" not in client.session
    attempt = AccessAttempt.objects.get()
    # 5 failures, plus the refused attempt while locked (which also restarts the hour for
    # this username and address only).
    assert (attempt.username, attempt.ip_address, attempt.failures_since_start) == (
        "boss", "203.0.113.9", 6)
    assert PASSWORD not in str(attempt.post_data) and "wrong-" not in str(attempt.post_data)


def test_lock_is_per_address(client, admin_user):
    for i in range(5):
        login(client, f"wrong-{i}", ip="198.51.100.66")  # an attacker elsewhere
    assert login(client, PASSWORD, ip="198.51.100.66").status_code == 429
    # The real admin, from another address, is not locked out.
    r = login(client, PASSWORD, ip="203.0.113.9")
    assert r.status_code == 302 and client.session["_auth_user_id"] == str(admin_user.pk)


def test_spoofing_x_forwarded_for_does_not_reset_the_count(client, admin_user):
    for i in range(5):
        login(client, f"wrong-{i}", spoof=f"10.9.9.{i}")  # a new fake address each time
    assert login(client, PASSWORD, spoof="10.9.9.99").status_code == 429
    assert AccessAttempt.objects.get().ip_address == "203.0.113.9"


def test_lock_expires_after_an_hour(client, admin_user):
    for i in range(5):
        login(client, f"wrong-{i}")
    assert login(client, PASSWORD).status_code == 429
    a = AccessAttempt.objects.get()
    AccessAttempt.objects.filter(pk=a.pk).update(
        attempt_time=a.attempt_time - timedelta(hours=1, minutes=1))
    assert login(client, PASSWORD).status_code == 302


def test_success_resets_the_count(client, admin_user):
    for i in range(4):
        login(client, f"wrong-{i}")
    assert login(client, PASSWORD).status_code == 302
    client.logout()
    for i in range(4):
        assert login(client, f"again-{i}").status_code == 200  # counting from zero again


# --- sessions -----------------------------------------------------------------------

def test_admin_session_cookie_is_8_hours_and_httponly(client, admin_user):
    r = login(client, PASSWORD)
    assert r.status_code == 302
    cookie = r.cookies["sessionid"]
    assert cookie["max-age"] == 8 * 60 * 60
    assert cookie["httponly"] is True


def _settings_with(debug: str) -> dict:
    code = ("import django; django.setup(); from django.conf import settings as s; "
            "print(repr({k: getattr(s, k) for k in ('SESSION_COOKIE_SECURE', "
            "'CSRF_COOKIE_SECURE', 'SESSION_COOKIE_HTTPONLY', 'CSRF_COOKIE_HTTPONLY', "
            "'SESSION_COOKIE_SAMESITE', 'CSRF_COOKIE_SAMESITE', "
            "'SESSION_COOKIE_AGE', 'TRUST_X_FORWARDED_FOR')}))")
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings", "DJANGO_DEBUG": debug,
           "DJANGO_SECRET_KEY": "test-only", "DATABASE_URL": "sqlite:///:memory:",
           "DATABASE_URL_DIRECT": ""}
    env.pop("SSM_PREFIX", None)
    backend = Path(__file__).resolve().parents[2]
    out = subprocess.run([sys.executable, "-c", code], cwd=backend, env=env, check=True,
                         capture_output=True, text=True, timeout=60)
    return ast.literal_eval(out.stdout)


def test_deployed_cookies_are_secure_and_httponly():
    """DEBUG off (the Lambda config): Secure, HttpOnly and SameSite=Lax on both cookies,
    8-hour sessions, and the client IP taken from API Gateway's X-Forwarded-For hop."""
    assert _settings_with("0") == {
        "SESSION_COOKIE_SECURE": True, "CSRF_COOKIE_SECURE": True,
        "SESSION_COOKIE_HTTPONLY": True, "CSRF_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax", "CSRF_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_AGE": 28800, "TRUST_X_FORWARDED_FOR": True}


def test_local_debug_does_not_trust_x_forwarded_for():
    s = _settings_with("1")
    assert s["TRUST_X_FORWARDED_FOR"] is False and s["SESSION_COOKIE_SECURE"] is False
