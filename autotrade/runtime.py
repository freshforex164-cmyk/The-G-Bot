"""Wires the components together. ``build_runtime`` is the one place to change when integrating."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .auth.handshake import CaptureBackend, HandshakeManager, PlaywrightBackend
from .auth.health import SessionHealth
from .clock import Clock
from .config import Config
from .market.feed import SharedMarketFeed
from .notify import NullNotifier, TelegramNotifier
from .storage.repo import InMemoryRepository, Repository
from .trading.broker import BrokerClient, PocketOptionBroker
from .trading.manager import TradingManager
from .vault.crypto import KeyRing
from .vault.service import TokenVault


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

    async def sink(user_id: str, tokens: dict[str, str], meta: dict[str, Any]) -> None:
        derived = set(meta.get("derived", []))
        # Captured cookies are transient until encrypted; never include them in row metadata/audit.
        browser_session = meta.pop("_browser_session", None)
        for kind, ssid in tokens.items():
            await vault.store(user_id, kind, ssid, {"method": meta.get("method"), "derived": kind in derived},
                              browser_session=browser_session)
        await repo.save_settings(user_id, await repo.get_settings(user_id))  # creates the Demo default for new users
        await manager.reset_to_demo(user_id, "new login")
        await repo.audit(user_id, "tokens_captured", {"kinds": sorted(tokens), "method": meta.get("method")})

    def default_backend() -> PlaywrightBackend:
        return PlaywrightBackend(cfg.headless, low_memory=cfg.low_memory, block_assets=cfg.block_login_assets,
                                 block_third_party=cfg.block_third_party)

    handshakes = HandshakeManager(cfg, backend_factory or default_backend, sink, clock)
    return Runtime(cfg, repo, vault, health, feed, manager, handshakes, notifier)
