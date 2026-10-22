from __future__ import annotations

import unittest
from unittest.mock import patch

import config
from data.candles import Candle
from indicators.bollinger_bands import BollingerBandSeries
from indicators.stochastic import StochasticSeries
from strategy.close_engine import (
    CloseOnlyStrategy,
    arm_measurements,
    explain_close_state,
    score_close_strength,
    trigger_measurements,
)


def candle(
    time: int,
    open_: float = 100.0,
    high: float = 100.2,
    low: float = 99.8,
    close: float = 100.0,
) -> Candle:
    return Candle(time, open_, high, low, close)


def k_values(length: int, tail: tuple[float, float, float]) -> list[float]:
    values = [50.0] * length
    values[-3:] = tail
    return values


class ArmMeasurementTests(unittest.TestCase):
    def test_sell_arm_passes_and_each_s1_check_can_fail(self) -> None:
        self.assertEqual(arm_measurements("SELL", 78, 100, 80)[:3], (True, 100, 22))
        cases = [
            ((60, 70, 79), "peak below overbought"),
            ((90, 100, 80), "drop outside range"),
            ((80, 70, 100), "Stochastic did not turn down"),
        ]
        for values, failure in cases:
            with self.subTest(values=values):
                passed, *_rest, reason = arm_measurements("SELL", *values)
                self.assertFalse(passed)
                self.assertEqual(reason, failure)

    def test_buy_arm_passes_and_each_s1_check_can_fail(self) -> None:
        self.assertEqual(arm_measurements("BUY", 22, 0, 20)[:3], (True, 0, 22))
        cases = [
            ((40, 30, 21), "trough above oversold"),
            ((10, 0, 20), "rise outside range"),
            ((22, 30, 0), "Stochastic did not turn up"),
        ]
        for values, failure in cases:
            with self.subTest(values=values):
                passed, *_rest, reason = arm_measurements("BUY", *values)
                self.assertFalse(passed)
                self.assertEqual(reason, failure)


class TriggerMeasurementTests(unittest.TestCase):
    def test_sell_middle_and_lower_rejections_pass(self) -> None:
        middle = trigger_measurements(
            "SELL",
            candle(1, 99.8, 100.5, 99.7, 99.9),
            atr=1,
            middle=100,
            upper=101,
            lower_band=99,
        )
        self.assertTrue(middle.middle)

        lower = trigger_measurements(
            "SELL",
            candle(2, 98.8, 99.2, 98.6, 98.9),
            atr=1,
            middle=100,
            upper=101,
            lower_band=99,
        )
        self.assertTrue(lower.lower)

    def test_buy_middle_and_upper_rejections_pass(self) -> None:
        middle = trigger_measurements(
            "BUY",
            candle(1, 100.2, 100.3, 99.5, 100.1),
            atr=1,
            middle=100,
            upper=101,
            lower_band=99,
        )
        self.assertTrue(middle.middle)

        upper = trigger_measurements(
            "BUY",
            candle(2, 100.6, 101.2, 100.1, 100.4),
            atr=1,
            middle=100,
            upper=101,
            lower_band=99,
        )
        self.assertTrue(upper.lower)

    def test_rejection_checks_fail_for_wrong_direction_body_wick_or_overshoot(self) -> None:
        for test_candle in (
            candle(1, 100.1, 100.5, 99.7, 99.9),  # bearish SELL candle
            candle(2, 99.0, 101.0, 98.0, 100.0),  # oversized body
            candle(3, 99.8, 101.0, 99.7, 99.9),  # overshoot above limit
            candle(4, 99.8, 100.1, 99.9, 100.0),  # no required wick/touch
        ):
            with self.subTest(candle=test_candle):
                check = trigger_measurements(
                    "SELL",
                    test_candle,
                    atr=1,
                    middle=100,
                    upper=101,
                    lower_band=99,
                )
                self.assertFalse(check.middle)


class CloseStateMachineTests(unittest.TestCase):
    def _evaluate(
        self,
        strategy: CloseOnlyStrategy,
        candles: list[Candle],
        tail: tuple[float, float, float],
    ) -> str | None:
        length = len(candles)
        stochastic = StochasticSeries(
            slow_k=k_values(length, tail),
            slow_d=[50.0] * length,
        )
        bands = BollingerBandSeries(
            middle=[100.0] * length,
            upper=[101.0] * length,
            lower=[99.0] * length,
        )
        with (
            patch("strategy.close_engine.calculate_stochastic", return_value=stochastic),
            patch("strategy.close_engine.calculate_bollinger_bands", return_value=bands),
            patch("strategy.close_engine.calculate_moving_average", return_value=[100.0] * length),
            patch("strategy.close_engine.calculate_atr", return_value=[1.0] * length),
        ):
            return strategy.on_closed_candles("EURGBP_otc", candles, payout=92)

    def test_setup_fires_once_only_after_a_closed_rejection(self) -> None:
        candles = [candle(index * 15) for index in range(config.MIN_STRATEGY_CANDLES)]
        strategy = CloseOnlyStrategy()
        self.assertIsNone(self._evaluate(strategy, candles, (80, 100, 78)))
        self.assertEqual(strategy.state("EURGBP_otc", "SELL").status, "ARMED")
        self.assertIsNone(strategy.on_tick("EURGBP_otc", 100.0, candle(999)))

        trigger = candle(candles[-1].time + 15, 99.8, 100.5, 99.7, 99.9)
        candles.append(trigger)
        self.assertEqual(self._evaluate(strategy, candles, (80, 100, 90)), "SELL")
        signal = strategy.take_closed_signal("EURGBP_otc")
        self.assertIsNotNone(signal)
        self.assertEqual(signal.price, trigger.close)
        self.assertEqual(signal.recommended_expiry_seconds, config.EXPIRATION_SECONDS)
        self.assertEqual(strategy.state("EURGBP_otc", "SELL").status, "FIRE")

    def test_upper_band_close_cancels_armed_sell(self) -> None:
        candles = [candle(index * 15) for index in range(config.MIN_STRATEGY_CANDLES)]
        strategy = CloseOnlyStrategy()
        self._evaluate(strategy, candles, (80, 100, 78))
        candles.append(candle(candles[-1].time + 15, 100.5, 101.5, 100.2, 101.2))
        self.assertIsNone(self._evaluate(strategy, candles, (80, 100, 90)))
        self.assertEqual(strategy.state("EURGBP_otc", "SELL").status, "CANCEL")
        self.assertEqual(strategy.state("EURGBP_otc", "SELL").reason, "close above upper band")

    def test_strength_uses_requested_weighted_formula_without_payout(self) -> None:
        kwargs = dict(
            direction="SELL",
            drop=config.STRENGTH_TARGET_DROP,
            trigger_type="MIDDLE",
            wick_ratio=config.STRENGTH_WICK_FULL_SCORE_RATIO,
            overshoot_atr=0,
            moving_average=[100.0] * 10,
            index=9,
            bars_after=1,
            atr=1,
        )
        self.assertEqual(score_close_strength(**kwargs)[0], 100)
        kwargs["trigger_type"] = "LOWER"
        self.assertEqual(score_close_strength(**kwargs)[0], 94)

    def test_explain_reports_numeric_rules_state_and_first_failure(self) -> None:
        candles = [candle(index * 15) for index in range(config.MIN_STRATEGY_CANDLES)]
        length = len(candles)
        stochastic = StochasticSeries(
            slow_k=k_values(length, (80, 100, 78)),
            slow_d=[50.0] * length,
        )
        bands = BollingerBandSeries(
            middle=[100.0] * length,
            upper=[101.0] * length,
            lower=[99.0] * length,
        )
        with (
            patch("strategy.close_engine.calculate_stochastic", return_value=stochastic),
            patch("strategy.close_engine.calculate_bollinger_bands", return_value=bands),
            patch("strategy.close_engine.calculate_moving_average", return_value=[100.0] * length),
            patch("strategy.close_engine.calculate_atr", return_value=[1.0] * length),
        ):
            lines = explain_close_state("EURGBP_otc", candles, CloseOnlyStrategy())
        text = "\n".join(lines)
        self.assertIn("ATR=1", text)
        self.assertIn("state=IDLE", text)
        self.assertIn("first failure:", text)
        self.assertIn("MIDDLE", text)
        self.assertIn("UPPER", text)


if __name__ == "__main__":
    unittest.main()
