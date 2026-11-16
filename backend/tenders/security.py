"""Content-Security-Policy on every response (Django 5.2 has no built-in CSP).

The public pages load one self-hosted stylesheet and nothing else: no scripts, no inline
styles, no images, no third-party origins. The admin also needs its own static scripts and
the data: icons in its CSS, so /admin/ gets that much more and no more.
X-Frame-Options (DENY) and Referrer-Policy come from Django's own middleware and settings.
"""

_COMMON = "default-src 'none'; style-src 'self'; base-uri 'none'; form-action 'self'; " \
          "frame-ancestors 'none'"
PUBLIC_CSP = _COMMON
ADMIN_CSP = f"{_COMMON}; script-src 'self'; img-src 'self' data:; font-src 'self'; " \
            "connect-src 'self'"


class ContentSecurityPolicyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if "Content-Security-Policy" not in response:
            admin = request.path.startswith("/admin/")
            response["Content-Security-Policy"] = ADMIN_CSP if admin else PUBLIC_CSP
        return response
