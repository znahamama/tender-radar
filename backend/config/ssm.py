"""Production secrets from SSM Parameter Store, fetched once per process and cached.

Only parameter NAMES appear in errors and logs, never values.
"""

from __future__ import annotations

import functools

from django.core.exceptions import ImproperlyConfigured

# Loaded at every cold start (settings.py), into os.environ under the same names.
SECRET_NAMES = ("DATABASE_URL", "ANTHROPIC_API_KEY", "DJANGO_SECRET_KEY")
# Neon's direct (unpooled) URL. Only the worker's "migrate" task loads it, as
# DATABASE_URL_DIRECT; the API and the daily task keep the pooled DATABASE_URL.
DIRECT_URL_PARAM = "database_url_direct"
# One-time: created by hand just before the worker's "create_admin" task, deleted right
# after (infra/README.md). Never loaded at cold start and never cached.
ADMIN_BOOTSTRAP_PARAM = "admin_bootstrap_password"


def _check_prefix(prefix: str) -> None:
    if not prefix.startswith("/") or not prefix.endswith("/"):
        raise ImproperlyConfigured(f"SSM_PREFIX={prefix!r} must start and end with '/'")


def _client(client):
    if client is None:
        import boto3  # only in production; local runs never import it

        client = boto3.client("ssm")
    return client


def _get_many(client, full_names: list[str]) -> dict[str, str]:
    """One ssm:GetParameters call for all names, decrypted: one round trip at cold start
    instead of one per secret. GetParameters doesn't raise for a missing name, it lists it
    under InvalidParameters, so that is checked here."""
    try:
        resp = client.get_parameters(Names=full_names, WithDecryption=True)
    except Exception as e:  # botocore errors carry names and codes, never values
        code = getattr(e, "response", {}).get("Error", {}).get("Code") or type(e).__name__
        raise ImproperlyConfigured(f"SSM parameters {', '.join(full_names)}: {code}") from e
    values = {p["Name"]: p["Value"] for p in resp.get("Parameters", [])}
    for name in full_names:
        if name not in values:
            raise ImproperlyConfigured(f"SSM parameter {name}: ParameterNotFound")
        if not values[name].strip():
            raise ImproperlyConfigured(f"SSM parameter {name} is empty")
    return values


@functools.cache
def fetch_secrets(prefix: str, names: tuple[str, ...] = SECRET_NAMES, client=None) -> dict[str, str]:
    """Return {name: value} for the ``<prefix><name>`` SecureStrings. A missing or
    unreadable parameter fails startup with its name only."""
    _check_prefix(prefix)
    values = _get_many(_client(client), [f"{prefix}{name}" for name in names])
    return {name: values[f"{prefix}{name}"] for name in names}


@functools.cache
def fetch_direct_database_url(prefix: str, client=None) -> dict[str, str]:
    """Return {"DATABASE_URL_DIRECT": value} from ``<prefix>database_url_direct``."""
    _check_prefix(prefix)
    full_name = f"{prefix}{DIRECT_URL_PARAM}"
    return {"DATABASE_URL_DIRECT": _get_many(_client(client), [full_name])[full_name]}


def fetch_admin_bootstrap_password(prefix: str, client=None) -> str:
    """The ``<prefix>admin_bootstrap_password`` SecureString. Not cached: it is read once,
    by the task that uses it, and the parameter is deleted afterwards."""
    _check_prefix(prefix)
    full_name = f"{prefix}{ADMIN_BOOTSTRAP_PARAM}"
    return _get_many(_client(client), [full_name])[full_name]
