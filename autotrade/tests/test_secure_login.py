import asyncio
import unittest
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from ..api.app import create_app
from ..auth.handshake import PlaywrightBackend
from ..auth.session_state import sanitize_browser_session
from ..storage.repo import CredentialRow
from ..vault.service import TokenVault
from ..errors import VaultError
from .fakes import Env
from .test_handshake_api import FakeBackend, good_tokens, token

COOKIE = {"name": "po_session", "value": "fake-broker-cookie", "domain": ".pocketoption.com",
          "path": "/", "expires": -1, "httpOnly": True, "secure": True, "sameSite": "Lax"}
PASSWORD = "fake-password-only-for-tests"


class CredentialBackend(FakeBackend):
    def __init__(self):
        super().__init__(good_tokens())
        self.submitted = asyncio.Event()
        self.accepted = False

    async def submit_credentials(self, email, password):
        self.accepted = email == "demo@example.invalid" and password == PASSWORD
        self.submitted.set()

    async def capture_tokens(self, timeout, on_status):
        on_status("waiting_for_login")
        await asyncio.wait_for(self.submitted.wait(), timeout=timeout)
        return self.tokens, {"method": "websocket", "derived": [], "_browser_session": {"cookies": [COOKIE]}}


class SecureLoginApiTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.backend = CredentialBackend()
        self.env.rt.handshakes._factory = lambda: self.backend
        self.app = create_app(self.env.rt, background=False)
        self.login = {"Authorization": "Bearer " + token(self.env.cfg.jwt_secret, scope="login")}
        self.full = {"Authorization": "Bearer " + token(self.env.cfg.jwt_secret)}
        self.other = {"Authorization": "Bearer " + token(self.env.cfg.jwt_secret, sub="other", scope="login")}

    def begin(self, c):
        sid = c.post("/auth/handshake", headers=self.login).json()["session_id"]
        for _ in range(100):
            state = c.get(f"/auth/handshake/{sid}", headers=self.login).json()["state"]
            if state == "waiting_for_login":
                return sid
            import time
            time.sleep(.01)
        self.fail("Login did not become ready")

    def test_form_submission_captures_encrypted_sessions_and_disconnect_erases_them(self):
        with TestClient(self.app, base_url="https://testserver") as c:
            sid = self.begin(c)
            endpoint = f"/auth/handshake/{sid}/credentials"
            response = c.post(endpoint, headers=self.login, json={"email": " demo@example.invalid ", "password": PASSWORD})
            self.assertEqual(response.status_code, 204)
            self.assertTrue(self.backend.accepted)
            for _ in range(100):
                state = c.get(f"/auth/handshake/{sid}", headers=self.login).json()["state"]
                if state == "done": break
                import time
                time.sleep(.01)
            self.assertEqual(state, "done")
            self.assertTrue(self.backend.closed)
            for row in self.env.repo.credentials.values():
                self.assertNotIn(COOKIE["value"].encode(), row.ciphertext)
                self.assertNotIn(PASSWORD.encode(), row.ciphertext)
                self.assertNotIn(COOKIE["value"], repr(row.meta))
            self.assertNotIn(COOKIE["value"], repr(self.env.repo.audit_log))
            self.assertNotIn(PASSWORD, repr(self.env.repo.audit_log))
            status = c.get("/auth/status", headers=self.full)
            self.assertEqual(status.json()["demo"]["status"], "active")
            self.assertNotIn(COOKIE["value"], status.text)
            self.assertEqual(c.delete("/auth/sessions", headers=self.full).status_code, 204)
            self.assertEqual(self.env.repo.credentials, {})

    def test_credentials_require_session_owner_and_bearer(self):
        with TestClient(self.app, base_url="https://testserver") as c:
            sid = self.begin(c)
            url = f"/auth/handshake/{sid}/credentials"
            body = {"email": "demo@example.invalid", "password": PASSWORD}
            self.assertEqual(c.post(url, json=body).status_code, 401)
            self.assertEqual(c.post(url, headers=self.other, json=body).status_code, 404)
            self.assertFalse(self.backend.accepted)
            c.delete(f"/auth/handshake/{sid}", headers=self.login)

    def test_invalid_input_and_error_responses_never_echo_passwords(self):
        with TestClient(self.app, base_url="https://testserver") as c:
            sid = self.begin(c)
            url = f"/auth/handshake/{sid}/credentials"
            for body in ({"email": "not-email", "password": PASSWORD},
                         {"email": "demo@example.invalid", "password": PASSWORD * 20},
                         {"email": "demo@example.invalid", "password": PASSWORD, "cookie": "unexpected"}):
                response = c.post(url, headers=self.login, json=body)
                self.assertEqual(response.status_code, 422)
                self.assertNotIn(PASSWORD, response.text)
            self.backend.submit_credentials = AsyncMock(side_effect=RuntimeError(PASSWORD))
            response = c.post(url, headers=self.login, json={"email": "demo@example.invalid", "password": PASSWORD})
            self.assertEqual(response.status_code, 409)
            self.assertNotIn(PASSWORD, response.text)
            c.delete(f"/auth/handshake/{sid}", headers=self.login)

    def test_nonlocal_plain_http_is_rejected(self):
        with TestClient(self.app, base_url="http://testserver", client=("203.0.113.8", 12345)) as c:
            sid = self.begin(c)
            response = c.post(f"/auth/handshake/{sid}/credentials", headers=self.login,
                              json={"email": "demo@example.invalid", "password": PASSWORD})
            self.assertEqual(response.status_code, 400)
            self.assertFalse(self.backend.accepted)
            c.delete(f"/auth/handshake/{sid}", headers=self.login)


class CookieVaultTests(unittest.IsolatedAsyncioTestCase):
    async def test_cookies_survive_vault_recreation_and_user_isolation(self):
        env = Env()
        ssid = good_tokens()["demo"]
        await env.rt.vault.store("u1", "demo", ssid, browser_session={"cookies": [COOKIE]})
        recreated = TokenVault(env.repo, env.rt.vault._keys)
        self.assertEqual(await recreated.get("u1", "demo"), ssid)
        self.assertEqual((await recreated.get_browser_session("u1", "demo"))["cookies"][0]["value"], COOKIE["value"])
        self.assertIsNone(await recreated.get_browser_session("other", "demo"))
        row = await env.repo.get_credential("u1", "demo")
        await env.repo.upsert_credential(CredentialRow("other", "demo", row.ciphertext, row.key_version))
        with self.assertRaises(VaultError):
            await recreated.get_browser_session("other", "demo")
        await recreated.mark_expired("u1", "demo", "expired")
        self.assertIsNone(await recreated.get_browser_session("u1", "demo"))

    def test_google_cookies_are_not_saved(self):
        google = {**COOKIE, "domain": ".google.com", "value": "fake-google-cookie"}
        state = sanitize_browser_session({"cookies": [COOKIE, google]})
        self.assertEqual(len(state["cookies"]), 1)
        self.assertEqual(state["cookies"][0]["domain"], ".pocketoption.com")

    def test_password_fields_cannot_enter_cookie_storage(self):
        with self.assertRaises(ValueError):
            sanitize_browser_session({"cookies": [COOKIE], "password": PASSWORD})


class BrokerFormBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_standard_form_submits_only_to_broker_origin_and_clears_password(self):
        backend = PlaywrightBackend()
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            html = """<form><input type=email name=email><input type=password name=password><button>Connect</button></form>
            <script>document.querySelector('form').onsubmit=e=>{e.preventDefault();document.body.dataset.submitted='yes'}</script>"""
            await page.route("https://pocketoption.com/**", lambda route: route.fulfill(body=html, content_type="text/html"))
            await page.goto("https://pocketoption.com/en/login/")
            backend._pages = [page]
            await backend.submit_credentials("demo@example.invalid", PASSWORD)
            self.assertEqual(await page.locator("body").get_attribute("data-submitted"), "yes")
            self.assertEqual(await page.locator('input[type="password"]').input_value(), "")
            await page.goto("data:text/html," + html)
            with self.assertRaises(RuntimeError):
                await backend.submit_credentials("demo@example.invalid", PASSWORD)
            await browser.close()