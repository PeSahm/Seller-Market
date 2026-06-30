"""Invalid-credentials detection for the bot's broker logins.

When a broker POSITIVELY rejects the username/password (as opposed to a captcha
misread, which must keep retrying), we want to SKIP that account for the run
instead of burning the full retry budget (ephoenix 100x, exir 6x) on a password
that will never work.

The classifiers are CONSERVATIVE: only a high-confidence reject marker raises
``InvalidCredentialsError``; anything ambiguous (bad captcha, transport error,
non-JSON, unknown code) returns False so the caller keeps its existing
retry-the-captcha behaviour. A false positive would skip a GOOD account and
stop it trading — unacceptable. Markers are from the live probe; see
``scratch/CRED_STATUS_FINDINGS.md``.
"""
from __future__ import annotations


class InvalidCredentialsError(Exception):
    """The broker positively rejected the username/password (NOT a captcha miss).

    Raised from the login path so the per-account caller can skip the account
    for this run rather than retrying a password that will never succeed.
    """


# ephoenix family + ibtrader: the login is HTTP 200 in every case; the body's
# numeric ``errorCode`` is the language-independent discriminator.
#   0     → success (token present)
#   3000  → wrong username/password   → INVALID_CREDENTIALS
#   -1000 → wrong captcha (retry)
_EPH_ERRCODE_INVALID_CREDENTIALS = 3000


def ephoenix_login_is_invalid_credentials(body: object) -> bool:
    """True iff an ephoenix-family login body is a high-confidence wrong-password
    reject. Conservative: only ``errorCode == 3000`` with no token qualifies."""
    return (
        isinstance(body, dict)
        and not body.get("token")
        and body.get("errorCode") == _EPH_ERRCODE_INVALID_CREDENTIALS
    )


# exir / Rayan HamAfza: the failure body carries ``type=="error"`` + a numeric
# ``errorCode`` (LIVE-confirmed on khobregan — see CRED_STATUS_FINDINGS.md):
#   40037 (HTTP 403) → wrong username/password → INVALID_CREDENTIALS
#   9002  (HTTP 401) → wrong captcha           → retry
# We key on the numeric code (language-independent) rather than the Persian
# ``description`` (which has a trailing space + yeh-spelling variants).
_EXIR_ERRCODE_INVALID_CREDENTIALS = 40037


def exir_login_is_invalid_credentials(body: object) -> bool:
    """True iff an exir login body is a high-confidence wrong-password reject.
    Conservative: only ``errorCode == 40037`` qualifies."""
    return (
        isinstance(body, dict)
        and body.get("errorCode") == _EXIR_ERRCODE_INVALID_CREDENTIALS
    )


# OnlinePlus / Tadbir Online+ (Hafez et al.): the login is HTTP 200 in every
# case; the body's string ``MessageCode`` is the language-independent
# discriminator (LIVE-confirmed on Hafez — see CRED_STATUS_FINDINGS.md):
#   IsSuccessfull:true + Data.Token → success
#   oms_1000       → wrong username/password → INVALID_CREDENTIALS
#   InvalidCaptcha → wrong captcha           → retry
# We key on the code (case-insensitive), not the Persian ``MessageDesc``.
_ONLINEPLUS_MSGCODE_INVALID_CREDENTIALS = "oms_1000"


def onlineplus_login_is_invalid_credentials(body: object) -> bool:
    """True iff an OnlinePlus login body is a high-confidence wrong-password
    reject. Conservative: only ``MessageCode == 'oms_1000'`` (case-insensitive)
    on a non-success body qualifies; everything else (incl. ``InvalidCaptcha``)
    returns False so the caller keeps retrying the captcha."""
    if not isinstance(body, dict) or body.get("IsSuccessfull"):
        return False
    code = body.get("MessageCode")
    return (
        isinstance(code, str)
        and code.strip().lower() == _ONLINEPLUS_MSGCODE_INVALID_CREDENTIALS
    )


# Mofid / Orbis (easytrader.ir): the OAuth login is an HTML form on
# login.emofid.com — failures come back as Persian text inside a
# ``<div class="validation-summary-errors">``. We key on the Persian markers
# (the only discriminator the HTML gives — there is no numeric code on this page;
# LIVE-confirmed shapes from the decompiled CheetahPlus EasyTraderWebApi + the
# Phase-0 spike, see scratch/MOFID_FINDINGS.md):
#   "نام کاربری یا کلمه عبور نادرست است" → wrong username/password → INVALID_CREDENTIALS
#   "کد امنیتی را وارد کنید."           → captcha required (retry WITH a captcha)
#   "کد امنیتی اشتباه است"              → wrong captcha (retry)
_MOFID_MARK_INVALID_CREDENTIALS = "نام کاربری یا کلمه عبور نادرست است"
_MOFID_MARK_CAPTCHA_REQUIRED = "کد امنیتی را وارد کنید"
_MOFID_MARK_WRONG_CAPTCHA = "کد امنیتی اشتباه است"


def mofid_login_reject(html: object) -> str | None:
    """Classify a Mofid login-page reject from its HTML body.

    Returns ``"invalid_credentials"`` | ``"captcha_required"`` | ``"wrong_captcha"``
    | ``None`` (no recognised marker). Conservative: the creds marker is checked
    FIRST (it's only shown once captcha passes, so it's the high-confidence
    reject); captcha markers mean "retry". Anything else → ``None`` (retry)."""
    if not isinstance(html, str):
        return None
    if _MOFID_MARK_INVALID_CREDENTIALS in html:
        return "invalid_credentials"
    if _MOFID_MARK_WRONG_CAPTCHA in html:
        return "wrong_captcha"
    if _MOFID_MARK_CAPTCHA_REQUIRED in html:
        return "captcha_required"
    return None


def mofid_login_is_invalid_credentials(html: object) -> bool:
    """True iff a Mofid login HTML body is a high-confidence wrong-password
    reject. Conservative: only the exact wrong-credentials marker qualifies."""
    return mofid_login_reject(html) == "invalid_credentials"
