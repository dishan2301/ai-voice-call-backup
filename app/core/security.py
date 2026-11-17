import hmac
import hashlib
import ipaddress
import re
from urllib.parse import urlparse

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import get_settings


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
LOOPBACK_NAMES = {"127.0.0.1", "localhost", "test", "testserver"}


def phone_session_token(install_code: str) -> str:
    return hmac.new(install_code.encode(), b"morning-mail-phone-session-v1", hashlib.sha256).hexdigest()


class LocalSecurityMiddleware:
    """Pure ASGI middleware; BaseHTTPMiddleware can stall on dependency errors."""

    def __init__(self, app):
        self.app = app

    @staticmethod
    def _secure_send(send):
        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Content-Security-Policy"] = (
                    "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "img-src 'self' data:; media-src 'self' blob:; form-action 'self'; frame-ancestors 'none'"
                )
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "no-referrer"
            await send(message)
        return send_with_headers

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        host = request.url.hostname
        client_host = request.client.host if request.client else ""
        settings = get_settings()
        response = None
        try:
            personal_lan = settings.deployment_mode == "personal" and ipaddress.ip_address(host).is_private
        except ValueError:
            personal_lan = False
        if host not in settings.allowed_hosts and not personal_lan:
            response = JSONResponse({"detail": "Untrusted request host"}, status_code=403)
        elif settings.deployment_mode == "hosted":
            # Only routes that resolve server-owned context may serve hosted data.
            # Expand this list as each complete route is migrated and tested.
            owned_read = request.method == "GET" and (
                request.url.path in {"/phone/recent", "/phone/commitments", "/health",
                                     "/phone/resume", "/phone/pending-actions"}
                or re.fullmatch(r"/phone/agent/[0-9]+/(audit|incident)", request.url.path))
            owned_review = request.method == "POST" and re.fullmatch(
                r"/phone/drafts/[0-9]+/(review|review-complete)", request.url.path)
            own_logout = request.method == "DELETE" and request.url.path == "/auth/session"
            owned_sync = request.method == "POST" and request.url.path == "/phone/refresh"
            owned_voice = (request.method == "POST" and request.url.path == "/phone/whatsapp/rewrite") or (request.method == "GET" and request.url.path == "/phone/voice/config") or (
                request.method == "POST" and request.url.path == "/phone/voice/offer") or (
                request.method == "DELETE" and request.url.path.startswith("/phone/voice/"))
            owned_voice = owned_voice or (request.method == "POST" and request.url.path.startswith("/phone/planner/"))
            owned_voice = owned_voice or (request.method in {'GET', 'POST'} and request.url.path.startswith('/phone/daily/'))
            google_sign_in = request.method == "GET" and request.url.path in {
                "/auth/google/start", "/auth/google/callback", "/auth/calendar/start", "/auth/calendar/callback", "/static/calendar-consent.js"}
            microsoft_pilot = (
                request.method == "GET" and request.url.path in {"/auth/microsoft/start", "/auth/microsoft/callback", "/auth/microsoft/status"}
                or request.method == "POST" and request.url.path == "/auth/microsoft/search"
                or request.method == "DELETE" and request.url.path == "/auth/microsoft/connection")
            google_sign_in = google_sign_in or microsoft_pilot
            google_sign_in = google_sign_in or (request.method == "POST" and request.url.path == "/auth/calendar/finish")
            if not owned_read and not owned_review and not own_logout and not google_sign_in and not owned_sync and not owned_voice:
                response = JSONResponse({"detail": "This feature is not available in hosted mode yet"}, status_code=503)
        elif host not in LOOPBACK_NAMES or client_host not in LOOPBACK_NAMES:
            settings = get_settings()
            supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
            briefing_allowed = (
                (request.url.path in {"/phone/briefing", "/phone/recent", "/phone/commitments", "/phone/resume", "/phone/waiting", "/phone/refresh", "/phone/agent", "/phone/pending-actions"}
                 or request.url.path.startswith("/phone/drafts/")
                 or request.url.path.startswith("/phone/voice/")
                 or (request.url.path.startswith("/phone/agent/") and request.method in SAFE_METHODS))
                and bool(settings.phone_api_token)
                and hmac.compare_digest(supplied, settings.phone_api_token)
            )
            install_code = request.query_params.get("code", "")
            paired = bool(settings.phone_install_code) and (
                hmac.compare_digest(install_code, settings.phone_install_code)
                or hmac.compare_digest(request.cookies.get("morning_mail_phone", ""),
                                       phone_session_token(settings.phone_install_code))
            )
            install_allowed = (
                request.url.path in {"/phone/app", "/phone/preview", "/phone/local-ca", "/static/phone-preview.js", "/static/voice-session.js"}
                and paired
            )
            # The preview endpoint renders a code-entry page when unpaired; it
            # never returns mail data until the code has set the session cookie.
            pairing_page_allowed = request.url.path == "/phone/preview" and request.method == "GET"
            preview_allowed = (
                (request.url.path in {"/phone/briefing", "/phone/recent", "/phone/commitments", "/phone/resume", "/phone/waiting", "/phone/refresh", "/phone/agent"}
                 or request.url.path.startswith("/phone/drafts/")
                 or request.url.path.startswith("/phone/voice/"))
                and paired
            )
            public_script = request.method in SAFE_METHODS and request.url.path in {
                "/static/phone-preview.js", "/static/voice-session.js", "/static/planner.js", "/static/planner-sw.js", "/static/calendar-consent.js", "/static/daily-briefing.js"}
            rewrite_allowed = request.method == "POST" and request.url.path == "/phone/whatsapp/rewrite" and (paired or (bool(settings.phone_api_token) and hmac.compare_digest(supplied, settings.phone_api_token)))
            rewrite_allowed = rewrite_allowed or (request.method == "POST" and request.url.path.startswith("/phone/planner/") and (paired or (bool(settings.phone_api_token) and hmac.compare_digest(supplied, settings.phone_api_token))))
            rewrite_allowed = rewrite_allowed or (request.method in {'GET', 'POST'} and request.url.path.startswith('/phone/daily/') and (paired or (bool(settings.phone_api_token) and hmac.compare_digest(supplied, settings.phone_api_token))))
            if not briefing_allowed and not preview_allowed and not install_allowed and not pairing_page_allowed and not public_script and not rewrite_allowed:
                response = JSONResponse({"detail": "Remote access refused"}, status_code=403)
        origin = request.headers.get("origin")
        if response is None and request.method not in SAFE_METHODS and origin:
            try:
                origin_host = urlparse(origin).hostname
            except ValueError:
                origin_host = None
            browser_same_origin = request.headers.get("sec-fetch-site") in {
                "same-origin",
                "none",
            }
            if origin_host not in LOOPBACK_NAMES and not browser_same_origin:
                response = JSONResponse(
                    {"detail": "Cross-origin state change refused"}, status_code=403
                )
        secure_send = self._secure_send(send)
        if response is not None:
            await response(scope, receive, secure_send)
        else:
            await self.app(scope, receive, secure_send)
