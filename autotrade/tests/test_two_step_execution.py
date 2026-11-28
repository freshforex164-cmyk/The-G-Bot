"""Exact requested execution rules, with offline brokers; never place real orders."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from .fakes import Env, FakeBroker, wait_until
from .test_signal_follow import gbot_signal
from ..errors import SettingsError
from ..trading.models import TradingSettings


class QualificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_account_modes_require_twenty_dollars_on_selection_and_start(self):
        for mode, kind in (("demo", "demo"), ("live", "real")):
            e = Env(allow_live=True)
            await e.add_user("u", kinds=(kind,))
            await e.repo.save_settings("u", TradingSettings(mode=mode, live_confirmed=mode == "live"))
            try:
                with patch.object(FakeBroker, "balance", AsyncMock(return_value=19.99)):
                    with self.assertRaisesRegex(SettingsError, "at least \\$20"):
                        await e.rt.manager.update_settings("u", {"stake_mode": "martingale"})
                    self.assertEqual((await e.repo.get_settings("u")).stake_mode, "fixed")
                    await e.repo.save_settings("u", TradingSettings(mode=mode, live_confirmed=mode == "live",
                                                                   stake_mode="martingale"))
                    with self.assertRaisesRegex(SettingsError, "at least \\$20"):
                        await e.rt.manager.start("u")
                    self.assertFalse(e.rt.manager.is_running("u"))
                with patch.object(FakeBroker, "balance", AsyncMock(return_value=20.0)):
                    await e.repo.save_settings("u", TradingSettings(mode=mode, live_confirmed=mode == "live"))
                    saved = await e.rt.manager.update_settings("u", {"stake_mode": "martingale"})
                    self.assertEqual(saved.stake_mode, "martingale")
            finally:
                await e.rt.manager.shutdown()

    async def test_unavailable_balance_never_qualifies(self):
        e = Env()
        await e.add_user("u")
        try:
            with patch.object(FakeBroker, "balance", AsyncMock(return_value=None)):
                with self.assertRaisesRegex(SettingsError, "Cannot verify"):
                    await e.rt.manager.update_settings("u", {"stake_mode": "martingale"})
        finally:
            await e.rt.manager.shutdown()

    def test_saved_ladders_cannot_exceed_two_recoveries_or_double_multiplier(self):
        s = TradingSettings.from_dict({"stake_mode": "martingale", "stake": 1,
                                      "martingale_multiplier": 5, "martingale_max_steps": 6})
        self.assertEqual((s.martingale_multiplier, s.martingale_max_steps), (2, 2))
        self.assertEqual([s.ladder_stake(n) for n in (0, 1, 2, 3, 100)], [1, 2, 4, 4, 4])


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_demo_and_real_same_direction_recovery_reset_and_fixed_stake(self):
        for mode, kind in (("demo", "demo"), ("live", "real")):
            for final in ("win", "loss"):
                e = Env(allow_live=True)
                await e.add_user("u", kinds=(kind,))
                await e.repo.save_settings("u", TradingSettings(mode=mode, live_confirmed=mode == "live",
                                                               stake_mode="martingale"))
                world = e.world("u")
                world.results.extend(["loss", "loss", final, "win"])
                try:
                    await e.rt.manager.start("u")
                    w = e.rt.manager._workers["u"]
                    self.assertTrue(await wait_until(lambda: w.balance is not None))
                    d = gbot_signal(e, signal_id="first")
                    await e.rt.manager.submit_signal("u", d)
                    self.assertTrue(await wait_until(lambda: len(world.placed) == 3 and not w._martingale_active))
                    self.assertEqual([p[2] for p in world.placed], [1, 2, 4])
                    self.assertEqual([(p[0], p[1]) for p in world.placed], [(d.asset, d.direction)] * 3)
                    self.assertEqual(w.stakes.step, 0)
                    await e.rt.manager.submit_signal("u", gbot_signal(e, signal_id="second"))
                    self.assertTrue(await wait_until(lambda: len(world.placed) == 4 and not w.open_count))
                    self.assertEqual(world.placed[-1][2], 1)
                    await e.rt.manager.update_settings("u", {"stake_mode": "fixed", "stake": 2})
                    world.results.extend(["loss", "win"])
                    for i in range(2):
                        await e.rt.manager.submit_signal("u", gbot_signal(e, signal_id=f"fixed{i}"))
                        self.assertTrue(await wait_until(lambda: len(world.placed) == 5+i and not w.open_count))
                    self.assertEqual([p[2] for p in world.placed[-2:]], [2, 2])
                finally:
                    await e.rt.manager.shutdown()

    async def test_independent_signal_cannot_join_an_active_recovery_chain(self):
        e = Env()
        await e.add_user("u")
        await e.repo.save_settings("u", TradingSettings(stake_mode="martingale", max_concurrent=5))
        world = e.world("u")
        world.gate = asyncio.Event()
        world.results.extend(["loss", "loss", "loss"])
        try:
            await e.rt.manager.start("u")
            w = e.rt.manager._workers["u"]
            await e.rt.manager.submit_signal("u", gbot_signal(e, signal_id="first"))
            self.assertTrue(await wait_until(lambda: len(world.placed) == 1))
            await e.rt.manager.submit_signal("u", gbot_signal(e, signal_id="unrelated", asset="XAUUSD_otc"))
            self.assertTrue(await wait_until(lambda: w.last_reject == "martingale_in_progress"))
            self.assertEqual(len(world.placed), 1)
            world.gate.set()
            self.assertTrue(await wait_until(lambda: len(world.placed) == 3 and not w._martingale_active))
            self.assertEqual([p[2] for p in world.placed], [1, 2, 4])
        finally:
            world.gate.set()
            await e.rt.manager.shutdown()