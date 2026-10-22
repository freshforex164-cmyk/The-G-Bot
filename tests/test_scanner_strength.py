from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import config
from data.candles import Candle
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
        )
        observed: list[Signal] = []
        scanner.on_signal = observed.append
        tick = {"timestamp": 1_760_000_000 + 7200, "price": 1.05}
        scanner._handle_tick("PAIR", state, tick)
        scanner._handle_tick("PAIR", state, tick)
        self.assertEqual(len(scanner.signals), 1)
        self.assertEqual(observed, [produced])


class FakeMultiPairBroker:
    def __init__(self) -> None:
        self.payout_map = {"A": 90.0, "B": 85.0, "LOW": 70.0}
        self.subscribed: set[str] = set()
        self.closed = False

    async def connect(self) -> None:
        return None

    async def payouts(self) -> dict[str, float]:
        return dict(self.payout_map)

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
    async def test_scanner_streams_all_eligible_pairs_and_closes_cleanly(self) -> None:
        broker = FakeMultiPairBroker()
        output: list[str] = []
        scanner = SignalScanner(broker, output=output.append)
        await scanner.run(minutes=0.001)
        self.assertEqual(set(scanner.pairs), {"A", "B"})
        self.assertEqual(broker.subscribed, {"A", "B"})
        self.assertTrue(broker.closed)
        self.assertTrue(
            any("requested_pairs=2 receiving_tick_pairs=2" in line for line in output)
        )

    async def test_refresh_adds_and_removes_pairs(self) -> None:
        broker = FakeMultiPairBroker()
        scanner = SignalScanner(broker, output=lambda _: None)
        await broker.connect()
        await scanner.refresh_pairs()
        self.assertEqual(set(scanner.pairs), {"A", "B"})
        scanner._receiving_pairs.update({"A", "B"})
        broker.payout_map = {"B": 88.0, "C": 92.0}
        await scanner.refresh_pairs()
        await asyncio.sleep(0)
        self.assertEqual(set(scanner.pairs), {"B", "C"})
        self.assertEqual(scanner._receiving_pairs, {"B", "C"})
        self.assertNotIn("A", scanner._receiving_pairs)
        self.assertEqual(scanner.pairs["B"].payout, 88.0)
        for task in scanner._tasks.values():
            task.cancel()
        await asyncio.gather(*scanner._tasks.values(), return_exceptions=True)
        await broker.close()


if __name__ == "__main__":
    unittest.main()
