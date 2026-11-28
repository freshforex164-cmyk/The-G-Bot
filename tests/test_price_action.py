from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path

from data.candles import Candle
from strategy.price_action import (
    average_true_range,
    classify_regime,
    confirmed_swings,
)
from strategy.settings import STRATEGY_SETTINGS


def candle(index: int, high: float, low: float) -> Candle:
    middle = (high + low) / 2
    return Candle(index * 60, middle, high, low, middle)


def trend_history(*, down: bool = False) -> list[Candle]:
    highs = [10, 11, 14, 12, 11, 13, 16, 14, 13, 15, 18, 16, 15, 17, 20, 18, 17, 19, 22, 20]
    lows = [8, 9, 10, 9, 8, 10, 12, 10, 9, 11, 13, 11, 10, 12, 14, 12, 11, 13, 15, 13]
    if down:
        highs, lows = [30 - low for low in lows], [30 - high for high in highs]
    return [candle(index, 10, 9) for index in range(10)] + [
        candle(index + 10, high, low)
        for index, (high, low) in enumerate(zip(highs, lows))
    ]


class StrategyConfigurationTests(unittest.TestCase):
    def test_strategy_json_contains_requested_price_action_settings(self) -> None:
        payload = json.loads(Path("strategy.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["min_regime_candles"], 30)
        self.assertEqual(payload["swing_lag"], 1)
        self.assertEqual(payload["regime_lookback"], 20)
        self.assertEqual(payload["min_range_atr_mult"], 1.5)
        self.assertEqual(payload["consolidation_zone_width_pct"], 0.25)
        self.assertEqual(payload["pullback_window_candles"], 1)
        self.assertEqual(payload["timeframe_seconds"], 60)
        self.assertEqual(payload["expiry_seconds"], 60)

    def test_strategy_rejects_non_minute_timeframe_or_expiry(self) -> None:
        with self.assertRaisesRegex(ValueError, "1-minute"):
            replace(STRATEGY_SETTINGS, timeframe_seconds=15)
        with self.assertRaisesRegex(ValueError, "1-minute"):
            replace(STRATEGY_SETTINGS, expiry_seconds=35)


class RegimeClassificationTests(unittest.TestCase):
    def test_confirmed_swings_use_one_bar_confirmation(self) -> None:
        values = [10, 12, 9, 11, 8]
        candles = [candle(index, value, value - 1) for index, value in enumerate(values)]
        swings = confirmed_swings(candles, "high", lag=1)
        self.assertEqual([swing.price for swing in swings], [11, 12])
        self.assertEqual(swings[1].time, candles[1].time)

    def test_rolling_swing_structure_classifies_both_trend_directions(self) -> None:
        self.assertEqual(classify_regime(trend_history()).name, "UPTREND")
        self.assertEqual(classify_regime(trend_history(down=True)).name, "DOWNTREND")

    def test_history_shorter_than_minimum_cannot_confirm_a_trend(self) -> None:
        history = trend_history()
        regime = classify_regime(history[: STRATEGY_SETTINGS.min_regime_candles - 1])
        self.assertEqual(regime.name, "CONSOLIDATION")
        self.assertFalse(regime.range_valid)

    def test_consolidation_uses_atr_threshold_and_quarter_zones(self) -> None:
        history = [candle(index, 10, 9.5) for index in range(30)]
        history[20] = candle(20, 12, 9.5)
        history[24] = candle(24, 10, 8)

        regime = classify_regime(history)
        self.assertEqual(regime.name, "CONSOLIDATION")
        self.assertTrue(regime.range_valid)
        self.assertEqual(regime.range_high, 12)
        self.assertEqual(regime.range_low, 8)
        self.assertEqual(regime.high_zone_low, 11)
        self.assertEqual(regime.low_zone_high, 9)
        self.assertIsNotNone(average_true_range(history, 14))

    def test_tight_range_replaces_stale_trend(self) -> None:
        history = [candle(index, 10, 9.5) for index in range(30)]
        settings = replace(STRATEGY_SETTINGS, min_range_atr_mult=20)
        regime = classify_regime(history, settings, previous_name="UPTREND")
        self.assertEqual(regime.name, "CONSOLIDATION")
        self.assertFalse(regime.range_valid)


if __name__ == "__main__":
    unittest.main()
