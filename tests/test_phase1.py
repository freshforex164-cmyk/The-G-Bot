from __future__ import annotations

import ast
import os
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

import broker.pocket_option as pocket_option_module
import main as main_module
from broker.pocket_option import (
    PocketOptionAuthenticationError,
    PocketOptionBroker,
    PocketOptionOperationError,
    PocketOptionOrderRejected,
)
from config import (
    DEFAULT_MODE,
    EXPIRY_SECONDS,
    POCKET_OPTION_TIME_OFFSET_SECONDS,
    SSID_ENV_BY_MODE,
    STATE_KEYS_BY_MODE,
    TEST_TRADE_STAKE_CAP,
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
    order_error: str | None = None

    def __init__(self, ssid: str, config: object) -> None:
        self.connected = True
        self.ssid = ssid
        self.shutdown_called = False
        self.reconnect_count = 0
        self.subscriptions = 0
        self.unsubscriptions = 0
        self.orders: list[tuple[str, str, float, int]] = []
        self.order_call_count = 0
        self.check_win_calls: list[tuple[str, int | None]] = []
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
        self.order_call_count += 1
        if type(self).order_error:
            raise RuntimeError(type(self).order_error)
        self.orders.append((asset, "call", amount, time))
        return "trade-id", {"side": "call"}

    async def sell(
        self, asset: str, amount: float, time: int, check_win: bool = False
    ) -> tuple[str, dict[str, str]]:
        self.order_call_count += 1
        if type(self).order_error:
            raise RuntimeError(type(self).order_error)
        self.orders.append((asset, "put", amount, time))
        return "trade-id", {"side": "put"}

    async def check_win(
        self, trade_id: str, timeout_seconds: int | None = None
    ) -> dict[str, object]:
        self.check_win_calls.append((trade_id, timeout_seconds))
        return {"result": "win", "profit": 0.85}

    async def shutdown(self) -> None:
        self.shutdown_called = True
        self.connected = False


class PocketOptionBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        FakePocketOptionClient.instances.clear()
        FakePocketOptionClient.demo_account = True
        FakePocketOptionClient.balance_failure = None
        FakePocketOptionClient.order_error = None
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

    async def test_demo_order_stake_cap_limits_the_submitted_amount(self) -> None:
        broker = PocketOptionBroker(FAKE_DEMO_SSID, mode="demo")
        await broker.connect()

        await broker.buy(
            "EURUSD_otc",
            amount=5.0,
            direction="call",
            expiry_seconds=30,
            stake_cap=TEST_TRADE_STAKE_CAP,
        )

        self.assertEqual(
            FakePocketOptionClient.instances[0].orders,
            [("EURUSD_otc", "call", 1.0, 30)],
        )
        await broker.close()

    async def test_order_submission_failure_is_not_retried(self) -> None:
        FakePocketOptionClient.order_error = "Market closed"
        broker = PocketOptionBroker(FAKE_DEMO_SSID, mode="demo")
        await broker.connect()

        with self.assertRaises(PocketOptionOrderRejected) as captured:
            await broker.buy("EURUSD_otc", 1.0, "call", 30)

        self.assertEqual(captured.exception.reason, "Market closed")
        self.assertEqual(FakePocketOptionClient.instances[0].order_call_count, 1)
        self.assertEqual(FakePocketOptionClient.instances[0].orders, [])
        await broker.close()

    async def test_order_failure_reason_redacts_the_ssid(self) -> None:
        FakePocketOptionClient.order_error = (
            f"Provider error included {FAKE_DEMO_SSID}"
        )
        broker = PocketOptionBroker(FAKE_DEMO_SSID, mode="demo")
        await broker.connect()

        with self.assertRaises(PocketOptionOrderRejected) as captured:
            await broker.buy("EURUSD_otc", 1.0, "call", 30)

        self.assertNotIn(FAKE_DEMO_SSID, captured.exception.reason)
        self.assertIn("[REDACTED SSID]", captured.exception.reason)
        await broker.close()

    async def test_check_win_passes_timeout_to_installed_client_api(self) -> None:
        broker = PocketOptionBroker(FAKE_DEMO_SSID, mode="demo")
        await broker.connect()

        result = await broker.check_win("trade-id", timeout_seconds=60)

        self.assertEqual(result, {"result": "win", "profit": 0.85})
        self.assertEqual(
            FakePocketOptionClient.instances[0].check_win_calls,
            [("trade-id", 60)],
        )
        await broker.close()

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
        test_trade_function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "run_test_trade"
        )
        self.assertEqual(len(order_calls), 1)
        self.assertIn(order_calls[0], list(ast.walk(test_trade_function)))


class FakeTestTradeBroker:
    instances: list[FakeTestTradeBroker] = []
    payout_data = {
        "EURUSD_otc": 85.0,
        "USDCHF_otc": 92.0,
        "GBPJPY_otc": 79.0,
    }
    result_data: dict[str, object] = {"result": "win", "profit": 0.85}
    order_failure: PocketOptionOrderRejected | None = None

    def __init__(self, _ssid: str, mode: str) -> None:
        self.mode = mode
        self.closed = False
        self.balance_calls = 0
        self.orders: list[tuple[str, float, str, int, float | None]] = []
        self.result_calls: list[tuple[str, int]] = []
        type(self).instances.append(self)

    async def connect(self) -> None:
        return None

    async def balance(self) -> float:
        self.balance_calls += 1
        return 1000.0 if self.balance_calls == 1 else 999.0

    async def payouts(self) -> dict[str, float]:
        return dict(type(self).payout_data)

    async def buy(
        self,
        pair: str,
        amount: float,
        direction: str,
        expiry_seconds: int,
        *,
        stake_cap: float | None = None,
    ) -> tuple[str, dict[str, object]]:
        if type(self).order_failure is not None:
            raise type(self).order_failure
        submitted_amount = min(amount, stake_cap) if stake_cap is not None else amount
        self.orders.append(
            (pair, submitted_amount, direction, expiry_seconds, stake_cap)
        )
        return "fake-trade-id", {"accepted": True}

    async def check_win(
        self, trade_id: str, *, timeout_seconds: int
    ) -> dict[str, object]:
        self.result_calls.append((trade_id, timeout_seconds))
        return dict(type(self).result_data)

    async def close(self) -> None:
        self.closed = True


class TestTradeCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeTestTradeBroker.instances.clear()
        FakeTestTradeBroker.payout_data = {
            "EURUSD_otc": 85.0,
            "USDCHF_otc": 92.0,
            "GBPJPY_otc": 79.0,
        }
        FakeTestTradeBroker.result_data = {"result": "win", "profit": 0.85}
        FakeTestTradeBroker.order_failure = None

    def _run(
        self, args: list[str], *, mode: str = "demo"
    ) -> tuple[int, str]:
        output = StringIO()
        with (
            patch.object(main_module, "MODE", mode),
            patch.object(
                main_module, "PocketOptionBroker", FakeTestTradeBroker
            ) as broker_class,
            patch.dict(
                os.environ,
                {SSID_ENV_BY_MODE[mode]: FAKE_DEMO_SSID if mode == "demo" else FAKE_REAL_SSID},
                clear=True,
            ),
            redirect_stdout(output),
        ):
            exit_code = main_module.main(["--run", "test-trade", *args])
        return exit_code, output.getvalue()

    def test_success_selects_highest_payout_and_places_exactly_one_capped_order(
        self,
    ) -> None:
        with patch.object(main_module, "run_engine") as run_engine:
            exit_code, output = self._run([])

        self.assertEqual(exit_code, 0)
        broker = FakeTestTradeBroker.instances[0]
        self.assertEqual(
            broker.orders,
            [("USDCHF_otc", 1.0, "call", EXPIRY_SECONDS, TEST_TRADE_STAKE_CAP)],
        )
        self.assertEqual(broker.result_calls, [("fake-trade-id", EXPIRY_SECONDS + 30)])
        self.assertTrue(broker.closed)
        self.assertEqual(broker.balance_calls, 2)
        self.assertIn("Demo balance before: $1000.00", output)
        self.assertIn("pair=USDCHF_otc payout=92%", output)
        self.assertIn("direction=call", output)
        self.assertIn(f"expiry={EXPIRY_SECONDS}s", output)
        self.assertIn("Order confirmed in ", output)
        self.assertIn("Result: WIN | profit: $+0.85", output)
        self.assertIn("Demo balance after: $999.00", output)
        self.assertNotIn(FAKE_DEMO_SSID, output)
        run_engine.assert_not_called()

    def test_put_pair_and_expiry_options_are_passed_to_the_single_order(self) -> None:
        exit_code, output = self._run(
            ["--direction", "put", "--pair", "EURUSD_otc", "--expiry", "45"]
        )

        self.assertEqual(exit_code, 0)
        broker = FakeTestTradeBroker.instances[0]
        self.assertEqual(
            broker.orders,
            [("EURUSD_otc", 1.0, "put", 45, TEST_TRADE_STAKE_CAP)],
        )
        self.assertIn("pair=EURUSD_otc payout=85%", output)
        self.assertIn("direction=put expiry=45s", output)
        self.assertEqual(broker.result_calls, [("fake-trade-id", 75)])

    def test_loss_result_is_reported_with_profit_and_balance(self) -> None:
        FakeTestTradeBroker.result_data = {"result": "loss", "profit": -1.0}

        exit_code, output = self._run([])

        self.assertEqual(exit_code, 0)
        self.assertIn("Result: LOSS | profit: $-1.00", output)
        self.assertIn("Demo balance after: $999.00", output)
        self.assertEqual(len(FakeTestTradeBroker.instances[0].orders), 1)

    def test_rejection_prints_exact_reason_and_does_not_check_result_or_retry(
        self,
    ) -> None:
        FakeTestTradeBroker.order_failure = PocketOptionOrderRejected(
            "Unsupported expiry 7 seconds; supported: 30, 60"
        )

        with self.assertLogs("g_bots", level="ERROR") as captured:
            exit_code, _output = self._run(["--expiry", "7"])

        self.assertEqual(exit_code, 1)
        messages = "\n".join(captured.output)
        self.assertIn("Unsupported expiry 7 seconds; supported: 30, 60", messages)
        self.assertIn("No retry was sent", messages)
        broker = FakeTestTradeBroker.instances[0]
        self.assertEqual(broker.orders, [])
        self.assertEqual(broker.result_calls, [])
        self.assertTrue(broker.closed)

    def test_real_mode_refuses_before_constructing_a_broker(self) -> None:
        with self.assertLogs("g_bots", level="ERROR") as captured:
            exit_code, _output = self._run([], mode="real")

        self.assertEqual(exit_code, 1)
        self.assertIn("demo-only", "\n".join(captured.output))
        self.assertEqual(FakeTestTradeBroker.instances, [])


if __name__ == "__main__":
    unittest.main()
