"""SSID parsing helpers. Nothing here ever returns or logs the session value."""
from __future__ import annotations

import json
from typing import Any

DEMO_KIND = "demo"
REAL_KIND = "real"
KINDS = (DEMO_KIND, REAL_KIND)


def parse_ssid(raw: str) -> dict[str, Any] | None:
    """Return the auth payload of ``42["auth",{...}]`` or None when malformed."""
    raw = (raw or "").strip()
    if not raw.startswith('42["auth",'):
        return None
    try:
        payload = json.loads(raw[2:])
    except json.JSONDecodeError:
        return None
    if not (isinstance(payload, list) and len(payload) == 2 and payload[0] == "auth" and isinstance(payload[1], dict)):
        return None
    body = payload[1]
    if not isinstance(body.get("session"), str) or not body["session"]:
        return None
    return body


def account_kind(raw: str) -> str | None:
    body = parse_ssid(raw)
    if body is None or "isDemo" not in body:
        return None
    try:
        return DEMO_KIND if int(body["isDemo"]) == 1 else REAL_KIND
    except (TypeError, ValueError):
        return None


def is_well_formed(raw: str, kind: str | None = None) -> bool:
    k = account_kind(raw)
    return k is not None and (kind is None or k == kind)


def build_ssid(session: str, kind: str, uid: int = 0, platform: int = 2) -> str:
    """Build an SSID from a session cookie value (fallback capture path)."""
    return "42" + json.dumps(
        ["auth", {"session": session, "isDemo": 1 if kind == DEMO_KIND else 0, "uid": uid, "platform": platform}],
        separators=(",", ":"),
    )


def with_kind(raw: str, kind: str) -> str | None:
    """Same session, other account type. Used only when only one frame was seen."""
    body = parse_ssid(raw)
    if body is None:
        return None
    body = dict(body)
    body["isDemo"] = 1 if kind == DEMO_KIND else 0
    return "42" + json.dumps(["auth", body], separators=(",", ":"))


def describe(raw: str) -> str:
    """Safe description for logs."""
    return f"<ssid kind={account_kind(raw) or 'invalid'} len={len(raw or '')}>"
