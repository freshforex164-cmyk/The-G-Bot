"""Phase 2 account isolation and handshake regressions; no live broker access."""
import asyncio
import unittest
from unittest.mock import AsyncMock, Mock

from .fakes import Env, ssid_for, wait_until
from ..auth.ssid import account_kind
from ..errors import VaultError
from ..trading.broker import PocketOptionBroker
from ..trading.models import TradingSettings


class Phase2Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = Env(allow_live=True)

    async def asyncTearDown(self):
        await self.env.rt.manager.shutdown()

    async def test_demo_replacement_preserves_real_ciphertext_and_status(self):
        e = self.env
        await e.add_user("u", ("demo", "real"))
        before = await e.repo.get_credential("u", "real")
        await e.rt.vault.store("u", "demo", ssid_for("replacement", "demo"))
        self.assertEqual(await e.repo.get_credential("u", "real"), before)
        self.assertEqual(await e.rt.vault.get("u", "real"), ssid_for("u", "real"))

    async def test_real_replacement_preserves_demo_ciphertext_and_status(self):
        e = self.env
        await e.add_user("u", ("demo", "real"))
        before = await e.repo.get_credential("u", "demo")
        await e.rt.vault.store("u", "real", ssid_for("replacement", "real"))
        self.assertEqual(await e.repo.get_credential("u", "demo"), before)

    async def test_invalid_flags_cannot_enter_real_vault(self):
        for flag in (2, -1, "real", None, 0.5):
            import json
            raw = "42" + json.dumps(["auth", {"session": "test-only", "isDemo": flag}])
            self.assertIsNone(account_kind(raw))
            with self.assertRaises(VaultError):
                await self.env.rt.vault.store("u", "real", raw)

    async def test_balance_checks_share_one_socket_even_concurrently(self):
        e = self.env
        await e.add_user("u")
        values = await asyncio.gather(*(e.rt.manager.balance("u") for _ in range(5)))
        self.assertEqual([v["balance"] for v in values], [10000.0] * 5)
        self.assertEqual(e.world("u").connects, 1)
        self.assertEqual(e.world("u").closes, 0)

    async def test_changed_token_replaces_only_its_pool_slot(self):
        e = self.env
        await e.add_user("u", ("demo", "real"))
        await e.rt.manager._read_balance_once("u", "demo")
        await e.rt.manager._read_balance_once("u", "real")
        real = e.rt.manager._balances.entries[("u", "real")].broker
        await e.rt.vault.store("u", "demo", ssid_for("new", "demo"))
        await e.rt.manager._read_balance_once("u", "demo")
        self.assertIs(e.rt.manager._balances.entries[("u", "real")].broker, real)
        self.assertEqual(e.world("new").connects, 1)

    async def test_idle_connections_and_shutdown_are_closed(self):
        e = self.env
        await e.add_user("u")
        await e.rt.manager.balance("u")
        e.clock.advance(61)
        await e.rt.manager.balance("u")
        self.assertEqual(e.world("u").connects, 2)
        self.assertEqual(e.world("u").closes, 1)
        await e.rt.manager.shutdown()
        self.assertEqual(e.world("u").closes, 2)

    async def test_slow_running_balance_never_opens_duplicate_socket(self):
        e = self.env
        await e.add_user("u")
        await e.rt.manager.start("u")
        worker = e.rt.manager._workers["u"]
        self.assertTrue(await wait_until(lambda: worker.balance_kind == "demo"))
        before = e.world("u").connects
        worker.refresh_balance_now = AsyncMock(return_value=None)
        result = await e.rt.manager.balance("u")
        self.assertFalse(result["fresh"])
        self.assertEqual(e.world("u").connects, before)

    async def test_real_worker_accepts_is_demo_zero(self):
        e = self.env
        await e.add_user("u", ("real",))
        await e.repo.save_settings("u", TradingSettings(mode="live", live_confirmed=True))
        await e.rt.manager.start("u")
        worker = e.rt.manager._workers["u"]
        self.assertTrue(await wait_until(lambda: worker.balance_kind == "real"))
        self.assertEqual(worker.status.value, "running")
        self.assertIsNone(worker.last_error)

    async def test_adapter_normalizes_real_zero_string_not_truthiness(self):
        broker = PocketOptionBroker(ssid_for("u", "real"))
        for flag in (0, "0", False):
            broker._api = Mock()
            broker._api.is_demo.return_value = flag
            self.assertFalse(await broker.is_demo())
        for flag in (1, "1", True):
            broker._api.is_demo.return_value = flag
            self.assertTrue(await broker.is_demo())