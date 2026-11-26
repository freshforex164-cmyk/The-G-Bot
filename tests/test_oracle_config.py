"""Oracle VPS configuration regressions. No network or production secrets."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import config
from runtime_env import load_environment
from autotrade.config import Config
from autotrade_client import AutotradeClient

SSID = '42["auth",{"session":"offline-test","isDemo":1}]'


class OracleConfigurationTests(unittest.TestCase):
    def test_dotenv_loads_even_when_working_directory_is_elsewhere(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            path = Path(folder) / '.env'
            path.write_text('TELEGRAM_BOT_TOKEN=offline-token\nDATABASE_URL=postgresql://offline.invalid/bot\n')
            load_environment(path)
            self.assertEqual(Config.from_env().telegram_bot_token, 'offline-token')
            self.assertEqual(Config.from_env().database_url, 'postgresql://offline.invalid/bot')

    def test_service_environment_takes_precedence_over_dotenv(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'service-token'}, clear=True):
            path = Path(folder) / '.env'
            path.write_text('TELEGRAM_BOT_TOKEN=file-token\n')
            load_environment(path)
            self.assertEqual(Config.from_env().telegram_bot_token, 'service-token')

    def test_allow_live_one_enables_live(self):
        with patch.dict(os.environ, {'AUTOTRADE_ALLOW_LIVE': '1'}, clear=True):
            self.assertTrue(Config.from_env().allow_live)

    def test_operator_can_still_disable_live(self):
        with patch.dict(os.environ, {'AUTOTRADE_ALLOW_LIVE': '0'}, clear=True):
            self.assertFalse(Config.from_env().allow_live)

    def test_feed_accepts_po_ssid_fallback(self):
        with patch.dict(os.environ, {'PO_SSID': f'  {SSID}  '}, clear=True):
            self.assertEqual(Config.from_env().feed_ssid(), SSID)

    def test_scanner_accepts_autotrade_feed_ssid_fallback(self):
        with patch.dict(os.environ, {'AUTOTRADE_FEED_SSID': SSID}, clear=True):
            self.assertEqual(config.load_ssid(), SSID)
            self.assertEqual(config.account_type(), 'DEMO')

    def test_explicit_feed_session_takes_precedence(self):
        with patch.dict(os.environ, {'AUTOTRADE_FEED_SSID': SSID, 'PO_SSID': 'other'}, clear=True):
            self.assertEqual(Config.from_env().feed_ssid(), SSID)

    def test_missing_sessions_fail_without_host_specific_secret_loader(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(config.ConfigurationError):
                config.load_ssid()

    def test_internal_api_defaults_to_same_host_port_8100(self):
        with patch.dict(os.environ, {'AUTOTRADE_JWT_SECRET': 'x' * 40}, clear=True):
            client = AutotradeClient.from_env()
            self.assertIsNotNone(client)
            self.assertEqual(client.base_url, 'http://127.0.0.1:8100')

    def test_environment_can_override_internal_and_public_urls(self):
        env = {'AUTOTRADE_JWT_SECRET': 'x' * 40, 'AUTOTRADE_API_URL': 'http://127.0.0.1:9100',
               'AUTOTRADE_PUBLIC_URL': 'https://bot.example.invalid'}
        with patch.dict(os.environ, env, clear=True):
            client = AutotradeClient.from_env()
            self.assertEqual(client.base_url, env['AUTOTRADE_API_URL'])
            self.assertEqual(client.public_url, env['AUTOTRADE_PUBLIC_URL'])

    def test_vps_browser_does_not_inherit_constrained_host_flags(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = Config.from_env()
            self.assertFalse(cfg.low_memory)
            self.assertFalse(cfg.chromium_single_process)
            self.assertFalse(cfg.block_login_assets)

    def test_low_memory_remains_explicitly_available(self):
        env = {name: '1' for name in ('AUTOTRADE_LOW_MEMORY', 'AUTOTRADE_CHROMIUM_SINGLE_PROCESS', 'AUTOTRADE_BLOCK_LOGIN_ASSETS')}
        with patch.dict(os.environ, env, clear=True):
            cfg = Config.from_env()
            self.assertTrue(cfg.low_memory)
            self.assertTrue(cfg.chromium_single_process)
            self.assertTrue(cfg.block_login_assets)

    def test_raw_forwarded_headers_are_not_trusted_by_default(self):
        from starlette.requests import Request
        from autotrade.api.app import is_secure_request
        request = Request({'type': 'http', 'scheme': 'http', 'server': ('bot.example.invalid', 80),
                           'path': '/', 'headers': [(b'x-forwarded-proto', b'https')],
                           'client': ('203.0.113.10', 1234)})
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(is_secure_request(request, Config.from_env().trust_proxy))


if __name__ == '__main__':
    unittest.main()
