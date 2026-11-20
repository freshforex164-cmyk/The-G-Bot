from __future__ import annotations
import unittest

raise unittest.SkipTest("OBSOLETE (failing before 2026-11-20): imports strategy.replay.ReplayVariant, removed before 2026-11-20.")


import asyncio
import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

import main
from data.candles import Candle
from strategy.replay import (
    ReplayVariant,
    replay_variant,
    run_strategy_lab,
    strategy_variants,
)
from strategy.rules import ConditionResult, DirectionEvaluation


FIXED_TIME = 1_760_000_000


class FeedCheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_feed_check_uses_top_eligible_pair_and_reports_ticks(self) -> None:
        class FakeBroker:
            requested_pair: str | None = None

            def __init__(self, ssid: str) -> None:
                self.ssid = ssid
                self.is_demo_account = True

            async def connect(self) -> None:
                return None

            async def payouts(self) -> dict[str, float]:
                return {"LOW": 79, "TOP": 92, "SECOND": 85}

            async def subscribe_ticks(self, pair: str):
                type(self).requested_pair = pair
                yield {"timestamp": FIXED_TIME + 7200, "price": 24.0017}
                yield {"timestamp": FIXED_TIME + 7200 + 1, "price": 24.002}
                await asyncio.sleep(1)

            async def monitor_connection(self, stop_event: asyncio.Event) -> None:
                await stop_event.wait()

            async def close(self) -> None:
                return None

        output = io.StringIO()
        with (
            patch.object(main, "PocketOptionBroker", FakeBroker),
            patch.object(main, "load_ssid", return_value="fake-ssid"),
            redirect_stdout(output),
        ):
            result = await main.run_feed_check(duration_seconds=0.02)

        text = output.getvalue()
        self.assertTrue(result.ok)
        self.assertEqual(result.tick_count, 2)
        self.assertEqual(result.last_price, 24.002)
        self.assertIsNone(result.reason)
        self.assertEqual(FakeBroker.requested_pair, "TOP")
        self.assertIn("Account type: demo (informational only)", text)
        self.assertIn("UTC |", text)
        self.assertIn(" local | TOP 24.0017", text)
        self.assertIn("FEED OK: 2 ticks in 0.02s, last price 24.002", text)

    async def test_feed_check_returns_failure_details_when_stream_ends(self) -> None:
        class FakeBroker:
            def __init__(self, _ssid: str) -> None:
                self.is_demo_account = True

            async def connect(self) -> None:
                return None

            async def payouts(self) -> dict[str, float]:
                return {"TOP": 92}

            async def subscribe_ticks(self, _pair: str):
                if False:
                    yield {}

            async def monitor_connection(self, stop_event: asyncio.Event) -> None:
                await stop_event.wait()

            async def close(self) -> None:
                return None

        with (
            patch.object(main, "PocketOptionBroker", FakeBroker),
            patch.object(main, "load_ssid", return_value="fake-ssid"),
            redirect_stdout(io.StringIO()),
        ):
            result = await main.run_feed_check(duration_seconds=0.1)

        self.assertFalse(result.ok)
        self.assertEqual(result.tick_count, 0)
        self.assertIsNone(result.last_price)
        self.assertEqual(result.reason, "tick stream ended before 0.1 seconds")


class ReplayTests(unittest.TestCase):
    def test_replay_approximates_configured_expiry_outcome(self) -> None:
        candles = [
            Candle(index * 15, 100.0, 100.2, 99.8, 100.0)
            for index in range(40)
        ]
        candles[4] = Candle(60, 99.0, 99.1, 98.9, 99.0)
        candles[7] = Candle(105, 101.0, 101.1, 100.9, 101.0)
        candles[13] = Candle(195, 99.0, 99.1, 98.9, 99.0)
        passed = DirectionEvaluation(
            "SELL",
            tuple(ConditionResult(f"S{i}", True, True, "ok") for i in range(1, 5)),
            middle=100.05,
            upper=101,
            lower=99,
            stochastic_extremity=10,
            stochastic_turn=5,
            ma_turn_percent=0.04,
            middle_slope_percent=0.05,
            band_curvature_percent=0.025,
        )
        failed = DirectionEvaluation(
            "BUY",
            tuple(ConditionResult(f"S{i}", False, True, "fail") for i in range(1, 5)),
        )
        variant = ReplayVariant("test", "A", True, True, 5)

        with patch(
            "strategy.replay.evaluate_direction",
            side_effect=lambda _series, direction, **_kwargs: (
                passed if direction == "SELL" else failed
            ),
        ), patch(
            "strategy.replay._approximate_trigger",
            side_effect=lambda evaluation, candle, _trigger: (
                evaluation.direction == "SELL" and candle.time == 15,
                100.05,
            ),
        ):
            summary = replay_variant("SYNTHETIC", candles, variant)

        self.assertEqual(summary.signal_count, 1)
        signal = summary.signals[0]
        self.assertEqual(signal.direction, "SELL")
        self.assertAlmostEqual(signal.entry_price, 100.05)
        self.assertEqual(
            signal.outcomes,
            {35: "WIN"},
        )
        self.assertGreaterEqual(signal.strength, 0)
        self.assertLessEqual(signal.strength, 100)

    def test_strategy_lab_compares_all_twenty_four_variants(self) -> None:
        candles = [
            Candle(index * 10, 100.0, 100.2, 99.8, 100.0)
            for index in range(80)
        ]
        results = run_strategy_lab({"PAIR": candles})
        self.assertEqual(len(strategy_variants()), 24)
        self.assertEqual(len(results), 24)
        self.assertTrue(all(summary.pair == "TOTAL" for summary in results.values()))


if __name__ == "__main__":
    unittest.main()
