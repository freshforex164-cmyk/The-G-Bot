"""Typed errors. Workers branch on these; none of them may carry SSID material."""
from __future__ import annotations


class AutotradeError(Exception):
    pass


class SessionExpired(AutotradeError):
    """The broker rejected the SSID. The user has to log in again (handshake)."""


class RateLimited(AutotradeError):
    def __init__(self, message: str = "rate limited", retry_after: float = 30.0):
        super().__init__(message)
        self.retry_after = retry_after


class Disconnected(AutotradeError):
    """Transient connectivity problem. Safe to retry with backoff."""


class BrokerError(AutotradeError):
    """The broker refused or failed one operation (e.g. asset closed). Do not retry blindly."""


class ModeMismatch(AutotradeError):
    """The connected account type (demo/real) differs from the user's chosen mode."""


class ReauthRequired(AutotradeError):
    def __init__(self, kind: str, reason: str = "session_missing_or_expired"):
        super().__init__(f"re-authentication required for {kind} account: {reason}")
        self.kind = kind
        self.reason = reason


class SettingsError(AutotradeError, ValueError):
    pass


class VaultError(AutotradeError):
    pass
