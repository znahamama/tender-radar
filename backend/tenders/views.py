import logging

from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET

log = logging.getLogger(__name__)


@require_GET
def health(request):
    """Liveness for the Web Adapter's readiness check. Never touches the DB, so it never
    wakes the Neon compute."""
    return JsonResponse({"status": "ok", "version": settings.APP_VERSION})


@require_GET
def health_db(request):
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception:
        log.exception("health/db: SELECT 1 failed")
        return JsonResponse({"status": "error", "db": "unavailable"}, status=503)
    return JsonResponse({"status": "ok", "db": "ok"})
