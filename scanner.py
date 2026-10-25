"""Read-only multi-pair scanner coordinating candles and strategy signals."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import config
from broker.pocket_option import CandleHistoryUnavailable, PocketOptionBroker
from data.candles import Candle, CandleBuilder, CandleDataError, _normalize_broker_timestamp
from market_assets import (
    LiveTestCandidate,
    MarketAsset,
    eligible_assets,
    read_market_asset_snapshot,
)
from strategy.close_engine import CloseOnlyStrategy
from strategy.pullback_reversal import Signal

logger = logging.getLogger("gbot.scanner")
SignalCallback = Callable[[Signal], None | Awaitable[None]]
TickCallback = Callable[[str, float, datetime], None]
OutputCallback = Callable[[str], None]
DecisionCallback = Callable[[str, Candle, Sequence[Candle], Any, Signal | None], None]


@dataclass
class PairScanState:
    payout: float
    asset: MarketAsset
    candles: CandleBuilder
    strategy: CloseOnlyStrategy


class SignalScanner:
    """Scan eligible pairs concurrently and retain ranked, time-limited signals."""

    def __init__(
        self,
        broker: PocketOptionBroker,
        *,
        on_signal: SignalCallback | None = None,
        on_tick: TickCallback | None = None,
        on_candle: Callable[[str, Candle], None] | None = None,
        on_decision: DecisionCallback | None = None,
        on_watchdog_event: Callable[[str], None | Awaitable[None]] | None = None,
        output: OutputCallback = print,
    ) -> None:
        self.broker = broker
        self.on_signal = on_signal
        self.on_tick = on_tick
        self.on_candle = on_candle
        self.on_decision = on_decision
        self.on_watchdog_event = on_watchdog_event
        self.output = output
        self.pairs: dict[str, PairScanState] = {}
        self.signals: dict[str, Signal] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._receiving_pairs: set[str] = set()
        self._stopping = False
        self._paused = False
        self._started_at: float | None = None
        self._last_any_tick_at: float | None = None
        self._last_tick_received: dict[str, float] = {}
        self._last_tick_timestamp: dict[str, datetime] = {}
        self._ready = asyncio.Event()
        self._watchdog_rebuilding = False
        self._watchdog_failures = 0
        self._watchdog_retry_at = 0.0
        self.last_signal_latency_ms: float | None = None

    @property
    def last_tick_age_seconds(self) -> float | None:
        if self._last_any_tick_at is None:
            return None
        return max(0.0, time.monotonic() - self._last_any_tick_at)

    @property
    def is_paused(self) -> bool:
        return self._paused

    async def wait_until_ready(self) -> None:
        await self._ready.wait()

    def pause(self) -> None:
        self._paused = True
        for pair, state in self.pairs.items():
            state.strategy.cancel_all(pair, "scanner paused")

    def resume(self) -> None:
        self._paused = False

    def expire_signals(self, now: datetime | None = None) -> None:
        current = now or datetime.now(timezone.utc)
        for signal in self.signals.values():
            if (
                signal.status == "ACTIVE"
                and signal.valid_until is not None
                and signal.valid_until <= current
            ):
                signal.status = "EXPIRED"

    @property
    def active_signals(self) -> list[Signal]:
        self.expire_signals()
        active = [signal for signal in self.signals.values() if signal.status == "ACTIVE"]
        active.sort(key=lambda signal: (-signal.strength, signal.time, signal.id))
        return active[: config.MAX_ACTIVE_SIGNALS]

    def stream_health_line(self) -> str:
        requested = len(self.pairs)
        receiving = len(self._receiving_pairs)
        self._receiving_pairs.clear()
        return f"STREAM HEALTH requested_pairs={requested} receiving_tick_pairs={receiving}"

    async def refresh_pairs(self) -> None:
        snapshot = await read_market_asset_snapshot(
            self.broker,
            timeout_seconds=config.WATCHDOG_CALL_TIMEOUT_SECONDS,
        )
        selected = eligible_assets(
            snapshot.assets, config.MIN_PAYOUT, config.MAX_SCAN_PAIRS
        )
        desired = {asset.symbol: asset for asset in selected}

        removed = set(self.pairs) - set(desired)
        for pair in removed:
            self._receiving_pairs.discard(pair)
            self._last_tick_received.pop(pair, None)
            self._last_tick_timestamp.pop(pair, None)
            task = self._tasks.pop(pair, None)
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.pairs.pop(pair, None)

        for asset in selected:
            pair, payout = asset.symbol, asset.payout
            state = self.pairs.get(pair)
            if state is not None:
                state.payout = payout
                state.asset = asset
                continue
            builder = CandleBuilder(
                period_seconds=config.CANDLE_INTERVAL_SECONDS,
                max_candles=config.MAX_CANDLES_PER_PAIR,
            )
            try:
                history = await asyncio.wait_for(
                    self.broker.candle_history(
                        pair,
                        config.CANDLE_INTERVAL_SECONDS,
                        config.HISTORY_LOOKBACK_SECONDS,
                    ),
                    timeout=config.WATCHDOG_CALL_TIMEOUT_SECONDS,
                )
                builder.seed(history)
                if self.on_candle is not None:
                    for candle in builder.closed:
                        self.on_candle(pair, candle)
            except CandleHistoryUnavailable as exc:
                logger.warning("%s", exc)
            except CandleDataError as exc:
                logger.warning(
                    "Historical candle seed for %s was skipped (%s).",
                    pair,
                    type(exc).__name__,
                )
            state = PairScanState(
                payout=payout,
                asset=asset,
                candles=builder,
                strategy=CloseOnlyStrategy(),
            )
            self.pairs[pair] = state
        # Seed all pairs in sequence on the shared broker client before any
        # concurrent tick subscriptions are opened.
        for pair in desired:
            state = self.pairs[pair]
            if pair not in self._tasks:
                self._tasks[pair] = asyncio.create_task(
                    self._stream_pair(pair, state),
                    name=f"scanner-{pair}",
                )

    async def live_test_candidates(
        self,
    ) -> tuple[Any, list[LiveTestCandidate]]:
        """Return current scanner signals that still pass a fresh market snapshot."""
        snapshot = await read_market_asset_snapshot(
            self.broker,
            timeout_seconds=config.WATCHDOG_CALL_TIMEOUT_SECONDS,
        )
        now = datetime.now(timezone.utc)
        now_monotonic = time.monotonic()
        # The scanner's already-capped active pair map is the source of truth.
        # Refresh metadata for those symbols without selecting a second list.
        eligible = {
            pair: asset
            for pair in self.pairs
            if (asset := snapshot.assets.get(pair)) is not None
            and asset.is_open
            and asset.payout >= config.MIN_PAYOUT
        }
        candidates: list[LiveTestCandidate] = []
        seen_pairs: set[str] = set()
        for signal in sorted(
            self.signals.values(),
            key=lambda item: (-item.strength, item.time, item.id),
        ):
            asset = eligible.get(signal.pair)
            state = self.pairs.get(signal.pair)
            received_at = self._last_tick_received.get(signal.pair)
            if (
                asset is None
                or state is None
                or received_at is None
                or now_monotonic - received_at > 5.0
                or signal.status != "ACTIVE"
                or (signal.valid_until is not None and signal.valid_until <= now)
                or signal.pair in seen_pairs
                or not state.candles.closed
                or int(signal.time.timestamp()) != state.candles.closed[-1].time
                or state.candles.forming_candle is None
            ):
                continue
            candidates.append(
                LiveTestCandidate(
                    signal=signal,
                    asset=asset,
                    live_price=float(state.candles.forming_candle.close),
                )
            )
            seen_pairs.add(signal.pair)
            if len(candidates) == 3:
                break
        return snapshot, candidates

    async def _stream_pair(self, pair: str, state: PairScanState) -> None:
        while not self._stopping and self.pairs.get(pair) is state:
            ticks = self.broker.subscribe_ticks(pair)
            try:
                async for tick in ticks:
                    if self._stopping or self.pairs.get(pair) is not state:
                        return
                    self._receiving_pairs.add(pair)
                    self._handle_tick(pair, state, tick)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Tick stream for %s stopped (%s); reconnecting.",
                    pair,
                    type(exc).__name__,
                )
            finally:
                await ticks.aclose()
            if not self._stopping and self.pairs.get(pair) is state:
                await asyncio.sleep(0.25)

    def _handle_tick(
        self, pair: str, state: PairScanState, tick: dict[str, Any]
    ) -> None:
        try:
            timestamp = datetime.fromtimestamp(
                _normalize_broker_timestamp(tick.get("timestamp", tick.get("time"))),
                timezone.utc,
            )
            price = float(tick.get("close", tick.get("price")))
        except (TypeError, ValueError, OverflowError, OSError, CandleDataError):
            return
        age = (datetime.now(timezone.utc) - timestamp).total_seconds()
        if age > config.WATCHDOG_TICK_MAX_AGE_SECONDS:
            logger.debug("Stale tick ignored for %s (age=%.1fs).", pair, age)
            return
        received_at = time.monotonic()
        self._last_tick_received[pair] = received_at
        self._last_tick_timestamp[pair] = timestamp
        self._last_any_tick_at = received_at
        closed = state.candles.update(tick)
        if closed is not None:
            if self.on_candle is not None:
                try:
                    self.on_candle(pair, closed)
                except Exception as exc:
                    logger.error(
                        "Candle recording failed (%s).", type(exc).__name__
                    )
            closed_signal = None
            if not self._paused:
                state.strategy.on_closed_candles(
                    pair, tuple(state.candles.closed), payout=state.payout
                )
                closed_signal = state.strategy.take_closed_signal(pair)
                if closed_signal is not None:
                    self._publish_signal(closed_signal)
            if self.on_decision is not None:
                try:
                    self.on_decision(
                        pair,
                        closed,
                        tuple(state.candles.closed),
                        state.strategy,
                        closed_signal,
                    )
                except Exception as exc:
                    logger.error("Decision recording failed (%s).", type(exc).__name__)
        if self.on_tick is not None:
            try:
                self.on_tick(pair, price, timestamp)
            except Exception as exc:
                logger.error("Paper tracking update failed (%s).", type(exc).__name__)
        on_tick = getattr(state.strategy, "on_tick", None)
        if not callable(on_tick):
            return
        signal = on_tick(
            pair,
            price,
            state.candles.forming_candle,
            timestamp=timestamp,
            payout=state.payout,
        )
        if signal is None:
            return
        self._publish_signal(signal)

    def _publish_signal(self, signal: Signal) -> None:
        state = self.pairs.get(signal.pair)
        if state is None or not state.asset.is_open or state.payout < config.MIN_PAYOUT:
            return
        last_tick = self._last_tick_received.get(signal.pair)
        if (
            last_tick is None
            or time.monotonic() - last_tick > config.WATCHDOG_TICK_MAX_AGE_SECONDS
        ):
            logger.info("Signal suppressed because %s has no fresh tick.", signal.pair)
            return
        signal.asset_name = state.asset.display_name
        signal_time = signal.time
        if signal_time.tzinfo is None:
            signal_time = signal_time.replace(tzinfo=timezone.utc)
        self.last_signal_latency_ms = max(
            0.0,
            (datetime.now(timezone.utc) - signal_time).total_seconds() * 1000.0,
        )
        self.expire_signals(signal.time)
        if any(
            active.status == "ACTIVE"
            and active.pair == signal.pair
            and active.direction == signal.direction
            for active in self.signals.values()
        ):
            return
        self.signals[signal.id] = signal
        if self.on_signal is not None:
            try:
                result = self.on_signal(signal)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception as exc:
                logger.error(
                    "Signal callback failed (%s).", type(exc).__name__
                )

    async def _notify_watchdog(self, message: str) -> None:
        if self.on_watchdog_event is None:
            return
        try:
            result = self.on_watchdog_event(message)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            logger.warning("Watchdog alert failed (%s).", type(exc).__name__)

    async def _rebuild_feed(self, reason: str) -> None:
        if self._watchdog_rebuilding or time.monotonic() < self._watchdog_retry_at:
            return
        self._watchdog_rebuilding = True
        try:
            logger.warning("%s; rebuilding the Pocket Option connection.", reason)
            for task in self._tasks.values():
                task.cancel()
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
            self._tasks.clear()
            self.pairs.clear()
            self._receiving_pairs.clear()
            self._last_tick_received.clear()
            self._last_tick_timestamp.clear()

            rebuild = getattr(self.broker, "rebuild_connection", None)
            if callable(rebuild):
                await asyncio.wait_for(
                    rebuild(), timeout=config.WATCHDOG_CALL_TIMEOUT_SECONDS
                )
            else:
                await asyncio.wait_for(
                    self.broker.connect(),
                    timeout=config.WATCHDOG_CALL_TIMEOUT_SECONDS,
                )
            await asyncio.wait_for(
                self.refresh_pairs(), timeout=config.WATCHDOG_CALL_TIMEOUT_SECONDS
            )
            self._watchdog_failures = 0
            self._last_any_tick_at = time.monotonic()
            self._watchdog_retry_at = 0.0
            await self._notify_watchdog(
                "Pocket Option feed recovered after a stall; scanning has resumed."
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._watchdog_failures += 1
            self._watchdog_retry_at = (
                time.monotonic() + config.WATCHDOG_RETRY_SECONDS
            )
            logger.warning(
                "Feed rebuild %d/%d failed (%s).",
                self._watchdog_failures,
                config.WATCHDOG_REBUILD_FAILURE_LIMIT,
                type(exc).__name__,
            )
            if self._watchdog_failures == config.WATCHDOG_REBUILD_FAILURE_LIMIT:
                await self._notify_watchdog(
                    "Pocket Option feed rebuild failed three times. SSID may have expired."
                )
            else:
                await self._notify_watchdog(
                    "Pocket Option feed rebuild failed; another recovery attempt is scheduled."
                )
        finally:
            self._watchdog_rebuilding = False

    async def _check_feed_watchdog(self) -> None:
        baseline = self._last_any_tick_at or self._started_at
        if baseline is None:
            return
        if time.monotonic() - baseline >= config.WATCHDOG_GLOBAL_STALL_SECONDS:
            await self._rebuild_feed("No market ticks received from any pair")

    async def run(self, *, minutes: float | None) -> None:
        if minutes is not None and minutes <= 0:
            raise ValueError("minutes must be greater than zero")
        loop = asyncio.get_running_loop()
        deadline = (
            loop.time() + minutes * 60.0 if minutes is not None else float("inf")
        )
        next_refresh = loop.time()
        next_health = loop.time() + 60.0
        next_watchdog = loop.time() + config.WATCHDOG_CHECK_INTERVAL_SECONDS
        self._stopping = False
        self._started_at = loop.time()
        self._last_any_tick_at = None
        try:
            await asyncio.wait_for(
                self.broker.connect(),
                timeout=config.WATCHDOG_CALL_TIMEOUT_SECONDS,
            )
            await self.refresh_pairs()
            self._ready.set()
            next_refresh = loop.time() + config.PAYOUT_REFRESH_SECONDS
            while loop.time() < deadline:
                wake_at = min(deadline, next_refresh, next_health, next_watchdog)
                await asyncio.sleep(max(0.0, wake_at - loop.time()))
                now = loop.time()
                self.expire_signals()
                if now >= next_refresh and now < deadline:
                    try:
                        await self.refresh_pairs()
                    except Exception as exc:
                        logger.warning(
                            "Pair refresh stalled or failed (%s).",
                            type(exc).__name__,
                        )
                        await self._rebuild_feed("Pair refresh failed")
                    next_refresh = now + config.PAYOUT_REFRESH_SECONDS
                if now >= next_health:
                    self.output(self.stream_health_line())
                    next_health = now + 60.0
                if now >= next_watchdog:
                    await self._check_feed_watchdog()
                    next_watchdog = loop.time() + config.WATCHDOG_CHECK_INTERVAL_SECONDS
        finally:
            self._stopping = True
            for task in self._tasks.values():
                task.cancel()
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
            self._tasks.clear()
            self.output(self.stream_health_line())
            await self.broker.close()
