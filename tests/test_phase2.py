from __future__ import annotations

import math
import unittest
from datetime import datetime, timezone

from data.candles import Candle
from indicators.bollinger_bands import calculate_bollinger_bands
from indicators.keltner_channel import calculate_keltner_channel
from indicators.moving_average import calculate_moving_average
from indicators.stochastic import calculate_stochastic
from paper.tracker import PaperTracker
from strategy.pullback_reversal import PullbackReversalStrategy


def candles_from_closes(closes: list[float]) -> list[Candle]:
    return [
        Candle(
            time=index * 10,
            open=close,
            high=close + 1,
            low=close - 1,
            close=close,
        )
        for index, close in enumerate(closes)
    ]


def sell_setup_closes(tail: list[float] | None = None) -> list[float]:
    return (
        [100.0] * 9
        + [150.0]
        + [100.0] * 13
        + (tail or [108.0, 110.0, 112.0, 114.0, 111.0, 100.0, 88.0])
    )


class IndicatorTests(unittest.TestCase):
    def test_simple_and_exponential_average_values(self) -> None:
        candles = candles_from_closes([1.0, 2.0, 3.0, 6.0])
        sma = calculate_moving_average(candles, period=3, method="SMA")
        ema = calculate_moving_average(candles, period=3, method="EMA")
        self.assertEqual(sma[:2], [None, None])
        self.assertEqual(sma[2], 2.0)
        self.assertAlmostEqual(sma[3], 11.0 / 3.0)
        self.assertEqual(ema[:2], [None, None])
        self.assertEqual(ema[2], 2.0)
        self.assertEqual(ema[3], 4.0)

    def test_bollinger_band_values(self) -> None:
        bands = calculate_bollinger_bands(
            candles_from_closes([1.0, 2.0, 3.0, 4.0]),
            period=3,
            standard_deviations=2,
        )
        expected_deviation = math.sqrt(2.0 / 3.0)
        self.assertEqual(bands.middle[:2], [None, None])
        self.assertAlmostEqual(bands.middle[2], 2.0)
        self.assertAlmostEqual(bands.upper[2], 2.0 + 2.0 * expected_deviation)
        self.assertAlmostEqual(bands.lower[2], 2.0 - 2.0 * expected_deviation)

    def test_stochastic_values(self) -> None:
        candles = [
            Candle(index * 10, close, 10.0, 0.0, close)
            for index, close in enumerate([5.0, 10.0, 0.0, 5.0, 10.0])
        ]
        result = calculate_stochastic(candles, period=3, smooth_k=2, smooth_d=2)
        self.assertEqual(result.slow_k, [None, None, None, 25.0, 75.0])
        self.assertEqual(result.slow_d, [None, None, None, None, 50.0])

    def test_keltner_values(self) -> None:
        candles = [
            Candle(0, 9.0, 10.0, 8.0, 9.0),
            Candle(10, 11.0, 12.0, 9.0, 11.0),
            Candle(20, 12.0, 13.0, 10.0, 12.0),
        ]
        channel = calculate_keltner_channel(
            candles, ema_period=2, atr_period=2, multiplier=2.0
        )
        self.assertIsNone(channel.middle[0])
        self.assertAlmostEqual(channel.middle[1], 10.0)
        self.assertAlmostEqual(channel.atr[1], 2.5)
        self.assertAlmostEqual(channel.upper[1], 15.0)
        self.assertAlmostEqual(channel.lower[1], 5.0)
        self.assertAlmostEqual(channel.middle[2], 11.3333333333)


class PullbackReversalStrategyTests(unittest.TestCase):
    def _trigger(self, direction: str, candles: list[Candle]):
        strategy = PullbackReversalStrategy()
        pair = "EURUSD_otc"
        self.assertEqual(strategy.on_closed_candles(pair, candles), direction)
        middle = calculate_bollinger_bands(candles).middle[-1]
        assert middle is not None
        start = middle - 2.0 if direction == "SELL" else middle + 2.0
        first = Candle(candles[-1].time + 10, start, start, start, start)
        self.assertIsNone(strategy.on_tick(pair, start, first))
        live_price = middle + 2.0 if direction == "SELL" else middle - 2.0
        forming = Candle(
            first.time,
            start,
            max(start, live_price),
            min(start, live_price),
            live_price,
        )
        signal = strategy.on_tick(
            pair,
            live_price,
            forming,
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        return strategy, signal, forming

    def test_setup_waits_for_indicator_history(self) -> None:
        strategy = PullbackReversalStrategy()
        self.assertIsNone(
            strategy.on_closed_candles("EURUSD_otc", candles_from_closes([100.0] * 22))
        )

    def test_synthetic_sell_setup_and_live_trigger(self) -> None:
        strategy, signal, forming = self._trigger(
            "SELL", candles_from_closes(sell_setup_closes())
        )
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.direction, "SELL")
        self.assertGreaterEqual(signal.price, signal.bollinger_middle)
        self.assertIsNone(strategy.on_tick("EURUSD_otc", signal.price + 1, forming))

    def test_mirrored_buy_setup_and_live_trigger(self) -> None:
        buy_closes = (
            [100.0] * 9
            + [50.0]
            + [100.0] * 13
            + [92.0, 90.0, 88.0, 86.0, 89.0, 100.0, 112.0]
        )
        _strategy, signal, _forming = self._trigger(
            "BUY", candles_from_closes(buy_closes)
        )
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.direction, "BUY")

    def test_each_sell_setup_condition_can_fail_independently(self) -> None:
        failures = {
            "stochastic": [116.0, 97.0, 117.0, 101.0, 86.0, 103.0, 98.0],
            "moving average": [85.0, 110.0, 91.0, 110.0, 117.0, 108.0, 90.0],
            "Bollinger bands": [97.0, 110.0, 118.0, 114.0, 115.0, 91.0, 113.0],
        }
        for condition, tail in failures.items():
            with self.subTest(condition=condition):
                strategy = PullbackReversalStrategy()
                self.assertIsNone(
                    strategy.on_closed_candles(
                        "EURUSD_otc", candles_from_closes(sell_setup_closes(tail))
                    )
                )

    def test_setup_requires_live_middle_band_crossing(self) -> None:
        strategy = PullbackReversalStrategy()
        pair = "EURUSD_otc"
        candles = candles_from_closes(sell_setup_closes())
        self.assertEqual(strategy.on_closed_candles(pair, candles), "SELL")
        middle = calculate_bollinger_bands(candles).middle[-1]
        assert middle is not None
        start = middle + 0.5
        forming = Candle(candles[-1].time + 10, start, start, start, start)
        self.assertIsNone(strategy.on_tick(pair, start, forming))
        no_cross = Candle(forming.time, start, middle + 1, start, middle + 1)
        self.assertIsNone(strategy.on_tick(pair, middle + 1, no_cross))

    def test_cooldown_is_independent_per_pair(self) -> None:
        pair = "EURUSD_otc"
        candles = candles_from_closes(sell_setup_closes())
        strategy = PullbackReversalStrategy()
        self.assertEqual(strategy.on_closed_candles(pair, candles), "SELL")
        middle = calculate_bollinger_bands(candles).middle[-1]
        assert middle is not None
        start = middle - 2.0
        forming = Candle(candles[-1].time + 10, start, start, start, start)
        strategy.on_tick(pair, start, forming)
        live = middle + 2.0
        trigger = Candle(forming.time, start, live, start, live)
        self.assertIsNotNone(strategy.on_tick(pair, live, trigger))
        self.assertEqual(strategy._states[pair].cooldown_remaining, 3)
        self.assertEqual(
            strategy.on_closed_candles("USDCHF_otc", candles), "SELL"
        )


class PaperTrackerTests(unittest.TestCase):
    def test_price_outcomes_and_ties_are_losses(self) -> None:
        tracker = PaperTracker(expiry_seconds=30, summary_every=10, output=lambda _: None)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        sell_win = tracker.record_signal(
            "EURUSD_otc", "SELL", 1.2, now, monotonic_now=100
        )
        buy_loss = tracker.record_signal(
            "EURUSD_otc", "BUY", 1.2, now, monotonic_now=200
        )
        tie = tracker.record_signal("USDCHF_otc", "SELL", 1.0, now, monotonic_now=300)
        self.assertEqual(
            tracker.update_price("EURUSD_otc", 1.1, monotonic_now=130), [sell_win]
        )
        self.assertEqual(
            tracker.update_price("EURUSD_otc", 1.1, monotonic_now=230), [buy_loss]
        )
        self.assertEqual(
            tracker.update_price("USDCHF_otc", 1.0, monotonic_now=330), [tie]
        )
        self.assertEqual([sell_win.result, buy_loss.result, tie.result],
                         ["WIN", "LOSS", "LOSS"])
        self.assertEqual(tracker.total, 3)
        self.assertEqual((tracker.wins, tracker.losses), (1, 2))
        self.assertAlmostEqual(tracker.win_rate, 100.0 / 3.0)
        self.assertNotIn("$", tracker.summary())

    def test_tracker_summary_at_configured_signal_count(self) -> None:
        output: list[str] = []
        tracker = PaperTracker(summary_every=2, output=output.append)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        tracker.record_signal("A", "BUY", 1.0, now, monotonic_now=0)
        tracker.record_signal("A", "BUY", 1.0, now, monotonic_now=1)
        self.assertEqual(len(output), 1)
        self.assertIn("signals=2", output[0])
        self.assertIn("pending=2", output[0])


if __name__ == "__main__":
    unittest.main()
