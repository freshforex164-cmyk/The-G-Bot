import logging
import unittest
from unittest import mock

from ..auth.broker_web import (DEFAULT_BROKER_WEB_URL, broker_web_base, cabinet_urls, describe_proxy, login_url,
                               playwright_proxy)
from ..auth.handshake import CABINET_URL, LOGIN_URL, PlaywrightBackend, host_allowed
from ..config import Config
from ..runtime import sign_in_network


class BrokerWebUrlTest(unittest.TestCase):
    def test_default_site_is_unchanged(self):
        self.assertEqual(broker_web_base(""), DEFAULT_BROKER_WEB_URL)
        self.assertEqual(LOGIN_URL, "https://pocketoption.com/en/login/")
        self.assertEqual(CABINET_URL["demo"], "https://pocketoption.com/en/cabinet/demo-quick-high-low/")

    def test_official_mirror_builds_login_and_cabinet_urls(self):
        base = broker_web_base("https://po.cash/")
        self.assertEqual(login_url(base), "https://po.cash/en/login/")
        self.assertEqual(cabinet_urls(base)["real"], "https://po.cash/en/cabinet/quick-high-low/")
        self.assertTrue(host_allowed("https://po.trade/en/login/", navigation=True))

    def test_unknown_or_unsafe_sites_are_rejected(self):
        for bad in ("https://evil.example", "http://po.cash", "https://po.cash/en/login/",
                    "https://user:pw@po.cash", "https://po.cash:8443", "https://pocketoption.com.evil.io"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                broker_web_base(bad)


class BrowserProxyTest(unittest.TestCase):
    def test_empty_means_direct(self):
        self.assertIsNone(playwright_proxy(""))
        self.assertEqual(describe_proxy(None), "direct")

    def test_socks5_without_login(self):
        self.assertEqual(playwright_proxy("socks5://127.0.0.1:40000"), {"server": "socks5://127.0.0.1:40000"})
        self.assertEqual(playwright_proxy("socks5h://127.0.0.1:1080"), {"server": "socks5://127.0.0.1:1080"})

    def test_http_with_login_is_split_and_never_described(self):
        proxy = playwright_proxy("http://us%40r:p%3Ass@proxy.example:8080")
        self.assertEqual(proxy, {"server": "http://proxy.example:8080", "username": "us@r", "password": "p:ss"})
        text = describe_proxy(proxy)
        self.assertNotIn("p:ss", text)
        self.assertNotIn("us@r", text)

    def test_invalid_proxies_are_rejected(self):
        for bad in ("ftp://h:1", "socks5://127.0.0.1", "socks5://u:p@127.0.0.1:1080", "http://h:1/path", "h:8080"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                playwright_proxy(bad)


class RuntimeWiringTest(unittest.TestCase):
    def test_bad_settings_fall_back_without_crashing(self):
        cfg = Config(broker_web_url="https://evil.example", browser_proxy="ftp://x:1")
        with self.assertLogs("autotrade.runtime", logging.ERROR):
            base, proxy = sign_in_network(cfg)
        self.assertEqual((base, proxy), (DEFAULT_BROKER_WEB_URL, None))

    def test_valid_settings_reach_the_browser_backend(self):
        cfg = Config(broker_web_url="https://po.cash", browser_proxy="socks5://127.0.0.1:40000")
        base, proxy = sign_in_network(cfg)
        b = PlaywrightBackend(True, login_url=login_url(base), cabinet_urls=cabinet_urls(base), proxy=proxy)
        self.assertEqual(b._login_url, "https://po.cash/en/login/")
        self.assertEqual(b._cabinet_urls["demo"], "https://po.cash/en/cabinet/demo-quick-high-low/")
        self.assertEqual(b._proxy, {"server": "socks5://127.0.0.1:40000"})


class _Stop(Exception):
    pass


class LaunchProxyTest(unittest.IsolatedAsyncioTestCase):
    async def _launch_kwargs(self, proxy, fail_new=False):
        seen = {}

        class Chromium:
            async def launch(self, **kw):
                if fail_new and kw.get("channel") == "chromium":
                    raise RuntimeError("Executable doesn't exist")
                seen.update(kw)
                raise _Stop()

        class PW:
            chromium = Chromium()

            async def stop(self):
                pass

        class Starter:
            async def start(self):
                return PW()

        b = PlaywrightBackend(True, proxy=proxy)
        with mock.patch("playwright.async_api.async_playwright", lambda: Starter()):
            with self.assertRaises(_Stop):
                await b.open()
        return seen

    async def test_proxy_is_passed_to_chromium(self):
        kw = await self._launch_kwargs({"server": "socks5://127.0.0.1:40000"})
        self.assertEqual(kw["proxy"], {"server": "socks5://127.0.0.1:40000"})

    async def test_new_headless_is_preferred(self):
        kw = await self._launch_kwargs(None)
        self.assertEqual(kw["channel"], "chromium")

    async def test_falls_back_to_legacy_shell_when_full_browser_missing(self):
        with self.assertLogs("autotrade.handshake", logging.WARNING):
            kw = await self._launch_kwargs({"server": "http://127.0.0.1:8080"}, fail_new=True)
        self.assertNotIn("channel", kw)
        self.assertEqual(kw["proxy"], {"server": "http://127.0.0.1:8080"})

    async def test_no_proxy_means_direct_launch(self):
        kw = await self._launch_kwargs(None)
        self.assertNotIn("proxy", kw)


if __name__ == "__main__":
    unittest.main()
