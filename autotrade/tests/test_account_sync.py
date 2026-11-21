"""Login connects the new session right away; balances always come from the user's own account."""
import asyncio
import unittest

from .fakes import Env, ssid_for


class AccountSyncTests(unittest.TestCase):
    def test_login_connects_each_new_session_and_saves_its_balance(self):
        env = Env()

        async def go():
            await env.rt.handshakes._sink("u1", {"demo": ssid_for("u1", "demo")}, {"method": "form"})
            return await env.repo.get_balances("u1")
        balances = asyncio.run(go())
        self.assertEqual(env.world("u1").connects, 1)
        self.assertEqual(balances["demo"].balance, 10000.0)

    def test_rejected_session_after_login_does_not_fail_login(self):
        env = Env()
        env.world("u1").reject = True

        async def go():
            await env.rt.handshakes._sink("u1", {"demo": ssid_for("u1", "demo")}, {"method": "form"})
            return await env.rt.vault.status("u1")
        status = asyncio.run(go())
        self.assertEqual(status["demo"]["status"], "expired")

    def test_balance_uses_the_users_session_not_the_feed_account(self):
        env = Env()

        async def go():
            await env.add_user("u1")
            return await env.rt.manager.balance("u1")
        r = asyncio.run(go())
        self.assertEqual(r["account"], "demo")
        self.assertEqual(r["balance"], 10000.0)
        self.assertEqual(env.world("u1").connects, 1)
        self.assertEqual(env.world("feed").connects, 0)
