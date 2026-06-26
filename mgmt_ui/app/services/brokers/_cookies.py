"""Cookie-jar helpers shared by the cookie-auth broker families (exir, onlineplus)."""
from __future__ import annotations


def cookies_to_dict(jar) -> dict:
    """Flatten an httpx cookie jar to ``{name: value}`` WITHOUT a CookieConflict.

    ``dict(httpx.Cookies)`` goes through ``Cookies.__getitem__`` → ``.get(name)``,
    which RAISES ``httpx.CookieConflict`` when the response set two cookies with
    the SAME name on different paths/domains — the F5 BIG-IP ``f5avr…_session_``
    pair in front of Hafez (OnlinePlus). Iterating the underlying jar's ``Cookie``
    objects is duplicate-safe; the unique-named auth cookie
    (``AuthCookie_OnlineCookie`` / exir's session cookie) is preserved. Pass
    ``client.cookies.jar`` (the ``http.cookiejar.CookieJar``)."""
    return {c.name: c.value for c in jar}
