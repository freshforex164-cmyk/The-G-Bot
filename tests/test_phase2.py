from __future__ import annotations

import asyncio
import csv
import math
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import main as main_module
from config import POCKET_OPTION_TIME_OFFSET_SECONDS
from data.candles import Candle, CandleBuilder
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
    def test_sma_and_ema_known_values(self) -> None:
        candles = candles_from_closes([1.0, 2.0, 3.0, 6.0])
        sma = calculate_moving_average(candles, period=3, method="SMA")
        ema = calculate_moving_average(candles, period=3, method="EMA")
        self.assertEqual(sma[:2], [None, None])
        self.assertEqual(sma[2], 2.0)
        self.assertAlmostEqual(sma[3], 11.0 / 3.0)
        self.assertEqual(ema[:2], [None, None])
        self.assertEqual(ema[2], 2.0)
        self.assertEqual(ema[3], 4.0)

    def test_bollinger_bands_known_population_deviation(self) -> None:
        candles = candles_from_closes([1.0, 2.0, 3.0, 4.0])
        bands = calculate_bollinger_bands(candles, period=3, standard_deviations=2)
        expected_deviation = math.sqrt(2.0 / 3.0)
        self.assertEqual(bands.middle[:2], [None, None])
        self.assertAlmostEqual(bands.middle[2], 2.0)
        self.assertAlmostEqual(bands.upper[2], 2.0 + 2.0 * expected_deviation)
        self.assertAlmostEqual(bands.lower[2], 2.0 - 2.0 * expected_deviation)

    def test_slow_stochastic_known_values(self) -> None:
        closes = [5.0, 10.0, 0.0, 5.0, 10.0]
        candles = [
            Candle(
                time=index * 10,
                open=close,
                high=10.0,
                low=0.0,
                close=close,
            )
            for index, close in enumerate(closes)
        ]
        result = calculate_stochastic(
            candles,
            period=3,
            smooth_k=2,
            smooth_d=2,
        )
        self.assertEqual(result.slow_k, [None, None, None, 25.0, 75.0])
        self.assertEqual(result.slow_d, [None, None, None, None, 50.0])

    def test_keltner_channel_known_ema_and_wilder_atr(self) -> None:
        candles = [
            Candle(0, 9.0, 10.0, 8.0, 9.0),
            Candle(10, 11.0, 12.0, 9.0, 11.0),
            Candle(20, 12.0, 13.0, 10.0, 12.0),
        ]
        channel = calculate_keltner_channel(
            candles,
            ema_period=2,
            atr_period=2,
            multiplier=2.0,
        )
        self.assertEqual(channel.middle[0], None)
        self.assertAlmostEqual(channel.middle[1], 10.0)
        self.assertAlmostEqual(channel.atr[1], 2.5)
        self.assertAlmostEqual(channel.upper[1], 15.0)
        self.assertAlmostEqual(channel.lower[1], 5.0)
        self.assertAlmostEqual(channel.middle[2], 11.3333333333)
        self.assertAlmostEqual(channel.atr[2], 2.75)
        self.assertAlmostEqual(channel.upper[2], 16.8333333333)
        self.assertAlmostEqual(channel.lower[2], 5.8333333333)


class PullbackReversalStrategyTests(unittest.TestCase):
    def _trigger(
        self, direction: str, candles: list[Candle]
    ):
        strategy = PullbackReversalStrategy()
        pair = "EURUSD_otc"
        self.assertEqual(strategy.on_closed_candles(pair, candles), direction)
        middle = calculate_bollinger_bands(candles).middle[-1]
        assert middle is not None

        start_price = middle - 2.0 if direction == "SELL" else middle + 2.0
        first_forming = Candle(
            time=candles[-1].time + 10,
            open=start_price,
            high=start_price,
            low=start_price,
            close=start_price,
        )
        self.assertIsNone(strategy.on_tick(pair, start_price, first_forming))

        live_price = middle + 2.0 if direction == "SELL" else middle - 2.0
        forming = Candle(
            time=first_forming.time,
            open=start_price,
            high=max(start_price, live_price),
            low=min(start_price, live_price),
            close=live_price,
        )
        signal = strategy.on_tick(
            pair,
            live_price,
            forming,
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        return strategy, signal, forming

    def test_returns_no_setup_until_bollinger_history_is_ready(self) -> None:
        strategy = PullbackReversalStrategy()
        short_history = candles_from_closes([100.0] * 22)
        self.assertIsNone(strategy.on_closed_candles("EURUSD_otc", short_history))

    def test_synthetic_sell_setup_and_live_trigger(self) -> None:
        strategy, signal, forming = self._trigger(
            "SELL", candles_from_closes(sell_setup_closes())
        )
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.direction, "SELL")
        self.assertGreaterEqual(signal.price, signal.bollinger_middle)
        self.assertIsNone(
            strategy.on_tick("EURUSD_otc", signal.price + 1, forming)
        )

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

    def test_no_signal_when_stochastic_ma_or_bollinger_condition_fails(self) -> None:
        failing_tails = {
            "stochastic": [116.0, 97.0, 117.0, 101.0, 86.0, 103.0, 98.0],
            "moving average": [85.0, 110.0, 91.0, 110.0, 117.0, 108.0, 90.0],
            "bollinger": [97.0, 110.0, 118.0, 114.0, 115.0, 91.0, 113.0],
        }
        for failed_condition, tail in failing_tails.items():
            with self.subTest(failed_condition=failed_condition):
                strategy = PullbackReversalStrategy()
                result = strategy.on_closed_candles(
                    "EURUSD_otc",
                    candles_from_closes(sell_setup_closes(tail)),
                )
                self.assertIsNone(result)

    def test_no_signal_without_a_live_crossing(self) -> None:
        strategy = PullbackReversalStrategy()
        pair = "EURUSD_otc"
        candles = candles_from_closes(sell_setup_closes())
        self.assertEqual(strategy.on_closed_candles(pair, candles), "SELL")
        middle = calculate_bollinger_bands(candles).middle[-1]
        assert middle is not None
        start_price = middle + 0.5
        forming = Candle(
            candles[-1].time + 10,
            start_price,
            start_price,
            start_price,
            start_price,
        )
        self.assertIsNone(strategy.on_tick(pair, start_price, forming))
        next_tick = Candle(
            forming.time,
            start_price,
            middle + 1.0,
            start_price,
            middle + 1.0,
        )
        self.assertIsNone(strategy.on_tick(pair, middle + 1.0, next_tick))

    def test_cooldown_is_per_pair_and_waits_three_following_closed_candles(self) -> None:
        pair = "EURUSD_otc"
        candles = candles_from_closes(sell_setup_closes())
        strategy = PullbackReversalStrategy()
        self.assertEqual(strategy.on_closed_candles(pair, candles), "SELL")
        middle = calculate_bollinger_bands(candles).middle[-1]
        assert middle is not None
        start_price = middle - 2.0
        forming = Candle(candles[-1].time + 10, start_price, start_price, start_price, start_price)
        strategy.on_tick(pair, start_price, forming)
        live_price = middle + 2.0
        trigger_candle = Candle(
            forming.time,
            start_price,
            live_price,
            start_price,
            live_price,
        )
        self.assertIsNotNone(strategy.on_tick(pair, live_price, trigger_candle))
        self.assertEqual(strategy._states[pair].cooldown_remaining, 3)
        self.assertEqual(strategy.on_closed_candles("USDCHF_otc", candles), "SELL")

        updated = list(candles)
        for offset, expected_remaining in ((10, 3), (20, 2), (30, 1), (40, 0)):
            candle_time = candles[-1].time + offset
            updated.append(Candle(candle_time, 100.0, 101.0, 99.0, 100.0))
            strategy.on_closed_candles(pair, updated)
            self.assertEqual(
                strategy._states[pair].cooldown_remaining,
                expected_remaining,
            )


class PaperTrackerTests(unittest.TestCase):
    def test_win_loss_and_tie_at_expiry(self) -> None:
        tracker = PaperTracker(
            expiry_seconds=30,
            stake=1.0,
            summary_every=10,
            output=lambda _message: None,
        )
        timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
        sell_win = tracker.record_signal(
            "EURUSD_otc", "SELL", 1.2, timestamp, 85.0, monotonic_now=100.0
        )
        buy_loss = tracker.record_signal(
            "EURUSD_otc", "BUY", 1.2, timestamp, 85.0, monotonic_now=200.0
        )
        sell_tie = tracker.record_signal(
            "USDCHF_otc", "SELL", 1.0, timestamp, 80.0, monotonic_now=300.0
        )

        self.assertEqual(tracker.update_price("EURUSD_otc", 1.1, monotonic_now=129.9), [])
        self.assertEqual(
            tracker.update_price("EURUSD_otc", 1.1, monotonic_now=130.0),
            [sell_win],
        )
        self.assertEqual(
            tracker.update_price("EURUSD_otc", 1.1, monotonic_now=230.0),
            [buy_loss],
        )
        self.assertEqual(
            tracker.update_price("USDCHF_otc", 1.0, monotonic_now=330.0),
            [sell_tie],
        )

        self.assertEqual(sell_win.result, "WIN")
        self.assertAlmostEqual(sell_win.profit, 0.85)
        self.assertEqual(buy_loss.result, "LOSS")
        self.assertEqual(buy_loss.profit, -1.0)
        self.assertEqual(sell_tie.result, "LOSS")
        self.assertEqual(sell_tie.profit, -1.0)
        self.assertEqual(tracker.total, 3)
        self.assertEqual(tracker.wins, 1)
        self.assertEqual(tracker.losses, 2)
        self.assertAlmostEqual(tracker.win_rate, 100.0 / 3.0)
        self.assertAlmostEqual(tracker.estimated_profit, -1.15)

    def test_summary_is_printed_at_configured_signal_count(self) -> None:
        output: list[str] = []
        tracker = PaperTracker(
            expiry_seconds=30,
            summary_every=2,
            output=output.append,
        )
        signal_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        tracker.record_signal("A", "BUY", 1.0, signal_time, 80.0, monotonic_now=0)
        tracker.record_signal("A", "BUY", 1.0, signal_time, 80.0, monotonic_now=1)
        self.assertEqual(len(output), 1)
        self.assertIn("signals=2", output[0])
        self.assertIn("pending=2", output[0])


class SignalOutputTests(unittest.TestCase):
    def test_csv_appends_signal_indicators(self) -> None:
        from strategy.pullback_reversal import Signal

        signal = Signal(
            pair="EURUSD_otc",
            direction="SELL",
            price=1.2345,
            time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            stochastic_k=71.0,
            stochastic_d=74.0,
            ma_slope=-0.001,
            bollinger_middle=1.23,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "signals.csv"
            main_module._append_signal_csv(signal, 85.0, path)
            main_module._append_signal_csv(signal, 85.0, path)
            with path.open(newline="", encoding="utf-8") as file:
                rows = list(csv.DictReader(file))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["pair"], "EURUSD_otc")
        self.assertEqual(rows[0]["direction"], "SELL")
        self.assertEqual(rows[0]["stochastic_k"], "71.0")
        self.assertEqual(rows[0]["stochastic_d"], "74.0")
        self.assertEqual(rows[0]["ma_slope"], "-0.001")
        self.assertEqual(rows[0]["bollinger_middle"], "1.23")

    def test_cli_selects_signals_run_and_duration(self) -> None:
        with (
            patch.object(main_module, "MODE", "demo"),
            patch.object(main_module, "run_engine", new_callable=AsyncMock) as runner,
        ):
            exit_code = main_module.main(["--run", "signals", "--minutes", "7"])
        self.assertEqual(exit_code, 0)
        runner.assert_awaited_once_with(
            "demo",
            confirm_real=False,
            run_mode="signals",
            minutes=7,
        )

    def test_cli_defaults_to_watch(self) -> None:
        with (
            patch.object(main_module, "MODE", "demo"),
            patch.object(main_module, "run_engine", new_callable=AsyncMock) as runner,
        ):
            exit_code = main_module.main([])
        self.assertEqual(exit_code, 0)
        runner.assert_awaited_once_with(
            "demo",
            confirm_real=False,
            run_mode="watch",
            minutes=main_module.SIGNAL_RUN_DEFAULT_MINUTES,
        )


class SignalStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_mock_tick_stream_emits_and_records_signal_without_orders(self) -> None:
        class TickOnlyBroker:
            def __init__(self, ticks: list[dict[str, float]]) -> None:
                self.ticks = ticks

            async def subscribe_ticks(self, _pair: str):
                for tick in self.ticks:
                    yield tick

        pair = "EURUSD_otc"
        candles = candles_from_closes(sell_setup_closes())
        builder = CandleBuilder(period_seconds=10, max_candles=300)
        builder.closed.extend(candles)
        builder._latest_closed_time = candles[-1].time
        strategy = PullbackReversalStrategy()
        self.assertEqual(strategy.on_closed_candles(pair, tuple(builder.closed)), "SELL")
        middle = calculate_bollinger_bands(candles).middle[-1]
        assert middle is not None
        start_price = middle - 2.0
        trigger_price = middle + 2.0
        broker = TickOnlyBroker(
            [
                {
                    "timestamp": 301 + POCKET_OPTION_TIME_OFFSET_SECONDS,
                    "price": start_price,
                },
                {
                    "timestamp": 302 + POCKET_OPTION_TIME_OFFSET_SECONDS,
                    "price": trigger_price,
                },
            ]
        )
        tracker = PaperTracker(output=lambda _message: None)
        csv_writer = Mock()
        output = StringIO()
        with (
            patch.object(main_module, "_append_signal_csv", csv_writer),
            redirect_stdout(output),
        ):
            await main_module._stream_signal_pair(
                broker,
                pair,
                85.0,
                builder,
                strategy,
                tracker,
                asyncio.get_running_loop().time() + 10,
            )

        self.assertEqual(tracker.total, 1)
        self.assertEqual(tracker.trades[0].direction, "SELL")
        csv_writer.assert_called_once()
        self.assertIn("[SIGNAL]", output.getvalue())
        self.assertIn("EURUSD_otc SELL", output.getvalue())


if __name__ == "__main__":
    unittest.main()
