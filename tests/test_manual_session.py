"""/at_session: pasted SSID is extracted, routed to the right vault slot, and never echoed back."""
import asyncio
import unittest

from autotrade_client import AutotradeError
from autotrade_commands import AutotradeCommands, extract_ssid, ssid_kind

DEMO = '42["auth",{"session":"abcdefSECRET123","isDemo":1,"uid":7,"platform":2}]'
REAL = DEMO.replace('"isDemo":1', '"isDemo":0')


class FakeClient:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    async def submit_manual_session(self, uid, kind, ssid):
        self.calls.append((uid, kind, ssid))
        if self.fail:
            raise self.fail
        return {"stored": kind, "connected": True}


class ManualSessionTests(unittest.IsolatedAsyncioTestCase):
    def make(self, client):
        sent = []
        async def send(chat_id, text, reply_markup=None):
            sent.append(text)
        return AutotradeCommands(client, send), sent

    def test_extracts_and_detects_kind(self):
        self.assertEqual(extract_ssid(f"`{DEMO}`"), DEMO)
        self.assertEqual(ssid_kind(DEMO), "demo")
        self.assertEqual(ssid_kind(REAL), "real")
        self.assertIsNone(extract_ssid("hello"))

    async def test_real_ssid_goes_to_real_slot_and_is_not_echoed(self):
        c = FakeClient(); h, sent = self.make(c)
        await h.store_pasted_session(5, 5, REAL)
        self.assertEqual(c.calls, [(5, "real", REAL)])
        self.assertFalse(any("SECRET" in t for t in sent))

    async def test_chosen_kind_mismatch_saves_nothing(self):
        c = FakeClient(); h, sent = self.make(c)
        await h.handle("at_session", 5, 5, "real")
        await h.store_pasted_session(5, 5, DEMO)
        self.assertEqual(c.calls, [])

    async def test_group_chat_is_refused(self):
        c = FakeClient(); h, _ = self.make(c)
        await h.store_pasted_session(-100, 5, DEMO)
        self.assertEqual(c.calls, [])

    async def test_rejected_value_is_reported_without_secret(self):
        c = FakeClient(AutotradeError(422, "invalid_ssid", "bad")); h, sent = self.make(c)
        await h.store_pasted_session(5, 5, DEMO)
        self.assertFalse(any("SECRET" in t for t in sent))


if __name__ == "__main__":
    unittest.main()
