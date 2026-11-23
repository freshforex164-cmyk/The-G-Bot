"""Signal -> broker pipeline: confirmed GBot signals are not lost between the scanner and the order.

Rules (from the bottleneck fix): consecutive distinct signals are not blocked by the cooldown; a trade whose
1-minute expiry has passed does not block the next minute's signal; signals arriving during a reconnect are
queued; every auto-trade (Demo and Real) uses a 1-minute expiry; a settlement is threaded under its TRADE
OPENED message; routine skips post nothing, actionable ones post once."""
import asyncio
import io
import json
import unittest
from datetime import timedelta

from ..notify import TelegramNotifier
from ..trading.models import Decision, TradingSettings
from ..trading.worker import TRADE_EXPIRY_SECONDS, WorkerStatus
from .fakes import Env, wait_until


def sig(env, signal_id, asset="EURUSD_otc", direction="CALL", valid_for=15.0):
    return Decision(asset=asset, direction=direction, expiry_seconds=60, reason="UP_PULLBACK_BUY",
                    signal_id=signal_id, valid_until=env.clock.utcnow() + timedelta(seconds=valid_for))


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def start(self, settings=None, user="tg:5", kinds=("demo",), live=False):
        self.env = Env(allow_live=live)
        self.m = self.env.rt.manager
        self.addAsyncCleanup(self.m.shutdown)
        await self.env.add_user(user, kinds)
        await self.env.repo.save_settings(user, settings or TradingSettings())
        if live:
            await self.m.update_settings(user, {"mode": "live"}, confirm_live=True)
        await self.m.start(user)
        self.w = self.m._workers[user]
        self.assertTrue(await wait_until(lambda: self.w.status.value == "running"))
        return self.env.world(user)

    async def test_back_to_back_signals_on_two_assets_are_both_placed_despite_cooldown(self):
        world = await self.start(TradingSettings(cooldown_seconds=60, max_concurrent=2))
        world.gate = asyncio.Event()                  # keep both trades open
        await self.m.submit_signal("tg:5", sig(self.env, "a1", "EURUSD_otc"))
        await self.m.submit_signal("tg:5", sig(self.env, "a2", "XAUUSD_otc", "PUT"))
        self.assertTrue(await wait_until(lambda: len(world.placed) == 2))
        self.assertEqual(self.w.skipped.get("cooldown"), None)
        world.gate.set()

    async def test_expired_but_unsettled_trade_does_not_block_next_minute_signal(self):
        world = await self.start(TradingSettings(max_concurrent=1))
        world.gate = asyncio.Event()                  # the broker has not reported the first result yet
        await self.m.submit_signal("tg:5", sig(self.env, "m1"))
        self.assertTrue(await wait_until(lambda: len(world.placed) == 1))
        await self.m.submit_signal("tg:5", sig(self.env, "m1b", "GBPUSD_otc"))      # same minute: one open
        self.assertTrue(await wait_until(lambda: self.w.last_reject == "max_concurrent"))
        self.env.clock.advance(61)                    # first trade's 1-minute expiry has passed
        await self.m.submit_signal("tg:5", sig(self.env, "m2"))
        self.assertTrue(await wait_until(lambda: len(world.placed) == 2))
        world.gate.set()

    async def test_signal_arriving_while_reconnecting_is_queued_and_placed(self):
        world = await self.start()
        self.w.status = WorkerStatus.RECONNECTING
        result = await self.m.submit_signal("tg:5", sig(self.env, "r1"))
        self.assertEqual(result, {"accepted": True, "reason": "queued"})
        self.w.status = WorkerStatus.RUNNING
        self.w._wake.set()
        self.assertTrue(await wait_until(lambda: len(world.placed) == 1))

    async def test_demo_trade_uses_one_minute_expiry(self):
        world = await self.start()
        await self.m.submit_signal("tg:5", sig(self.env, "d1", direction="PUT"))
        self.assertTrue(await wait_until(lambda: len(world.placed) == 1))
        self.assertEqual(world.placed[0], ("EURUSD_otc", "PUT", 1.0, 60))
        self.assertEqual(TRADE_EXPIRY_SECONDS, 60)

    async def test_real_trade_uses_the_same_one_minute_expiry(self):
        world = await self.start(user="tg:1", kinds=("demo", "real"), live=True)
        self.assertFalse(self.w._client_is_demo)
        await self.m.submit_signal("tg:1", sig(self.env, "l1"))
        self.assertTrue(await wait_until(lambda: len(world.placed) == 1))
        self.assertEqual(world.placed[0][3], 60)
        self.assertEqual(self.env.repo.trades[0].mode, "live")

    async def test_settlement_is_threaded_under_its_opened_message(self):
        await self.start()
        await self.m.submit_signal("tg:5", sig(self.env, "t1"))
        self.assertTrue(await wait_until(lambda: len(self.env.alerts.sent) >= 2))
        (_, opened), (_, settled) = self.env.alerts.sent[:2]
        self.assertIn("TRADE OPENED", opened)
        self.assertIn("✅ WIN", settled)
        (key, _), (_, reply_to) = self.env.alerts.threads[:2]
        self.assertTrue(key)
        self.assertEqual(reply_to, key)
        self.assertEqual(key, self.env.repo.trades[0].client_ref)

    async def test_low_balance_is_told_once_not_for_every_signal(self):
        await self.start(TradingSettings(stake=50.0, daily_stop_loss=100.0, daily_take_profit=200.0))

        async def low(_broker):
            self.w.balance = 10.0
            return 10.0
        self.w._refresh_balance = low
        for i in range(3):
            await self.m.submit_signal("tg:5", sig(self.env, f"b{i}", ["EURUSD_otc", "XAUUSD_otc", "AUDCAD_otc"][i]))
        self.assertTrue(await wait_until(lambda: self.w.skipped.get("insufficient_balance") == 3))
        self.assertEqual(sum("Low balance" in t for _, t in self.env.alerts.sent), 1)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TelegramThreadingTests(unittest.IsolatedAsyncioTestCase):
    async def test_settled_alert_replies_to_the_opened_message(self):
        bodies = []

        def opener(req, timeout):
            bodies.append(json.loads(req.data))
            return _Resp(json.dumps({"ok": True, "result": {"message_id": 100 + len(bodies)}}).encode())
        n = TelegramNotifier("t", opener=opener, sleep=lambda s: None)
        n.send("tg:1", "opened", key="ref1")
        n.send("tg:1", "settled", reply_to="ref1")
        await n.drain()
        self.assertNotIn("reply_to_message_id", bodies[0])
        self.assertEqual((bodies[1]["reply_to_message_id"], bodies[1]["allow_sending_without_reply"]), (101, True))
        self.assertEqual(n._message_ids, {})          # thread closed after settlement


if __name__ == "__main__":
    unittest.main()
