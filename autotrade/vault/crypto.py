"""AES-256-GCM with a versioned key ring.

Blob layout: 0x01 (format) | key_version (2 bytes, big endian) | nonce (12) | ciphertext+tag.
The associated data binds each ciphertext to ``user_id`` and account kind, so a blob copied to
another row/user fails authentication.
"""
from __future__ import annotations

import base64
import os
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..errors import VaultError

_FORMAT = 1


def make_aad(user_id: str, kind: str) -> bytes:
    return f"autotrade:ssid:v1|{user_id}|{kind}".encode()


class KeyRing:
    def __init__(self, keys: dict[int, bytes], active: int):
        if active not in keys:
            raise VaultError("active key version is not in the key ring")
        for v, k in keys.items():
            if not (0 < v < 65536) or len(k) != 32:
                raise VaultError("vault keys must be 32 bytes with a version between 1 and 65535")
        self._keys = dict(keys)
        self.active = active

    @classmethod
    def generate_key(cls) -> str:
        return base64.b64encode(secrets.token_bytes(32)).decode()

    @classmethod
    def from_env(cls) -> "KeyRing":
        """AUTOTRADE_VAULT_KEYS="1:<b64>,2:<b64>" + AUTOTRADE_VAULT_ACTIVE=2 (or a single AUTOTRADE_VAULT_KEY)."""
        spec = os.environ.get("AUTOTRADE_VAULT_KEYS", "")
        keys: dict[int, bytes] = {}
        try:
            if spec:
                for part in spec.split(","):
                    ver, b64 = part.strip().split(":", 1)
                    keys[int(ver)] = base64.b64decode(b64, validate=True)
                active = int(os.environ.get("AUTOTRADE_VAULT_ACTIVE", max(keys)))
            else:
                single = os.environ.get("AUTOTRADE_VAULT_KEY", "")
                if not single:
                    raise VaultError("AUTOTRADE_VAULT_KEY (or AUTOTRADE_VAULT_KEYS) is not set")
                keys[1] = base64.b64decode(single, validate=True)
                active = 1
        except (ValueError, TypeError) as exc:
            raise VaultError("vault key configuration is malformed") from exc
        return cls(keys, active)

    def encrypt(self, plaintext: str, aad: bytes) -> tuple[bytes, int]:
        nonce = secrets.token_bytes(12)
        ct = AESGCM(self._keys[self.active]).encrypt(nonce, plaintext.encode(), aad)
        return bytes([_FORMAT]) + self.active.to_bytes(2, "big") + nonce + ct, self.active

    def decrypt(self, blob: bytes, aad: bytes) -> str:
        if len(blob) < 1 + 2 + 12 + 16 or blob[0] != _FORMAT:
            raise VaultError("unrecognised ciphertext format")
        version = int.from_bytes(blob[1:3], "big")
        key = self._keys.get(version)
        if key is None:
            raise VaultError(f"no key for version {version}")
        try:
            return AESGCM(key).decrypt(blob[3:15], blob[15:], aad).decode()
        except InvalidTag as exc:
            raise VaultError("ciphertext failed authentication") from exc

    def key_version_of(self, blob: bytes) -> int:
        return int.from_bytes(blob[1:3], "big")
