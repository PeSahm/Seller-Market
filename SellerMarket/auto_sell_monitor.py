"""Auto-sell monitor (#110) — watch buy-queue pushes, fire chunked SELLs at the floor.

Long-running, market-hours behaviour that runs INSIDE the bot container next to
the scheduler (see ``bot_entrypoint.py``). It:

1. reads ``config.ini`` and arms every section with ``auto_sell_threshold > 0``;
2. consumes live ``{isin, buy_volume}`` pushes from the local market-data WS
   service (``market_data_ws``), NEVER polling;
3. when an armed instrument's ``buy_volume`` drops to/below its threshold, sells
   the customer's ENTIRE holding at the day's floor price, chunked to the broker's
   per-order max volume (``auto_sell_engine``), via a DIRECT POST (``direct_sell``);
4. is fail-safe (missing/stale feed ⇒ HOLD), market-hours gated, and idempotent
   (a per-day ``(account, isin)`` latch persisted to ``run_results/`` so a restart
   never double-sells), and emits a side=2 fire-log line per position/day.

The decision logic (``on_buy_volume`` / ``_trigger``) takes injected dependencies
so it unit-tests with no broker, network, or WS. ``run()`` wires the real WS feed.

FLAT package layout — top-level module (Dockerfile ``COPY *.py ./``).
"""
from __future__ import annotations

import configparser
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timezone, timedelta
from typing import Callable, Optional

import auto_sell_engine
import direct_sell
import order_fire_log

logger = logging.getLogger(__name__)

TEHRAN = timezone(timedelta(hours=3, minutes=30))
# Default Tehran trading session; overridable via AUTO_SELL_WINDOW="HH:MM-HH:MM".
DEFAULT_WINDOW = "09:00-12:30"
_RUN_RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_results")


@dataclass
class AutoSellTarget:
    account: str
    password: str
    broker_code: str
    family: str
    isin: str
    threshold: int
    section_name: str


def load_auto_sell_targets(config_path: str = "/app/config.ini") -> list[AutoSellTarget]:
    """Parse ``config.ini`` → the sections armed for auto-sell (``threshold > 0``)."""
    cp = configparser.ConfigParser()
    try:
        cp.read(config_path, encoding="utf-8")
    except Exception:  # noqa: BLE001
        logger.exception("auto-sell: failed to read %s", config_path)
        return []
    targets: list[AutoSellTarget] = []
    for name in cp.sections():
        s = cp[name]
        try:
            threshold = int(s.get("auto_sell_threshold", "0") or 0)
        except (TypeError, ValueError):
            threshold = 0
        if threshold <= 0:
            continue
        targets.append(AutoSellTarget(
            account=s.get("username", ""),
            password=s.get("password", ""),
            broker_code=s.get("broker", ""),
            family=(s.get("broker_family", "ephoenix") or "ephoenix"),
            isin=s.get("isin", ""),
            threshold=threshold,
            section_name=name,
        ))
    logger.info("auto-sell: armed %d instrument(s) from %s", len(targets), config_path)
    return targets


def parse_window(window: str) -> tuple[dtime, dtime]:
    """``"HH:MM-HH:MM"`` → ``(start, end)`` ``time`` objects. Falls back to default."""
    try:
        a, b = (window or DEFAULT_WINDOW).split("-")
        sh, sm = (int(x) for x in a.strip().split(":"))
        eh, em = (int(x) for x in b.strip().split(":"))
        return dtime(sh, sm), dtime(eh, em)
    except Exception:  # noqa: BLE001
        sh, sm = 9, 0
        eh, em = 12, 30
        return dtime(sh, sm), dtime(eh, em)


class DayState:
    """Idempotent per-day ``(account, isin) → done`` latch, persisted to JSONL.

    Reloaded on start so a crash-restart never re-sells an already-flat position.
    Keyed by date in the filename, so it self-expires.
    """

    def __init__(self, today_str: str, directory: str = _RUN_RESULTS_DIR):
        self._done: set[tuple[str, str]] = set()
        # on_buy_volume runs on one QueueFeed thread PER ISIN, so the latch is
        # read/written concurrently — guard it.
        self._lock = threading.Lock()
        self._path = os.path.join(directory, f"auto_sell_state_{today_str}.jsonl")
        try:
            os.makedirs(directory, exist_ok=True)
            if os.path.exists(self._path):
                with open(self._path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        rec = json.loads(line)
                        self._done.add((rec.get("account", ""), rec.get("isin", "")))
        except Exception:  # noqa: BLE001
            logger.exception("auto-sell: failed to load day-state %s", self._path)

    def is_done(self, account: str, isin: str) -> bool:
        with self._lock:
            return (account, isin) in self._done

    def mark_done(self, account: str, isin: str) -> None:
        key = (account, isin)
        with self._lock:
            if key in self._done:
                return
            self._done.add(key)
            try:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"account": account, "isin": isin,
                                        "at": datetime.now(timezone.utc).isoformat()}) + "\n")
            except Exception:  # noqa: BLE001
                logger.exception("auto-sell: failed to persist day-state")


def _default_build_adapter(tgt: AutoSellTarget):
    from broker_adapters import get_adapter
    from captcha_utils import decode_captcha
    return get_adapter(
        tgt.broker_code,
        username=tgt.account,
        password=tgt.password,
        config_section={"broker_family": tgt.family},
        captcha_decoder=decode_captcha,
    )


class AutoSellMonitor:
    """Reactive auto-sell decision engine over a live buy-queue push feed."""

    def __init__(
        self,
        targets: list[AutoSellTarget],
        *,
        market_data_url: str = "",
        build_adapter: Callable[[AutoSellTarget], object] = _default_build_adapter,
        now_fn: Callable[[], datetime] = lambda: datetime.now(TEHRAN),
        window: str = "",
        day_state: Optional[DayState] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.targets = targets
        self.market_data_url = market_data_url
        self._build_adapter = build_adapter
        self._now = now_fn
        self._start, self._end = parse_window(window or os.environ.get("AUTO_SELL_WINDOW", ""))
        self._sleep = sleep
        self._day_state = day_state or DayState(self._now().strftime("%Y%m%d"))
        self._by_isin: dict[str, list[AutoSellTarget]] = {}
        for t in targets:
            self._by_isin.setdefault(t.isin, []).append(t)

    def market_open(self) -> bool:
        t = self._now().time()
        return self._start <= t <= self._end

    def on_buy_volume(self, isin: str, buy_volume: Optional[int]) -> None:
        """Handle one pushed buy-queue update for ``isin`` (None ⇒ feed down → HOLD)."""
        for tgt in self._by_isin.get(isin, []):
            if not self.market_open():
                continue
            if self._day_state.is_done(tgt.account, tgt.isin):
                continue
            if buy_volume is None:
                # Fail-safe: a missing / stale feed must NEVER trigger a sell.
                continue
            if buy_volume <= tgt.threshold:
                logger.info("auto-sell TRIGGER %s %s@%s: buy_volume=%s <= threshold=%s",
                            tgt.isin, tgt.account, tgt.broker_code, buy_volume, tgt.threshold)
                self._trigger(tgt)

    def _trigger(self, tgt: AutoSellTarget) -> None:
        try:
            adapter = self._build_adapter(tgt)
            ctx = adapter.open_sell_context(
                isin=tgt.isin, config_section={"broker_family": tgt.family}
            )
        except Exception:  # noqa: BLE001 — auth/price failure → HOLD, retry next push
            logger.exception("auto-sell %s %s: open_sell_context failed (will retry)",
                             tgt.isin, tgt.account)
            return

        res = auto_sell_engine.sell_entire_position(
            isin=tgt.isin,
            floor_price=ctx.floor_price,
            max_order_volume=ctx.max_order_volume,
            fetch_holdings=ctx.fetch_holdings,
            place_order=lambda _price, vol: direct_sell.send_prepared_order(ctx.prepare_chunk(vol)),
            emit_fire=lambda _vol, body: self._emit_fire(tgt, body),
            sleep=self._sleep,
        )
        if res.flat:
            self._day_state.mark_done(tgt.account, tgt.isin)
            logger.info("auto-sell %s %s: position FLAT — done for the day", tgt.isin, tgt.account)

    def _emit_fire(self, tgt: AutoSellTarget, body: object) -> None:
        try:
            payload = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else body
        except Exception:  # noqa: BLE001
            payload = None
        order_fire_log.emit_order_fire(
            tgt.account, tgt.broker_code, tgt.isin, 2, order_response=payload
        )

    def run(self) -> None:
        """Block: wire the local market-data WS feed to ``on_buy_volume``.

        Imported lazily so the decision logic above stays unit-testable without
        the WS client dependency.
        """
        if not self.targets:
            logger.info("auto-sell: no armed instruments — monitor idle")
            while True:
                self._sleep(3600)
        from market_data_ws import QueueFeed

        isins = sorted(self._by_isin.keys())
        logger.info("auto-sell: monitoring %d instrument(s) via %s: %s",
                    len(isins), self.market_data_url, isins)
        feed = QueueFeed(self.market_data_url, on_update=self.on_buy_volume)
        for isin in isins:
            feed.subscribe(isin)
        feed.run_forever()


__all__ = ["AutoSellTarget", "AutoSellMonitor", "DayState",
           "load_auto_sell_targets", "parse_window"]
