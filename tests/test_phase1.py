from __future__ import annotations

import ast
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import broker.pocket_option as pocket_option_module
from broker.pocket_option import (
    PocketOptionAuthenticationError,
    PocketOptionBroker,
)
from config import (
    POCKET_OPTION_TIME_OFFSET_SECONDS,
    PO_SSID_ENV_VAR,
    ConfigurationError,
    load_demo_ssid,
)
from data.candles import CandleBuilder, CandleDataError


FIXED_TIME = 1_760_000_000


class CandleBuilderTests(unittest.TestCase):
    def test_builds_closed_ohlc_candles_and_retains_seed(self) -> None:
        builder = CandleBuilder(period_seconds=10, max_candles=3)
        offset = POCKET_OPTION_TIME_OFFSET_SECONDS
        seed_rows = [
            {
                "time": FIXED_TIME - 20 + offset,
                "open": 1.0,
                "high": 1.2,
                "low": 0.9,
                "close": 1.1,
            },
            {
                "time": FIXED_TIME - 10 + offset,
                "open": 1.1,
                "high": 1.3,
                "low": 1.0,
                "close": 1.2,
            },
        ]

        with patch("data.candles.time.time", return_value=FIXED_TIME):
            self.assertEqual(builder.seed(seed_rows), 2)

        self.assertIsNone(
            builder.update(
                {"timestamp": FIXED_TIME + offset + 1, "price": 1.5}
            )
        )
        self.assertIsNone(
            builder.update(
                {"timestamp": FIXED_TIME + offset + 4, "price": 1.8}
            )
        )
        self.assertIsNone(
            builder.update(
                {"timestamp": FIXED_TIME + offset + 8, "price": 1.4}
            )
        )
        closed = builder.update(
            {"timestamp": FIXED_TIME + offset + 10, "price": 1.6}
        )

        self.assertIsNotNone(closed)
        assert closed is not None
        self.assertEqual(
            (closed.time, closed.open, closed.high, closed.low, closed.close),
            (FIXED_TIME, 1.5, 1.8, 1.4, 1.4),
        )
        self.assertEqual(len(builder.closed), 3)
        self.assertEqual(builder.closed[-1], closed)

    def test_seed_keeps_only_most_recent_candles(self) -> None:
        builder = CandleBuilder(period_seconds=10, max_candles=3)
        offset = POCKET_OPTION_TIME_OFFSET_SECONDS
        history = [
            {
                "time": FIXED_TIME - (50 - index * 10) + offset,
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
            }
            for index in range(5)
        ]

        with patch("data.candles.time.time", return_value=FIXED_TIME):
            count = builder.seed(history)

        self.assertEqual(count, 3)
        self.assertEqual(
            [candle.time for candle in builder.closed],
            [FIXED_TIME - 30, FIXED_TIME - 20, FIXED_TIME - 10],
        )

    def test_invalid_tick_fails_clearly(self) -> None:
        builder = CandleBuilder()
        with self.assertRaisesRegex(CandleDataError, "timestamp"):
            builder.update({"price": 1.2})


class FakeSubscription:
    def __init__(self) -> None:
        self.sent = False

    def __aiter__(self) -> FakeSubscription:
        return self

    async def __anext__(self) -> dict[str, float]:
        if self.sent:
            raise StopAsyncIteration
        self.sent = True
        return {"timestamp": FIXED_TIME, "price": 1.25}


class FakePocketOptionClient:
    instances: list[FakePocketOptionClient] = []
    demo_account = True

    def __init__(self, ssid: str, config: object) -> None:
        self.connected = True
        self.ssid = ssid
        self.shutdown_called = False
        self.reconnect_count = 0
        self.subscriptions = 0
        self.unsubscriptions = 0
        self.orders: list[tuple[str, str, float, int]] = []
        self.instances.append(self)

    async def wait_for_assets(self, timeout: float = 60.0) -> None:
        return None

    def is_connected(self) -> bool:
        return self.connected

    def is_ssid_valid(self) -> bool:
        return True

    def is_demo(self) -> bool:
        return self.demo_account

    async def balance(self) -> float:
        return 1234.5

    async def payout(self, asset: str | None = None) -> dict[str, int]:
        return {"EURUSD_otc": 85, "USDCHF_otc": 80}

    async def compile_candles(
        self, asset: str, custom_period: int, lookback_period: int
    ) -> list[dict[str, float]]:
        return []

    async def subscribe_symbol(self, asset: str) -> FakeSubscription:
        self.subscriptions += 1
        return FakeSubscription()

    async def unsubscribe(self, asset: str) -> None:
        self.unsubscriptions += 1

    async def reconnect(self) -> None:
        self.reconnect_count += 1
        self.connected = True

    async def buy(
        self, asset: str, amount: float, time: int, check_win: bool = False
    ) -> tuple[str, dict[str, str]]:
        self.orders.append((asset, "call", amount, time))
        return "trade-id", {"side": "call"}

    async def sell(
        self, asset: str, amount: float, time: int, check_win: bool = False
    ) -> tuple[str, dict[str, str]]:
        self.orders.append((asset, "put", amount, time))
        return "trade-id", {"side": "put"}

    async def shutdown(self) -> None:
        self.shutdown_called = True
        self.connected = False


class PocketOptionBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        FakePocketOptionClient.instances.clear()
        FakePocketOptionClient.demo_account = True
        self.client_patch = patch.object(
            pocket_option_module, "PocketOptionAsync", FakePocketOptionClient
        )
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

    async def test_demo_balance_payouts_history_and_order_direction_mapping(self) -> None:
        broker = PocketOptionBroker("fake-demo-session")
        await broker.connect()

        self.assertEqual(await broker.balance(), 1234.5)
        self.assertEqual(
            await broker.payouts(),
            {"EURUSD_otc": 85.0, "USDCHF_otc": 80.0},
        )
        self.assertEqual(
            await broker.candle_history("EURUSD_otc", 10, 3000),
            [],
        )
        await broker.buy("EURUSD_otc", 2.0, "call", 60)
        await broker.buy("EURUSD_otc", 2.0, "put", 60)
        self.assertEqual(
            FakePocketOptionClient.instances[0].orders,
            [
                ("EURUSD_otc", "call", 2.0, 60),
                ("EURUSD_otc", "put", 2.0, 60),
            ],
        )

        await broker.close()
        self.assertTrue(FakePocketOptionClient.instances[0].shutdown_called)

    async def test_refuses_non_demo_session(self) -> None:
        FakePocketOptionClient.demo_account = False
        broker = PocketOptionBroker("fake-session")

        with self.assertRaises(PocketOptionAuthenticationError):
            await broker.connect()

        self.assertTrue(FakePocketOptionClient.instances[0].shutdown_called)

    async def test_reconnects_after_disconnect(self) -> None:
        broker = PocketOptionBroker("fake-demo-session")
        await broker.connect()
        client = FakePocketOptionClient.instances[0]
        client.connected = False

        with (
            patch.object(pocket_option_module, "RECONNECT_BASE_DELAY_SECONDS", 0),
            patch.object(pocket_option_module, "RECONNECT_MAX_ATTEMPTS", 2),
        ):
            await broker._mark_disconnected("test disconnect")
            await broker._reconnect_with_backoff()

        self.assertEqual(client.reconnect_count, 1)
        self.assertTrue(broker.is_connected)
        await broker.close()

    async def test_tick_stream_yields_raw_updates_and_unsubscribes(self) -> None:
        broker = PocketOptionBroker("fake-demo-session")
        await broker.connect()
        stream = broker.subscribe_ticks("EURUSD_otc")
        tick = await anext(stream)

        self.assertEqual(tick, {"timestamp": FIXED_TIME, "price": 1.25})
        await stream.aclose()
        client = FakePocketOptionClient.instances[0]
        self.assertEqual(client.subscriptions, 1)
        self.assertEqual(client.unsubscriptions, 1)
        await broker.close()


class ConfigurationTests(unittest.TestCase):
    def test_ssid_errors_do_not_echo_secret_contents(self) -> None:
        fake_secret = "not-a-real-secret-value"
        with patch.dict(os.environ, {PO_SSID_ENV_VAR: fake_secret}):
            with self.assertRaises(ConfigurationError) as captured:
                load_demo_ssid()
        self.assertNotIn(fake_secret, str(captured.exception))

    def test_main_does_not_call_order_method(self) -> None:
        tree = ast.parse(Path("main.py").read_text(encoding="utf-8"))
        order_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"buy", "sell"}
        ]
        self.assertEqual(order_calls, [])


if __name__ == "__main__":
    unittest.main()
