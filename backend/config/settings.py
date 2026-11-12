import os
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
    "tenders",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",  # admin static files, no S3/CDN
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

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
