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


def _check_prefix(prefix: str) -> None:
    if not prefix.startswith("/") or not prefix.endswith("/"):
        raise ImproperlyConfigured(f"SSM_PREFIX={prefix!r} must start and end with '/'")


def _client(client):
    if client is None:
        import boto3  # only in production; local runs never import it

        client = boto3.client("ssm")
    return client


def _get(client, full_name: str) -> str:
    """One ssm:GetParameter call (the only SSM action IAM grants), decrypted."""
    try:
        resp = client.get_parameter(Name=full_name, WithDecryption=True)
    except Exception as e:  # botocore errors carry names and codes, never values
        code = getattr(e, "response", {}).get("Error", {}).get("Code") or type(e).__name__
        raise ImproperlyConfigured(f"SSM parameter {full_name}: {code}") from e
    value = resp["Parameter"]["Value"]
    if not value.strip():
        raise ImproperlyConfigured(f"SSM parameter {full_name} is empty")
    return value


@functools.cache
def fetch_secrets(prefix: str, names: tuple[str, ...] = SECRET_NAMES, client=None) -> dict[str, str]:
    """Return {name: value} for the ``<prefix><name>`` SecureStrings. A missing or
    unreadable parameter fails startup with its name only."""
    _check_prefix(prefix)
    client = _client(client)
    return {name: _get(client, f"{prefix}{name}") for name in names}


@functools.cache
def fetch_direct_database_url(prefix: str, client=None) -> dict[str, str]:
    """Return {"DATABASE_URL_DIRECT": value} from ``<prefix>database_url_direct``."""
    _check_prefix(prefix)
    return {"DATABASE_URL_DIRECT": _get(_client(client), f"{prefix}{DIRECT_URL_PARAM}")}
