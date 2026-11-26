"""Environment-driven configuration. Secrets are only ever read from the environment."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from runtime_env import load_environment

load_environment()


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
    allow_live: bool = True           # global switch: Demo and Real auto-trading (Live still needs /at_live confirm)
    trading_halted: bool = False      # global kill switch at start-up (can be toggled at runtime)
    max_stake: float = 100.0          # hard cap, Demo
    live_max_stake: float = 10.0      # hard cap, Live
    max_workers: int = 200
    feed_ssid_env: str = "AUTOTRADE_FEED_SSID"  # name of the env var holding the feed account SSID
    handshake_timeout: float = 300.0
    max_handshakes: int = 1           # bounded Chromium concurrency; increase only after measuring VPS memory
    handshake_idle_timeout: float = 150.0  # login page closed / stopped polling: free its Chromium
    headless: bool = True
    low_memory: bool = False           # Chromium without GPU/shared memory, small JS heap (512 MB hosts)
    chromium_single_process: bool = False  # low-memory Chromium in one OS process (turn off if a site crashes)
    screenshot_interval: float = 1.5  # low-memory: seconds between live pictures of the broker page
    handshake_min_free_mb: float = 150.0   # do not start Chromium with less free container memory (0 = off)
    handshake_memory_floor_mb: float = 40.0  # close Chromium if free memory falls below this (0 = off)
    block_login_assets: bool = False   # no images/fonts/media/stylesheets from the broker during login
    block_third_party: bool = True    # no trackers/ads during login (Google/CAPTCHA always allowed)
    cabinet_switch_delay: float = 3.5 # pause between the Real and Demo cabinet pages (fast switching gets blocked)
    # Per-user brute-force limits: "<attempts>/<seconds>".
    handshake_rate: str = "6/600"
    credentials_rate: str = "5/900"
    telegram_bot_token: str = ""      # trade open/settle alerts go straight to the user's Telegram chat
    recheck_seconds: float = 6 * 3600
    cors_origins: tuple[str, ...] = field(default_factory=tuple)
    trust_proxy: bool = False         # Uvicorn handles headers from explicitly trusted proxy IPs

    @classmethod
    def from_env(cls) -> "Config":
        origins = tuple(o.strip() for o in os.environ.get("AUTOTRADE_CORS_ORIGINS", "").split(",") if o.strip())
        return cls(
            database_url=os.getenv("DATABASE_URL", "").strip(),
            jwt_secret=os.getenv("AUTOTRADE_JWT_SECRET", "").strip(),
            jwt_audience=os.environ.get("AUTOTRADE_JWT_AUDIENCE") or None,
            allow_live=_bool("AUTOTRADE_ALLOW_LIVE", True),
            trading_halted=_bool("AUTOTRADE_HALTED", False),
            max_stake=_num("AUTOTRADE_MAX_STAKE", 100.0),
            live_max_stake=_num("AUTOTRADE_LIVE_MAX_STAKE", 10.0),
            max_workers=int(_num("AUTOTRADE_MAX_WORKERS", 200)),
            handshake_timeout=_num("AUTOTRADE_HANDSHAKE_TIMEOUT", 300),
            max_handshakes=int(_num("AUTOTRADE_MAX_HANDSHAKES", 1)),
            handshake_idle_timeout=_num("AUTOTRADE_HANDSHAKE_IDLE_TIMEOUT", 150),
            headless=_bool("AUTOTRADE_HEADLESS", True),
            low_memory=_bool("AUTOTRADE_LOW_MEMORY", False),
            chromium_single_process=_bool("AUTOTRADE_CHROMIUM_SINGLE_PROCESS", False),
            screenshot_interval=_num("AUTOTRADE_SCREENSHOT_INTERVAL", 1.5),
            handshake_min_free_mb=_num("AUTOTRADE_HANDSHAKE_MIN_FREE_MB", 150),
            handshake_memory_floor_mb=_num("AUTOTRADE_HANDSHAKE_MEMORY_FLOOR_MB", 40),
            block_login_assets=_bool("AUTOTRADE_BLOCK_LOGIN_ASSETS", False),
            block_third_party=_bool("AUTOTRADE_BLOCK_THIRD_PARTY", True),
            cabinet_switch_delay=_num("AUTOTRADE_CABINET_SWITCH_DELAY", 3.5),
            handshake_rate=os.environ.get("AUTOTRADE_HANDSHAKE_RATE", "6/600"),
            credentials_rate=os.environ.get("AUTOTRADE_CREDENTIALS_RATE", "5/900"),
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            recheck_seconds=_num("AUTOTRADE_RECHECK_SECONDS", 6 * 3600),
            cors_origins=origins,
            trust_proxy=_bool("AUTOTRADE_TRUST_PROXY", False),
        )

    def feed_ssid(self) -> str | None:
        return (os.getenv(self.feed_ssid_env, "").strip() or os.getenv("PO_SSID", "").strip()) or None
