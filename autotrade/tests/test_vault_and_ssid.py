import unittest

from ..auth.ssid import account_kind, build_ssid, is_well_formed, parse_ssid, with_kind
from ..errors import VaultError
from ..logging_utils import redact
from ..storage.repo import InMemoryRepository
from ..vault.crypto import KeyRing, make_aad
from ..vault.service import TokenVault


class VaultTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.keys = KeyRing({1: bytes(range(32))}, 1)
        self.repo = InMemoryRepository()
        self.vault = TokenVault(self.repo, self.keys)
        self.ssid = build_ssid("SECRET-SESSION-VALUE", "demo", uid=5)

    async def test_round_trip_and_not_plaintext_at_rest(self):
        await self.vault.store("u1", "demo", self.ssid)
        row = await self.repo.get_credential("u1", "demo")
        self.assertNotIn(b"SECRET-SESSION-VALUE", row.ciphertext)
        self.assertEqual(await self.vault.get("u1", "demo"), self.ssid)

    async def test_each_encryption_uses_a_fresh_nonce(self):
        a, _ = self.keys.encrypt("x", b"aad")
        b, _ = self.keys.encrypt("x", b"aad")
        self.assertNotEqual(a, b)

    async def test_blob_is_bound_to_user_and_kind(self):
        await self.vault.store("u1", "demo", self.ssid)
        row = await self.repo.get_credential("u1", "demo")
        with self.assertRaises(VaultError):
            self.keys.decrypt(row.ciphertext, make_aad("u2", "demo"))
        with self.assertRaises(VaultError):
            self.keys.decrypt(row.ciphertext, make_aad("u1", "real"))

    async def test_tampered_ciphertext_is_rejected(self):
        blob, _ = self.keys.encrypt("hello", b"a")
        bad = blob[:-1] + bytes([blob[-1] ^ 1])
        with self.assertRaises(VaultError):
            self.keys.decrypt(bad, b"a")

    async def test_rejects_malformed_or_wrong_kind(self):
        with self.assertRaises(VaultError):
            await self.vault.store("u1", "demo", "not-an-ssid")
        with self.assertRaises(VaultError):
            await self.vault.store("u1", "real", self.ssid)  # demo SSID filed as real

    async def test_expired_token_is_not_returned(self):
        await self.vault.store("u1", "demo", self.ssid)
        await self.vault.mark_expired("u1", "demo", "rejected")
        self.assertIsNone(await self.vault.get("u1", "demo"))
        self.assertEqual((await self.vault.status("u1"))["demo"]["status"], "expired")

    async def test_status_never_contains_token(self):
        await self.vault.store("u1", "demo", self.ssid)
        self.assertNotIn("SECRET-SESSION-VALUE", repr(await self.vault.status("u1")))

    async def test_key_rotation_rewraps_and_old_key_can_retire(self):
        await self.vault.store("u1", "demo", self.ssid)
        ring2 = KeyRing({1: bytes(range(32)), 2: bytes(range(1, 33))}, 2)
        vault2 = TokenVault(self.repo, ring2)
        self.assertEqual(await vault2.rewrap("u1"), 1)
        self.assertEqual((await self.repo.get_credential("u1", "demo")).key_version, 2)
        only_new = TokenVault(self.repo, KeyRing({2: bytes(range(1, 33))}, 2))
        self.assertEqual(await only_new.get("u1", "demo"), self.ssid)

    def test_key_length_enforced(self):
        with self.assertRaises(VaultError):
            KeyRing({1: b"short"}, 1)


class SsidTests(unittest.TestCase):
    def test_kind_detection(self):
        self.assertEqual(account_kind(build_ssid("s", "demo")), "demo")
        self.assertEqual(account_kind(build_ssid("s", "real")), "real")
        self.assertIsNone(account_kind("garbage"))

    def test_with_kind_flips_flag_only(self):
        d = build_ssid("s", "demo", uid=7)
        r = with_kind(d, "real")
        self.assertEqual(account_kind(r), "real")
        self.assertEqual(parse_ssid(r)["session"], "s")
        self.assertTrue(is_well_formed(r, "real"))

    def test_log_redaction(self):
        s = build_ssid("TOPSECRET", "real", uid=3)
        self.assertNotIn("TOPSECRET", redact(f"connecting with {s} now"))
        self.assertNotIn("TOPSECRET", redact('{"session":"TOPSECRET"}'))
        self.assertNotIn("abc.def", redact("Authorization: Bearer abc.def"))
