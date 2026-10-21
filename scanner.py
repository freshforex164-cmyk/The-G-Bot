"""Read-only multi-pair scanner coordinating candles and strategy signals."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import config
from broker.pocket_option import CandleHistoryUnavailable, PocketOptionBroker
from data.candles import Candle, CandleBuilder, CandleDataError, _normalize_broker_timestamp
from strategy.pullback_reversal import PullbackReversalStrategy, Signal

logger = logging.getLogger("gbot.scanner")
SignalCallback = Callable[[Signal], None | Awaitable[None]]
TickCallback = Callable[[str, float, datetime], None]
OutputCallback = Callable[[str], None]


@dataclass
class PairScanState:
    payout: float
    candles: CandleBuilder
    strategy: PullbackReversalStrategy


class SignalScanner:
    """Scan eligible pairs concurrently and retain ranked, time-limited signals."""

    def __init__(
        self,
        broker: PocketOptionBroker,
        *,
        on_signal: SignalCallback | None = None,
        on_tick: TickCallback | None = None,
        on_candle: Callable[[str, Candle], None] | None = None,
        output: OutputCallback = print,
    ) -> None:
        self.broker = broker
        self.on_signal = on_signal
        self.on_tick = on_tick
        self.on_candle = on_candle
        self.output = output
        self.pairs: dict[str, PairScanState] = {}
        self.signals: dict[str, Signal] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._receiving_pairs: set[str] = set()
        self._stopping = False

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
        payouts = await self.broker.payouts()
        selected = sorted(
            (
                (pair, float(payout))
                for pair, payout in payouts.items()
                if float(payout) >= config.MIN_PAYOUT
            ),
            key=lambda item: (-item[1], item[0]),
        )[: config.MAX_SCAN_PAIRS]
        desired = dict(selected)

        removed = set(self.pairs) - set(desired)
        for pair in removed:
            self._receiving_pairs.discard(pair)
            task = self._tasks.pop(pair, None)
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.pairs.pop(pair, None)

        for pair, payout in selected:
            state = self.pairs.get(pair)
            if state is not None:
                state.payout = payout
                continue
            builder = CandleBuilder(
                period_seconds=config.CANDLE_INTERVAL_SECONDS,
                max_candles=config.MAX_CANDLES_PER_PAIR,
            )
            try:
                history = await self.broker.candle_history(
                    pair,
                    config.CANDLE_INTERVAL_SECONDS,
                    config.HISTORY_LOOKBACK_SECONDS,
                )
                builder.seed(history)
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
                candles=builder,
                strategy=PullbackReversalStrategy(),
            )
            self.pairs[pair] = state
            self._tasks[pair] = asyncio.create_task(
                self._stream_pair(pair, state),
                name=f"scanner-{pair}",
            )

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
        closed = state.candles.update(tick)
        if closed is not None:
            if self.on_candle is not None:
                try:
                    self.on_candle(pair, closed)
                except Exception as exc:
                    logger.error(
                        "Candle recording failed (%s).", type(exc).__name__
                    )
            state.strategy.on_closed_candles(
                pair, tuple(state.candles.closed), payout=state.payout
            )
            closed_signal = state.strategy.take_closed_signal(pair)
            if closed_signal is not None:
                self._publish_signal(closed_signal)
        try:
            price = float(tick.get("close", tick.get("price")))
            timestamp = datetime.fromtimestamp(
                _normalize_broker_timestamp(tick.get("timestamp", tick.get("time"))),
                timezone.utc,
            )
        except (TypeError, ValueError, OverflowError, OSError, CandleDataError):
            return
        if self.on_tick is not None:
            try:
                self.on_tick(pair, price, timestamp)
            except Exception as exc:
                logger.error("Paper tracking update failed (%s).", type(exc).__name__)
        signal = state.strategy.on_tick(
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

    async def run(self, *, minutes: float) -> None:
        if minutes <= 0:
            raise ValueError("minutes must be greater than zero")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + minutes * 60.0
        next_refresh = loop.time()
        next_health = loop.time() + 60.0
        self._stopping = False
        try:
            await self.broker.connect()
            await self.refresh_pairs()
            next_refresh = loop.time() + config.PAYOUT_REFRESH_SECONDS
            while loop.time() < deadline:
                wake_at = min(deadline, next_refresh, next_health)
                await asyncio.sleep(max(0.0, wake_at - loop.time()))
                now = loop.time()
                self.expire_signals()
                if now >= next_refresh and now < deadline:
                    await self.refresh_pairs()
                    next_refresh = now + config.PAYOUT_REFRESH_SECONDS
                if now >= next_health:
                    self.output(self.stream_health_line())
                    next_health = now + 60.0
        finally:
            self._stopping = True
            for task in self._tasks.values():
                task.cancel()
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
            self._tasks.clear()
            self.output(self.stream_health_line())
            await self.broker.close()
