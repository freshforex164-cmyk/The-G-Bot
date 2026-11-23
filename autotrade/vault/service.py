from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ..auth.ssid import KINDS, is_well_formed
from ..auth.session_state import sanitize_browser_session
from ..clock import Clock
from ..errors import VaultError
from ..storage.repo import CredentialRow, Repository
from .crypto import KeyRing, make_aad


class TokenVault:
    """Encrypted SSID storage keyed by the internal user id. Plaintext exists only in memory,
    only inside ``get()``'s return value, and is never logged."""

    def __init__(self, repo: Repository, keyring: KeyRing, clock: Clock | None = None):
        self._repo = repo
        self._keys = keyring
        self._clock = clock or Clock()

    async def store(self, user_id: str, kind: str, ssid: str, meta: dict[str, Any] | None = None,
                    *, browser_session: dict | None = None) -> None:
        if kind not in KINDS:
            raise VaultError("unknown account kind")
        if not is_well_formed(ssid, kind):
            raise VaultError(f"value is not a well-formed {kind} SSID")
        plaintext = ssid
        if browser_session is not None:
            try:
                state = sanitize_browser_session(browser_session)
            except ValueError:
                raise VaultError("Invalid broker browser session.") from None
            plaintext = json.dumps({"version": 1, "ssid": ssid, "browser_session": state})
        blob, version = self._keys.encrypt(plaintext, make_aad(user_id, kind))
        now = self._clock.utcnow()
        await self._repo.upsert_credential(CredentialRow(
            user_id=user_id, kind=kind, ciphertext=blob, key_version=version, status="active",
            captured_at=now, last_checked_at=None, last_error=None, meta=dict(meta or {})))

    async def get(self, user_id: str, kind: str, *, include_expired: bool = False) -> str | None:
        if kind not in KINDS:
            raise VaultError("unknown account kind")
        row = await self._repo.get_credential(user_id, kind)
        if row is None or (row.status != "active" and not include_expired):
            return None
        plain = self._keys.decrypt(row.ciphertext, make_aad(user_id, kind))
        if plain.startswith('42['):
            if not is_well_formed(plain, kind):
                raise VaultError("Stored session does not match its account slot.")
            return plain  # Existing rows remain readable without a migration.
        return self._unpack(plain, kind)["ssid"]

    @staticmethod
    def _unpack(plain: str, kind: str) -> dict:
        try:
            payload = json.loads(plain)
            if payload.get("version") != 1 or not is_well_formed(payload.get("ssid", ""), kind):
                raise ValueError
            payload["browser_session"] = sanitize_browser_session(payload["browser_session"])
            return payload
        except (ValueError, TypeError, KeyError, AttributeError):
            raise VaultError("Invalid stored broker session.") from None

    async def get_browser_session(self, user_id: str, kind: str) -> dict | None:
        """Server-only internal access; browser cookies are never returned by a public API."""
        row = await self._repo.get_credential(user_id, kind)
        if row is None or row.status != "active":
            return None
        plain = self._keys.decrypt(row.ciphertext, make_aad(user_id, kind))
        if plain.startswith('42['):
            return None
        return self._unpack(plain, kind)["browser_session"]

    async def mark_expired(self, user_id: str, kind: str, reason: str) -> None:
        await self._repo.mark_credential(user_id, kind, "expired", reason[:200], self._clock.utcnow())

    async def mark_valid(self, user_id: str, kind: str) -> None:
        await self._repo.mark_credential(user_id, kind, "active", None, self._clock.utcnow())

    async def status(self, user_id: str) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {k: {"present": False, "status": "missing"} for k in KINDS}
        for row in await self._repo.list_credentials(user_id):
            out[row.kind] = {
                "present": True,
                "status": row.status,
                "captured_at": row.captured_at.isoformat() if row.captured_at else None,
                "last_checked_at": row.last_checked_at.isoformat() if row.last_checked_at else None,
                "reason": row.last_error,
            }
        return out

    async def delete_all(self, user_id: str) -> None:
        await self._repo.delete_credentials(user_id)

    async def delete(self, user_id: str, kind: str) -> None:
        """Erase one account slot (Demo or Real); the other slot is untouched."""
        if kind not in KINDS:
            raise VaultError("unknown account kind")
        await self._repo.delete_credential(user_id, kind)

    async def rewrap(self, user_id: str) -> int:
        """Re-encrypt this user's tokens under the active key (key rotation). Returns rows changed."""
        changed = 0
        for row in await self._repo.list_credentials(user_id):
            if row.key_version == self._keys.active:
                continue
            plain = self._keys.decrypt(row.ciphertext, make_aad(user_id, row.kind))
            row.ciphertext, row.key_version = self._keys.encrypt(plain, make_aad(user_id, row.kind))
            await self._repo.upsert_credential(row)
            changed += 1
        return changed
