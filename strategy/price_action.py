"""Closed 1-minute swing, trend, range, and price-action signals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

from data.candles import Candle
from indicators.atr import calculate_atr
from strategy.model import Signal
from strategy.settings import STRATEGY_SETTINGS, StrategySettings


@dataclass(frozen=True)
class Swing:
    index: int
    time: int
    price: float


@dataclass(frozen=True)
class Regime:
    name: str
    swing_highs: tuple[Swing, ...] = ()
    swing_lows: tuple[Swing, ...] = ()
    range_high: float | None = None
    range_low: float | None = None
    high_zone_low: float | None = None
    low_zone_high: float | None = None
    range_key: tuple[int, int] | None = None


SETUP_LABELS = {
    "UP_PULLBACK_SELL": "Uptrend pullback",
    "UP_CONTINUATION_BUY": "Uptrend continuation",
    "DOWN_PULLBACK_BUY": "Downtrend pullback",
    "DOWN_CONTINUATION_SELL": "Downtrend continuation",
    "RANGE_HIGH_SELL": "Range high rejection",
    "RANGE_LOW_BUY": "Range low rejection",
}


def confirmed_swings(
    candles: Sequence[Candle], kind: str, side: int
) -> tuple[Swing, ...]:
    """Return confirmed pivots in newest-first order (index 0 is the latest)."""
    if kind not in {"high", "low"}:
        raise ValueError("kind must be 'high' or 'low'")
    latest_first = tuple(reversed(candles))
    found: list[Swing] = []
    for index in range(side, len(latest_first) - side):
        candle = latest_first[index]
        value = float(candle.high if kind == "high" else candle.low)
        before = latest_first[index - side : index]
        after = latest_first[index + 1 : index + side + 1]
        neighbors = [float(bar.high if kind == "high" else bar.low) for bar in (*before, *after)]
        if kind == "high" and all(value > other for other in neighbors):
            found.append(Swing(index, candle.time, value))
        elif kind == "low" and all(value < other for other in neighbors):
            found.append(Swing(index, candle.time, value))
    return tuple(found)


def classify_regime(
    candles: Sequence[Candle],
    settings: StrategySettings = STRATEGY_SETTINGS,
) -> Regime:
    highs = confirmed_swings(candles, "high", settings.swing_side)
    lows = confirmed_swings(candles, "low", settings.swing_side)
    count = settings.trend_swings
    if count >= 2 and len(highs) >= count and len(lows) >= count:
        recent_highs = highs[:count]
        recent_lows = lows[:count]
        if all(a.price > b.price for a, b in zip(recent_highs, recent_highs[1:])) and all(
            a.price > b.price for a, b in zip(recent_lows, recent_lows[1:])
        ):
            return Regime("UP", highs, lows)
        if all(a.price < b.price for a, b in zip(recent_highs, recent_highs[1:])) and all(
            a.price < b.price for a, b in zip(recent_lows, recent_lows[1:])
        ):
            return Regime("DOWN", highs, lows)

    if len(candles) < settings.range_candles:
        return Regime("NONE", highs, lows)
    window = candles[-settings.range_candles :]
    high_candle = max(window, key=lambda item: float(item.high))
    low_candle = min(window, key=lambda item: float(item.low))
    range_high = float(high_candle.high)
    range_low = float(low_candle.low)
    height = range_high - range_low
    atr_values = calculate_atr(candles, settings.atr_period)
    atr = atr_values[-1] if atr_values else None
    if atr is None or atr <= 0 or height < settings.range_min_height_atr * atr:
        return Regime("NONE", highs, lows)
    return Regime(
        "RANGE",
        highs,
        lows,
        range_high=range_high,
        range_low=range_low,
        high_zone_low=range_high - settings.zone_fraction * height,
        low_zone_high=range_low + settings.zone_fraction * height,
        range_key=(high_candle.time, low_candle.time),
    )


class PriceActionStrategy:
    """Evaluate only closed candles; each structural level can fire once."""

    def __init__(self, settings: StrategySettings = STRATEGY_SETTINGS) -> None:
        self.settings = settings
        self._last_closed_time: int | None = None
        self._fired_levels: set[tuple[object, ...]] = set()
        self._range_key: tuple[int, int] | None = None
        self._range_pending: dict[str, int | None] = {"high": None, "low": None}
        self._range_waiting_for_exit = {"high": False, "low": False}

    def on_closed_candles(
        self,
        pair: str,
        candles: Sequence[Candle],
        *,
        asset_name: str,
        payout: float | None = None,
    ) -> Signal | None:
        if not candles or candles[-1].time == self._last_closed_time:
            return None
        self._last_closed_time = candles[-1].time
        current = candles[-1]
        regime = classify_regime(candles, self.settings)
        if regime.name == "UP":
            return self._up_signal(pair, asset_name, payout, candles, regime)
        if regime.name == "DOWN":
            return self._down_signal(pair, asset_name, payout, candles, regime)
        if regime.name == "RANGE":
            return self._range_signal(pair, asset_name, payout, current, regime)
        return None

    def _up_signal(
        self,
        pair: str,
        asset_name: str,
        payout: float | None,
        candles: Sequence[Candle],
        regime: Regime,
    ) -> Signal | None:
        high = regime.swing_highs[0]
        current = candles[-1]
        if current.time > high.time and (
            float(current.high) < high.price
            or (float(current.high) >= high.price and current.close < current.open)
        ):
            key = ("UP_PULLBACK_SELL", high.time, high.price)
            signal = self._fire(key, "UP_PULLBACK_SELL", "SELL", pair, asset_name, payout, current)
            if signal:
                return signal
        after_high = [bar for bar in candles if bar.time > high.time]
        previous = after_high[:-1]
        if previous and any(bar.close < bar.open for bar in previous):
            low_bar = min(previous, key=lambda bar: float(bar.low))
            if current.time > low_bar.time and current.low >= low_bar.low:
                key = ("UP_CONTINUATION_BUY", high.time, low_bar.time, low_bar.low)
                return self._fire(
                    key, "UP_CONTINUATION_BUY", "BUY", pair, asset_name, payout, current
                )
        return None

    def _down_signal(
        self,
        pair: str,
        asset_name: str,
        payout: float | None,
        candles: Sequence[Candle],
        regime: Regime,
    ) -> Signal | None:
        low = regime.swing_lows[0]
        current = candles[-1]
        if current.time > low.time and (
            float(current.low) > low.price
            or (float(current.low) <= low.price and current.close > current.open)
        ):
            key = ("DOWN_PULLBACK_BUY", low.time, low.price)
            signal = self._fire(key, "DOWN_PULLBACK_BUY", "BUY", pair, asset_name, payout, current)
            if signal:
                return signal
        after_low = [bar for bar in candles if bar.time > low.time]
        previous = after_low[:-1]
        if previous and any(bar.close > bar.open for bar in previous):
            high_bar = max(previous, key=lambda bar: float(bar.high))
            if current.time > high_bar.time and current.high <= high_bar.high:
                key = ("DOWN_CONTINUATION_SELL", low.time, high_bar.time, high_bar.high)
                return self._fire(
                    key, "DOWN_CONTINUATION_SELL", "SELL", pair, asset_name, payout, current
                )
        return None

    def _range_signal(
        self,
        pair: str,
        asset_name: str,
        payout: float | None,
        candle: Candle,
        regime: Regime,
    ) -> Signal | None:
        if regime.range_key != self._range_key:
            self._range_key = regime.range_key
            self._range_pending = {"high": None, "low": None}
            self._range_waiting_for_exit = {"high": False, "low": False}
        assert regime.high_zone_low is not None and regime.low_zone_high is not None
        high_pending = self._range_pending["high"]
        if (
            high_pending is not None
            and candle.time > high_pending
            and candle.high <= float(regime.range_high)
        ):
            self._range_waiting_for_exit["high"] = True
            return self._fire(
                ("RANGE_HIGH_SELL", regime.range_key, high_pending),
                "RANGE_HIGH_SELL",
                "SELL",
                pair,
                asset_name,
                payout,
                candle,
            )
        if candle.high < regime.high_zone_low:
            self._range_pending["high"] = None
            self._range_waiting_for_exit["high"] = False
        elif not self._range_waiting_for_exit["high"] and high_pending is None:
            self._range_pending["high"] = candle.time

        low_pending = self._range_pending["low"]
        if (
            low_pending is not None
            and candle.time > low_pending
            and candle.low >= float(regime.range_low)
        ):
            self._range_waiting_for_exit["low"] = True
            return self._fire(
                ("RANGE_LOW_BUY", regime.range_key, low_pending),
                "RANGE_LOW_BUY",
                "BUY",
                pair,
                asset_name,
                payout,
                candle,
            )
        if candle.low > regime.low_zone_high:
            self._range_pending["low"] = None
            self._range_waiting_for_exit["low"] = False
        elif not self._range_waiting_for_exit["low"] and low_pending is None:
            self._range_pending["low"] = candle.time
        return None

    def _fire(
        self,
        key: tuple[object, ...],
        setup: str,
        direction: str,
        pair: str,
        asset_name: str,
        payout: float | None,
        candle: Candle,
    ) -> Signal | None:
        if key in self._fired_levels:
            return None
        self._fired_levels.add(key)
        closed_at = datetime.fromtimestamp(
            candle.time + self.settings.timeframe_seconds, timezone.utc
        )
        return Signal(
            pair=pair,
            direction=direction,
            setup=setup,
            price=float(candle.close),
            time=closed_at,
            asset_name=asset_name,
            payout=payout,
            expiry_seconds=self.settings.expiry_seconds,
            valid_until=closed_at + timedelta(seconds=self.settings.signal_valid_seconds),
        )
