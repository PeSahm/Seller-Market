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


# Sentinel the mgmt renderer appends to a COMPLETE config.ini (#110 hot-reload).
# Its presence lets the bot trust a freshly-read file immediately; its absence
# (a torn in-place write, or an older render) falls back to a stability re-read.
CONFIG_END_SENTINEL = "# auto-sell-config-end"


def parse_auto_sell_targets(content: str) -> list[AutoSellTarget]:
    """Parse config.ini *text* → armed auto-sell targets. RAISES on a malformed
    structure (``configparser.Error``).

    Distinct from :func:`load_auto_sell_targets` (which swallows read/parse
    errors into ``[]`` for the boot path): the hot-reload supervisor MUST be
    able to tell a torn/garbage file apart from a legitimate disarm-all, so it
    needs the raising form. Per-section value problems (a non-numeric threshold)
    are still tolerated — that section is simply not armed.
    """
    cp = configparser.ConfigParser()
    cp.read_string(content)  # raises configparser.Error on a structural problem
    targets: list[AutoSellTarget] = []
    for name in cp.sections():
        s = cp[name]
        try:
            threshold = int(s.get("auto_sell_threshold", "0") or 0)
        except (TypeError, ValueError):
            threshold = 0
        try:
            side = int(s.get("side", "0") or 0)
        except (TypeError, ValueError):
            side = 0
        # Arm BUY sections only — a stray threshold on a SELL section (manual edit
        # / migration drift) must not auto-fire.
        if threshold <= 0 or side != 1:
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
    return targets


def load_auto_sell_targets(config_path: str = "/app/config.ini") -> list[AutoSellTarget]:
    """Boot-time loader: read ``config.ini`` → armed targets, never raising."""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            content = f.read()
        targets = parse_auto_sell_targets(content)
    except Exception:  # noqa: BLE001
        logger.exception("auto-sell: failed to read/parse %s", config_path)
        return []
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
        status_dir: str = _RUN_RESULTS_DIR,
    ):
        self.targets = targets
        self.market_data_url = market_data_url
        self._build_adapter = build_adapter
        self._now = now_fn
        self._start, self._end = parse_window(window or os.environ.get("AUTO_SELL_WINDOW", ""))
        self._sleep = sleep
        # Rotate the per-day latch when the calendar day changes (the monitor
        # process stays up across midnight). An injected day_state (tests) is
        # used as-is and never rotated.
        self._external_day_state = day_state is not None
        self._day_key = self._now().strftime("%Y%m%d")
        self._day_state = day_state or DayState(self._day_key)
        self._by_isin: dict[str, list[AutoSellTarget]] = {}
        for t in targets:
            self._by_isin.setdefault(t.isin, []).append(t)
        # --- hot-reload supervisor state (#110 real-time threshold) ---
        # Per-(account,isin) in-flight guard: one ladder per position even if two
        # feed threads (e.g. a stale + a fresh feed during a rebuild) race.
        self._inflight: set[tuple[str, str]] = set()
        self._inflight_guard = threading.Lock()
        # Feed generation: a stale feed's in-flight delivery (one recv can survive
        # stop()) is dropped unless its captured gen == the current one.
        self._current_gen = 0
        self._feed = None
        self._feed_factory: Optional[Callable] = None
        self._applied_content: Optional[str] = None   # last-applied raw config text
        self._pending_disarm: Optional[str] = None     # disarm awaiting a 2nd confirm
        self._last_untrusted: Optional[str] = None     # log-dedup for untrusted reads
        self._stop_supervisor = threading.Event()
        self._status_dir = status_dir
        self._status_path = os.path.join(status_dir, "auto_sell_status.json")

    def _ds(self) -> DayState:
        """The current-day latch, rotating it when the calendar day flips."""
        if self._external_day_state:
            return self._day_state
        key = self._now().strftime("%Y%m%d")
        if key != self._day_key:
            self._day_key = key
            self._day_state = DayState(key)
        return self._day_state

    def market_open(self) -> bool:
        t = self._now().time()
        return self._start <= t <= self._end

    def on_buy_volume(self, isin: str, buy_volume: Optional[int]) -> None:
        """Handle one pushed buy-queue update for ``isin`` (None ⇒ feed down → HOLD)."""
        for tgt in self._by_isin.get(isin, []):
            if not self.market_open():
                continue
            if self._ds().is_done(tgt.account, tgt.isin):
                continue
            if buy_volume is None:
                # Fail-safe: a missing / stale feed must NEVER trigger a sell.
                continue
            if buy_volume <= tgt.threshold:
                logger.info("auto-sell TRIGGER %s %s@%s: buy_volume=%s <= threshold=%s",
                            tgt.isin, tgt.account, tgt.broker_code, buy_volume, tgt.threshold)
                self._trigger(tgt)

    def _trigger(self, tgt: AutoSellTarget) -> None:
        # One ladder per (account, isin) at a time. A feed rebuild can briefly
        # leave a stale feed thread alive (one recv survives stop()); without
        # this guard it could start a SECOND concurrent ladder on the same
        # position (both read holdings before either's orders land → 2x sell).
        key = (tgt.account, tgt.isin)
        with self._inflight_guard:
            if key in self._inflight:
                logger.info("auto-sell %s %s: ladder already in flight — skip", tgt.isin, tgt.account)
                return
            self._inflight.add(key)
        try:
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
                self._ds().mark_done(tgt.account, tgt.isin)
                logger.info("auto-sell %s %s: position FLAT — done for the day", tgt.isin, tgt.account)
        finally:
            with self._inflight_guard:
                self._inflight.discard(key)

    def _emit_fire(self, tgt: AutoSellTarget, body: object) -> None:
        try:
            payload = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else body
        except Exception:  # noqa: BLE001
            payload = None
        order_fire_log.emit_order_fire(
            tgt.account, tgt.broker_code, tgt.isin, 2, order_response=payload
        )

    # ------------------------------------------------------------------ feed
    def _make_on_update(self, gen: int) -> Callable[[str, Optional[int]], None]:
        """Feed callback bound to generation ``gen``; drops stale-feed deliveries."""
        def cb(isin: str, buy_volume: Optional[int]) -> None:
            if gen != self._current_gen:
                return  # a previous feed generation — its targets are gone
            self.on_buy_volume(isin, buy_volume)
        return cb

    def _rebuild_feed(self, isins: list[str]) -> None:
        """Stop the current feed and start a fresh one for ``isins`` (new gen)."""
        new_gen = self._current_gen + 1
        feed = None
        if self.market_data_url and isins:
            feed = self._feed_factory(self.market_data_url, on_update=self._make_on_update(new_gen))
            for isin in isins:
                feed.subscribe(isin)
        # Bump the generation BEFORE starting the new feed so any in-flight
        # delivery from the old feed (its gen != new_gen) is dropped.
        self._current_gen = new_gen
        old = self._feed
        self._feed = feed
        if old is not None:
            old.stop()
        if feed is not None:
            feed.start()

    # ------------------------------------------------------------- supervisor
    def _read_content(self, path: str) -> Optional[str]:
        """Read config text; None on unreadable OR empty (a torn in-place write)."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = f.read()
        except OSError:
            return None
        # A legit config.ini always carries the renderer header, so blank ⇒ torn.
        return data if data.strip() else None

    def _trusted(self, content: str) -> bool:
        return content.rstrip().endswith(CONFIG_END_SENTINEL)

    @staticmethod
    def _keys(targets: list[AutoSellTarget]) -> set[tuple[str, str]]:
        return {(t.account, t.isin) for t in targets}

    @staticmethod
    def _sig(targets: list[AutoSellTarget]) -> frozenset:
        """Order-independent signature of the armed set (everything the monitor
        acts on: creds + family + threshold). Equal sig ⇒ a no-op reload."""
        return frozenset(
            (t.account, t.isin, t.threshold, t.broker_code, t.family, t.password)
            for t in targets
        )

    def _apply(self, content: Optional[str], new_targets: list[AutoSellTarget],
               *, force_feed: bool = False) -> None:
        """Swap in ``new_targets`` (GIL-atomic), rebuild the feed iff the ISIN set
        changed (or ``force_feed``), write the status marker, log the diff."""
        old_keys = self._keys(self.targets)
        old_isins = set(self._by_isin.keys())
        old_thr = {(t.account, t.isin): t.threshold for t in self.targets}

        new_by_isin: dict[str, list[AutoSellTarget]] = {}
        for t in new_targets:
            new_by_isin.setdefault(t.isin, []).append(t)
        new_isins = set(new_by_isin.keys())

        # Atomic reference swaps — on_buy_volume reads these without a lock.
        self.targets = new_targets
        self._by_isin = new_by_isin
        self._applied_content = content

        rebuild = force_feed or (new_isins != old_isins) or (self._feed is None and bool(new_isins))
        if rebuild:
            self._rebuild_feed(sorted(new_isins))

        self._write_status_marker(new_targets)

        new_keys = self._keys(new_targets)
        added = sorted(new_keys - old_keys)
        removed = sorted(old_keys - new_keys)
        changed = sorted(
            (k, old_thr[k], t.threshold)
            for t in new_targets
            for k in [(t.account, t.isin)]
            if k in old_thr and old_thr[k] != t.threshold
        )
        log = logger.warning if removed else logger.info
        log("auto-sell reload: armed %d (feed %s) added=%s removed=%s changed=%s",
            len(new_targets), "rebuilt" if rebuild else "kept",
            added or "-", removed or "-",
            [f"{isin} {a}->{b}" for (_acc, isin), a, b in changed] or "-")

    def _tick(self, config_path: str) -> str:
        """One supervisor poll cycle. Returns a short status string (for tests)."""
        content = self._read_content(config_path)
        if content is None:
            return "skip-unreadable"
        if content == self._applied_content:
            self._pending_disarm = None
            return "nochange"
        # HARD GATE: a reload is applied ONLY from a sentinel-terminated file.
        # The mgmt renderer appends CONFIG_END_SENTINEL as the LAST line and the
        # SFTP write is an in-place front-to-back truncate+rewrite, so a torn
        # read is a PREFIX that lacks it. A torn prefix can parse CLEANLY with a
        # WRONG threshold (e.g. the new higher value with the tail missing) and
        # can stay torn for SECONDS on a flaky SSH link — a settle re-read is
        # NOT sufficient proof. No sentinel ⇒ hold current targets, retry next
        # poll (the completed write brings the sentinel). Configs from a
        # pre-sentinel mgmt UI simply don't hot-reload until mgmt is upgraded.
        if not self._trusted(content):
            if self._last_untrusted != content:  # log once per distinct content
                self._last_untrusted = content
                logger.warning("auto-sell: config not sentinel-terminated "
                               "(torn write or old mgmt render) — holding %d armed",
                               len(self.targets))
            return "untrusted"
        self._last_untrusted = None
        try:
            new_targets = parse_auto_sell_targets(content)
        except Exception:  # noqa: BLE001 — torn/garbage structure → keep current
            logger.warning("auto-sell: config parse error — keeping %d armed", len(self.targets))
            return "parse-error"
        # Cosmetic save (mgmt pushes config.ini on EVERY customer mutation): the
        # bytes changed but the armed set is identical → record the bytes so we
        # don't re-evaluate, but don't rebuild the feed or rewrite the marker.
        if self._sig(new_targets) == self._sig(self.targets):
            self._applied_content = content
            self._pending_disarm = None
            logger.debug("auto-sell: config changed but armed set identical — no reload")
            return "nochange-cosmetic"
        # Asymmetric disarm guard: a reload that REMOVES an armed position must
        # be confirmed by an identical NEXT tick before it takes effect (a torn
        # file that drops a section must never silently un-protect a holding).
        if self._applied_content is not None and self._keys(self.targets) - self._keys(new_targets):
            if self._pending_disarm != content:
                self._pending_disarm = content
                logger.warning("auto-sell: disarm detected — awaiting confirm tick")
                return "disarm-pending"
        self._pending_disarm = None
        self._apply(content, new_targets)
        return "applied"

    def stop_supervisor(self) -> None:
        self._stop_supervisor.set()

    def run_supervised(self, config_path: str = "/app/config.ini", *,
                       poll_interval: float = 3.0,
                       feed_factory: Optional[Callable] = None) -> None:
        """Block: run the auto-sell feed AND hot-reload armed targets from
        ``config_path`` whenever it changes — no container restart needed.

        Establishes the feed from the current on-disk config, then polls every
        ``poll_interval`` s: threshold-only changes swap atomically (feed kept,
        new threshold consulted on the next push); ISIN-set changes rebuild the
        feed. DayState (the fired-today latch) is preserved across reloads.
        """
        if feed_factory is None:
            from market_data_ws import QueueFeed
            feed_factory = QueueFeed
        self._feed_factory = feed_factory
        logger.info("auto-sell: supervisor up (config=%s, poll=%.1fs, url=%s)",
                    config_path, poll_interval, self.market_data_url or "(unset)")

        # Initial establishment — authoritative from disk; fall back to the
        # constructor targets if the file isn't readable yet. force_feed builds
        # the feed even when the ISIN set matches the constructor's. At BOOT the
        # file is normally at rest, so parsing a sentinel-less file (an older
        # mgmt render) is fine — but ``_applied_content`` is recorded ONLY for
        # sentinel-terminated content, so if we did boot mid-write the first
        # trusted tick re-applies the completed file (content != None).
        content = self._read_content(config_path)
        init_targets = list(self.targets)
        if content is not None:
            try:
                init_targets = parse_auto_sell_targets(content)
            except Exception:  # noqa: BLE001
                content = None  # keep constructor targets; no applied_content yet
        applied_marker = content if (content is not None and self._trusted(content)) else None
        self._apply(applied_marker, init_targets, force_feed=True)

        while not self._stop_supervisor.is_set():
            self._sleep(poll_interval)
            try:
                self._tick(config_path)
            except Exception:  # noqa: BLE001 — a bad tick must never exit the loop
                logger.exception("auto-sell: supervisor tick failed")

    # Back-compat shim: the old blocking entry. Delegates to the supervisor so
    # it gains hot-reload (and loses the old idle-forever-on-empty trap).
    def run(self) -> None:
        self.run_supervised(os.environ.get("CONFIG_INI", "/app/config.ini"))

    def _write_status_marker(self, targets: list[AutoSellTarget]) -> None:
        """Overwrite ``run_results/auto_sell_status.json`` so the mgmt UI can
        confirm WHICH thresholds the live bot has actually applied (#110)."""
        try:
            os.makedirs(self._status_dir, exist_ok=True)
            payload = {
                "schema": 1,
                "applied_at": datetime.now(timezone.utc).isoformat(),
                "armed": [{"account": t.account, "isin": t.isin, "threshold": t.threshold}
                          for t in targets],
            }
            tmp = self._status_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, self._status_path)  # atomic; local dir, not a single-file mount
        except Exception:  # noqa: BLE001 — a marker write must never break the monitor
            logger.exception("auto-sell: failed to write status marker")


__all__ = ["AutoSellTarget", "AutoSellMonitor", "DayState",
           "load_auto_sell_targets", "parse_auto_sell_targets", "parse_window",
           "CONFIG_END_SENTINEL"]
