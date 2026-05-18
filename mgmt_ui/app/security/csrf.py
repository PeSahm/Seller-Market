"""CSRF protection -- double-submit cookie pattern.

Issues an HMAC-signed token on every GET response, validates on every
state-changing request. Validation accepts the token in either the
``X-CSRF-Token`` header (HTMX path) or the ``csrf_token`` form field
(plain HTML form path), as long as it matches the same value in the
``csrf_token`` cookie.

Why double-submit cookie
------------------------
* Attacker on a different origin cannot read the ``csrf_token`` cookie
  (it is same-origin-only by browser policy).
* Attacker cannot set the ``X-CSRF-Token`` header on a cross-origin
  request (CORS preflight blocks custom headers, and we do not advertise
  this one as allowed).
* Attacker CAN auto-submit a form to our origin, but cannot read the
  cookie to put the matching value in the form's ``csrf_token`` field.

So matching cookie + header/field = the request was authored on our
origin by code that could read the cookie = same-origin = OK.

Token shape
-----------
Each token is the hex encoding of ``nonce || timestamp || hmac`` where:

* ``nonce``     is 16 random bytes (``secrets.token_bytes(16)``)
* ``timestamp`` is the issue time as 8 big-endian bytes (unix seconds)
* ``hmac``      is HMAC-SHA256(secret, nonce || timestamp), 32 bytes

Total raw 56 bytes -> 112 hex chars. The TTL guard rejects tokens
older than ``TOKEN_TTL_SECONDS`` (matches the access-token cookie
lifetime so a logged-in user's CSRF token never outlives their session).
"""
from __future__ import annotations

import hmac
import secrets
import time
from hashlib import sha256
from typing import Optional

from fastapi import HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse, JSONResponse, Response


def _csrf_reject(request: Request, detail: str) -> Response:
    """Build a 403 response in the right shape for the caller.

    BaseHTTPMiddleware runs OUTSIDE the FastAPI exception-handler
    stack, so raising HTTPException from here ends up wrapped as a
    500. Returning a plain Response is the only way to get a clean
    403 back to the client.

    HTMX callers get a tiny HTML snippet so the swap target isn't
    blanked out; plain browser navigations get a full HTML page;
    JSON / API clients get a JSON body.
    """
    is_htmx = request.headers.get("HX-Request", "").lower() == "true"
    accept = request.headers.get("accept", "")
    if is_htmx:
        return HTMLResponse(
            content=f'<div class="flash flash--error">{detail}. Reload the page and retry.</div>',
            status_code=status.HTTP_403_FORBIDDEN,
        )
    if "application/json" in accept and "text/html" not in accept:
        return JSONResponse(
            content={"detail": detail},
            status_code=status.HTTP_403_FORBIDDEN,
        )
    return HTMLResponse(
        content=(
            "<!DOCTYPE html><html><head><title>Forbidden</title>"
            "<meta charset='utf-8'></head><body>"
            "<h1>403 Forbidden</h1>"
            f"<p>{detail}.</p>"
            "<p><a href='/'>Reload</a> and try again.</p>"
            "</body></html>"
        ),
        status_code=status.HTTP_403_FORBIDDEN,
    )

# ---------------------------------------------------------------------------
# Public constants -- import these from templates / route handlers rather
# than re-typing the string literals.
# ---------------------------------------------------------------------------
CSRF_COOKIE = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FORM_FIELD = "csrf_token"

# Match the access-token cookie's max-age so a tab kept open all day does
# not start failing CSRF before the user is logged out anyway.
TOKEN_TTL_SECONDS = 8 * 3600

# Routes that NEVER require CSRF verification, even on POST. Keep this list
# tight -- anything that mutates state and is NOT on this list MUST pass.
EXEMPT_PATHS_EXACT: set[str] = {
    # User has no session yet on the login POST, so there is no cookie to
    # double-submit against. The login form itself is the credential check.
    "/auth/login",
    # Health / readiness probes are usually called by load balancers and
    # monitoring -- no browser, no cookie.
    "/health",
    "/ready",
    # FastAPI's auto-docs surface.
    "/openapi.json",
    "/docs",
    "/redoc",
    "/docs/oauth2-redirect",
}

# Path prefixes that are wholesale exempt. ``/static/`` is GET-only assets
# (so the mutation guard would already let it through), but listing it here
# also short-circuits the cookie rotation below. ``/ws/`` is the WebSocket
# upgrade path -- the WS protocol is not a CSRF target (browsers don't send
# auth cookies in cross-origin WS handshakes the same way) and the parallel
# ws-token JWT covers it.
EXEMPT_PATH_PREFIXES: tuple[str, ...] = ("/static/", "/ws/")


# ---------------------------------------------------------------------------
# Token primitives. The split into ``_build_token`` / ``_verify_token`` /
# ``issue_token`` is so the unit tests can reach into the low-level helpers
# without going through the middleware.
# ---------------------------------------------------------------------------
def _build_token(secret: bytes) -> str:
    """Construct a fresh signed token.

    Returns the hex encoding of ``nonce || timestamp || hmac`` so the
    whole value is safe to put in an HTTP cookie / header without further
    escaping.
    """
    nonce = secrets.token_bytes(16)
    ts = int(time.time()).to_bytes(8, "big")
    body = nonce + ts
    sig = hmac.new(secret, body, sha256).digest()
    return (body + sig).hex()


def _verify_token(secret: bytes, token: str) -> bool:
    """Constant-time verification of a signed token.

    Returns ``True`` only when EVERY check passes:

    * the token decodes from hex
    * the raw length matches the expected nonce + timestamp + hmac layout
    * the HMAC signature is correct under ``secret``
    * the embedded timestamp is not in the future and not older than
      ``TOKEN_TTL_SECONDS``
    """
    try:
        raw = bytes.fromhex(token)
    except ValueError:
        return False
    if len(raw) != 16 + 8 + 32:
        return False
    body, sig = raw[:24], raw[24:]
    expected = hmac.new(secret, body, sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return False
    ts = int.from_bytes(body[16:24], "big")
    age = int(time.time()) - ts
    if age < 0 or age > TOKEN_TTL_SECONDS:
        return False
    return True


def issue_token(secret: bytes) -> str:
    """Public wrapper around ``_build_token`` for callers that want a fresh
    token outside the middleware (e.g. a test helper)."""
    return _build_token(secret)


def get_csrf_token_from_request(request: Request) -> Optional[str]:
    """Return the cookie-side CSRF token for use as a template variable.

    The template context renders this into both a ``<meta>`` tag (so
    ``app.js`` can attach it to every HTMX request) and a hidden form
    field via the ``partials/csrf.html`` partial.
    """
    return request.cookies.get(CSRF_COOKIE)


def _is_exempt(path: str) -> bool:
    """Whether this request path should skip the CSRF check entirely."""
    if path in EXEMPT_PATHS_EXACT:
        return True
    for prefix in EXEMPT_PATH_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
class CSRFMiddleware(BaseHTTPMiddleware):
    """Issue + verify CSRF tokens.

    Register order matters: ``app.add_middleware(CSRFMiddleware, ...)``
    must run BEFORE the route is hit. Starlette wraps middleware in LIFO
    order so the call site in ``app.main.create_app`` should add this
    middleware once, near the StaticFiles mount.

    The 403 ``HTTPException`` raised on a CSRF failure is caught by the
    existing ``http_exception_handler`` in ``app.main`` and rendered as
    JSON for API clients / HTML for browser clients.
    """

    def __init__(self, app, *, secret: bytes, cookie_secure: bool = True) -> None:
        super().__init__(app)
        self.secret = secret
        self.cookie_secure = cookie_secure

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        path = request.url.path
        method = request.method.upper()
        is_mutation = method in {"POST", "PUT", "PATCH", "DELETE"}

        if is_mutation and not _is_exempt(path):
            cookie_token = request.cookies.get(CSRF_COOKIE)
            if not cookie_token or not _verify_token(self.secret, cookie_token):
                # NOTE: must return a Response here, NOT raise
                # HTTPException — Starlette's BaseHTTPMiddleware wraps
                # any raised exception into a 500 (because the middleware
                # runs OUTSIDE the FastAPI exception handler stack).
                return _csrf_reject(request, "CSRF token missing or invalid")

            # Submitted token can arrive in EITHER the X-CSRF-Token header
            # (HTMX / API JSON path) OR a csrf_token form field (plain
            # browser form POST). We accept both so the same middleware
            # covers every state-changing route in the app.
            submitted: Optional[str] = request.headers.get(CSRF_HEADER)
            if not submitted:
                content_type = request.headers.get("content-type", "")
                if content_type.startswith(
                    ("application/x-www-form-urlencoded", "multipart/form-data")
                ):
                    # Reading the form here caches it on ``request._form``
                    # so the downstream FastAPI ``Form(...)`` dependency
                    # gets the same parsed object back -- no body re-read.
                    form = await request.form()
                    submitted = form.get(CSRF_FORM_FIELD)

            if not submitted or not hmac.compare_digest(
                str(submitted), str(cookie_token)
            ):
                return _csrf_reject(request, "CSRF token mismatch")

        response: Response = await call_next(request)

        # Token rotation: every GET response gets a fresh token. This
        # caps how long any single token is exposed in browser memory /
        # devtools, and keeps the value alive for the user's session
        # without ever having to invalidate the old one explicitly --
        # the new Set-Cookie just supersedes it.
        #
        # We skip the rotation on asset endpoints both to save bandwidth
        # (Set-Cookie on every image / CSS request is wasted bytes) and
        # to keep the OpenAPI / docs response bodies clean for clients
        # that inspect them.
        if method == "GET" and not path.startswith(
            ("/static/", "/openapi.json", "/docs", "/redoc")
        ):
            new_token = _build_token(self.secret)
            response.set_cookie(
                key=CSRF_COOKIE,
                value=new_token,
                max_age=TOKEN_TTL_SECONDS,
                httponly=False,  # JS reads this so HTMX can echo it
                secure=self.cookie_secure,
                samesite="lax",
                path="/",
            )
        return response
