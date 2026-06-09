"""Auto-sell order ladder — split a held position into max-volume chunks and
fire them all at the day's FLOOR price (#110).

The bot watches an instrument's best-buy-queue over the RLC WebSocket; when it
drops to/below a per-instrument threshold, it sells the customer's ENTIRE
holding at the floor (lowest day price). A single broker order can't exceed the
instrument's per-order max volume, so the holding is split into a ladder of
chunks. Operator's example: floor 5, holdings 1001, max-volume 100 →
``[100]*10 + [1]`` orders, every one priced at 5.

This module is pure orchestration with INJECTED I/O (``fetch_holdings`` /
``place_order``), so it unit-tests without a broker, a network, or locust. The
caller (``auto_sell_monitor``) binds those to the broker adapter + ``direct_sell``.

FLAT package layout — top-level module (Dockerfile ``COPY *.py ./``).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# The broker enforces a ~300ms min interval between two orders on one account
# (rejection codes 1018 ephoenix / 1005 exir). Auto-sell is NOT a head-of-queue
# race, so we simply space the ladder a hair above that and let any chunk the
# broker still drops re-fire on the next sub-threshold push.
DEFAULT_MIN_INTERVAL_S = 0.35
# Safety backstop so a bad ``max_order_volume`` (e.g. 1) can't fire thousands of
# orders. A real position never needs this many chunks.
DEFAULT_MAX_CHUNKS = 500


@dataclass
class SellResult:
    """Outcome of one ``sell_entire_position`` invocation."""

    isin: str
    chunks_fired: int
    holdings_before: int
    holdings_after: int
    flat: bool                 # holdings_after == 0
    error: Optional[str] = None


def chunk_volumes(holdings: int, max_order_volume: int) -> list[int]:
    """Split ``holdings`` into per-order chunks of at most ``max_order_volume``.

    ``[max, max, …, remainder]`` — e.g. ``chunk_volumes(1001, 100) ==
    [100]*10 + [1]``. A non-positive ``max_order_volume`` means "no per-order
    cap" → a single order for the whole holding. Non-positive ``holdings`` → ``[]``.
    """
    if holdings <= 0:
        return []
    if max_order_volume is None or max_order_volume <= 0 or holdings <= max_order_volume:
        return [holdings]
    full, remainder = divmod(holdings, max_order_volume)
    chunks = [max_order_volume] * full
    if remainder:
        chunks.append(remainder)
    return chunks


def sell_entire_position(
    *,
    isin: str,
    floor_price: int,
    max_order_volume: int,
    fetch_holdings: Callable[[], int],
    place_order: Callable[[int, int], tuple[int, object]],
    emit_fire: Optional[Callable[[int, object], None]] = None,
    min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
    max_chunks: int = DEFAULT_MAX_CHUNKS,
    sleep: Callable[[float], None] = time.sleep,
    log: logging.Logger = logger,
) -> SellResult:
    """Sell the full current holding of ``isin`` at ``floor_price``, chunked.

    Dependencies are injected so this is unit-testable:

    * ``fetch_holdings()`` → the customer's CURRENT whole-share holding (LIVE,
      uncached — re-read here so a restart mid-ladder re-sizes to what's left,
      never over-sells).
    * ``place_order(price, volume)`` → ``(status_code, body)`` for one direct
      SELL POST (bound to the broker adapter + ``direct_sell.send_prepared_order``).
    * ``emit_fire(volume, body)`` (optional) → record a side=2 fire-log line; we
      call it once, after the first accepted (HTTP 200) chunk.

    Guards:
    * floor_price ≤ 0 → abort (never fire a garbage/zero price).
    * holdings ≤ 0 → no-op, ``flat=True``.
    * Chunks are spaced ≥ ``min_interval_s`` to clear the broker's 300ms guard.
    * Any chunk the broker rejects simply doesn't reduce holdings; the monitor
      re-invokes this on the next sub-threshold push until ``flat``.
    """
    if floor_price is None or floor_price <= 0:
        log.error("auto-sell %s: refusing to sell at floor_price=%r", isin, floor_price)
        return SellResult(isin=isin, chunks_fired=0, holdings_before=0,
                          holdings_after=0, flat=False, error="bad floor price")

    holdings = int(fetch_holdings() or 0)
    if holdings <= 0:
        log.info("auto-sell %s: nothing held (holdings=%d) — already flat", isin, holdings)
        return SellResult(isin=isin, chunks_fired=0, holdings_before=holdings,
                          holdings_after=holdings, flat=True)

    ladder = chunk_volumes(holdings, max_order_volume)
    if len(ladder) > max_chunks:
        log.warning("auto-sell %s: ladder of %d chunks exceeds cap %d — truncating "
                    "(check max_order_volume=%r)", isin, len(ladder), max_chunks,
                    max_order_volume)
        ladder = ladder[:max_chunks]

    log.info("auto-sell %s: selling holdings=%d at floor=%d in %d chunk(s) of <=%s",
             isin, holdings, floor_price, len(ladder), max_order_volume)

    fired = 0
    fired_first = False
    error: Optional[str] = None
    for i, vol in enumerate(ladder):
        try:
            status, body = place_order(floor_price, vol)
        except Exception as exc:  # noqa: BLE001 — one bad chunk can't abort the ladder
            log.exception("auto-sell %s: chunk %d/%d (vol=%d) raised", isin, i + 1,
                          len(ladder), vol)
            error = str(exc)
            continue
        ok = status == 200
        log.info("auto-sell %s: chunk %d/%d vol=%d price=%d -> HTTP %s%s", isin,
                 i + 1, len(ladder), vol, floor_price, status, "" if ok else " (rejected)")
        if ok:
            fired += 1
            if not fired_first and emit_fire is not None:
                try:
                    emit_fire(vol, body)
                except Exception:  # noqa: BLE001 — fire-log must never break a sell
                    log.exception("auto-sell %s: emit_fire failed", isin)
                fired_first = True
        if i < len(ladder) - 1:
            sleep(min_interval_s)

    holdings_after = int(fetch_holdings() or 0)
    flat = holdings_after <= 0
    log.info("auto-sell %s: fired %d chunk(s); holdings %d -> %d (%s)", isin, fired,
             holdings, holdings_after, "FLAT" if flat else "remaining")
    return SellResult(isin=isin, chunks_fired=fired, holdings_before=holdings,
                      holdings_after=holdings_after, flat=flat, error=error)


__all__ = ["SellResult", "chunk_volumes", "sell_entire_position"]
