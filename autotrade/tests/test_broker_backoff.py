import asyncio
import unittest

from ..auth.handshake import (BACKOFF_CAP, BrokerRateLimited, HandshakeManager, LoginCooldown,
                              PlaywrightBackend, backoff_delay)
from .fakes import Env
from .test_handshake_api import FakeBackend, good_tokens


class FakeResponse:
    def __init__(self, status, url="https://pocketoption.com/en/login/", headers=None):
        self.status, self.url, self.headers = status, url, headers or {}


class BackoffMathTests(unittest.TestCase):
    def test_doubles_and_caps(self):
        self.assertEqual([backoff_delay(n) for n in (1, 2, 3)], [60.0, 120.0, 240.0])
        self.assertEqual(backoff_delay(20), BACKOFF_CAP)

    def test_retry_after_wins_when_longer(self):
        self.assertEqual(backoff_delay(1, 300), 300.0)


class DetectionTests(unittest.TestCase):
    def test_broker_429_raises(self):
        b = PlaywrightBackend()
        b._on_response(FakeResponse(429, headers={"retry-after": "90"}))
        with self.assertRaises(BrokerRateLimited) as cm:
            b._check_alive()
        self.assertEqual(cm.exception.retry_after, 90.0)

    def test_non_broker_429_ignored(self):
        b = PlaywrightBackend()
        b._on_response(FakeResponse(429, url="https://www.google.com/recaptcha"))
        b._check_alive()


class CooldownTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = Env()
        self.made, self.notices = [], []
        self.next_error = BrokerRateLimited(None)

        def factory():
            b = FakeBackend(good_tokens(), error=self.next_error)
            self.made.append(b)
            return b
        self.hm = HandshakeManager(self.env.cfg, factory, self.env.rt.handshakes._sink, self.env.clock,
                                   on_failure=lambda u, m: self.notices.append(m))

    async def test_429_message_and_cooldown_blocks_new_browser(self):
        s = await self.hm.start("u1")
        await asyncio.gather(s.task, return_exceptions=True)
        self.assertEqual((s.state, s.error), ("failed", "broker_rate_limited"))
        self.assertIn("limiting sign-ins", self.notices[-1])
        self.assertTrue(self.made[0].closed)
        with self.assertRaises(LoginCooldown):
            await self.hm.start("u1")
        self.assertEqual(len(self.made), 1)

    async def test_success_resets_backoff(self):
        s = await self.hm.start("u1")
        await asyncio.gather(s.task, return_exceptions=True)
        self.hm._cooldown_until.clear(); self.hm._global_until = 0.0
        self.next_error = None
        s = await self.hm.start("u1")
        await asyncio.gather(s.task, return_exceptions=True)
        self.assertEqual(s.state, "done")
        self.assertNotIn("u1", self.hm._failures)
