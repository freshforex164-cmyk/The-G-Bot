from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import broker.pocket_option as pocket_option_module
from broker.pocket_option import PocketOptionBroker
from config import (
    ConfigurationError,
    POCKET_OPTION_TIME_OFFSET_SECONDS,
    load_ssid,
)
from data.candles import CandleBuilder, CandleDataError

FIXED_TIME = 1_760_000_000
FAKE_SSID = '42["auth",{"session":"not-a-real-session","uid":123,"isDemo":1}]'


class CandleBuilderTests(unittest.TestCase):
    def test_builds_closed_candles_from_ticks_and_retains_history(self) -> None:
        builder = CandleBuilder(period_seconds=10, max_candles=3)
        offset = POCKET_OPTION_TIME_OFFSET_SECONDS
        history = [
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
            self.assertEqual(builder.seed(history), 2)

        for second, price in ((1, 1.5), (4, 1.8), (8, 1.4)):
            self.assertIsNone(
                builder.update(
                    {"timestamp": FIXED_TIME + offset + second, "price": price}
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

    def test_seed_keeps_only_most_recent_candles(self) -> None:
        builder = CandleBuilder(period_seconds=10, max_candles=3)
        offset = POCKET_OPTION_TIME_OFFSET_SECONDS
        rows = [
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
            self.assertEqual(builder.seed(rows), 3)
        self.assertEqual(
            [candle.time for candle in builder.closed],
            [FIXED_TIME - 30, FIXED_TIME - 20, FIXED_TIME - 10],
        )

    def test_invalid_tick_fails_clearly(self) -> None:
        with self.assertRaisesRegex(CandleDataError, "timestamp"):
            CandleBuilder().update({"price": 1.2})


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
    is_demo_value = True

    def __init__(self, ssid: str, config: object) -> None:
        self.connected = True
        self.ssid = ssid
        self.shutdown_called = False
        self.reconnect_count = 0
        self.unsubscriptions = 0
        type(self).instances.append(self)

    async def wait_for_assets(self, timeout: float = 60.0) -> None:
        return None

    def is_connected(self) -> bool:
        return self.connected

    def is_ssid_valid(self) -> bool:
        return True

    def is_demo(self) -> bool:
        return type(self).is_demo_value

    async def payout(self, asset: str | None = None) -> dict[str, int]:
        return {"EURUSD_otc": 85, "USDCHF_otc": 80}

    async def compile_candles(
        self, asset: str, custom_period: int, lookback_period: int
    ) -> list[dict[str, float]]:
        return [{"time": FIXED_TIME, "open": 1, "high": 2, "low": 0, "close": 1}]

    async def subscribe_symbol(self, asset: str) -> FakeSubscription:
        return FakeSubscription()

    async def unsubscribe(self, asset: str) -> None:
        self.unsubscriptions += 1

    async def reconnect(self) -> None:
        self.reconnect_count += 1
        self.connected = True

    async def shutdown(self) -> None:
        self.shutdown_called = True
        self.connected = False


class ReadOnlyBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        FakePocketOptionClient.instances.clear()
        FakePocketOptionClient.is_demo_value = True
        self.client_patch = patch.object(
            pocket_option_module, "PocketOptionAsync", FakePocketOptionClient
        )
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

    async def test_connect_payout_history_and_account_type_are_read_only(self) -> None:
        broker = PocketOptionBroker(FAKE_SSID)
        await broker.connect()
        self.assertTrue(broker.is_connected)
        self.assertTrue(broker.is_demo_account)
        self.assertEqual(
            await broker.payouts(), {"EURUSD_otc": 85.0, "USDCHF_otc": 80.0}
        )
        self.assertEqual(
            await broker.candle_history("EURUSD_otc", 10, 3000),
            [{"time": FIXED_TIME, "open": 1, "high": 2, "low": 0, "close": 1}],
        )
        await broker.close()
        self.assertTrue(FakePocketOptionClient.instances[0].shutdown_called)

    async def test_account_type_is_informational_not_a_connection_gate(self) -> None:
        FakePocketOptionClient.is_demo_value = False
        broker = PocketOptionBroker(FAKE_SSID)
        await broker.connect()
        self.assertFalse(broker.is_demo_account)
        self.assertTrue(broker.is_connected)
        await broker.close()

    async def test_tick_subscription_yields_and_cleans_up(self) -> None:
        broker = PocketOptionBroker(FAKE_SSID)
        await broker.connect()
        ticks = broker.subscribe_ticks("EURUSD_otc")
        self.assertEqual(await anext(ticks), {"timestamp": FIXED_TIME, "price": 1.25})
        await ticks.aclose()
        self.assertEqual(FakePocketOptionClient.instances[0].unsubscriptions, 1)
        await broker.close()

    async def test_reconnects_after_a_connection_drop(self) -> None:
        broker = PocketOptionBroker(FAKE_SSID)
        await broker.connect()
        client = FakePocketOptionClient.instances[0]
        client.connected = False
        with (
            patch.object(pocket_option_module, "RECONNECT_BASE_DELAY_SECONDS", 0),
            patch.object(pocket_option_module, "RECONNECT_MAX_DELAY_SECONDS", 0),
        ):
            await broker.connect()
        self.assertEqual(client.reconnect_count, 1)
        self.assertTrue(broker.is_connected)
        await broker.close()


class SecretConfigurationTests(unittest.TestCase):
    def test_missing_secret_has_clear_message(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ConfigurationError, "PO_SSID"):
                load_ssid()

    def test_malformed_secret_is_not_echoed(self) -> None:
        fake_secret = "not-a-valid-session-secret"
        with patch.dict(os.environ, {"PO_SSID": fake_secret}, clear=True):
            with self.assertRaises(ConfigurationError) as captured:
                load_ssid()
        self.assertNotIn(fake_secret, str(captured.exception))


if __name__ == "__main__":
    unittest.main()
