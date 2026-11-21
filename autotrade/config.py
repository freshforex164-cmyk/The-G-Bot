"""Environment-driven configuration. Secrets are only ever read from the environment."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _num(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


@dataclass(frozen=True)
class Config:
    database_url: str = ""
    jwt_secret: str = ""
    jwt_audience: str | None = None
    allow_live: bool = False          # global switch: nobody can enable Live until this is true
    trading_halted: bool = False      # global kill switch at start-up (can be toggled at runtime)
    max_stake: float = 100.0          # hard cap, Demo
    live_max_stake: float = 10.0      # hard cap, Live
    max_workers: int = 200
    feed_ssid_env: str = "AUTOTRADE_FEED_SSID"  # name of the env var holding the feed account SSID
    handshake_timeout: float = 300.0
    max_handshakes: int = 3
    headless: bool = True
    recheck_seconds: float = 6 * 3600
    cors_origins: tuple[str, ...] = field(default_factory=tuple)
    trust_proxy: bool = True          # TLS terminated by the host's proxy (Render)

    @classmethod
    def from_env(cls) -> "Config":
        origins = tuple(o.strip() for o in os.environ.get("AUTOTRADE_CORS_ORIGINS", "").split(",") if o.strip())
        return cls(
            database_url=os.environ.get("DATABASE_URL", ""),
            jwt_secret=os.environ.get("AUTOTRADE_JWT_SECRET", ""),
            jwt_audience=os.environ.get("AUTOTRADE_JWT_AUDIENCE") or None,
            allow_live=_bool("AUTOTRADE_ALLOW_LIVE", False),
            trading_halted=_bool("AUTOTRADE_HALTED", False),
            max_stake=_num("AUTOTRADE_MAX_STAKE", 100.0),
            live_max_stake=_num("AUTOTRADE_LIVE_MAX_STAKE", 10.0),
            max_workers=int(_num("AUTOTRADE_MAX_WORKERS", 200)),
            handshake_timeout=_num("AUTOTRADE_HANDSHAKE_TIMEOUT", 300),
            max_handshakes=int(_num("AUTOTRADE_MAX_HANDSHAKES", 3)),
            headless=_bool("AUTOTRADE_HEADLESS", True),
            recheck_seconds=_num("AUTOTRADE_RECHECK_SECONDS", 6 * 3600),
            cors_origins=origins,
            trust_proxy=_bool("AUTOTRADE_TRUST_PROXY", True),
        )

    def feed_ssid(self) -> str | None:
        return os.environ.get(self.feed_ssid_env) or None
