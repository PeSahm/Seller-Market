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
