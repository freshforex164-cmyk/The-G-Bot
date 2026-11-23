"""Pluggable strategies. A strategy only turns market data into a Decision; it never touches the
broker. Risk checks and execution happen elsewhere, so a new strategy cannot bypass them."""
from __future__ import annotations

from dataclasses import replace
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


class MultiAssetStrategy:
    """Runs one independent instance of a single-symbol strategy per scanned symbol (symbol_otc plus
    route_assets). Each instance keeps its own candles and closed-minute timing, so a decision is only ever
    produced from that symbol's own pattern; it is tagged with ``setup_asset`` and never re-targeted."""

    name = "multi_asset"

    def __init__(self, factory: Callable[[], Strategy]):
        self._factory = factory
        self._per: dict[str, Strategy] = {}

    def required_symbols(self, s: TradingSettings) -> tuple[str, ...]:
        return tuple(dict.fromkeys((s.symbol_otc, *s.route_assets)))

    def _get(self, symbol: str) -> Strategy:
        st = self._per.get(symbol)
        if st is None:
            st = self._per[symbol] = self._factory()
        return st

    def next_wake_seconds(self) -> float:
        waits = [w() for w in (getattr(st, "next_wake_seconds", None) for st in self._per.values()) if callable(w)]
        return min(waits) if waits else 1.0

    def on_tick_all(self, ticks: dict[str, Tick | None], s: TradingSettings) -> list[Decision]:
        out: list[Decision] = []
        for symbol in self.required_symbols(s):
            d = self._get(symbol).on_tick({symbol: ticks.get(symbol)}, s.with_changes(symbol_otc=symbol))
            if d is not None and d.asset == symbol:
                out.append(replace(d, meta={**d.meta, "setup_asset": symbol}))
        return out

    def on_tick(self, ticks: dict[str, Tick | None], s: TradingSettings) -> Decision | None:
        found = self.on_tick_all(ticks, s)
        return found[0] if found else None


# Register new strategies here. A user selects one by name in settings.strategy.
STRATEGIES: dict[str, Callable[[], Strategy]] = {"price_action": PriceActionExecution}
