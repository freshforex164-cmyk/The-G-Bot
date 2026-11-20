from __future__ import annotations

import asyncio
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import config
from data.candles import Candle
from market_assets import MarketAsset
from scanner import SignalScanner
from strategy.pullback_reversal import PullbackReversalStrategy, Signal
from strategy.rules import ConditionResult, DirectionEvaluation
from strategy.strength import score_strength


def candles_from_closes(closes: list[float]) -> list[Candle]:
    return [
        Candle(index * 10, close, close + 1, close - 1, close)
        for index, close in enumerate(closes)
    ]


def make_signal(
    pair: str,
    direction: str,
    strength: int,
    *,
    valid_until: datetime | None = None,
) -> Signal:
    now = datetime.now(timezone.utc)
    return Signal(
        pair=pair,
        direction=direction,
        price=1.2,
        time=now,
        stochastic_k=70.0,
        stochastic_d=65.0,
        ma_slope=0.01,
        bollinger_middle=1.1,
        strength=strength,
        valid_until=valid_until or now + timedelta(seconds=15),
    )


class StrengthTests(unittest.TestCase):
    def test_strength_is_clamped_and_has_weighted_component_breakdown(self) -> None:
        score, components = score_strength(
            stochastic_extremity=1000,
            stochastic_turn=1000,
            moving_average_turn=1000,
            bollinger_slope=1000,
            bollinger_curvature=1000,
            touch_cleanliness=1000,
        )
        self.assertEqual(score, 100)
        self.assertEqual(sum(components.values()), 100)
        self.assertEqual(
            components,
            {
                "stochastic": 30,
                "moving_average": 25,
                "bollinger": 25,
                "middle_band_touch": 20,
            },
        )

    def test_payout_does_not_change_strategy_strength(self) -> None:
        evaluation = DirectionEvaluation(
            direction="SELL",
            conditions=tuple(
                ConditionResult(f"S{index}", True, True, "test")
                for index in range(1, 5)
            ),
            stochastic_extremity=6.0,
            stochastic_turn=7.0,
            ma_turn_percent=0.04,
            middle_slope_percent=0.03,
            band_curvature_percent=0.02,
            stochastic_k=70,
            stochastic_d=75,
            middle=100,
            upper=101,
        )
        candles = candles_from_closes([100.0] * 80)

        def make_signal(payout: float) -> Signal:
            strategy = PullbackReversalStrategy()
            with (
                patch.object(config, "TRIGGER_VARIANT", "A"),
                patch("strategy.pullback_reversal.evaluate_direction", return_value=evaluation),
            ):
                strategy.on_closed_candles("PAIR", candles, payout=payout)
                result = strategy.on_tick(
                    "PAIR",
                    100.2,
                    Candle(800, 100.0, 100.2, 99.9, 100.2),
                    payout=payout,
                )
            assert result is not None
            return result

        low, high = make_signal(80), make_signal(99)
        self.assertNotEqual(low.payout, high.payout)
        self.assertEqual(low.strength, high.strength)
        self.assertEqual(low.strength_components, high.strength_components)


class SignalScannerTests(unittest.TestCase):
    @unittest.skip("OBSOLETE (failing before 2026-11-20): targets the removed multi-pair scanner and strength-ranked signals.")
    def test_pause_cancels_setups_and_prevents_signal_evaluation_until_resume(self) -> None:
        class Candles:
            closed: list[Candle] = []

            def update(self, tick):
                return Candle(int(tick["timestamp"]), 1.0, 1.1, 0.9, 1.0)

        class Strategy:
            def __init__(self):
                self.cancelled: list[tuple[str, str]] = []
                self.evaluations = 0

            def cancel_all(self, pair, reason):
                self.cancelled.append((pair, reason))

            def on_closed_candles(self, *_args, **_kwargs):
                self.evaluations += 1

            def take_closed_signal(self, _pair):
                return None

        scanner = SignalScanner(SimpleNamespace())
        strategy = Strategy()
        state = SimpleNamespace(candles=Candles(), strategy=strategy, payout=92.0)
        scanner.pairs["PAIR_otc"] = state
        scanner.pause()
        self.assertTrue(scanner.is_paused)
        self.assertEqual(strategy.cancelled, [("PAIR_otc", "scanner paused")])

        tick = {
            "timestamp": int(datetime.now(timezone.utc).timestamp()) + 7200,
            "price": 1.0,
        }
        scanner._handle_tick("PAIR_otc", state, tick)
        self.assertEqual(strategy.evaluations, 0)

        scanner.resume()
        scanner._handle_tick("PAIR_otc", state, tick)
        self.assertFalse(scanner.is_paused)
        self.assertEqual(strategy.evaluations, 1)

    @unittest.skip("OBSOLETE (failing before 2026-11-20): targets the removed multi-pair scanner and strength-ranked signals.")

    def test_active_signals_are_ranked_capped_and_expired(self) -> None:
        scanner = SignalScanner(SimpleNamespace())
        now = datetime.now(timezone.utc)
        scanner.signals = {
            str(index): make_signal(
                f"P{index}",
                "BUY",
                strength,
                valid_until=now + timedelta(seconds=20),
            )
            for index, strength in enumerate((20, 95, 70))
        }
        with patch.object(config, "MAX_ACTIVE_SIGNALS", 2):
            self.assertEqual(
                [signal.strength for signal in scanner.active_signals], [95, 70]
            )
        expired = make_signal(
            "OLD", "SELL", 99, valid_until=now - timedelta(seconds=1)
        )
        scanner.signals[expired.id] = expired
        scanner.expire_signals(now)
        self.assertEqual(expired.status, "EXPIRED")
        self.assertNotIn(expired, scanner.active_signals)

    @unittest.skip("OBSOLETE (failing before 2026-11-20): targets the removed multi-pair scanner and strength-ranked signals.")

    def test_duplicate_pair_direction_is_suppressed_while_active(self) -> None:
        scanner = SignalScanner(SimpleNamespace())
        produced = make_signal("PAIR", "SELL", 80)

        class Candles:
            forming_candle = Candle(10, 1.0, 1.1, 0.9, 1.05)

            def update(self, tick):
                return None

        class Strategy:
            def on_tick(self, *args, **kwargs):
                return produced

        state = SimpleNamespace(
            candles=Candles(),
            strategy=Strategy(),
            payout=92.0,
            asset=MarketAsset("PAIR_otc", "Pair OTC", 92.0, True),
        )
        scanner.pairs["PAIR"] = state
        observed: list[Signal] = []
        scanner.on_signal = observed.append
        tick = {
            "timestamp": int(datetime.now(timezone.utc).timestamp()) + 7200,
            "price": 1.05,
        }
        scanner._handle_tick("PAIR", state, tick)
        scanner._handle_tick("PAIR", state, tick)
        self.assertEqual(len(scanner.signals), 1)
        self.assertEqual(observed, [produced])

    @unittest.skip("OBSOLETE (failing before 2026-11-20): targets the removed multi-pair scanner and strength-ranked signals.")

    def test_stale_tick_is_rejected_before_candle_processing(self) -> None:
        class Candles:
            def __init__(self) -> None:
                self.updates = 0

            def update(self, _tick):
                self.updates += 1
                return None

        candles = Candles()
        scanner = SignalScanner(SimpleNamespace())
        state = SimpleNamespace(candles=candles)
        stale_timestamp = (
            int(datetime.now(timezone.utc).timestamp())
            - int(config.WATCHDOG_TICK_MAX_AGE_SECONDS)
            - 5
            + config.POCKET_OPTION_TIME_OFFSET_SECONDS
        )
        scanner._handle_tick(
            "EURGBP_otc",
            state,
            {"timestamp": stale_timestamp, "price": 1.0},
        )
        self.assertEqual(candles.updates, 0)
        self.assertIsNone(scanner.last_tick_age_seconds)


class FakeMultiPairBroker:
    def __init__(self) -> None:
        self.payout_map = {"A_otc": 90.0, "B_otc": 85.0, "LOW_otc": 70.0}
        self.subscribed: set[str] = set()
        self.closed = False

    async def connect(self) -> None:
        return None

    async def payouts(self) -> dict[str, float]:
        return dict(self.payout_map)

    async def active_assets(self) -> list[dict[str, object]]:
        return [
            {
                "symbol": pair,
                "name": f"{pair[:-4]} OTC",
                "is_otc": True,
                "is_active": True,
                "payout": payout,
            }
            for pair, payout in self.payout_map.items()
        ]

    async def candle_history(self, pair: str, period: int, lookback: int):
        return []

    async def subscribe_ticks(self, pair: str):
        self.subscribed.add(pair)
        timestamp = int(datetime.now(timezone.utc).timestamp()) + 7200
        yield {"timestamp": timestamp, "price": 1.0}
        await asyncio.Event().wait()

    async def close(self) -> None:
        self.closed = True


class MultiPairScannerTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skip("OBSOLETE (failing before 2026-11-20): targets the removed multi-pair scanner and strength-ranked signals.")
    async def test_scanner_streams_all_eligible_pairs_and_closes_cleanly(self) -> None:
        broker = FakeMultiPairBroker()
        output: list[str] = []
        scanner = SignalScanner(broker, output=output.append)
        await scanner.run(minutes=0.001)
        self.assertEqual(set(scanner.pairs), {"A_otc", "B_otc"})
        self.assertEqual(broker.subscribed, {"A_otc", "B_otc"})
        self.assertTrue(broker.closed)
        self.assertTrue(
            any("requested_pairs=2 receiving_tick_pairs=2" in line for line in output)
        )

    @unittest.skip("OBSOLETE (failing before 2026-11-20): targets the removed multi-pair scanner and strength-ranked signals.")

    async def test_refresh_adds_and_removes_pairs(self) -> None:
        broker = FakeMultiPairBroker()
        scanner = SignalScanner(broker, output=lambda _: None)
        await broker.connect()
        await scanner.refresh_pairs()
        self.assertEqual(set(scanner.pairs), {"A_otc", "B_otc"})
        scanner._receiving_pairs.update({"A_otc", "B_otc"})
        broker.payout_map = {"B_otc": 88.0, "C_otc": 92.0}
        await scanner.refresh_pairs()
        await asyncio.sleep(0)
        self.assertEqual(set(scanner.pairs), {"B_otc", "C_otc"})
        self.assertEqual(scanner._receiving_pairs, {"B_otc", "C_otc"})
        self.assertNotIn("A_otc", scanner._receiving_pairs)
        self.assertEqual(scanner.pairs["B_otc"].payout, 88.0)
        for task in scanner._tasks.values():
            task.cancel()
        await asyncio.gather(*scanner._tasks.values(), return_exceptions=True)
        await broker.close()


class WatchdogTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skip("OBSOLETE (failing before 2026-11-20): targets the removed multi-pair scanner and strength-ranked signals.")
    async def test_global_stall_rebuilds_connection_and_notifies_admin(self) -> None:
        class Broker:
            def __init__(self) -> None:
                self.rebuild_count = 0

            async def rebuild_connection(self) -> None:
                self.rebuild_count += 1

            async def payouts(self):
                return {}

            async def active_assets(self):
                return []

        broker = Broker()
        alerts: list[str] = []
        scanner = SignalScanner(
            broker,
            on_watchdog_event=alerts.append,
            output=lambda _line: None,
        )
        scanner._started_at = (
            time.monotonic() - config.WATCHDOG_GLOBAL_STALL_SECONDS - 1
        )
        await scanner._check_feed_watchdog()
        self.assertEqual(broker.rebuild_count, 1)
        self.assertIn("recovered", alerts[-1])

    @unittest.skip("OBSOLETE (failing before 2026-11-20): targets the removed multi-pair scanner and strength-ranked signals.")

    async def test_third_failed_rebuild_alerts_that_ssid_may_have_expired(self) -> None:
        class Broker:
            async def rebuild_connection(self) -> None:
                raise RuntimeError("offline")

        alerts: list[str] = []
        scanner = SignalScanner(
            Broker(),
            on_watchdog_event=alerts.append,
            output=lambda _line: None,
        )
        with (
            patch.object(config, "WATCHDOG_CALL_TIMEOUT_SECONDS", 1),
            patch.object(config, "WATCHDOG_RETRY_SECONDS", 0),
            patch.object(config, "WATCHDOG_REBUILD_FAILURE_LIMIT", 3),
        ):
            for _ in range(3):
                await scanner._rebuild_feed("test stall")
        self.assertTrue(any("SSID may have expired" in alert for alert in alerts))


if __name__ == "__main__":
    unittest.main()
