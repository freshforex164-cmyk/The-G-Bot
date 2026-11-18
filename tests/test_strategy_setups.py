from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from data.candles import Candle
from strategy.price_action import (
    PriceActionStrategy,
    Regime,
    Swing,
    classify_regime,
)
from strategy.settings import STRATEGY_SETTINGS, load_strategy_settings


def bar(time: int, open_: float, high: float, low: float, close: float) -> Candle:
    return Candle(time, open_, high, low, close)


class PriceActionSetupTests(unittest.TestCase):
    def evaluate(self, strategy, candles, regime):
        with patch("strategy.price_action.classify_regime", return_value=regime):
            return strategy.on_closed_candles(
                "PAIR_otc", candles, asset_name="Pair"
            )

    def test_up_pullback_sell_fires_and_has_a_non_firing_case(self):
        regime = Regime("UP", swing_highs=(Swing(0, 0, 10),))
        fired = self.evaluate(
            PriceActionStrategy(),
            [bar(0, 9, 10, 8, 9), bar(60, 9.8, 9.9, 9.1, 9.4)],
            regime,
        )
        no_fire = self.evaluate(
            PriceActionStrategy(),
            [bar(0, 9, 10, 8, 9), bar(60, 9.4, 10.2, 9.3, 9.8)],
            regime,
        )
        self.assertEqual((fired.setup, fired.direction), ("UP_PULLBACK_SELL", "SELL"))
        self.assertIsNone(no_fire)

    def test_up_continuation_buy_fires_only_after_a_bearish_candle(self):
        regime = Regime("UP", swing_highs=(Swing(0, 0, 10),))
        candles = [
            bar(0, 9, 10, 8, 9),
            bar(60, 10, 10.2, 8, 9.4),
            bar(120, 10.1, 10.4, 8.2, 10.3),
        ]
        fired = self.evaluate(PriceActionStrategy(), candles, regime)
        without_bearish = [candles[0], bar(60, 9.5, 10.2, 8, 10), candles[2]]
        no_fire = self.evaluate(PriceActionStrategy(), without_bearish, regime)
        self.assertEqual((fired.setup, fired.direction), ("UP_CONTINUATION_BUY", "BUY"))
        self.assertIsNone(no_fire)

    def test_down_pullback_buy_fires_and_has_a_non_firing_case(self):
        regime = Regime("DOWN", swing_lows=(Swing(0, 0, 10),))
        fired = self.evaluate(
            PriceActionStrategy(),
            [bar(0, 11, 12, 10, 11), bar(60, 10.1, 10.8, 10.1, 10.6)],
            regime,
        )
        no_fire = self.evaluate(
            PriceActionStrategy(),
            [bar(0, 11, 12, 10, 11), bar(60, 10.2, 10.8, 9.9, 10.0)],
            regime,
        )
        self.assertEqual((fired.setup, fired.direction), ("DOWN_PULLBACK_BUY", "BUY"))
        self.assertIsNone(no_fire)

    def test_down_continuation_sell_fires_only_after_a_bullish_candle(self):
        regime = Regime("DOWN", swing_lows=(Swing(0, 0, 10),))
        candles = [
            bar(0, 11, 12, 10, 11),
            bar(60, 10, 12, 9.8, 10.5),
            bar(120, 10.3, 11.9, 9.8, 10.0),
        ]
        fired = self.evaluate(PriceActionStrategy(), candles, regime)
        without_bullish = [candles[0], bar(60, 10.5, 12, 9.8, 10), candles[2]]
        no_fire = self.evaluate(PriceActionStrategy(), without_bullish, regime)
        self.assertEqual(
            (fired.setup, fired.direction), ("DOWN_CONTINUATION_SELL", "SELL")
        )
        self.assertIsNone(no_fire)

    def test_range_high_touch_fires_on_first_later_close_and_once_per_touch(self):
        regime = Regime(
            "RANGE",
            range_high=10,
            range_low=0,
            high_zone_low=7.5,
            low_zone_high=2.5,
            range_key=(0, 30),
        )
        strategy = PriceActionStrategy()
        touch = [bar(0, 7, 8, 4, 7.5)]
        self.assertIsNone(self.evaluate(strategy, touch, regime))
        fired = self.evaluate(strategy, touch + [bar(60, 6, 7, 5, 6.5)], regime)
        self.assertEqual((fired.setup, fired.direction), ("RANGE_HIGH_SELL", "SELL"))
        self.assertIsNone(
            self.evaluate(strategy, touch + [bar(60, 6, 7, 5, 6.5), bar(120, 8, 8.5, 6, 8)], regime)
        )
        self.assertIsNone(
            self.evaluate(
                PriceActionStrategy(),
                [bar(0, 6, 7, 4, 6), bar(60, 6, 7, 5, 6.5)],
                regime,
            )
        )

    def test_range_low_touch_fires_on_first_later_close_and_has_non_firing_case(self):
        regime = Regime(
            "RANGE",
            range_high=10,
            range_low=0,
            high_zone_low=7.5,
            low_zone_high=2.5,
            range_key=(0, 30),
        )
        touch = [bar(0, 3, 6, 2, 3)]
        strategy = PriceActionStrategy()
        self.assertIsNone(self.evaluate(strategy, touch, regime))
        fired = self.evaluate(strategy, touch + [bar(60, 4, 6, 3, 4.5)], regime)
        self.assertEqual((fired.setup, fired.direction), ("RANGE_LOW_BUY", "BUY"))
        no_fire = self.evaluate(
            PriceActionStrategy(),
            [bar(0, 4, 6, 3, 4), bar(60, 4, 6, 3, 4.5)],
            regime,
        )
        self.assertIsNone(no_fire)

    def test_trend_range_and_none_use_hand_made_sequences(self):
        up = [
            (10, 7), (11, 8), (12, 9), (10, 8), (9, 6), (11, 7),
            (14, 9), (12, 8.5), (12, 8), (13, 9), (15, 10),
        ]
        up_candles = [
            bar(i * 60, 9, high, low, 9)
            for i, (high, low) in enumerate(up)
        ]
        down_candles = [
            bar(item.time, 20 - item.open, 20 - item.low, 20 - item.high, 20 - item.close)
            for item in up_candles
        ]
        self.assertEqual(classify_regime(up_candles).name, "UP")
        self.assertEqual(classify_regime(down_candles).name, "DOWN")
        range_values = [(10, 9) for _ in range(20)]
        range_values[4], range_values[10] = (12, 9), (10, 7)
        range_candles = [
            bar(i * 60, 9.5, high, low, 9.5)
            for i, (high, low) in enumerate(range_values)
        ]
        self.assertEqual(classify_regime(range_candles).name, "RANGE")
        self.assertEqual(classify_regime(range_candles[:10]).name, "NONE")

    def test_editing_strategy_json_changes_classification_behavior(self):
        source = Path("strategy.json")
        payload = json.loads(source.read_text(encoding="utf-8"))
        payload["range_candles"] = STRATEGY_SETTINGS.range_candles + 1
        range_values = [(10, 9) for _ in range(STRATEGY_SETTINGS.range_candles)]
        range_values[4], range_values[10] = (12, 9), (10, 7)
        candles = [
            bar(i * 60, 9.5, high, low, 9.5)
            for i, (high, low) in enumerate(range_values)
        ]
        with TemporaryDirectory() as temp_dir:
            modified_path = Path(temp_dir) / "strategy.json"
            modified_path.write_text(json.dumps(payload), encoding="utf-8")
            changed_settings = load_strategy_settings(modified_path)
        self.assertEqual(classify_regime(candles).name, "RANGE")
        self.assertEqual(classify_regime(candles, changed_settings).name, "NONE")

    def test_each_swing_level_fires_only_once(self):
        regime = Regime("UP", swing_highs=(Swing(0, 0, 10),))
        strategy = PriceActionStrategy()
        first = self.evaluate(
            strategy,
            [bar(0, 9, 10, 8, 9), bar(60, 9.4, 9.9, 9.1, 9.8)],
            regime,
        )
        second = self.evaluate(
            strategy,
            [bar(0, 9, 10, 8, 9), bar(60, 9.4, 9.9, 9.1, 9.8), bar(120, 9.4, 9.9, 9.1, 9.8)],
            regime,
        )
        self.assertEqual(first.setup, "UP_PULLBACK_SELL")
        self.assertIsNone(second)

    def test_all_six_setup_levels_fire_once_until_the_level_changes(self):
        scenarios = [
            (
                Regime("UP", swing_highs=(Swing(0, 0, 10),)),
                [
                    bar(0, 9, 10, 8, 9),
                    bar(60, 9.4, 9.9, 9.1, 9.8),
                    bar(120, 9.4, 9.9, 9.1, 9.8),
                ],
                "UP_PULLBACK_SELL",
            ),
            (
                Regime("UP", swing_highs=(Swing(0, 0, 10),)),
                [
                    bar(0, 9, 10, 8, 9),
                    bar(60, 10, 10.2, 8, 9.4),
                    bar(120, 10.1, 10.4, 8.2, 10.3),
                    bar(180, 10.1, 10.5, 8.3, 10.4),
                ],
                "UP_CONTINUATION_BUY",
            ),
            (
                Regime("DOWN", swing_lows=(Swing(0, 0, 10),)),
                [
                    bar(0, 11, 12, 10, 11),
                    bar(60, 10.6, 10.8, 10.1, 10.3),
                    bar(120, 10.6, 10.8, 10.2, 10.3),
                ],
                "DOWN_PULLBACK_BUY",
            ),
            (
                Regime("DOWN", swing_lows=(Swing(0, 0, 10),)),
                [
                    bar(0, 11, 12, 10, 11),
                    bar(60, 10, 12, 9.8, 10.5),
                    bar(120, 10.3, 11.9, 9.8, 10),
                    bar(180, 10.2, 11.8, 9.7, 9.9),
                ],
                "DOWN_CONTINUATION_SELL",
            ),
        ]
        for regime, candles, expected in scenarios:
            with self.subTest(expected=expected):
                strategy = PriceActionStrategy()
                first = self.evaluate(strategy, candles[:-1], regime)
                second = self.evaluate(strategy, candles, regime)
                self.assertEqual(first.setup, expected)
                self.assertIsNone(second)

        range_regime = Regime(
            "RANGE",
            range_high=10,
            range_low=0,
            high_zone_low=7.5,
            low_zone_high=2.5,
            range_key=(0, 30),
        )
        for side, expected, candles in (
            (
                "high",
                "RANGE_HIGH_SELL",
                [
                    bar(0, 7, 8, 4, 7.5),
                    bar(60, 6, 7, 5, 6.5),
                    bar(120, 8, 8.5, 6, 8),
                ],
            ),
            (
                "low",
                "RANGE_LOW_BUY",
                [
                    bar(0, 3, 6, 2, 3),
                    bar(60, 4, 6, 3, 4.5),
                    bar(120, 4, 6, 3, 4.5),
                ],
            ),
        ):
            with self.subTest(expected=expected):
                strategy = PriceActionStrategy()
                touched = self.evaluate(strategy, candles[:1], range_regime)
                first = self.evaluate(strategy, candles[:2], range_regime)
                second = self.evaluate(strategy, candles, range_regime)
                self.assertIsNone(touched)
                self.assertEqual(first.setup, expected)
                self.assertIsNone(second)


if __name__ == "__main__":
    unittest.main()
