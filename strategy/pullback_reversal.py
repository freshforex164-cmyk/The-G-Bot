"""Closed-candle pullback/reversal setup with a live middle-band trigger."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

import config
from config import ConfigurationError
from data.candles import Candle
from indicators.bollinger_bands import calculate_bollinger_bands
from indicators.moving_average import calculate_moving_average
from indicators.stochastic import calculate_stochastic


@dataclass(frozen=True)
class Signal:
    pair: str
    direction: str
    price: float
    time: datetime
    stochastic_k: float | None
    stochastic_d: float | None
    ma_slope: float | None
    bollinger_middle: float


@dataclass(frozen=True)
class _Setup:
    direction: str
    middle_band: float
    stochastic_k: float | None
    stochastic_d: float | None
    ma_slope: float | None


@dataclass
class _PairState:
    last_closed_time: int | None = None
    cooldown_remaining: int = 0
    signal_candle_time: int | None = None
    pending_setup: _Setup | None = None
    closed_candles: tuple[Candle, ...] = ()
    forming_time: int | None = None
    previous_live_price: float | None = None
    previous_live_middle: float | None = None


class PullbackReversalStrategy:
    """Generate paper-only signals; this class has no broker/order interface."""

    def __init__(self) -> None:
        if not config.USE_BOLLINGER_BANDS:
            raise ConfigurationError(
                "Bollinger Bands must remain enabled for pullback/reversal signals."
            )
        self._states: dict[str, _PairState] = {}
        self._overbought = (
            config.STOCHASTIC_ALTERNATE_OVERBOUGHT
            if config.STOCHASTIC_USE_ALTERNATE_LEVELS
            else config.STOCHASTIC_OVERBOUGHT
        )
        self._oversold = (
            config.STOCHASTIC_ALTERNATE_OVERSOLD
            if config.STOCHASTIC_USE_ALTERNATE_LEVELS
            else config.STOCHASTIC_OVERSOLD
        )

    def on_closed_candles(
        self, pair: str, candles: Sequence[Candle]
    ) -> str | None:
        """Evaluate a new closed-candle setup; return its direction if ready."""
        if not candles:
            return None
        state = self._states.setdefault(pair, _PairState())
        latest = candles[-1]
        if state.last_closed_time == latest.time:
            return state.pending_setup.direction if state.pending_setup else None

        state.last_closed_time = latest.time
        state.closed_candles = tuple(candles)
        state.pending_setup = None
        if state.cooldown_remaining > 0:
            if (
                state.signal_candle_time is not None
                and latest.time <= state.signal_candle_time
            ):
                return None
            state.cooldown_remaining -= 1
        if state.cooldown_remaining > 0:
            return None
        state.signal_candle_time = None

        setup = self._find_setup(candles)
        state.pending_setup = setup
        return setup.direction if setup else None

    def _find_setup(self, candles: Sequence[Candle]) -> _Setup | None:
        count = len(candles)
        last_index = count - 1
        bb_back = config.BB_TREND_LOOKBACK_CANDLES
        if last_index - bb_back < 0 or last_index - 1 < 0:
            return None

        bands = calculate_bollinger_bands(candles)
        mid_now = bands.middle[last_index]
        mid_back = bands.middle[last_index - bb_back]
        upper_now = bands.upper[last_index]
        upper_previous = bands.upper[last_index - 1]
        lower_now = bands.lower[last_index]
        lower_previous = bands.lower[last_index - 1]
        if any(
            value is None
            for value in (
                mid_now,
                mid_back,
                upper_now,
                upper_previous,
                lower_now,
                lower_previous,
            )
        ):
            return None

        last_close = float(candles[last_index].close)
        stoch_k: float | None = None
        stoch_d: float | None = None
        stoch_sell = stoch_buy = True
        if config.USE_STOCHASTIC:
            stochastic = calculate_stochastic(candles)
            k_window_start = last_index - config.STOCHASTIC_OVERBOUGHT_LOOKBACK_CANDLES + 1
            if k_window_start < 0:
                return None
            k_window = stochastic.slow_k[k_window_start : last_index + 1]
            stoch_k = stochastic.slow_k[last_index]
            stoch_d = stochastic.slow_d[last_index]
            if (
                len(k_window) != config.STOCHASTIC_OVERBOUGHT_LOOKBACK_CANDLES
                or any(value is None for value in k_window)
                or stoch_k is None
                or stoch_d is None
            ):
                return None
            stoch_sell = (
                any(value >= self._overbought for value in k_window if value is not None)
                and stoch_k < stoch_d
            )
            stoch_buy = (
                any(value <= self._oversold for value in k_window if value is not None)
                and stoch_k > stoch_d
            )

        ma_slope: float | None = None
        ma_sell = ma_buy = True
        if config.USE_MOVING_AVERAGE:
            moving_average = calculate_moving_average(
                candles,
                period=config.MOVING_AVERAGE_PERIOD,
                method=config.MOVING_AVERAGE_METHOD,
            )
            prior_rise_index = last_index - config.MA_PRIOR_RISE_LOOKBACK_CANDLES
            turn_index = last_index - config.MA_TURN_LOOKBACK_CANDLES
            if prior_rise_index < 0 or turn_index < 0:
                return None
            ma_now = moving_average[last_index]
            ma_before_rise = moving_average[prior_rise_index]
            ma_at_turn = moving_average[turn_index]
            if ma_now is None or ma_before_rise is None or ma_at_turn is None:
                return None
            tolerance = (
                abs(last_close) * config.MA_FLAT_TOLERANCE_PERCENT / 100.0
            )
            ma_slope = ma_now - ma_at_turn
            ma_sell = (
                ma_at_turn > ma_before_rise
                and ma_now <= ma_at_turn + tolerance
            )
            ma_buy = (
                ma_at_turn < ma_before_rise
                and ma_now >= ma_at_turn - tolerance
            )

        bollinger_sell = (
            mid_now <= mid_back
            and upper_now < upper_previous
            and last_close < mid_now
        )
        bollinger_buy = (
            mid_now >= mid_back
            and lower_now > lower_previous
            and last_close > mid_now
        )

        if stoch_sell and ma_sell and bollinger_sell:
            return _Setup("SELL", mid_now, stoch_k, stoch_d, ma_slope)
        if stoch_buy and ma_buy and bollinger_buy:
            return _Setup("BUY", mid_now, stoch_k, stoch_d, ma_slope)
        return None

    def on_tick(
        self,
        pair: str,
        live_price: float,
        forming_candle: Candle | None,
        *,
        timestamp: datetime | None = None,
    ) -> Signal | None:
        """Check a live trigger against the active forming candle on each tick."""
        state = self._states.setdefault(pair, _PairState())
        try:
            price = float(live_price)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(price) or forming_candle is None:
            return None

        setup = state.pending_setup
        current_middle: float | None = None
        if setup is not None and state.closed_candles:
            live_bands = calculate_bollinger_bands(
                (*state.closed_candles, forming_candle)
            )
            current_middle = live_bands.middle[-1]

        if state.forming_time != forming_candle.time:
            state.forming_time = forming_candle.time
            state.previous_live_price = price
            state.previous_live_middle = current_middle
            return None

        previous_price = state.previous_live_price
        previous_middle = state.previous_live_middle
        signal: Signal | None = None
        if (
            setup is not None
            and previous_price is not None
            and previous_middle is not None
            and current_middle is not None
        ):
            if setup.direction == "SELL":
                triggered = (
                    previous_price <= previous_middle
                    and current_middle <= price
                    and price > float(forming_candle.open)
                )
            else:
                triggered = (
                    previous_price >= previous_middle
                    and current_middle >= price
                    and price < float(forming_candle.open)
                )

            if triggered:
                signal = Signal(
                    pair=pair,
                    direction=setup.direction,
                    price=price,
                    time=timestamp or datetime.now(timezone.utc),
                    stochastic_k=setup.stochastic_k,
                    stochastic_d=setup.stochastic_d,
                    ma_slope=setup.ma_slope,
                    bollinger_middle=current_middle,
                )
                state.pending_setup = None
                state.cooldown_remaining = config.SIGNAL_COOLDOWN_CANDLES
                state.signal_candle_time = forming_candle.time

        state.previous_live_price = price
        state.previous_live_middle = current_middle
        return signal
