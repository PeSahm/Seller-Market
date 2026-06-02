"""Pure Jalali (Solar Hijri) <-> Gregorian converters — no new dependency.

Exir / Rayan HamAfza speaks **Jalali** dates on the wire (``orderbookReport``
takes Jalali ``YYYY/MM/DD`` bounds and returns ``entryDateTime`` as a Jalali
``YYYY/MM/DD-HH:mm:ss`` string), while the rest of the mgmt UI — and the
dispatcher/reconciler that calls the adapter — speaks Gregorian. These helpers
bridge the two without pulling in ``jdatetime`` / ``khayyam`` or any other
third-party package.

The algorithm is the classic JDF (Jalali Date Functions) routine, which is the
de-facto reference implementation. A couple of known pairs are asserted at
import time so a regression trips loudly:

    Gregorian 2026-06-02 == Jalali 1405/03/12
    Gregorian 2024-03-20 == Jalali 1403/01/01  (Nowruz)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Asia/Tehran is a fixed +03:30 offset since 2022 (Iran abolished DST). We use a
# fixed offset deliberately so we do NOT depend on zoneinfo/tzdata being present.
_TEHRAN_TZ = timezone(timedelta(hours=3, minutes=30))

_G_DAYS_IN_MONTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
_J_DAYS_IN_MONTH = (31, 31, 31, 31, 31, 31, 30, 30, 30, 30, 30, 29)


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    """Convert a Gregorian (y, m, d) to a Jalali (y, m, d) tuple (JDF algorithm).

    >>> gregorian_to_jalali(2026, 6, 2)
    (1405, 3, 12)
    >>> gregorian_to_jalali(2024, 3, 20)
    (1403, 1, 1)
    """
    gy2 = gy - 1600
    gm2 = gm - 1
    gd2 = gd - 1

    g_day_no = 365 * gy2 + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400
    for i in range(gm2):
        g_day_no += _G_DAYS_IN_MONTH[i]
    if gm2 > 1 and ((gy % 4 == 0 and gy % 100 != 0) or (gy % 400 == 0)):
        # leap and after February
        g_day_no += 1
    g_day_no += gd2

    j_day_no = g_day_no - 79
    j_np = j_day_no // 12053
    j_day_no %= 12053

    jy = 979 + 33 * j_np + 4 * (j_day_no // 1461)
    j_day_no %= 1461

    if j_day_no >= 366:
        jy += (j_day_no - 1) // 365
        j_day_no = (j_day_no - 1) % 365

    jm = 0
    for i in range(11):
        if j_day_no < _J_DAYS_IN_MONTH[i]:
            jm = i + 1
            break
        j_day_no -= _J_DAYS_IN_MONTH[i]
    else:
        jm = 12
    jd = j_day_no + 1

    return jy, jm, jd


def jalali_to_gregorian(jy: int, jm: int, jd: int) -> tuple[int, int, int]:
    """Inverse of :func:`gregorian_to_jalali` (JDF algorithm).

    >>> jalali_to_gregorian(1405, 3, 12)
    (2026, 6, 2)
    >>> jalali_to_gregorian(1403, 1, 1)
    (2024, 3, 20)
    """
    jy2 = jy - 979
    jm2 = jm - 1
    jd2 = jd - 1

    j_day_no = 365 * jy2 + (jy2 // 33) * 8 + (jy2 % 33 + 3) // 4
    for i in range(jm2):
        j_day_no += _J_DAYS_IN_MONTH[i]
    j_day_no += jd2

    g_day_no = j_day_no + 79

    gy = 1600 + 400 * (g_day_no // 146097)
    g_day_no %= 146097

    leap = True
    if g_day_no >= 36525:
        g_day_no -= 1
        gy += 100 * (g_day_no // 36524)
        g_day_no %= 36524
        if g_day_no >= 365:
            g_day_no += 1
        else:
            leap = False

    gy += 4 * (g_day_no // 1461)
    g_day_no %= 1461

    if g_day_no >= 366:
        leap = False
        g_day_no -= 1
        gy += g_day_no // 365
        g_day_no %= 365

    gm = 0
    for i in range(12):
        days = _G_DAYS_IN_MONTH[i]
        if i == 1 and leap:
            days += 1
        if g_day_no < days:
            gm = i + 1
            break
        g_day_no -= days
    gd = g_day_no + 1

    return gy, gm, gd


def gregorian_str_to_jalali_str(s: str) -> str:
    """Parse a Gregorian ``"YYYY/MM/DD"`` and return a Jalali ``"YYYY/MM/DD"``.

    An empty string passes through unchanged (Exir treats an empty bound as
    "unbounded"). Non-empty input MUST be a well-formed ``YYYY/MM/DD``.

    >>> gregorian_str_to_jalali_str("2026/06/02")
    '1405/03/12'
    >>> gregorian_str_to_jalali_str("")
    ''
    """
    if not s:
        return ""
    parts = s.split("/")
    if len(parts) != 3:
        raise ValueError(f"expected Gregorian 'YYYY/MM/DD', got {s!r}")
    gy, gm, gd = (int(p) for p in parts)
    jy, jm, jd = gregorian_to_jalali(gy, gm, gd)
    return f"{jy:04d}/{jm:02d}/{jd:02d}"


def parse_jalali_datetime(s: str) -> datetime | None:
    """Parse Exir ``entryDateTime`` (Jalali ``"YYYY/MM/DD-HH:mm:ss"``).

    Returns a tz-aware Gregorian :class:`datetime` in Asia/Tehran (+03:30), or
    ``None`` on any malformed / empty input. The Tehran offset is a fixed
    ``timedelta(hours=3, minutes=30)`` so this does NOT depend on
    zoneinfo/tzdata being installed.

    >>> parse_jalali_datetime("1405/03/12-13:27:08").isoformat()
    '2026-06-02T13:27:08+03:30'
    >>> parse_jalali_datetime("garbage") is None
    True
    """
    if not s:
        return None
    try:
        date_part, _, time_part = s.partition("-")
        dy, dm, dd = (int(p) for p in date_part.split("/"))
        hh, mm, ss = (int(p) for p in time_part.split(":"))
        gy, gm, gd = jalali_to_gregorian(dy, dm, dd)
        return datetime(gy, gm, gd, hh, mm, ss, tzinfo=_TEHRAN_TZ)
    except (ValueError, TypeError):
        return None


# --- import-time sanity checks on known pairs (trip loudly on regression) ---
assert gregorian_to_jalali(2026, 6, 2) == (1405, 3, 12)
assert gregorian_to_jalali(2024, 3, 20) == (1403, 1, 1)
assert jalali_to_gregorian(1405, 3, 12) == (2026, 6, 2)
assert jalali_to_gregorian(1403, 1, 1) == (2024, 3, 20)
