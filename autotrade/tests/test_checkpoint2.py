"""Checkpoint 2: Demo/Real vault isolation, one reused socket per account (no 429 bursts), and Real
sessions (isDemo=0) starting without a mode mismatch. Offline only; no live broker."""
import unittest

from .fakes import Env, ssid_for, wait_until
from ..auth.ssid import build_ssid
from ..errors import BrokerError, RateLimited


class VaultIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = Env(allow_live=True)

    async def asyncTearDown(self):
        await self.env.rt.manager.shutdown()

    async def test_cookie_rebuilt_session_never_replaces_a_genuine_one(self):
        e = self.env
        await e.add_user("u", ("real",))
        genuine = await e.repo.get_credential("u", "real")
        await e.rt.handshakes._sink("u", {"demo": build_ssid("cookie", "demo", uid=3),
                                          "real": build_ssid("cookie", "real", uid=3)},
                                    {"method": "cookie", "derived": ["demo", "real"]})
        self.assertEqual((await e.repo.get_credential("u", "real")).ciphertext, genuine.ciphertext)
        self.assertEqual(await e.rt.vault.get("u", "real"), ssid_for("u", "real"))
        self.assertEqual(await e.rt.vault.get("u", "demo"), build_ssid("cookie", "demo", uid=3))

    async def test_expired_genuine_session_can_be_replaced(self):
        e = self.env
        await e.add_user("u", ("real",))
        await e.rt.vault.mark_expired("u", "real", "test")
        await e.rt.handshakes._sink("u", {"real": build_ssid("cookie", "real", uid=3)},
                                    {"method": "cookie", "derived": ["real"]})
        self.assertEqual(await e.rt.vault.get("u", "real"), build_ssid("cookie", "real", uid=3))

    async def test_expiring_demo_leaves_real_active(self):
        e = self.env
        await e.add_user("u", ("demo", "real"))
        await e.rt.vault.mark_expired("u", "demo", "test")
        status = await e.rt.vault.status("u")
        self.assertEqual((status["demo"]["status"], status["real"]["status"]), ("expired", "active"))


class ConnectionReuseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = Env(allow_live=True)

    async def asyncTearDown(self):
        await self.env.rt.manager.shutdown()

    async def test_start_uses_one_socket_for_check_and_trading(self):
        e = self.env
        await e.add_user("u")
        await e.rt.manager.balance("u")                     # pooled balance read
        await e.rt.manager.start("u")
        w = e.rt.manager._workers["u"]
        self.assertTrue(await wait_until(lambda: w.status.value == "running"))
        self.assertEqual(e.world("u").connects, 1)
        self.assertEqual(e.world("u").closes, 0)
        self.assertNotIn(("u", "demo"), e.rt.manager._balances.entries)

    async def test_rate_limit_pauses_new_sockets(self):
        e = self.env
        await e.add_user("u")
        e.world("u").connect_exc = RateLimited("429 too many requests", retry_after=30)
        first = await e.rt.manager.balance("u")
        second = await e.rt.manager.balance("u")
        self.assertEqual((first["error"], second["error"]), ("rate_limited", "rate_limited"))
        self.assertEqual(e.world("u").connects, 1)
        e.world("u").connect_exc = None
        e.clock.advance(31)
        self.assertEqual((await e.rt.manager.balance("u"))["balance"], 10000.0)

    async def test_background_health_check_does_not_keep_a_socket(self):
        e = self.env
        await e.add_user("u")
        res = await e.rt.health.check("u", "demo")
        self.assertTrue(res.ok)
        self.assertEqual(e.rt.manager._balances.entries, {})
        self.assertEqual(e.world("u").closes, 1)


class RealAccountStartTests(unittest.IsolatedAsyncioTestCase):
    async def start_real(self, report):
        env = Env(allow_live=True)
        self.addAsyncCleanup(env.rt.manager.shutdown)
        await env.add_user("tg:1", ("demo", "real"))
        env.world("tg:1").demo_report = report
        await env.rt.manager.update_settings("tg:1", {"mode": "live"}, confirm_live=True)
        await env.rt.manager.start("tg:1")
        w = env.rt.manager._workers["tg:1"]
        self.assertTrue(await wait_until(lambda: w.status.value == "running"))
        self.assertFalse(w._client_is_demo)
        self.assertIsNone(w.last_error)
        self.assertEqual(w.balance_kind, "real")
        return env

    async def test_real_token_starts_when_library_reports_demo(self):
        await self.start_real(True)

    async def test_real_token_starts_when_library_mode_unavailable(self):
        await self.start_real(BrokerError("broker account mode is unavailable"))

    async def test_real_token_starts_with_correct_report(self):
        env = await self.start_real(None)
        self.assertEqual((await env.rt.manager.balance("tg:1"))["account"], "real")


if __name__ == "__main__":
    unittest.main()
