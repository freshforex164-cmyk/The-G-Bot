import asyncio
import unittest

from ..errors import BrokerError, ReauthRequired
from ..trading.manager import CapacityError, LiveDisabled, TradingHalted
from ..trading.models import TradingSettings
from .fakes import Env, wait_async, wait_until
from .fakes import ScriptedDecision
from unittest.mock import patch


class EngineBase(unittest.IsolatedAsyncioTestCase):
    allow_live = False

    async def asyncSetUp(self):
        strategy_patch = patch.dict("autotrade.trading.strategy.STRATEGIES", {"price_action": ScriptedDecision})
        strategy_patch.start()
        self.addCleanup(strategy_patch.stop)
        self.env = Env(allow_live=self.allow_live, engine_mode=True)
        self.m = self.env.rt.manager
        self.repo = self.env.repo

    async def asyncTearDown(self):
        await self.m.shutdown()

    async def trades(self, user):
        return [t for t in self.repo.trades if t.user_id == user]

    async def run_fast(self, user, **settings):
        base = TradingSettings(cooldown_seconds=5, **settings)
        await self.repo.save_settings(user, base)


class SharedFeedTests(EngineBase):
    async def test_one_stream_per_symbol_for_many_users(self):
        for u in ("a", "b", "c"):
            await self.env.add_user(u)
            await self.m.start(u)
        await wait_until(lambda: self.env.bus.streams >= 2)
        await asyncio.sleep(0.1)
        self.assertEqual(self.env.bus.streams, 2)              # EURUSD + EURUSD_otc, not 6
        self.assertEqual(set(self.env.rt.feed.status()["symbols"]), {"EURUSD", "EURUSD_otc"})

    async def test_stream_stops_when_last_user_stops(self):
        await self.env.add_user("a")
        await self.m.start("a")
        await wait_until(lambda: self.env.bus.streams >= 2)
        await self.m.stop("a")
        self.assertEqual(self.env.rt.feed.status()["symbols"], [])


class TradingFlowTests(EngineBase):
    async def test_gap_signal_places_demo_trade_and_logs_result(self):
        await self.env.add_user("a")
        await self.run_fast("a")
        await self.m.start("a")
        await wait_until(lambda: self.env.bus.streams >= 2)
        self.env.tick_gap(0.0020)
        self.assertTrue(await wait_until(lambda: self.env.world("a").placed))
        self.assertEqual(self.env.world("a").placed[0], ("EURUSD_otc", "CALL", 1.0, 60))
        ok = await wait_until(lambda: any(t.status == "settled" for t in self.repo.trades))
        self.assertTrue(ok)
        t = self.repo.trades[0]
        self.assertEqual((t.mode, t.outcome, t.profit, t.payout), ("demo", "win", 0.85, 85))

    async def test_put_when_otc_above_real(self):
        await self.env.add_user("a")
        await self.m.start("a")
        await wait_until(lambda: self.env.bus.streams >= 2)
        self.env.tick_gap(-0.0020)
        await wait_until(lambda: self.env.world("a").placed)
        self.assertEqual(self.env.world("a").placed[0][1], "PUT")

    async def test_cooldown_blocks_second_order_until_time_passes(self):
        await self.env.add_user("a")
        await self.m.start("a")
        await wait_until(lambda: self.env.bus.streams >= 2)
        self.env.tick_gap(0.002)
        await wait_until(lambda: self.env.world("a").placed)
        self.env.tick_gap(0.002)
        await asyncio.sleep(0.2)
        self.assertEqual(len(self.env.world("a").placed), 1)
        self.env.clock.advance(61)
        self.env.tick_gap(0.002)
        self.assertTrue(await wait_until(lambda: len(self.env.world("a").placed) == 2))

    async def test_low_payout_blocks_trade(self):
        await self.env.add_user("a")
        self.env.world("a").payout = 70
        await self.m.start("a")
        await wait_until(lambda: self.env.bus.streams >= 2)
        self.env.tick_gap(0.002)
        await asyncio.sleep(0.2)
        self.assertEqual(self.env.world("a").placed, [])
        self.assertEqual((await self.m.stats("a"))["bot"]["last_skip_reason"], "payout_below_minimum")

    async def test_stale_prices_block_trade(self):
        await self.env.add_user("a")
        await self.m.start("a")
        await wait_until(lambda: self.env.bus.streams >= 2)
        self.env.tick_gap(0.0)            # tick data exists but no gap
        await asyncio.sleep(0.1)
        self.env.clock.advance(10)        # then the feed goes quiet
        self.env.bus.push("EURUSD", 1.0820)   # only one side refreshed
        await asyncio.sleep(0.2)
        self.assertEqual(self.env.world("a").placed, [])

    async def test_daily_stop_loss_pauses_until_next_day(self):
        await self.env.add_user("a")
        await self.repo.save_settings("a", TradingSettings(stake=2, daily_stop_loss=4, cooldown_seconds=5))
        w = self.env.world("a")
        w.default_result = "loss"
        await self.m.start("a")
        await wait_until(lambda: self.env.bus.streams >= 2)
        for _ in range(2):                                   # two losses of 2 = -4 = stop loss
            self.env.tick_gap(0.002)
            await wait_until(lambda n=len(w.placed): len(w.placed) > n)
            await wait_async(lambda: self._settled(2 if len(w.placed) == 2 else 1))
            self.env.clock.advance(6)
        self.env.tick_gap(0.002)
        await asyncio.sleep(0.2)
        self.assertEqual(len(w.placed), 2)
        st = await self.m.stats("a")
        self.assertEqual(st["bot"]["status"], "paused_daily_limit")
        self.assertEqual(st["today"]["realized_pnl"], -4.0)
        self.env.clock.advance(24 * 3600)                    # next UTC day: limit resets
        self.env.tick_gap(0.002)
        self.assertTrue(await wait_until(lambda: len(w.placed) == 3))

    async def _settled(self, n):
        return sum(1 for t in self.repo.trades if t.status == "settled") >= n

    async def test_max_concurrent_one(self):
        await self.env.add_user("a")
        await self.repo.save_settings("a", TradingSettings(cooldown_seconds=5, max_concurrent=1))
        w = self.env.world("a")
        w.gate = asyncio.Event()                                 # first trade stays open
        await self.m.start("a")
        await wait_until(lambda: self.env.bus.streams >= 2)
        self.env.tick_gap(0.002)
        await wait_until(lambda: w.placed)
        self.env.clock.advance(6)                                # cooldown over
        self.env.tick_gap(0.002)
        await asyncio.sleep(0.2)
        self.assertEqual(len(w.placed), 1)
        self.assertEqual((await self.m.stats("a"))["bot"]["last_skip_reason"], "max_concurrent")
        w.gate.set()                                             # first trade settles
        await wait_until(lambda: any(t.status == "settled" for t in self.repo.trades))
        self.env.tick_gap(0.002)
        self.assertTrue(await wait_until(lambda: len(w.placed) == 2))

    async def test_stop_waits_for_open_trade_result(self):
        await self.env.add_user("a")
        w = self.env.world("a")
        w.gate = asyncio.Event()
        await self.m.start("a")
        await wait_until(lambda: self.env.bus.streams >= 2)
        self.env.tick_gap(0.002)
        await wait_until(lambda: w.placed)
        stopping = asyncio.create_task(self.m.stop("a"))
        await wait_until(lambda: self.m._workers["a"].status.value == "draining")
        w.gate.set()
        await stopping
        self.assertEqual(self.repo.trades[0].status, "settled")
        self.assertFalse(self.m.is_running("a"))
        self.assertFalse((await self.repo.get_bot_state("a")).desired_running)


class IsolationAndErrorTests(EngineBase):
    async def test_one_users_rejected_session_does_not_stop_another(self):
        for u in ("a", "b"):
            await self.env.add_user(u)
            await self.m.start(u)
        await wait_until(lambda: self.env.bus.streams >= 2)
        from ..errors import SessionExpired
        self.env.world("a").place_exc = SessionExpired("kicked out")
        self.env.tick_gap(0.002)
        ok = await wait_until(lambda: self.m._workers["a"].status.value == "reauth_required")
        self.assertTrue(ok)
        self.assertEqual((await self.env.rt.vault.status("a"))["demo"]["status"], "expired")
        self.assertTrue(self.m.is_running("b"))
        self.assertTrue(await wait_until(lambda: self.env.world("b").placed))

    async def test_expired_token_at_start_requires_reauth_and_never_starts(self):
        await self.env.add_user("a")
        self.env.world("a").reject = True
        with self.assertRaises(ReauthRequired) as ctx:
            await self.m.start("a")
        self.assertEqual(ctx.exception.kind, "demo")
        self.assertFalse(self.m.is_running("a"))

    async def test_missing_token_requires_reauth(self):
        with self.assertRaises(ReauthRequired):
            await self.m.start("nobody")

    async def test_unreachable_broker_does_not_expire_token(self):
        from ..trading.manager import ServiceUnavailable
        await self.env.add_user("a")
        self.env.world("a").connect_failures = 1
        with self.assertRaises(ServiceUnavailable):
            await self.m.start("a")
        self.assertEqual((await self.env.rt.vault.status("a"))["demo"]["status"], "active")

    async def test_disconnect_then_reconnect(self):
        await self.env.add_user("a")
        made = []
        orig = self.m._factory

        def spy(ssid):
            b = orig(ssid)
            made.append(b)
            return b
        self.m._factory = spy
        await self.m.start("a")
        worker = self.m._workers["a"]
        await wait_until(lambda: worker.status.value == "running")
        w = self.env.world("a")
        before = w.connects
        w.connect_failures = 2
        made[-1]._open = False                                   # live connection drops
        self.assertTrue(await wait_until(lambda: w.connects >= before + 3, timeout=5))
        self.assertTrue(await wait_until(lambda: worker.status.value == "running"))
        self.assertEqual((await self.env.rt.vault.status("a"))["demo"]["status"], "active")

    async def test_order_error_does_not_retry_and_is_logged(self):
        await self.env.add_user("a")
        self.env.world("a").place_exc = BrokerError("asset closed")
        await self.m.start("a")
        await wait_until(lambda: self.env.bus.streams >= 2)
        self.env.tick_gap(0.002)
        await wait_until(lambda: any(t.status == "failed" for t in self.repo.trades))
        self.assertEqual(len(self.repo.trades), 1)
        self.assertTrue(self.m.is_running("a"))

    async def test_disconnect_during_order_marks_unknown_not_retried(self):
        from ..errors import Disconnected
        await self.env.add_user("a")
        self.env.world("a").place_exc = Disconnected("socket closed")
        await self.m.start("a")
        await wait_until(lambda: self.env.bus.streams >= 2)
        self.env.tick_gap(0.002)
        await wait_until(lambda: any(t.status == "unknown" for t in self.repo.trades))
        self.assertEqual(len(self.repo.trades), 1)
        self.assertEqual(self.env.world("a").placed, [])

    async def test_halt_blocks_start_and_orders(self):
        await self.env.add_user("a")
        self.m.halted = True
        with self.assertRaises(TradingHalted):
            await self.m.start("a")

    async def test_capacity_limit(self):
        self.env.cfg = type(self.env.cfg)(**{**self.env.cfg.__dict__, "max_workers": 1})
        self.m.cfg = self.env.cfg
        for u in ("a", "b"):
            await self.env.add_user(u)
        await self.m.start("a")
        with self.assertRaises(CapacityError):
            await self.m.start("b")


class ModeTests(EngineBase):
    async def test_new_user_defaults_to_demo(self):
        self.assertEqual((await self.repo.get_settings("new")).mode, "demo")

    async def test_live_blocked_when_service_disallows(self):
        await self.env.add_user("a", kinds=("demo", "real"))
        with self.assertRaises(LiveDisabled):
            await self.m.update_settings("a", {"mode": "live"}, confirm_live=True)

    async def test_live_needs_confirm_and_real_session(self):
        self.env.cfg = type(self.env.cfg)(**{**self.env.cfg.__dict__, "allow_live": True})
        self.m.cfg = self.env.cfg
        await self.env.add_user("a")                          # demo only
        from ..errors import SettingsError
        with self.assertRaises(SettingsError):
            await self.m.update_settings("a", {"mode": "live"}, confirm_live=False)
        with self.assertRaises(ReauthRequired):
            await self.m.update_settings("a", {"mode": "live"}, confirm_live=True)
        await self.env.add_user("a", kinds=("real",))
        s = await self.m.update_settings("a", {"mode": "live", "stake": 5}, confirm_live=True)
        self.assertEqual((s.mode, s.live_confirmed), ("live", True))

    async def test_live_worker_uses_real_ssid_and_back_to_demo_is_free(self):
        self.env.cfg = type(self.env.cfg)(**{**self.env.cfg.__dict__, "allow_live": True})
        self.m.cfg = self.env.cfg
        await self.env.add_user("a", kinds=("demo", "real"))
        await self.m.update_settings("a", {"mode": "live"}, confirm_live=True)
        await self.m.start("a")
        await wait_until(lambda: self.env.bus.streams >= 2)
        self.env.tick_gap(0.002)
        await wait_until(lambda: self.repo.trades)
        self.assertEqual(self.repo.trades[0].mode, "live")
        s = await self.m.update_settings("a", {"mode": "demo"})
        self.assertEqual((s.mode, s.live_confirmed), ("demo", False))

    async def test_new_login_resets_live_to_demo(self):
        self.env.cfg = type(self.env.cfg)(**{**self.env.cfg.__dict__, "allow_live": True})
        self.m.cfg = self.env.cfg
        await self.env.add_user("a", kinds=("demo", "real"))
        await self.m.update_settings("a", {"mode": "live"}, confirm_live=True)
        await self.m.reset_to_demo("a", "new login")
        self.assertEqual((await self.repo.get_settings("a")).mode, "demo")

    async def test_settings_validation_blocks_bad_patch_and_keeps_old(self):
        from ..errors import SettingsError
        await self.m.update_settings("a", {"stake": 3})
        with self.assertRaises(SettingsError):
            await self.m.update_settings("a", {"stake": 500})
        self.assertEqual((await self.repo.get_settings("a")).stake, 3)
