from __future__ import annotations

import ast
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import broker.pocket_option as pocket_option_module
import main as main_module
from broker.pocket_option import (
    PocketOptionAuthenticationError,
    PocketOptionBroker,
    PocketOptionOperationError,
)
from config import (
    DEFAULT_MODE,
    POCKET_OPTION_TIME_OFFSET_SECONDS,
    SSID_ENV_BY_MODE,
    STATE_KEYS_BY_MODE,
    ConfigurationError,
    load_mode_ssid,
    state_keys_for_mode,
)
from data.candles import CandleBuilder, CandleDataError


FIXED_TIME = 1_760_000_000
FAKE_DEMO_SSID = (
    '42["auth",{"session":"0123456789","uid":123,"isDemo":1}]'
)
FAKE_REAL_SSID = (
    '42["auth",{"session":"abcdefghij","uid":456,"isDemo":0}]'
)


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
    balance_failure: str | None = None

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
        return type(self).demo_account

    async def balance(self) -> float:
        if type(self).balance_failure:
            raise RuntimeError(type(self).balance_failure)
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
        FakePocketOptionClient.balance_failure = None
        self.client_patch = patch.object(
            pocket_option_module, "PocketOptionAsync", FakePocketOptionClient
        )
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

    async def test_demo_balance_payouts_history_and_order_direction_mapping(self) -> None:
        broker = PocketOptionBroker(FAKE_DEMO_SSID, mode="demo")
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
        broker = PocketOptionBroker(FAKE_REAL_SSID, mode="demo")

        with self.assertRaises(PocketOptionAuthenticationError):
            await broker.connect()

        self.assertTrue(FakePocketOptionClient.instances[0].shutdown_called)

    async def test_reconnects_after_disconnect(self) -> None:
        broker = PocketOptionBroker(FAKE_DEMO_SSID, mode="demo")
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
        broker = PocketOptionBroker(FAKE_DEMO_SSID, mode="demo")
        await broker.connect()
        stream = broker.subscribe_ticks("EURUSD_otc")
        tick = await anext(stream)

        self.assertEqual(tick, {"timestamp": FIXED_TIME, "price": 1.25})
        await stream.aclose()
        client = FakePocketOptionClient.instances[0]
        self.assertEqual(client.subscriptions, 1)
        self.assertEqual(client.unsubscriptions, 1)
        await broker.close()

    async def test_refuses_demo_account_in_real_mode(self) -> None:
        FakePocketOptionClient.demo_account = True
        broker = PocketOptionBroker(FAKE_REAL_SSID, mode="real")

        with self.assertRaisesRegex(PocketOptionAuthenticationError, "demo account"):
            await broker.connect()

        self.assertTrue(FakePocketOptionClient.instances[0].shutdown_called)

    async def test_real_mode_reads_balance_but_cannot_place_orders(self) -> None:
        FakePocketOptionClient.demo_account = False
        broker = PocketOptionBroker(FAKE_REAL_SSID, mode="real")
        await broker.connect()

        self.assertEqual(await broker.balance(), 1234.5)
        self.assertEqual(
            await broker.payouts(),
            {"EURUSD_otc": 85.0, "USDCHF_otc": 80.0},
        )
        FakePocketOptionClient.balance_failure = "temporary provider failure"
        with self.assertRaisesRegex(PocketOptionOperationError, "read the real balance"):
            await broker.balance()
        FakePocketOptionClient.balance_failure = None
        stream = broker.subscribe_ticks("EURUSD_otc")
        self.assertEqual(
            await anext(stream),
            {"timestamp": FIXED_TIME, "price": 1.25},
        )
        await stream.aclose()
        with self.assertRaisesRegex(PocketOptionOperationError, "read-only"):
            await broker.buy("EURUSD_otc", 2.0, "call", 60)
        self.assertEqual(FakePocketOptionClient.instances[0].orders, [])
        await broker.close()


class ConfigurationTests(unittest.TestCase):
    def test_ssid_errors_do_not_echo_secret_contents(self) -> None:
        fake_secret = "not-a-real-secret-value"
        for mode in ("demo", "real"):
            with self.subTest(mode=mode):
                with patch.dict(
                    os.environ,
                    {SSID_ENV_BY_MODE[mode]: fake_secret},
                    clear=True,
                ):
                    with self.assertRaises(ConfigurationError) as captured:
                        load_mode_ssid(mode, confirm_real=True)
                self.assertNotIn(fake_secret, str(captured.exception))

    def test_real_mode_requires_the_real_secret(self) -> None:
        with patch.dict(
            os.environ,
            {SSID_ENV_BY_MODE["demo"]: FAKE_DEMO_SSID},
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigurationError, "PO_SSID_REAL"):
                load_mode_ssid("real", confirm_real=True)

    def test_real_mode_requires_explicit_cli_confirmation(self) -> None:
        with (
            patch.object(main_module, "MODE", "real"),
            patch.dict(
                os.environ,
                {SSID_ENV_BY_MODE["real"]: FAKE_REAL_SSID},
                clear=True,
            ),
            patch.object(main_module, "PocketOptionBroker") as broker_class,
            self.assertLogs("g_bots", level="ERROR") as captured,
        ):
            exit_code = main_module.main([])

        self.assertEqual(exit_code, 1)
        self.assertIn("--confirm-real", "\n".join(captured.output))
        broker_class.assert_not_called()

    def test_real_confirmation_flag_is_passed_through_cli(self) -> None:
        with (
            patch.object(main_module, "MODE", "real"),
            patch.object(main_module, "run_engine", new_callable=AsyncMock) as runner,
        ):
            exit_code = main_module.main(["--confirm-real"])

        self.assertEqual(exit_code, 0)
        runner.assert_awaited_once_with(
            "real",
            confirm_real=True,
            run_mode="watch",
            minutes=main_module.SIGNAL_RUN_DEFAULT_MINUTES,
        )

    def test_demo_mode_uses_demo_secret_and_remains_default(self) -> None:
        with patch.dict(
            os.environ,
            {
                SSID_ENV_BY_MODE["demo"]: FAKE_DEMO_SSID,
                SSID_ENV_BY_MODE["real"]: FAKE_REAL_SSID,
            },
            clear=True,
        ):
            self.assertEqual(load_mode_ssid("demo"), FAKE_DEMO_SSID)
        self.assertEqual(DEFAULT_MODE, "demo")

    def test_state_keys_are_separate_by_mode(self) -> None:
        demo_keys = state_keys_for_mode("demo")
        real_keys = state_keys_for_mode("real")
        self.assertEqual(set(demo_keys), {"peak_balance", "trades", "settings"})
        self.assertEqual(set(real_keys), {"peak_balance", "trades", "settings"})
        self.assertTrue(set(demo_keys.values()).isdisjoint(real_keys.values()))
        self.assertTrue(all(key.startswith("g_bots:demo:") for key in demo_keys.values()))
        self.assertTrue(all(key.startswith("g_bots:real:") for key in real_keys.values()))
        self.assertEqual(STATE_KEYS_BY_MODE["demo"], demo_keys)
        self.assertEqual(STATE_KEYS_BY_MODE["real"], real_keys)


class RealModeEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_real_mode_uses_real_secret_and_read_only_flow(self) -> None:
        class FakeEngineBroker:
            instances: list[FakeEngineBroker] = []

            def __init__(self, ssid: str, mode: str) -> None:
                self.ssid = ssid
                self.mode = mode
                self.instances.append(self)

            async def connect(self) -> None:
                return None

            async def balance(self) -> float:
                return 99.0

            async def payouts(self) -> dict[str, float]:
                return {}

            async def monitor_connection(self, stop_event: object) -> None:
                await stop_event.wait()

            async def close(self) -> None:
                return None

        FakeEngineBroker.instances.clear()
        with (
            patch.dict(
                os.environ,
                {SSID_ENV_BY_MODE["real"]: FAKE_REAL_SSID},
                clear=True,
            ),
            patch.object(main_module, "PocketOptionBroker", FakeEngineBroker),
            patch("builtins.print"),
        ):
            await main_module.run_engine("real", confirm_real=True)

        broker = FakeEngineBroker.instances[0]
        self.assertEqual(broker.ssid, FAKE_REAL_SSID)
        self.assertEqual(broker.mode, "real")

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
