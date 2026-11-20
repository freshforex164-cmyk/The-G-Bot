"""Pluggable strategies. A strategy only turns market data into a Decision; it never touches the
broker. Risk checks and execution happen elsewhere, so a new strategy cannot bypass them."""
from __future__ import annotations

from typing import Callable, Protocol

from .models import Decision, Tick, TradingSettings


class Strategy(Protocol):
    name: str

    def required_symbols(self, s: TradingSettings) -> tuple[str, ...]: ...
    def on_tick(self, ticks: dict[str, Tick | None], s: TradingSettings) -> Decision | None: ...


class GapStrategy:
    """Port of PocketOptionARR_BOT's logic: gap = real - otc.
    gap >= +threshold -> CALL on the OTC asset, gap <= -threshold -> PUT."""

    name = "gap"

    def required_symbols(self, s: TradingSettings) -> tuple[str, ...]:
        return (s.symbol_real, s.symbol_otc)

    def on_tick(self, ticks: dict[str, Tick | None], s: TradingSettings) -> Decision | None:
        real, otc = ticks.get(s.symbol_real), ticks.get(s.symbol_otc)
        if real is None or otc is None:
            return None
        gap = real.price - otc.price
        if gap >= s.gap_threshold:
            direction = "CALL"
        elif gap <= -s.gap_threshold:
            direction = "PUT"
        else:
            return None
        return Decision(
            asset=s.symbol_otc, direction=direction, expiry_seconds=s.expiry_seconds,
            reason=f"gap {gap:+.5f} vs threshold {s.gap_threshold}",
            meta={"gap": round(gap, 6), "real": real.price, "otc": otc.price})


# Register new strategies here. A user selects one by name in settings.strategy.
STRATEGIES: dict[str, Callable[[], Strategy]] = {"gap": GapStrategy}
