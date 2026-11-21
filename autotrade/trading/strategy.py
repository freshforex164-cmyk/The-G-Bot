"""Pluggable strategies. A strategy only turns market data into a Decision; it never touches the
broker. Risk checks and execution happen elsewhere, so a new strategy cannot bypass them."""
from __future__ import annotations

from typing import Callable, Protocol

from data.candles import CandleBuilder
from data.broker_clock import BrokerClock
from strategy.price_action import PriceActionStrategy

from .models import Decision, Tick, TradingSettings


class Strategy(Protocol):
    name: str

    def required_symbols(self, s: TradingSettings) -> tuple[str, ...]: ...
    def on_tick(self, ticks: dict[str, Tick | None], s: TradingSettings) -> Decision | None: ...


class PriceActionExecution:
    """The same closed-minute strategy as signals; never decide from a partial candle."""

    name = "price_action"

    def __init__(self):
        self.clock = BrokerClock()
        self.candles = CandleBuilder(clock=self.clock.now)
        self.strategy = PriceActionStrategy()
        self._last_received = None

    def required_symbols(self, s: TradingSettings) -> tuple[str, ...]:
        return (s.symbol_otc,)

    def next_wake_seconds(self) -> float:
        if not self.clock.synchronized:
            return 1.0
        now = self.clock.now()
        return max(0.01, min(1.0, (int(now) // 60 + 1) * 60 - now))

    def on_tick(self, ticks: dict[str, Tick | None], s: TradingSettings) -> Decision | None:
        tick = ticks.get(s.symbol_otc)
        closed = None
        if tick is not None and tick.broker_time is not None and tick.received_at != self._last_received:
            self._last_received = tick.received_at
            self.clock.observe(tick.broker_time)
            closed = self.candles.update({"timestamp": tick.broker_time, "price": tick.price})
        if self.clock.synchronized and closed is None:
            closed = self.candles.close_due(self.clock.now())
        if closed is None or not 0 <= self.clock.now() - (closed.time + 60) < 1:
            return None
        signal = self.strategy.on_closed_candles(s.symbol_otc, tuple(self.candles.closed), asset_name=s.symbol_otc)
        if signal is None:
            return None
        return Decision(asset=signal.pair, direction="CALL" if signal.direction == "BUY" else "PUT",
                        expiry_seconds=60, reason=signal.setup, meta={"candle_time": closed.time})


# Register new strategies here. A user selects one by name in settings.strategy.
STRATEGIES: dict[str, Callable[[], Strategy]] = {"price_action": PriceActionExecution, "gap": PriceActionExecution}
# Existing saved strategy names remain readable; they no longer execute gap rules.
