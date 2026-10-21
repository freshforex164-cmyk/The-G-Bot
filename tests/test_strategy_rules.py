from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import config
from data.candles import Candle
from data.candles import CandleBuilder
from main import _explain_for_pair
from strategy.pullback_reversal import PullbackReversalStrategy
from strategy.rules import (
    ConditionResult,
    DirectionEvaluation,
    IndicatorSet,
    evaluate_direction,
    trigger_a,
    trigger_b,
)


def fixture(direction: str, failure: str | None = None):
    candles = [
        Candle(index * 10, 100.0, 100.2, 99.8, 100.0)
        for index in range(80)
    ]
    middle = [100.0] * 80
    upper = [101.0] * 80
    lower = [99.0] * 80
    moving_average = [100.0] * 80
    slow_k = [50.0] * 80
    slow_d = [50.0] * 80
    widths = [0.02] * 80

    if direction == "SELL":
        candles[-1] = Candle(790, 99.4, 100.2, 99.0, 99.8)
        upper[-1] = 100.8
        moving_average[-7] = 99.0
        slow_k[-5:] = [90.0, 85.0, 82.0, 78.0, 70.0]
        slow_d[-1] = 75.0
        if failure == "S1":
            slow_k[-5:] = [60.0, 62.0, 64.0, 66.0, 68.0]
            slow_d[-1] = 65.0
        elif failure == "S2":
            moving_average[-7] = 100.0
        elif failure == "S3":
            upper[-1] = upper[-2]
        elif failure == "S4":
            widths[-1] = 0.08
    else:
        candles[-1] = Candle(790, 100.6, 101.0, 99.8, 100.8)
        lower[-1] = 99.2
        moving_average[-7] = 101.0
        slow_k[-5:] = [10.0, 12.0, 15.0, 18.0, 22.0]
        slow_d[-1] = 18.0
        if failure == "S1":
            slow_k[-5:] = [40.0, 42.0, 44.0, 46.0, 48.0]
            slow_d[-1] = 50.0
        elif failure == "S2":
            moving_average[-7] = 100.0
        elif failure == "S3":
            lower[-1] = lower[-2]
        elif failure == "S4":
            widths[-1] = 0.08

    prepared = IndicatorSet(
        bands=SimpleNamespace(middle=middle, upper=upper, lower=lower),
        moving_average=moving_average,
        stochastic=SimpleNamespace(slow_k=slow_k, slow_d=slow_d),
        widths=widths,
    )
    return candles, prepared


def report(direction: str, failure: str | None = None) -> DirectionEvaluation:
    candles, indicators = fixture(direction, failure)
    return evaluate_direction(candles, direction, indicators=indicators)


class StrategyRuleTests(unittest.TestCase):
    def test_all_four_conditions_pass_for_both_directions(self) -> None:
        for direction in ("SELL", "BUY"):
            with self.subTest(direction=direction):
                evaluated = report(direction)
                self.assertTrue(evaluated.setup_passes)
                self.assertEqual(
                    [condition.passed for condition in evaluated.conditions],
                    [True, True, True, True],
                )

    def test_each_condition_can_fail_independently(self) -> None:
        for direction in ("SELL", "BUY"):
            for key in ("S1", "S2", "S3", "S4"):
                with self.subTest(direction=direction, condition=key):
                    evaluated = report(direction, failure=key)
                    results = {item.key: item.passed for item in evaluated.conditions}
                    self.assertFalse(results[key])
                    self.assertTrue(
                        all(value for name, value in results.items() if name != key)
                    )
                    self.assertEqual(evaluated.first_failure.key, key)

    def test_disabled_condition_does_not_block_setup(self) -> None:
        candles, indicators = fixture("SELL", "S2")
        evaluated = evaluate_direction(
            candles,
            "SELL",
            indicators=indicators,
            enabled={"S1": True, "S2": False, "S3": True, "S4": True},
        )
        self.assertTrue(evaluated.setup_passes)
        self.assertIn("⏭", evaluated.conditions[1].line())

    def test_trigger_a_touch_and_zone_are_mirrored(self) -> None:
        sell = report("SELL")
        sell_candle = Candle(800, 99.9, 100.3, 99.8, 100.2)
        self.assertTrue(trigger_a(sell, 100.2, sell_candle)[0])
        self.assertFalse(trigger_a(sell, 100.2, Candle(800, 100.3, 100.4, 100.1, 100.2))[0])

        buy = report("BUY")
        buy_candle = Candle(800, 100.1, 100.2, 99.7, 99.8)
        self.assertTrue(trigger_a(buy, 99.8, buy_candle)[0])
        self.assertFalse(trigger_a(buy, 99.8, Candle(800, 99.7, 99.9, 99.6, 99.8))[0])

    def test_trigger_b_closed_rejection_is_mirrored(self) -> None:
        sell = report("SELL")
        self.assertTrue(trigger_b(sell, Candle(800, 100.1, 100.4, 99.7, 99.8))[0])
        self.assertFalse(trigger_b(sell, Candle(800, 99.7, 99.9, 99.5, 99.8))[0])
        buy = report("BUY")
        self.assertTrue(trigger_b(buy, Candle(800, 99.9, 100.3, 99.6, 100.2))[0])
        self.assertFalse(trigger_b(buy, Candle(800, 100.2, 100.4, 100.1, 100.3))[0])

    def test_closed_trigger_b_emits_a_signal_at_rejection_close(self) -> None:
        candles, _ = fixture("SELL")
        evaluation = report("SELL")
        strategy = PullbackReversalStrategy()
        with (
            patch.object(config, "TRIGGER_VARIANT", "B"),
            patch("strategy.pullback_reversal.evaluate_direction", return_value=evaluation),
        ):
            direction = strategy.on_closed_candles("PAIR", candles, payout=92)
            self.assertEqual(direction, "SELL")
            signal = strategy.take_closed_signal("PAIR")
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.price, candles[-1].close)
        self.assertEqual(signal.time, datetime.fromtimestamp(790, timezone.utc))

    def test_explain_output_lists_conditions_trigger_and_first_failure(self) -> None:
        builder = CandleBuilder()
        builder.closed.extend(fixture("SELL")[0])
        evaluated = report("SELL", "S2")
        with (
            patch.object(config, "TRIGGER_VARIANT", "A"),
            patch("main.evaluate_direction", return_value=evaluated),
            patch("main.trigger_a", return_value=(False, "price=100.2")),
        ):
            text = "\n".join(_explain_for_pair("PAIR", builder, 100.2))
        self.assertIn("S1 Stoch", text)
        self.assertIn("TRIGGER A price=100.2", text)
        self.assertIn("RESULT FAIL S2", text)


if __name__ == "__main__":
    unittest.main()
