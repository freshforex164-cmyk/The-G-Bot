"""Runs only when AUTOTRADE_TEST_DATABASE_URL points at a throwaway PostgreSQL database."""
import os
import unittest
from datetime import datetime, timedelta, timezone

from ..auth.ssid import build_ssid
from ..storage.repo import BotState
from ..trading.models import TradeRecord, TradingSettings
from ..vault.crypto import KeyRing
from ..vault.service import TokenVault

DSN = os.environ.get("AUTOTRADE_TEST_DATABASE_URL")


@unittest.skipUnless(DSN, "AUTOTRADE_TEST_DATABASE_URL not set")
class PostgresRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import psycopg
        from ..storage.postgres import PostgresRepository

        async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as c:
            await c.execute("drop table if exists autotrade_credentials, autotrade_settings, autotrade_trades, "
                            "autotrade_bot_state, autotrade_audit cascade")
        self.repo = PostgresRepository(DSN)
        await self.repo.open(apply_schema=True)
        await self.repo.open(apply_schema=False)

    async def asyncTearDown(self):
        await self.repo.close()

    async def test_schema_is_idempotent_and_locked_for_api_roles(self):
        import psycopg
        from pathlib import Path
        sql = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()
        async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as c:
            await c.execute(sql)   # second run must not fail
            cur = await c.execute("select has_table_privilege('anon','autotrade_credentials','select'), "
                                  "has_table_privilege('authenticated','autotrade_trades','select')")
            self.assertEqual(await cur.fetchone(), (False, False))

    async def test_vault_round_trip(self):
        vault = TokenVault(self.repo, KeyRing({1: bytes(32)}, 1))
        ssid = build_ssid("PG-SECRET", "real", uid=4)
        await vault.store("u1", "real", ssid, {"method": "websocket"})
        self.assertEqual(await vault.get("u1", "real"), ssid)
        row = await self.repo.get_credential("u1", "real")
        self.assertNotIn(b"PG-SECRET", row.ciphertext)
        await vault.mark_expired("u1", "real", "rejected")
        self.assertIsNone(await vault.get("u1", "real"))
        due = await self.repo.list_credentials_to_recheck(datetime.now(timezone.utc), 10)
        self.assertEqual(due, [])          # expired rows are not rechecked
        await vault.delete_all("u1")
        self.assertEqual(await self.repo.list_credentials("u1"), [])

    async def test_phase3_settings_round_trip_and_dual_vault(self):
        s = TradingSettings(stake_mode="martingale", martingale_multiplier=1.5, martingale_max_steps=4,
                            base_stake=2.0, route_assets=("UKBrent_otc", "XAUUSD_otc"))
        await self.repo.save_settings("u2", s)
        self.assertEqual(await self.repo.get_settings("u2"), s)
        vault = TokenVault(self.repo, KeyRing({1: bytes(32)}, 1))
        demo, real = build_ssid("D", "demo", uid=4), build_ssid("R", "real", uid=4)
        await vault.store("u2", "demo", demo)
        await vault.store("u2", "real", real)
        await vault.store("u2", "demo", build_ssid("D2", "demo", uid=4))
        self.assertEqual(await vault.get("u2", "real"), real)
        await vault.delete_all("u2")

    async def test_settings_trades_stats_state(self):
        self.assertEqual((await self.repo.get_settings("u1")).mode, "demo")
        await self.repo.save_settings("u1", TradingSettings(stake=3))
        self.assertEqual((await self.repo.get_settings("u1")).stake, 3)
        now = datetime.now(timezone.utc)
        for i, (outcome, profit) in enumerate((("win", 2.5), ("loss", -3.0), ("draw", 0.0))):
            t = TradeRecord("u1", f"r{i}", "demo", "EURUSD_otc", "CALL", 3, 60, opened_at=now, payout=83)
            await self.repo.insert_trade(t)
            await self.repo.update_trade("u1", f"r{i}", status="settled", outcome=outcome, profit=profit,
                                         settled_at=now, broker_trade_id=f"b{i}", not_a_column="ignored")
        await self.repo.insert_trade(TradeRecord("u1", "r9", "demo", "EURUSD_otc", "PUT", 3, 60, opened_at=now))
        self.assertEqual(await self.repo.realized_pnl_since("u1", now - timedelta(hours=1), "demo"), -0.5)
        self.assertEqual(await self.repo.realized_pnl_since("u1", now - timedelta(hours=1), "live"), 0.0)
        st = await self.repo.trade_stats("u1", None)
        self.assertEqual((st["trades"], st["wins"], st["losses"], st["draws"], st["open"], st["win_rate"], st["net_profit"]),
                         (4, 1, 1, 1, 1, 50.0, -0.5))
        self.assertEqual([t.client_ref for t in await self.repo.open_trades("u1")], ["r9"])
        self.assertEqual(len(await self.repo.recent_trades("u1", 2)), 2)
        await self.repo.set_bot_state(BotState("u1", True, "running", None, now))
        self.assertEqual(await self.repo.list_desired_running(), ["u1"])
        await self.repo.audit("u1", "x", {"a": 1})
