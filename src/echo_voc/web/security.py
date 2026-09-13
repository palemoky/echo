"""Safety for a local, unauthenticated tool: reject cross-site writes and foreign Host headers."""

from __future__ import annotations

import base64
import hmac
import os
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "testserver"}


def allowed_hosts(bind_host: str | None) -> set[str]:
    hosts = set(LOCAL_HOSTS)
    if bind_host and bind_host not in {"0.0.0.0", "::"}:
        hosts.add(bind_host)
    extra = os.getenv("ECHO_WEB_ALLOWED_HOSTS", "")
    hosts.update(h.strip() for h in extra.split(",") if h.strip())
    return hosts


class LocalGuardMiddleware(BaseHTTPMiddleware):
    """
    - Host allow-list: blocks DNS-rebinding attacks against the local server.
    - Writes must come from the page itself: htmx sends ``HX-Request``, which a cross-site form
      cannot set, and any ``Origin`` header must match the Host.
    - Optional HTTP Basic auth when ECHO_WEB_PASSWORD is set (e.g. when exposing on a LAN).
    """

    def __init__(self, app, hosts: set[str], password: str | None = None) -> None:
        super().__init__(app)
        self.hosts = hosts
        self.password = password

    async def dispatch(self, request: Request, call_next) -> Response:
        raw = request.headers.get("host", "")
        host = raw.split("]", 1)[0] + "]" if raw.startswith("[") else raw.split(":", 1)[0]
        if "*" not in self.hosts and host not in self.hosts:
            return PlainTextResponse("Host not allowed (set ECHO_WEB_ALLOWED_HOSTS)", 400)

        if self.password and not self._authorized(request):
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="echo"'})

        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return PlainTextResponse("Cross-origin request blocked", 403)
            if request.headers.get("hx-request") != "true":
                return PlainTextResponse("Missing HX-Request header", 403)
        return await call_next(request)

    def _authorized(self, request: Request) -> bool:
        header = request.headers.get("authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            _, _, password = base64.b64decode(header[6:]).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(password, self.password or "")
