from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta, timezone

import config
from data.candles import Candle
from indicators.bollinger_bands import calculate_bollinger_bands
from indicators.keltner_channel import calculate_keltner_channel
from indicators.moving_average import calculate_moving_average
from indicators.stochastic import calculate_stochastic
from paper.tracker import PaperTracker
from strategy.pullback_reversal import Signal


def candles_from_closes(closes: list[float]) -> list[Candle]:
    return [
        Candle(
            time=index * 15,
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
    def test_requested_default_indicator_settings(self) -> None:
        self.assertEqual((config.CANDLE_SECONDS, config.CANDLE_INTERVAL_SECONDS), (15, 15))
        self.assertEqual(
            (
                config.STOCHASTIC_PERIOD,
                config.STOCHASTIC_SMOOTH_K,
                config.STOCHASTIC_SMOOTH_D,
                config.STOCHASTIC_OVERBOUGHT,
                config.STOCHASTIC_OVERSOLD,
            ),
            (14, 3, 3, 80.0, 20.0),
        )
        self.assertEqual(
            (config.BOLLINGER_PERIOD, config.BOLLINGER_STANDARD_DEVIATIONS),
            (5, 1.0),
        )
        self.assertEqual(
            (config.MOVING_AVERAGE_PERIOD, config.MOVING_AVERAGE_METHOD),
            (10, "SMA"),
        )
        self.assertEqual(config.ATR_PERIOD, 14)
        self.assertEqual(config.KELTNER_ATR_PERIOD, 14)
        self.assertFalse(config.USE_KELTNER_CHANNEL)

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


class PaperTrackerTests(unittest.TestCase):
    def test_human_delay_and_configured_expiry_scores_ties_as_losses(self) -> None:
        tracker = PaperTracker(output=lambda _: None)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        signal = Signal(
            pair="EURUSD_otc",
            direction="SELL",
            price=1.2,
            time=now,
            stochastic_k=90,
            stochastic_d=85,
            ma_slope=-0.01,
            bollinger_middle=1.1,
            payout=92,
            strength=84,
            strength_components={"stochastic": 25},
            valid_until=now + timedelta(seconds=15),
        )
        record = tracker.record_signal(signal)

        self.assertEqual(
            tracker.update_price(
                "EURUSD_otc", 1.3, now + timedelta(seconds=4)
            ),
            [],
        )
        self.assertIsNone(record.entry_price)
        self.assertEqual(
            tracker.update_price(
                "EURUSD_otc", 1.2, now + timedelta(seconds=5)
            ),
            [],
        )
        self.assertEqual(record.entry_price, 1.2)
        self.assertEqual(
            [item.result for item in tracker.update_price(
                "EURUSD_otc", 1.1, now + timedelta(seconds=40)
            )],
            ["WIN"],
        )
        summary = tracker.summary()
        self.assertIn("expiry=35s count=1 wins=1 win_rate=100.0%", summary)
        self.assertIn(
            "strength=80-100 expiry=35s count=1 wins=1 win_rate=100.0%",
            summary,
        )


if __name__ == "__main__":
    unittest.main()
