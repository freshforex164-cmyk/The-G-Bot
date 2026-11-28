from __future__ import annotations

import unittest
from unittest.mock import patch

from data.candles import Candle
from dataclasses import replace

from strategy.price_action import PriceActionStrategy, Regime, Swing
from strategy.settings import STRATEGY_SETTINGS

# State-machine fixtures are short; the regime classifier is patched, so the
# live 30-candle warm-up does not apply to these unit tests.
SHORT_HISTORY_SETTINGS = replace(STRATEGY_SETTINGS, min_regime_candles=1)


def bar(
    time: int,
    open_: float,
    high: float,
    low: float,
    close: float,
) -> Candle:
    return Candle(time, open_, high, low, close)


def strategy_with_level(
    regime: str,
    *,
    time: int = 0,
    level: float = 10,
) -> PriceActionStrategy:
    strategy = PriceActionStrategy(SHORT_HISTORY_SETTINGS)
    strategy._regime_initialized = True
    strategy.regime_name = regime
    strategy.current_regime = Regime(regime)
    strategy._last_closed_time = time
    if regime == "UPTREND":
        strategy.H = Swing(0, time, level)
    elif regime == "DOWNTREND":
        strategy.L = Swing(0, time, level)
    return strategy


class TrendStateMachineTests(unittest.TestCase):
    def evaluate(
        self,
        strategy: PriceActionStrategy,
        candles: list[Candle],
        regime: Regime,
    ):
        with patch("strategy.price_action.classify_regime", return_value=regime):
            return strategy.on_closed_candles(
                "PAIR_otc", candles, asset_name="Test asset", payout=92
            )

    def test_uptrend_pullback_sell_follows_the_new_high(self) -> None:
        strategy = strategy_with_level("UPTREND")
        signal = self.evaluate(
            strategy,
            [bar(0, 9.5, 10, 9, 9.8), bar(60, 9.8, 9.9, 9.2, 9.4)],
            Regime("UPTREND"),
        )
        self.assertEqual((signal.setup, signal.direction), ("UP_PULLBACK_SELL", "SELL"))
        self.assertEqual(signal.expiry_seconds, 60)
        self.assertEqual(strategy.phase, "AWAIT_CONTINUATION")
        self.assertEqual(strategy.last_signal_type, "PULLBACK")

    def test_red_candle_that_breaks_the_high_can_trigger_the_pullback(self) -> None:
        strategy = strategy_with_level("UPTREND")
        signal = self.evaluate(
            strategy,
            [bar(0, 9.5, 10, 9, 9.8), bar(60, 10.1, 10.2, 9.8, 9.9)],
            Regime("UPTREND"),
        )
        self.assertEqual(signal.direction, "SELL")
        self.assertEqual(strategy.H.price, 10.2)
        self.assertEqual(strategy.H.time, 60)

    def test_continuation_waits_for_a_candle_above_the_tracked_pullback_low(self) -> None:
        strategy = strategy_with_level("UPTREND")
        candles = [bar(0, 9.5, 10, 9, 9.8)]
        pullback = bar(60, 9.8, 9.9, 9.0, 9.4)
        first = self.evaluate(strategy, candles + [pullback], Regime("UPTREND"))
        self.assertEqual(first.direction, "SELL")

        lower_low = bar(120, 9.4, 9.8, 8.8, 9.2)
        second = self.evaluate(
            strategy, candles + [pullback, lower_low], Regime("UPTREND")
        )
        self.assertIsNone(second)
        self.assertEqual(strategy.pullback_extreme, 8.8)

        above_tracked_low = bar(180, 9.3, 9.8, 8.9, 9.6)
        third = self.evaluate(
            strategy,
            candles + [pullback, lower_low, above_tracked_low],
            Regime("UPTREND"),
        )
        self.assertEqual((third.setup, third.direction), ("UP_CONTINUATION_BUY", "BUY"))
        self.assertEqual(strategy.last_signal_type, "CONTINUATION")
        self.assertEqual(strategy.phase, "AWAIT_PULLBACK")

    def test_new_high_pullback_then_candle_holding_the_low_is_buy_not_sell(self) -> None:
        """Regression: after the pullback SELL, a candle that does not take out
        the pullback low must give the continuation BUY, never another SELL."""
        strategy = strategy_with_level("UPTREND")
        candles = [bar(0, 9.5, 10, 9, 9.8)]
        new_high = bar(60, 9.8, 10.5, 9.7, 10.4)
        pullback = bar(120, 10.4, 10.45, 9.9, 10.0)
        holds_low = bar(180, 10.2, 10.3, 10.0, 10.1)
        outputs = []
        for next_bar in (new_high, pullback, holds_low):
            candles.append(next_bar)
            signal = self.evaluate(strategy, list(candles), Regime("UPTREND"))
            outputs.append(signal.direction if signal else None)
        self.assertEqual(outputs, [None, "SELL", "BUY"])
        self.assertEqual(strategy.pullback_extreme, 9.9)

    def test_trend_signals_alternate_pullback_continuation_pullback(self) -> None:
        strategy = strategy_with_level("UPTREND")
        candles = [bar(0, 9.5, 10, 9, 9.8)]
        outputs = []
        sequence = [
            bar(60, 9.8, 9.9, 9.0, 9.4),
            bar(120, 9.4, 9.8, 8.8, 9.2),
            bar(180, 9.3, 9.8, 8.9, 9.6),
            bar(240, 10.0, 10.2, 9.7, 10.1),
            bar(300, 10.0, 10.1, 9.8, 9.9),
        ]
        for next_bar in sequence:
            signal = self.evaluate(
                strategy, candles + [next_bar], Regime("UPTREND")
            )
            if signal:
                outputs.append(signal.direction)
            candles.append(next_bar)
        self.assertEqual(outputs, ["SELL", "BUY", "SELL"])

    def test_downtrend_rules_mirror_pullback_and_continuation(self) -> None:
        strategy = strategy_with_level("DOWNTREND")
        candles = [bar(0, 10.5, 11, 10, 10.2)]
        pullback = bar(60, 10.2, 11.0, 10.1, 10.8)
        first = self.evaluate(strategy, candles + [pullback], Regime("DOWNTREND"))
        self.assertEqual((first.setup, first.direction), ("DOWN_PULLBACK_BUY", "BUY"))

        higher_high = bar(120, 10.8, 11.2, 10.2, 10.9)
        self.assertIsNone(
            self.evaluate(
                strategy, candles + [pullback, higher_high], Regime("DOWNTREND")
            )
        )
        continuation = bar(180, 10.8, 11.1, 10.2, 10.4)
        signal = self.evaluate(
            strategy,
            candles + [pullback, higher_high, continuation],
            Regime("DOWNTREND"),
        )
        self.assertEqual(
            (signal.setup, signal.direction), ("DOWN_CONTINUATION_SELL", "SELL")
        )


class ConsolidationAndResetTests(unittest.TestCase):
    RANGE = Regime(
        "CONSOLIDATION",
        range_high=10,
        range_low=0,
        high_zone_low=7.5,
        low_zone_high=2.5,
        range_key=(0, 20),
        range_valid=True,
    )

    def setUp(self) -> None:
        # Consolidation signals are paused by default; these tests cover the state machine itself.
        enabled = patch("strategy.price_action.CONSOLIDATION_SIGNALS_ENABLED", True)
        enabled.start()
        self.addCleanup(enabled.stop)

    def evaluate(
        self,
        strategy: PriceActionStrategy,
        candles: list[Candle],
        regime: Regime,
    ):
        with patch("strategy.price_action.classify_regime", return_value=regime):
            return strategy.on_closed_candles(
                "PAIR_otc", candles, asset_name="Test asset"
            )

    def test_consolidation_upper_and_lower_rejections_signal_inward(self) -> None:
        upper = strategy_with_level("CONSOLIDATION")
        sell = self.evaluate(
            upper,
            [bar(0, 8, 9.5, 8, 9.0), bar(60, 9.4, 9.8, 8.5, 9.2)],
            self.RANGE,
        )
        self.assertIsNone(sell)

        lower = strategy_with_level("CONSOLIDATION")
        buy = self.evaluate(
            lower,
            [bar(0, 2, 3, 1, 2.2), bar(60, 1.2, 2, 0.5, 1.4)],
            self.RANGE,
        )
        self.assertIsNone(buy)

    def test_consolidation_structural_level_fires_only_once(self) -> None:
        strategy = strategy_with_level("CONSOLIDATION")
        candles = [bar(0, 8, 9.5, 8, 9.0)]
        first = bar(60, 9.4, 9.8, 8.5, 9.2)
        self.assertIsNone(self.evaluate(strategy, candles + [first], self.RANGE))
        second = bar(120, 9.3, 9.8, 8.4, 9.1)
        self.assertIsNone(
            self.evaluate(strategy, candles + [first, second], self.RANGE)
        )

    def test_regime_change_resets_trend_phase_and_active_levels(self) -> None:
        strategy = strategy_with_level("UPTREND")
        strategy.phase = "AWAIT_CONTINUATION"
        strategy.last_signal_type = "PULLBACK"
        strategy.pullback_extreme = 9
        strategy.pullback_extreme_time = 60
        strategy._fired_levels.add((strategy._regime_epoch, "existing-level"))
        old_epoch = strategy._regime_epoch

        self.assertIsNone(
            self.evaluate(
                strategy,
                [bar(0, 9.5, 10, 9, 9.8), bar(60, 9.7, 9.9, 9.4, 9.8)],
                Regime("CONSOLIDATION"),
            )
        )
        self.assertEqual(strategy.regime_name, "CONSOLIDATION")
        self.assertEqual(strategy.phase, "AWAIT_PULLBACK")
        self.assertIsNone(strategy.last_signal_type)
        self.assertIsNone(strategy.H)
        self.assertIsNone(strategy.pullback_extreme)
        self.assertGreater(strategy._regime_epoch, old_epoch)
        self.assertEqual(strategy._fired_levels, set())


class BootstrapAndDebugTests(unittest.TestCase):
    def test_bootstrap_replays_last_60_candles_without_returning_old_signals(self) -> None:
        candles = [bar(index * 60, 9.5, 10, 9, 9.5) for index in range(80)]

        def fake_classifier(history, _settings, *, previous_name=None):
            if len(history) < 30:
                return Regime("CONSOLIDATION")
            prior = history[-2]
            return Regime(
                "UPTREND",
                swing_highs=(Swing(len(history) - 2, prior.time, prior.high),),
                swing_lows=(Swing(len(history) - 2, prior.time, prior.low),),
            )

        strategy = PriceActionStrategy()
        with patch("strategy.price_action.classify_regime", side_effect=fake_classifier):
            strategy.bootstrap(candles)

        self.assertEqual(strategy.regime_name, "UPTREND")
        self.assertIsNotNone(strategy.H)
        self.assertEqual(strategy.last_signal_type, "CONTINUATION")
        self.assertEqual(strategy.phase, "AWAIT_PULLBACK")
        self.assertEqual(len(strategy._fired_levels), 2)
        self.assertEqual(strategy._last_closed_time, candles[-1].time)
        self.assertGreaterEqual(strategy.H.time, candles[-60].time)

    def test_debug_output_contains_strategy_state_only(self) -> None:
        strategy = strategy_with_level("UPTREND")
        state = strategy.debug_state("Example asset")
        rendered = strategy.format_debug_state("Example asset")
        self.assertEqual(state["regime"], "UPTREND")
        self.assertEqual(state["H"], "10@0")
        self.assertIn("ACTIVE LEVELS H=10@0", rendered)
        self.assertNotIn("token", rendered.lower())
        self.assertNotIn("ssid", rendered.lower())


if __name__ == "__main__":
    unittest.main()


class ConsolidationPausedTests(unittest.TestCase):
    def test_consolidation_rejection_sends_no_signal_by_default(self) -> None:
        import strategy.price_action as pa
        self.assertFalse(pa.CONSOLIDATION_SIGNALS_ENABLED)
        strategy = strategy_with_level("CONSOLIDATION")
        with patch("strategy.price_action.classify_regime", return_value=ConsolidationAndResetTests.RANGE):
            out = strategy.on_closed_candles(
                "PAIR_otc", [bar(0, 8, 9.5, 8, 9.0), bar(60, 9.4, 9.8, 8.5, 9.2)], asset_name="Test asset"
            )
        self.assertIsNone(out)

    def test_only_trend_setups_are_active(self) -> None:
        from strategy.price_action import TREND_SETUPS
        self.assertEqual(TREND_SETUPS, {"UP_CONTINUATION_BUY", "UP_PULLBACK_SELL",
                                        "DOWN_CONTINUATION_SELL", "DOWN_PULLBACK_BUY"})
