"""Closed one-minute price-action regime and alternating-signal state machine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Sequence

from data.candles import Candle
from strategy.model import Signal
from strategy.settings import STRATEGY_SETTINGS, StrategySettings

REGIMES = frozenset({"UPTREND", "DOWNTREND", "CONSOLIDATION"})


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
    range_valid: bool = False


SETUP_LABELS = {
    "UP_PULLBACK_SELL": "Uptrend pullback",
    "UP_CONTINUATION_BUY": "Uptrend continuation",
    "DOWN_PULLBACK_BUY": "Downtrend pullback",
    "DOWN_CONTINUATION_SELL": "Downtrend continuation",
    "RANGE_HIGH_SELL": "Consolidation high rejection",
    "RANGE_LOW_BUY": "Consolidation low rejection",
}


def confirmed_swings(
    candles: Sequence[Candle], kind: str, lag: int
) -> tuple[Swing, ...]:
    """Return confirmed pivots newest-first; a pivot needs ``lag`` later bars."""
    if kind not in {"high", "low"}:
        raise ValueError("kind must be 'high' or 'low'")
    if lag <= 0:
        raise ValueError("lag must be greater than zero")
    found: list[Swing] = []
    for index in range(len(candles) - lag - 1, lag - 1, -1):
        candle = candles[index]
        value = float(candle.high if kind == "high" else candle.low)
        neighbors = [
            float(item.high if kind == "high" else item.low)
            for item in (*candles[index - lag : index], *candles[index + 1 : index + lag + 1])
        ]
        if kind == "high" and all(value > other for other in neighbors):
            found.append(Swing(index, candle.time, value))
        elif kind == "low" and all(value < other for other in neighbors):
            found.append(Swing(index, candle.time, value))
    return tuple(found)


def average_true_range(candles: Sequence[Candle], period: int) -> float | None:
    """Return the simple mean of the latest true ranges."""
    if period <= 0 or len(candles) < 2:
        return None
    true_ranges: list[float] = []
    for previous, current in zip(candles, candles[1:]):
        high, low, previous_close = (
            float(current.high),
            float(current.low),
            float(previous.close),
        )
        true_ranges.append(
            max(high - low, abs(high - previous_close), abs(low - previous_close))
        )
    values = true_ranges[-period:]
    return sum(values) / len(values) if values else None


def classify_regime(
    candles: Sequence[Candle],
    settings: StrategySettings = STRATEGY_SETTINGS,
    *,
    previous_name: str | None = None,
) -> Regime:
    """Classify completed candles using rolling confirmed swings and an ATR range."""
    window = tuple(candles[-settings.regime_lookback :])
    highs = confirmed_swings(window, "high", settings.swing_lag)
    lows = confirmed_swings(window, "low", settings.swing_lag)

    if len(candles) < settings.min_regime_candles:
        return Regime(previous_name if previous_name in REGIMES else "CONSOLIDATION", highs, lows)

    if len(highs) >= 2 and len(lows) >= 2:
        if highs[0].price > highs[1].price and lows[0].price > lows[1].price:
            return Regime("UPTREND", highs, lows)
        if highs[0].price < highs[1].price and lows[0].price < lows[1].price:
            return Regime("DOWNTREND", highs, lows)

    if len(window) >= settings.regime_lookback:
        high_index = max(range(len(window)), key=lambda i: float(window[i].high))
        low_index = min(range(len(window)), key=lambda i: float(window[i].low))
        range_high = float(window[high_index].high)
        range_low = float(window[low_index].low)
        height = range_high - range_low
        atr = average_true_range(candles, settings.atr_period)
        if (
            atr is not None
            and atr > 0
            and height >= settings.min_range_atr_mult * atr
        ):
            zone_width = settings.consolidation_zone_width_pct * height
            return Regime(
                "CONSOLIDATION",
                highs,
                lows,
                range_high=range_high,
                range_low=range_low,
                high_zone_low=range_high - zone_width,
                low_zone_high=range_low + zone_width,
                range_key=(window[high_index].time, window[low_index].time),
                range_valid=True,
            )

    stable_name = previous_name if previous_name in REGIMES else "CONSOLIDATION"
    return Regime(stable_name, highs, lows)


class PriceActionStrategy:
    """Evaluate closed one-minute candles and enforce per-regime signal alternation."""

    def __init__(self, settings: StrategySettings = STRATEGY_SETTINGS) -> None:
        self.settings = settings
        self._last_closed_time: int | None = None
        self.regime_name = "CONSOLIDATION"
        self.current_regime = Regime("CONSOLIDATION")
        self.last_signal_type: str | None = None
        self.phase = "AWAIT_PULLBACK"
        self.H: Swing | None = None
        self.L: Swing | None = None
        self.pullback_extreme: float | None = None
        self.pullback_extreme_time: int | None = None
        self._regime_initialized = False
        self._regime_epoch = 0
        self._fired_levels: set[tuple[object, ...]] = set()

    def bootstrap(self, candles: Sequence[Candle]) -> None:
        """Replay at most 60 historical closed candles without emitting old signals."""
        self._reset()
        history = list(candles[-60:])
        for index, candle in enumerate(history):
            self._process_candle(history[: index + 1], emit=False)

    def on_closed_candles(
        self,
        pair: str,
        candles: Sequence[Candle],
        *,
        asset_name: str,
        payout: float | None = None,
    ) -> Signal | None:
        if not candles:
            return None
        history = list(candles)
        if self._last_closed_time is None:
            self.bootstrap(history[:-1][-60:])

        first_new = next(
            (
                index
                for index, candle in enumerate(history)
                if self._last_closed_time is None or candle.time > self._last_closed_time
            ),
            None,
        )
        if first_new is None:
            return None

        latest_signal: Signal | None = None
        for index in range(first_new, len(history)):
            signal = self._process_candle(
                history[: index + 1],
                pair=pair,
                asset_name=asset_name,
                payout=payout,
                emit=True,
            )
            if signal is not None:
                latest_signal = signal
        return latest_signal

    def debug_state(self, asset_name: str) -> dict[str, object]:
        """Return safe strategy diagnostics; no connection or credential fields exist here."""
        regime = self.current_regime
        return {
            "asset": asset_name,
            "regime": self.regime_name,
            "phase": self.phase,
            "last_signal_type": self.last_signal_type or "NONE",
            "H": self._level_text(self.H),
            "L": self._level_text(self.L),
            "pullback_extreme": (
                f"{self.pullback_extreme:g}@{self.pullback_extreme_time}"
                if self.pullback_extreme is not None
                else "NONE"
            ),
            "range_high": self._number_text(regime.range_high),
            "range_low": self._number_text(regime.range_low),
            "upper_zone_from": self._number_text(regime.high_zone_low),
            "lower_zone_to": self._number_text(regime.low_zone_high),
            "range_valid": regime.range_valid,
            "fired_levels": len(self._fired_levels),
            "last_closed_time": self._last_closed_time,
        }

    def format_debug_state(self, asset_name: str) -> str:
        state = self.debug_state(asset_name)
        return (
            f"STRATEGY DEBUG asset={state['asset']} regime={state['regime']} "
            f"phase={state['phase']} last_signal={state['last_signal_type']}\n"
            f"ACTIVE LEVELS H={state['H']} L={state['L']} "
            f"pullback_extreme={state['pullback_extreme']}\n"
            f"CONSOLIDATION high={state['range_high']} low={state['range_low']} "
            f"upper_zone_from={state['upper_zone_from']} "
            f"lower_zone_to={state['lower_zone_to']} "
            f"range_valid={state['range_valid']}\n"
            f"fired_levels={state['fired_levels']} "
            f"last_closed_time={state['last_closed_time']}"
        )

    def _process_candle(
        self,
        candles: Sequence[Candle],
        *,
        pair: str = "",
        asset_name: str = "",
        payout: float | None = None,
        emit: bool,
    ) -> Signal | None:
        if not candles:
            return None
        candle = candles[-1]
        if self._last_closed_time is not None and candle.time <= self._last_closed_time:
            return None
        self._last_closed_time = candle.time
        if len(candles) < self.settings.min_regime_candles:
            return None

        regime = classify_regime(
            candles,
            self.settings,
            previous_name=self.regime_name if self._regime_initialized else None,
        )
        if not self._regime_initialized or regime.name != self.regime_name:
            self._start_regime(regime, candles)
        else:
            self.current_regime = regime

        if self.regime_name == "UPTREND":
            return self._uptrend_signal(
                candles, pair, asset_name, payout, emit=emit
            )
        if self.regime_name == "DOWNTREND":
            return self._downtrend_signal(
                candles, pair, asset_name, payout, emit=emit
            )
        return self._consolidation_signal(
            candle, pair, asset_name, payout, emit=emit
        )

    def _start_regime(self, regime: Regime, candles: Sequence[Candle]) -> None:
        self.regime_name = regime.name
        self.current_regime = regime
        self._regime_initialized = True
        self._regime_epoch += 1
        self._fired_levels.clear()
        self.last_signal_type = None
        self.phase = "AWAIT_PULLBACK"
        self.H = None
        self.L = None
        self.pullback_extreme = None
        self.pullback_extreme_time = None
        if regime.name == "UPTREND" and regime.swing_highs:
            self.H = regime.swing_highs[0]
        elif regime.name == "DOWNTREND" and regime.swing_lows:
            self.L = regime.swing_lows[0]

    def _uptrend_signal(
        self,
        candles: Sequence[Candle],
        pair: str,
        asset_name: str,
        payout: float | None,
        *,
        emit: bool,
    ) -> Signal | None:
        current = candles[-1]
        if self.H is None:
            self.H = Swing(len(candles) - 1, current.time, float(current.high))
            return None

        high_index = self._index_of_time(candles, self.H.time)
        bars_after_high = (
            len(candles) - 1 - high_index if high_index is not None else 0
        )
        broke_high = float(current.high) > self.H.price
        red_close = float(current.close) < float(current.open)
        is_pullback = (
            self.phase == "AWAIT_PULLBACK"
            and 1 <= bars_after_high <= self.settings.pullback_window_candles
            and (not broke_high or red_close)
        )

        if is_pullback:
            if broke_high:
                self.H = Swing(len(candles) - 1, current.time, float(current.high))
            level = self.H
            key = ("UP_PULLBACK_SELL", level.time, level.price)
            self.phase = "AWAIT_CONTINUATION"
            self.last_signal_type = "PULLBACK"
            self.pullback_extreme = float(current.low)
            self.pullback_extreme_time = current.time
            return self._fire(
                key,
                "UP_PULLBACK_SELL",
                "SELL",
                pair,
                asset_name,
                payout,
                current,
                emit=emit,
            )

        if broke_high:
            self.H = Swing(len(candles) - 1, current.time, float(current.high))

        if self.phase != "AWAIT_CONTINUATION" or self.pullback_extreme is None:
            return None
        if float(current.low) < self.pullback_extreme:
            self.pullback_extreme = float(current.low)
            self.pullback_extreme_time = current.time
            return None

        key = (
            "UP_CONTINUATION_BUY",
            self.H.time if self.H else None,
            self.pullback_extreme_time,
            self.pullback_extreme,
        )
        self.phase = "AWAIT_PULLBACK"
        self.last_signal_type = "CONTINUATION"
        return self._fire(
            key,
            "UP_CONTINUATION_BUY",
            "BUY",
            pair,
            asset_name,
            payout,
            current,
            emit=emit,
        )

    def _downtrend_signal(
        self,
        candles: Sequence[Candle],
        pair: str,
        asset_name: str,
        payout: float | None,
        *,
        emit: bool,
    ) -> Signal | None:
        current = candles[-1]
        if self.L is None:
            self.L = Swing(len(candles) - 1, current.time, float(current.low))
            return None

        low_index = self._index_of_time(candles, self.L.time)
        bars_after_low = (
            len(candles) - 1 - low_index if low_index is not None else 0
        )
        broke_low = float(current.low) < self.L.price
        green_close = float(current.close) > float(current.open)
        is_pullback = (
            self.phase == "AWAIT_PULLBACK"
            and 1 <= bars_after_low <= self.settings.pullback_window_candles
            and (not broke_low or green_close)
        )

        if is_pullback:
            if broke_low:
                self.L = Swing(len(candles) - 1, current.time, float(current.low))
            level = self.L
            key = ("DOWN_PULLBACK_BUY", level.time, level.price)
            self.phase = "AWAIT_CONTINUATION"
            self.last_signal_type = "PULLBACK"
            self.pullback_extreme = float(current.high)
            self.pullback_extreme_time = current.time
            return self._fire(
                key,
                "DOWN_PULLBACK_BUY",
                "BUY",
                pair,
                asset_name,
                payout,
                current,
                emit=emit,
            )

        if broke_low:
            self.L = Swing(len(candles) - 1, current.time, float(current.low))

        if self.phase != "AWAIT_CONTINUATION" or self.pullback_extreme is None:
            return None
        if float(current.high) > self.pullback_extreme:
            self.pullback_extreme = float(current.high)
            self.pullback_extreme_time = current.time
            return None

        key = (
            "DOWN_CONTINUATION_SELL",
            self.L.time if self.L else None,
            self.pullback_extreme_time,
            self.pullback_extreme,
        )
        self.phase = "AWAIT_PULLBACK"
        self.last_signal_type = "CONTINUATION"
        return self._fire(
            key,
            "DOWN_CONTINUATION_SELL",
            "SELL",
            pair,
            asset_name,
            payout,
            current,
            emit=emit,
        )

    def _consolidation_signal(
        self,
        candle: Candle,
        pair: str,
        asset_name: str,
        payout: float | None,
        *,
        emit: bool,
    ) -> Signal | None:
        regime = self.current_regime
        if (
            not regime.range_valid
            or regime.range_high is None
            or regime.range_low is None
            or regime.high_zone_low is None
            or regime.low_zone_high is None
        ):
            return None

        high = float(candle.high)
        low = float(candle.low)
        close = float(candle.close)
        if (
            high >= regime.high_zone_low
            and high <= regime.range_high
            and close < float(candle.open)
        ):
            key = ("RANGE_HIGH_SELL", regime.range_high)
            return self._fire(
                key,
                "RANGE_HIGH_SELL",
                "SELL",
                pair,
                asset_name,
                payout,
                candle,
                emit=emit,
            )
        if (
            low <= regime.low_zone_high
            and low >= regime.range_low
            and close > float(candle.open)
        ):
            key = ("RANGE_LOW_BUY", regime.range_low)
            return self._fire(
                key,
                "RANGE_LOW_BUY",
                "BUY",
                pair,
                asset_name,
                payout,
                candle,
                emit=emit,
            )
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
        *,
        emit: bool,
    ) -> Signal | None:
        level_key = (self._regime_epoch, *key)
        if level_key in self._fired_levels:
            return None
        self._fired_levels.add(level_key)
        if not emit:
            return None
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
            valid_until=closed_at
            + timedelta(seconds=self.settings.signal_valid_seconds),
        )

    def _reset(self) -> None:
        self._last_closed_time = None
        self.regime_name = "CONSOLIDATION"
        self.current_regime = Regime("CONSOLIDATION")
        self.last_signal_type = None
        self.phase = "AWAIT_PULLBACK"
        self.H = None
        self.L = None
        self.pullback_extreme = None
        self.pullback_extreme_time = None
        self._regime_initialized = False
        self._regime_epoch = 0
        self._fired_levels.clear()

    @staticmethod
    def _index_of_time(candles: Sequence[Candle], timestamp: int) -> int | None:
        for index in range(len(candles) - 1, -1, -1):
            if candles[index].time == timestamp:
                return index
        return None

    @staticmethod
    def _number_text(value: float | None) -> str:
        if value is None or not isfinite(float(value)):
            return "NONE"
        return f"{float(value):g}"

    @classmethod
    def _level_text(cls, level: Swing | None) -> str:
        if level is None:
            return "NONE"
        return f"{cls._number_text(level.price)}@{level.time}"
