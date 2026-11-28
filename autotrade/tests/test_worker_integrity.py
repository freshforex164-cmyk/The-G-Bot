"""Transient failures and live account reporting; offline brokers only."""
import unittest
from unittest.mock import AsyncMock, patch

import psycopg

from .fakes import Env, wait_until
from ..storage.repo import BalanceRow
from ..trading.models import Decision, TradeRecord


class IntegrityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.e = Env()
        await self.e.add_user("u")

    async def asyncTearDown(self):
        await self.e.rt.manager.shutdown()

    async def worker(self):
        await self.e.rt.manager.start("u")
        w = self.e.rt.manager._workers["u"]
        self.assertTrue(await wait_until(lambda: w.balance_kind == "demo"))
        return w

    async def test_database_tick_failure_does_not_kill_worker_or_replay_order(self):
        w = await self.worker()
        original = self.e.repo.realized_pnl_since
        with patch.object(self.e.repo, "realized_pnl_since", AsyncMock(
                side_effect=psycopg.OperationalError("offline database interruption"))):
            w.submit_signal(Decision("EURUSD_otc", "CALL", 60, "test", signal_id="first"))
            self.assertTrue(await wait_until(lambda: w.last_error == "OperationalError"))
            self.assertTrue(self.e.rt.manager.is_running("u"))
            self.assertEqual(len(self.e.world("u").placed), 0)
        self.e.repo.realized_pnl_since = original
        w.submit_signal(Decision("EURUSD_otc", "CALL", 60, "test", signal_id="next"))
        self.assertTrue(await wait_until(lambda: len(self.e.world("u").placed) == 1))
        self.assertEqual(self.e.world("u").connects, 1)

    async def test_settings_read_failure_retries_without_ending_task(self):
        original = self.e.repo.get_settings
        await self.e.rt.manager.start("u")
        with patch.object(self.e.repo, "get_settings", AsyncMock(
                side_effect=psycopg.OperationalError("offline settings interruption"))):
            await __import__("asyncio").sleep(0.15)
            self.assertTrue(self.e.rt.manager.is_running("u"))
        self.e.repo.get_settings = original
        self.assertTrue(await wait_until(lambda: self.e.rt.manager._workers["u"].balance_kind == "demo"))

    async def test_failed_balance_never_returns_saved_value_even_with_fresh_false(self):
        await self.e.repo.save_balance(BalanceRow("u", "demo", 777, self.e.clock.utcnow()))
        self.e.world("u").balance_exc = ConnectionError("offline broker")
        data = await self.e.rt.manager.balance("u", fresh=False)
        self.assertIsNone(data["balance"])
        self.assertIsNone(data["at"])
        self.assertFalse(data["fresh"])

    async def test_failed_preorder_balance_skips_instead_of_using_worker_cache(self):
        w = await self.worker()
        self.assertEqual(w.balance, 10000)
        self.e.world("u").balance_exc = ConnectionError("offline broker")
        await w._try_trade(w._broker, Decision("EURUSD_otc", "CALL", 60, "test"), w.settings, (), pushed=True)
        self.assertEqual(len(self.e.world("u").placed), 0)
        self.assertIsNone(w.balance)

    async def test_stats_read_current_account_rows_not_other_users_or_modes(self):
        now = self.e.clock.utcnow()
        for user, mode, profit in (("u", "demo", 2), ("u", "live", 900), ("other", "demo", 700)):
            await self.e.repo.insert_trade(TradeRecord(user, user + mode, mode, "EURUSD_otc", "CALL", 1, 60,
                status="settled", outcome="win", profit=profit, opened_at=now, settled_at=now))
        data = await self.e.rt.manager.stats("u")
        self.assertEqual(data["today"]["trades"], 1)
        self.assertEqual(data["today"]["realized_pnl"], 2)
        self.assertEqual(data["all_time"]["net_profit"], 2)
        self.assertEqual(len(data["recent_trades"]), 1)
        await self.e.repo.insert_trade(TradeRecord("u", "new", "demo", "EURUSD_otc", "CALL", 1, 60,
            status="settled", outcome="loss", profit=-1, opened_at=now, settled_at=now))
        updated = await self.e.rt.manager.stats("u")
        self.assertEqual(updated["today"]["realized_pnl"], 1)
        self.assertEqual(len(updated["recent_trades"]), 2)

    async def test_stats_does_not_return_cached_balance_on_live_read_failure(self):
        await self.worker()
        self.e.world("u").balance_exc = ConnectionError("offline broker")
        data = await self.e.rt.manager.stats("u")
        self.assertIsNone(data["bot"]["balance"])
        self.assertIsNone(data["bot"]["balance_at"])

    async def test_acknowledged_order_is_tracked_despite_database_update_failure(self):
        w = await self.worker()
        original = self.e.repo.update_trade
        with patch.object(self.e.repo, "update_trade", AsyncMock(
                side_effect=psycopg.OperationalError("offline acknowledgement write"))):
            with self.assertRaises(psycopg.OperationalError):
                await w._try_trade(w._broker, Decision("EURUSD_otc", "CALL", 60, "test"), w.settings, (), pushed=True)
            self.assertEqual(len(self.e.world("u").placed), 1)
            self.assertEqual(w.open_count, 1)
        self.e.repo.update_trade = original
        self.assertTrue(await wait_until(lambda: w.open_count == 0))