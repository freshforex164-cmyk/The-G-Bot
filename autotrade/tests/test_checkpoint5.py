"""Checkpoint 5: real-time Telegram trade tracking (opened, settled, daily summary) delivered without
blocking trading. Offline only; no Telegram or broker traffic."""
import asyncio
import io
import json
import threading
import unittest
import urllib.error

from .fakes import Env, wait_until
from .test_checkpoint3 import MG
from .test_signal_follow import gbot_signal
from ..notify import TelegramNotifier, daily_summary_text, opened_text, settled_text
from ..trading.models import TradingSettings


class FormatTests(unittest.TestCase):
    def test_opened(self):
        t = opened_text(mode="live", asset="Gold OTC", direction="PUT", setup="downtrend_pullback", stake=2,
                        payout=92, expiry_seconds=60, step=1)
        for part in ("TRADE OPENED · REAL", "Asset: Gold OTC", "Direction: 🔴 SELL",
                     "Stake: $2.00 (Martingale step 1)", "Payout: 92%", "Expiry: 1m"):
            self.assertIn(part, t)

    def test_settled_win_and_loss(self):
        win = settled_text(mode="demo", asset="Brent Oil OTC", direction="CALL", stake=1, outcome="win",
                           profit=0.92, balance=10500.5, win_streak=3, step=0, martingale=True)
        for part in ("✅ WIN · DEMO", "Net P/L: +$0.92", "Balance: $10,500.50", "Win streak: 3", "Next step: 0"):
            self.assertIn(part, win)
        loss = settled_text(mode="live", asset="X", direction="PUT", stake=4, outcome="loss", profit=-4,
                            balance=None, win_streak=0)
        self.assertIn("❌ LOSS · REAL", loss)
        self.assertIn("Net P/L: -$4.00", loss)
        self.assertIn("Balance: unavailable", loss)
        self.assertNotIn("Next step", loss)

    def test_daily_summary(self):
        t = daily_summary_text(mode="demo", day="2026-11-20", trades=5, wins=3, losses=2, draws=0,
                               win_rate=60.0, pnl=0.76)
        for part in ("DAILY SUMMARY · DEMO · 2026-11-20", "Total trades: 5", "Win rate: 60.0% (3W / 2L / 0D)",
                     "Realized P/L: +$0.76"):
            self.assertIn(part, t)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TelegramDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_returns_immediately_and_keeps_order(self):
        gate, seen = threading.Event(), []

        def opener(req, timeout):
            gate.wait(2)
            seen.append(json.loads(req.data)["text"])
            return _Resp(b"{}")
        n = TelegramNotifier("t", opener=opener, sleep=lambda s: None)
        loop = asyncio.get_running_loop()
        start = loop.time()
        for i in range(3):
            n.send("tg:1", f"m{i}")
        self.assertLess(loop.time() - start, 0.05)      # never waits for Telegram
        gate.set()
        await n.drain()
        self.assertEqual(seen, ["m0", "m1", "m2"])
        self.assertEqual((n._queues, n._draining), ({}, {}))   # nothing idle left behind

    async def test_rate_limited_alert_is_retried(self):
        calls = []

        def opener(req, timeout):
            calls.append(1)
            if len(calls) == 1:
                raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {},
                                             io.BytesIO(b'{"parameters":{"retry_after":1}}'))
            return _Resp(b"{}")
        n = TelegramNotifier("t", opener=opener, sleep=lambda s: None)
        n.send("tg:1", "hello")
        await n.drain()
        self.assertEqual(len(calls), 2)

    async def test_queue_is_bounded(self):
        n = TelegramNotifier("t", opener=lambda r, timeout: _Resp(b"{}"), sleep=lambda s: None)
        for i in range(TelegramNotifier.MAX_QUEUE + 10):
            n.send("tg:2", str(i))
        self.assertEqual(len(n._queues[2]), TelegramNotifier.MAX_QUEUE)
        await n.drain()


class WorkerTrackingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = Env()
        self.m = self.env.rt.manager
        await self.env.add_user("tg:5")

    async def asyncTearDown(self):
        await self.m.shutdown()

    async def start(self, settings):
        await self.env.repo.save_settings("tg:5", settings)
        await self.m.start("tg:5")
        self.assertTrue(await wait_until(lambda: self.m._workers["tg:5"].status.value == "running"))

    async def trade(self, n, result):
        w = self.env.world("tg:5")
        w.results.append(result)
        await self.m.submit_signal("tg:5", gbot_signal(self.env, f"s{n}"))
        ok = await wait_until(lambda: len(w.placed) == n)
        self.assertTrue(ok, (self.m._workers["tg:5"].last_reject, self.m._workers["tg:5"].status, len(w.placed)))
        self.assertTrue(await wait_until(lambda: not self.m._workers["tg:5"]._trackers))
        self.env.clock.advance(6)

    def texts(self, marker):
        return [t for _, t in self.env.alerts.sent if marker in t]

    async def test_settled_alert_has_streak_and_step(self):
        await self.start(MG.with_changes(cooldown_seconds=5, max_concurrent=1, daily_stop_loss=20.0))
        await self.trade(1, "win")
        await self.trade(2, "loss")
        settled = self.texts("Net P/L")
        self.assertIn("Win streak: 1", settled[0])
        self.assertIn("Net P/L: -$1.00", settled[1])
        self.assertIn("Next step: 1", settled[1])
        await self.trade(3, "win")
        self.assertIn("Stake: $2.00 (Martingale step 1)", self.texts("TRADE OPENED")[2])

    async def test_daily_summary_after_daily_limit_and_new_day(self):
        await self.start(TradingSettings(cooldown_seconds=5, max_concurrent=1, daily_stop_loss=2.0))
        await self.trade(1, "loss")
        await self.trade(2, "loss")                                  # -2: stop-loss reached
        await self.m.submit_signal("tg:5", gbot_signal(self.env, "blocked"))
        self.assertTrue(await wait_until(lambda: self.texts("DAILY SUMMARY")))
        summary = self.texts("DAILY SUMMARY")[0]
        self.assertIn("Total trades: 2", summary)
        self.assertIn("Win rate: 0.0% (0W / 2L / 0D)", summary)
        self.assertIn("Realized P/L: -$2.00", summary)
        self.env.clock.advance(24 * 3600)                           # next UTC day: first result closes yesterday
        await self.trade(3, "win")
        self.assertTrue(await wait_until(lambda: len(self.texts("DAILY SUMMARY")) == 2))
        day_end = self.texts("DAILY SUMMARY")[1]
        self.assertIn("2026-11-20", day_end)
        self.assertIn("Total trades: 2", day_end)


if __name__ == "__main__":
    unittest.main()
