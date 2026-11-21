import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

from .envvars import env_decimal, env_float, env_int, env_str

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR.parent / ".env")  # local only; there is no .env in the image

# Lambda: SSM_PREFIX is set and the three secrets come from SSM SecureStrings, once per
# cold start. They go into os.environ so the Anthropic SDK finds its key the usual way.
SSM_PREFIX = os.environ.get("SSM_PREFIX", "").strip()
if SSM_PREFIX:
    from .ssm import fetch_secrets

    os.environ.update(fetch_secrets(SSM_PREFIX))

SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]
DEBUG = os.environ.get("DJANGO_DEBUG") == "1"
ALLOWED_HOSTS = [h.strip() for h in
                 os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
                 if h.strip()]
# Shown by /api/health; baked into the image at build time.
APP_VERSION = os.environ.get("APP_VERSION", "dev")

if not DEBUG:
    # API Gateway terminates TLS and sets X-Forwarded-Proto; the Web Adapter passes it on.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    # A wildcard, not the exact API URL: the URL only exists after the Lambda that would
    # need it, so naming it here would be a Terraform cycle.
    CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.environ.get(
        "DJANGO_CSRF_TRUSTED_ORIGINS", "https://*.execute-api.us-east-2.amazonaws.com",
    ).split(",") if o.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "axes",
    "tenders",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "tenders.security.SecurityHeadersMiddleware",  # CSP and Permissions-Policy
    "whitenoise.middleware.WhiteNoiseMiddleware",  # admin static files, no S3/CDN
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "axes.middleware.AxesMiddleware",  # last: turns a locked-out login into the lockout page
]

# Explicit, though these three are Django's defaults. CSP and Permissions-Policy are set in
# tenders.security.
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CONTENT_TYPE_NOSNIFF = True
# HSTS, one year. SecurityMiddleware sends it only on HTTPS requests (on Lambda: X-Forwarded-
# Proto from API Gateway, see SECURE_PROXY_SSL_HEADER), so never on local http. The host is
# AWS's execute-api domain: no includeSubDomains and no preload, which are for a domain we own.
SECURE_HSTS_SECONDS = 365 * 24 * 60 * 60
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "tenders.context_processors.site",
            ],
        },
    },
]

# Laptop: direct URL (long-lived process, migrations).
# Lambda: only DATABASE_URL is set, and it's the pooled one.
_db_url = os.environ.get("DATABASE_URL_DIRECT") or os.environ["DATABASE_URL"]
DATABASES = {"default": dj_database_url.parse(_db_url, conn_max_age=0)}
# Neon's pooler is PgBouncer in transaction mode: no server-side cursors.
DATABASES["default"]["DISABLE_SERVER_SIDE_CURSORS"] = True

# Login lockout (django-axes): 5 failed logins for the same username from the same client IP
# lock that pair for an hour. Keyed on the pair, so someone guessing from elsewhere locks
# only themselves out, never the real admin.
AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",  # first: refuses a locked-out pair before checking
    "django.contrib.auth.backends.ModelBackend",
]
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = timedelta(hours=1)
AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"]]
AXES_RESET_ON_SUCCESS = True
AXES_LOCKOUT_TEMPLATE = "tenders/locked.html"
AXES_CLIENT_IP_CALLABLE = "tenders.security.client_ip"
# Behind API Gateway only (DEBUG off is the deployed config): the client IP is the right-most
# X-Forwarded-For hop, the one API Gateway appended. See tenders.security.client_ip.
TRUST_X_FORWARDED_FOR = not DEBUG

# Admin sessions: 8 hours from login (not renewed per request). HttpOnly is Django's default,
# stated here; Secure is set above when DEBUG is off. The admin's JS never reads the CSRF
# cookie (it posts the form token), so that cookie is HttpOnly too.
SESSION_COOKIE_AGE = 8 * 60 * 60
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
# Django's default, stated. Lax, not Strict: Strict would drop the session when the admin is
# opened from a link elsewhere, and the admin has no state-changing GETs.
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-ca"
TIME_ZONE = "America/St_Johns"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"  # filled by collectstatic in the image build
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    # Not the Manifest variant: that one 500s on any template whose static file wasn't
    # collected, which includes the test suite. Admin assets rarely change.
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticatedOrReadOnly"],
}

# LLM extraction (tenders.extraction). The API key comes from ANTHROPIC_API_KEY in the env.
# Every value is validated here, so a typo fails at startup instead of on the first API call.
LLM_MODEL = env_str("LLM_MODEL", "claude-haiku-5-5")
LLM_EFFORT = env_str("LLM_EFFORT", "low", choices=("low", "medium", "high", "xhigh", "max"))
LLM_MAX_TOKENS = env_int("LLM_MAX_TOKENS", 2000, lo=256, hi=16_000)  # non-streaming requests
LLM_TIMEOUT_S = env_float("LLM_TIMEOUT_S", 60, lo=5, hi=600)
LLM_MAX_CALLS_PER_RUN = env_int("LLM_MAX_CALLS_PER_RUN", 50, lo=1, hi=1000)
LLM_DAILY_BUDGET_USD = env_decimal("LLM_DAILY_BUDGET_USD", "0.50",
                                   lo=Decimal(0), hi=Decimal(10))
LLM_MAX_DESCRIPTION_CHARS = env_int("LLM_MAX_DESCRIPTION_CHARS", 30_000, lo=1_000, hi=200_000)
