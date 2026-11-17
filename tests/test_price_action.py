from __future__ import annotations

import unittest
from dataclasses import replace

from data.candles import Candle
from strategy.price_action import classify_regime, confirmed_swings
from strategy.settings import STRATEGY_SETTINGS


def candle(index: int, high: float, low: float) -> Candle:
    middle = (high + low) / 2
    return Candle(index * 60, middle, high, low, middle)


class PriceActionRegimeTests(unittest.TestCase):
    def test_uptrend_uses_latest_two_confirmed_highs_and_lows(self) -> None:
        values = [
            (10, 7), (11, 8), (12, 9), (10, 8), (9, 6), (11, 7),
            (14, 9), (12, 8.5), (12, 8), (13, 9), (15, 10),
        ]
        candles = [candle(index, high, low) for index, (high, low) in enumerate(values)]
        self.assertEqual(classify_regime(candles).name, "UP")
        highs = confirmed_swings(candles, "high", 2)
        self.assertGreater(highs[0].price, highs[1].price)
        self.assertEqual(highs[0].time, candles[6].time)

    def test_range_and_none_are_distinguished(self) -> None:
        values = [(10, 9) for _ in range(20)]
        values[4] = (12, 9)
        values[10] = (10, 7)
        candles = [candle(index, high, low) for index, (high, low) in enumerate(values)]
        self.assertEqual(classify_regime(candles).name, "RANGE")
        self.assertEqual(classify_regime(candles[:10]).name, "NONE")
        self.assertAlmostEqual(
            classify_regime(candles).high_zone_low,
            12 - 0.25 * (12 - 7),
        )

    def test_strategy_settings_are_used_for_range_history_length(self) -> None:
        values = [(10, 9) for _ in range(20)]
        values[4] = (12, 9)
        values[10] = (10, 7)
        candles = [candle(index, high, low) for index, (high, low) in enumerate(values)]
        changed = replace(STRATEGY_SETTINGS, range_candles=21)
        self.assertEqual(classify_regime(candles, changed).name, "NONE")


if __name__ == "__main__":
    unittest.main()
