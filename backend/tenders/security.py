"""Response headers Django 5.2 has no setting for: Content-Security-Policy and
Permissions-Policy, on every response.

The public pages load one self-hosted stylesheet and nothing else: no scripts, no inline
styles, no images, no third-party origins. The admin also needs its own static scripts and
the data: icons in its CSS, so /admin/ gets that much more and no more. Neither needs
'unsafe-inline': the only inline <style> in Django's admin templates (change_list.html) is
rendered only for a ModelAdmin with actions_on_top and actions_on_bottom both False, and
none here sets those.

HSTS, nosniff, X-Frame-Options (DENY), Referrer-Policy and the cookie flags come from
Django's own middleware and settings (config/settings.py).

Also the client IP that the login lockout (django-axes) keys on: client_ip().
"""

import ipaddress

from django.conf import settings

_COMMON = "default-src 'none'; style-src 'self'; base-uri 'none'; form-action 'self'; " \
          "frame-ancestors 'none'"
PUBLIC_CSP = _COMMON
ADMIN_CSP = f"{_COMMON}; script-src 'self'; img-src 'self' data:; font-src 'self'; " \
            "connect-src 'self'"

# Nothing on the site uses a device API; deny the common ones to this page and any frame.
PERMISSIONS_POLICY = ", ".join(f"{feature}=()" for feature in (
    "accelerometer", "camera", "geolocation", "gyroscope", "magnetometer", "microphone",
    "payment", "usb"))


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if "Content-Security-Policy" not in response:
            admin = request.path.startswith("/admin/")
            response["Content-Security-Policy"] = ADMIN_CSP if admin else PUBLIC_CSP
        response.setdefault("Permissions-Policy", PERMISSIONS_POLICY)
        return response


def _ip(value: str | None) -> str | None:
    try:
        return str(ipaddress.ip_address((value or "").strip()))
    except ValueError:
        return None


def client_ip(request) -> str | None:
    """The address the login lockout keys on (settings.AXES_CLIENT_IP_CALLABLE).

    On Lambda, gunicorn's REMOTE_ADDR is always 127.0.0.1: the Web Adapter in the same
    sandbox. The client's address is in X-Forwarded-For, which API Gateway *appends* to:
    a client that sends "X-Forwarded-For: 1.2.3.4" arrives as "1.2.3.4, <real address>".
    So the left-most hop is whatever the client wrote, and only the right-most hop is
    API Gateway's own (the Web Adapter passes the header through unchanged). Taking the
    left-most would let an attacker send a new fake address per attempt and never be
    locked out; taking REMOTE_ADDR would put everyone on one address, so a stranger could
    lock the real admin out.

    Only when settings.TRUST_X_FORWARDED_FOR (DEBUG off, i.e. behind API Gateway). Locally
    there is no proxy, so the header is ignored. A missing or malformed hop falls back to
    REMOTE_ADDR: on Lambda that is the shared 127.0.0.1, which locks more, never less.
    """
    if settings.TRUST_X_FORWARDED_FOR:
        hops = request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")
        if ip := _ip(hops[-1]):
            return ip
    return _ip(request.META.get("REMOTE_ADDR"))
