"""Wires the components together. ``build_runtime`` is the one place to change when integrating."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from .auth.broker_web import (DEFAULT_BROKER_WEB_URL, broker_web_base, cabinet_urls, describe_proxy, login_url,
                              playwright_proxy)
from .auth.handshake import CaptureBackend, HandshakeManager, PlaywrightBackend
from .auth.health import SessionHealth
from .auth.ssid import KINDS
from .clock import Clock
from .config import Config
from .market.feed import SharedMarketFeed
from .notify import NullNotifier, TelegramNotifier
from .storage.repo import InMemoryRepository, Repository
from .trading.broker import BrokerClient, PocketOptionBroker
from .trading.manager import TradingManager
from .vault.crypto import KeyRing
from .vault.service import TokenVault

log = logging.getLogger(__name__)


@dataclass
class Runtime:
    cfg: Config
    repo: Repository
    vault: TokenVault
    health: SessionHealth
    feed: SharedMarketFeed
    manager: TradingManager
    handshakes: HandshakeManager
    notifier: Any = None


def sign_in_network(cfg: Config) -> tuple[str, dict[str, str] | None]:
    """Validated sign-in website and browser proxy. A bad value is logged and ignored instead of crashing
    the engine, so running trade workers and the Telegram bot keep working."""
    try:
        base = broker_web_base(getattr(cfg, "broker_web_url", ""))
    except ValueError as exc:
        log.error("ignoring AUTOTRADE_BROKER_WEB_URL: %s", exc)
        base = DEFAULT_BROKER_WEB_URL
    try:
        proxy = playwright_proxy(getattr(cfg, "browser_proxy", ""))
    except ValueError as exc:
        log.error("ignoring AUTOTRADE_BROWSER_PROXY: %s", exc)
        proxy = None
    log.info("sign-in browser: site %s, route %s", base, describe_proxy(proxy))
    return base, proxy


def build_runtime(cfg: Config, *, repo: Repository | None = None, keyring: KeyRing | None = None,
                  broker_factory: Callable[[str], BrokerClient] = PocketOptionBroker,
                  backend_factory: Callable[[], CaptureBackend] | None = None,
                  clock: Clock | None = None, reconnect_base: float = 2.0, notifier: Any = None) -> Runtime:
    clock = clock or Clock()
    repo = repo or InMemoryRepository()
    vault = TokenVault(repo, keyring or KeyRing.from_env(), clock)
    health = SessionHealth(vault, repo, broker_factory, clock)

    def feed_broker() -> BrokerClient | None:
        ssid = cfg.feed_ssid()
        return broker_factory(ssid) if ssid else None

    feed = SharedMarketFeed(feed_broker, clock)
    if notifier is None:
        notifier = TelegramNotifier(cfg.telegram_bot_token) if cfg.telegram_bot_token else NullNotifier()
    manager = TradingManager(cfg=cfg, repo=repo, vault=vault, health=health, feed=feed,
                             broker_factory=broker_factory, clock=clock, reconnect_base=reconnect_base,
                             notifier=notifier)

    async def sink(user_id: str, tokens: dict[str, str], meta: dict[str, Any]) -> list[str]:
        derived = set(meta.get("derived", []))
        # "Sign in again anyway": the user explicitly replaces whatever is saved with this fresh login.
        replace = bool(meta.pop("replace", False))
        # Captured cookies are transient until encrypted; never include them in row metadata/audit.
        browser_session = meta.pop("_browser_session", None)
        stored: list[str] = []
        removed: list[str] = []
        if replace and tokens:
            # Wipe slots this login did not capture, so no stale session from an older login survives
            # (it could even belong to a different broker account). Captured slots are overwritten below.
            for kind in KINDS:
                if kind not in tokens and await repo.get_credential(user_id, kind) is not None:
                    await vault.delete(user_id, kind)
                    removed.append(kind)
        for kind, ssid in tokens.items():
            if kind in derived and not replace:
                # Dual-vault isolation: a session rebuilt from the login cookie never replaces a genuine,
                # still-active session captured from that account's own WebSocket frame.
                row = await repo.get_credential(user_id, kind)
                if row is not None and row.status == "active" and not (row.meta or {}).get("derived"):
                    continue
            await vault.store(user_id, kind, ssid, {"method": meta.get("method"), "derived": kind in derived},
                              browser_session=browser_session)
            stored.append(kind)
        await repo.save_settings(user_id, await repo.get_settings(user_id))  # creates the Demo default for new users
        await manager.reset_to_demo(user_id, "new login")
        await repo.audit(user_id, "tokens_captured", {"kinds": sorted(stored), "method": meta.get("method"),
                                                      "replaced": replace, "removed": removed})
        # Connect each new session over WebSocket now, so balance and status come from this user's account.
        try:
            await manager.connect_new_sessions(user_id, sorted(stored))
        except Exception as exc:   # a slow broker must not fail the login; checked again before trading
            log.warning("post-login connection check failed: %s", type(exc).__name__)
        return sorted(stored)

    web_base, web_proxy = sign_in_network(cfg)

    def default_backend() -> PlaywrightBackend:
        return PlaywrightBackend(cfg.headless, low_memory=cfg.low_memory, block_assets=cfg.block_login_assets,
                                 block_third_party=cfg.block_third_party, switch_delay=cfg.cabinet_switch_delay,
                                 single_process=cfg.chromium_single_process,
                                 frame_interval=cfg.screenshot_interval if cfg.low_memory else None,
                                 login_url=login_url(web_base), cabinet_urls=cabinet_urls(web_base), proxy=web_proxy,
                                 new_headless=cfg.chromium_new_headless)

    handshakes = HandshakeManager(cfg, backend_factory or default_backend, sink, clock, on_failure=notifier.send)
    return Runtime(cfg, repo, vault, health, feed, manager, handshakes, notifier)
