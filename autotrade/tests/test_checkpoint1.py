"""Checkpoint 1: Render memory limits, login challenge reporting, no direct SSID login, expired-session
teardown with an /at_login prompt. Offline only; no live broker."""
import asyncio
import re
import unittest
from pathlib import Path

from .fakes import Env, wait_until
from ..auth.handshake import (CHALLENGE_HINTS, ERROR_MESSAGES, ChallengeBlocked, HandshakeManager,
                              classify_challenge)
from ..clock import Clock
from ..config import Config
from ..errors import SessionExpired

ROOT = Path(__file__).resolve().parents[2]


class ChallengeClassifierTests(unittest.TestCase):
    def test_cloudflare_turnstile_iframe(self):
        sig = {"frames": ["https://challenges.cloudflare.com/cdn-cgi/challenge-platform/turnstile/if/ov2"]}
        self.assertEqual(classify_challenge(sig), "turnstile")

    def test_cloudflare_interstitial_title(self):
        self.assertEqual(classify_challenge({"title": "Just a moment..."}), "turnstile")

    def test_invisible_recaptcha_badge_is_not_a_challenge(self):
        sig = {"frames": ["https://www.google.com/recaptcha/api2/anchor?k=x&size=invisible"]}
        self.assertIsNone(classify_challenge(sig))

    def test_visible_recaptcha_is_a_captcha(self):
        sig = {"frames": ["https://www.google.com/recaptcha/api2/bframe?k=x"]}
        self.assertEqual(classify_challenge(sig), "captcha")

    def test_two_factor_prompt(self):
        self.assertEqual(classify_challenge({"otp": True}), "two_factor")
        self.assertEqual(classify_challenge({"text": "Enter the verification code we emailed"}), "two_factor")

    def test_plain_login_page(self):
        self.assertIsNone(classify_challenge({"frames": [], "title": "Login", "text": "Email Password Sign in"}))
        self.assertIsNone(classify_challenge(None))


class _Backend:
    def __init__(self, exc):
        self.exc, self.closed = exc, False

    async def open(self):
        pass

    async def capture_tokens(self, timeout, on_status):
        raise self.exc

    async def close(self):
        self.closed = True


class HandshakeFailureReportingTests(unittest.IsolatedAsyncioTestCase):
    async def run_login(self, exc):
        notices = []
        backend = _Backend(exc)

        async def sink(*a):
            raise AssertionError("nothing may be stored")
        m = HandshakeManager(Config(jwt_secret="x" * 40), lambda: backend, sink, Clock(),
                             on_failure=lambda uid, text: notices.append((uid, text)))
        s = await m.start("tg:5")
        await asyncio.gather(s.task, return_exceptions=True)
        self.assertTrue(backend.closed)
        return s, notices

    async def test_turnstile_timeout_gives_actionable_guidance(self):
        s, notices = await self.run_login(ChallengeBlocked("turnstile"))
        self.assertEqual((s.state, s.error, s.challenge), ("timeout", "turnstile_blocked", "turnstile"))
        self.assertIn("Cloudflare", s.message)
        self.assertEqual(notices, [("tg:5", f"🔐 Pocket Option login: {ERROR_MESSAGES['turnstile_blocked']}")])

    async def test_captcha_and_2fa_have_their_own_messages(self):
        s, _ = await self.run_login(ChallengeBlocked("captcha"))
        self.assertEqual(s.error, "captcha_required")
        self.assertIn("CAPTCHA", s.message)
        s, _ = await self.run_login(ChallengeBlocked("two_factor"))
        self.assertEqual(s.error, "two_factor_required")
        self.assertIn("2FA", s.message)

    async def test_plain_timeout_still_reported(self):
        s, notices = await self.run_login(TimeoutError("slow"))
        self.assertEqual((s.state, s.error), ("timeout", "login_timeout"))
        self.assertEqual(len(notices), 1)
        self.assertIn("/at_login", notices[0][1])

    def test_every_challenge_has_a_hint(self):
        self.assertEqual(set(CHALLENGE_HINTS), {"turnstile", "captcha", "two_factor"})


class RenderMemoryTests(unittest.TestCase):
    def test_no_heap_flag_above_512_mb(self):
        for name in ("render.yaml", "autotrade/Dockerfile", "autotrade/auth/handshake.py"):
            for size in re.findall(r"max-old-space-size=(\d+)", (ROOT / name).read_text()):
                self.assertLessEqual(int(size), 256, name)

    def test_render_limits_malloc_arenas(self):
        text = (ROOT / "render.yaml").read_text()
        self.assertRegex(text, r"key: MALLOC_ARENA_MAX\s+value: \"2\"")
        self.assertIn("MALLOC_ARENA_MAX=2", (ROOT / "autotrade/Dockerfile").read_text())


class NoDirectSsidTests(unittest.TestCase):
    def test_no_direct_ssid_code_paths(self):
        for name in ("autotrade/api/app.py", "autotrade/trading/manager.py", "autotrade_client.py",
                     "autotrade_commands.py", "telegram_core.py"):
            text = (ROOT / name).read_text()
            for marker in ("/auth/ssid", "store_direct_ssid", "submit_ssid", "at:ssid", '"at_ssid"'):
                self.assertNotIn(marker, text, f"{marker} in {name}")


class ExpiredSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = Env()

    async def asyncTearDown(self):
        await self.env.rt.manager.shutdown()

    async def test_rejected_session_closes_socket_and_prompts_login_once(self):
        e = self.env
        await e.add_user("tg:9")
        self.assertEqual((await e.rt.manager.balance("tg:9"))["balance"], 10000.0)
        e.world("tg:9").balance_exc = SessionExpired("broker rejected the session")
        res = await e.rt.manager.balance("tg:9")
        self.assertEqual(res["error"], "session_expired")
        self.assertNotIn(("tg:9", "demo"), e.rt.manager._balances.entries)
        self.assertEqual(e.world("tg:9").closes, 1)
        self.assertEqual((await e.rt.vault.status("tg:9"))["demo"]["status"], "expired")
        prompts = [t for u, t in e.alerts.sent if u == "tg:9" and "/at_login" in t]
        self.assertEqual(len(prompts), 1)
        self.assertIn("Demo session has expired", prompts[0])
        await e.rt.manager.balance("tg:9")              # no session left: no second prompt
        self.assertEqual(len([t for u, t in e.alerts.sent if "/at_login" in t]), 1)

    async def test_running_bot_with_rejected_session_prompts_login(self):
        e = self.env
        await e.add_user("tg:8")
        await e.rt.manager.start("tg:8")
        worker = e.rt.manager._workers["tg:8"]
        self.assertTrue(await wait_until(lambda: worker._broker is not None))
        e.world("tg:8").reject = True
        await worker._broker.close()                    # socket drops; reconnect is rejected
        self.assertTrue(await wait_until(lambda: any("/at_login" in t for _, t in e.alerts.sent), 5))
        self.assertEqual(worker.status.value, "reauth_required")


if __name__ == "__main__":
    unittest.main()
