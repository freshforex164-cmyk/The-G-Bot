import unittest
from fastapi.testclient import TestClient

from autotrade.api.app import create_app
from autotrade.tests.fakes import Env
from autotrade.api.app import Principal

DEMO = '42["auth",{"session":"abcdefSECRET123","isDemo":1,"uid":7,"platform":2}]'


class ManualSessionApiTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        app = create_app(self.env.rt, auth=lambda: Principal("tg:1", False, "full"))
        self.http = TestClient(app)

    def test_valid_demo_is_stored_in_demo_slot_only(self):
        r = self.http.post("/auth/manual-session", json={"kind": "demo", "ssid": DEMO})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("SECRET", r.text)
        import asyncio
        self.assertEqual(asyncio.run(self.env.rt.vault.get("tg:1", "demo")), DEMO)
        self.assertIsNone(asyncio.run(self.env.rt.vault.get("tg:1", "real")))

    def test_demo_frame_filed_as_real_is_rejected(self):
        r = self.http.post("/auth/manual-session", json={"kind": "real", "ssid": DEMO})
        self.assertEqual(r.status_code, 422)
        self.assertNotIn("SECRET", r.text)
