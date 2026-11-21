"""Broker-time candle scheduler layered over the existing stream manager."""
from __future__ import annotations

import asyncio
import time
import math
from datetime import datetime, timezone

import scanner_core as core
from data.broker_clock import BrokerClock
from data.candles import Candle, CandleBuilder, CandleDataError, _normalize_broker_timestamp
from strategy.price_action import PriceActionStrategy

PairScanState = core.PairScanState
ASSET_TICK_WAIT_SECONDS = core.ASSET_TICK_WAIT_SECONDS
ASSET_SUBSCRIBE_ATTEMPTS = core.ASSET_SUBSCRIBE_ATTEMPTS
ASSET_TICK_MAX_AGE_SECONDS = core.ASSET_TICK_MAX_AGE_SECONDS
ASSET_UNSUBSCRIBE_GRACE_SECONDS = core.ASSET_UNSUBSCRIBE_GRACE_SECONDS


class SignalScanner(core.SignalScanner):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.broker_clock = BrokerClock()

    def _new_state(self, asset):
        return PairScanState(asset, CandleBuilder(clock=self.broker_clock.now), PriceActionStrategy())

    def _handle_tick(self, symbol, state, tick):
        try:
            stamp = _normalize_broker_timestamp(tick.get("timestamp", tick.get("time")))
            timestamp = datetime.fromtimestamp(stamp, timezone.utc)
            price = float(tick.get("close", tick.get("price")))
            age = time.time() - stamp
            if not math.isfinite(price) or age < 0 or age >= ASSET_TICK_MAX_AGE_SECONDS:
                return
            if state.latest_tick_at is not None and timestamp < state.latest_tick_at:
                return
            self.broker_clock.observe(stamp)
            closed = state.candles.update(tick)
        except (ValueError, TypeError, OverflowError, OSError, CandleDataError):
            return
        state.latest_price, state.latest_tick_at = price, timestamp
        state.first_tick.set()
        self._last_any_tick_at = time.monotonic()
        if self.on_tick is not None:
            try:
                self.on_tick(symbol, price, timestamp)
            except Exception as exc:
                core.logger.error("Signal outcome update failed (%s).", type(exc).__name__)
        if closed is not None:
            self._evaluate_closed(symbol, state, closed)

    async def _wait_for_fresh_tick(self, symbol, state):
        if self.price_for_symbol(symbol) is not None:
            return True
        state.first_tick.clear()
        try:
            await asyncio.wait_for(state.first_tick.wait(), timeout=ASSET_TICK_WAIT_SECONDS)
        except TimeoutError:
            return False
        return self.price_for_symbol(symbol) is not None

    def _evaluate_closed(self, symbol, state, closed):
        if self.on_candle is not None:
            try:
                self.on_candle(symbol, closed)
            except Exception as exc:
                core.logger.error("Candle outcome update failed (%s).", type(exc).__name__)
        # Scheduler latency or a stalled connection cannot turn an old close into a fresh alert.
        delay = self.broker_clock.now() - (closed.time + 60)
        if self._paused or not 0 <= delay < 1 or self.price_for_symbol(symbol) is None:
            return
        signal = state.strategy.on_closed_candles(symbol, tuple(state.candles.closed),
                                                  asset_name=state.asset.display_name, payout=None)
        if signal is not None:
            self._spawn(self._publish_with_live_payout(signal), f"signal-{symbol}")

    def _close_minutes(self):
        if not self.broker_clock.synchronized:
            return
        now = self.broker_clock.now()
        for symbol, state in tuple(self.pairs.items()):
            closed = state.candles.close_due(now)
            if closed is None:
                continue
            self._evaluate_closed(symbol, state, closed)

    async def _minute_loop(self):
        while True:
            if self.broker_clock.synchronized:
                now = self.broker_clock.now()
                await asyncio.sleep(max(0.01, (int(now) // 60 + 1) * 60 - now))
                self._close_minutes()
            else:
                await asyncio.sleep(0.1)

    async def run(self, *, minutes=None):
        scheduler = asyncio.create_task(self._minute_loop(), name="broker-minute-clock")
        try:
            await super().run(minutes=minutes)
        finally:
            scheduler.cancel()
            await asyncio.gather(scheduler, return_exceptions=True)