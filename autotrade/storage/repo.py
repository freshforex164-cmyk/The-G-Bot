"""Repository interface + in-memory implementation (tests and local dev)."""
from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from ..trading.models import TradeRecord, TradingSettings


@dataclass
class CredentialRow:
    user_id: str
    kind: str                      # demo | real
    ciphertext: bytes
    key_version: int
    status: str = "active"         # active | expired
    captured_at: datetime | None = None
    last_checked_at: datetime | None = None
    last_error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)  # non-secret refresh metadata


@dataclass
class BotState:
    user_id: str
    desired_running: bool = False
    status: str = "stopped"
    last_error: str | None = None
    updated_at: datetime | None = None


class Repository(Protocol):
    async def upsert_credential(self, row: CredentialRow) -> None: ...
    async def get_credential(self, user_id: str, kind: str) -> CredentialRow | None: ...
    async def list_credentials(self, user_id: str) -> list[CredentialRow]: ...
    async def mark_credential(self, user_id: str, kind: str, status: str, error: str | None, now: datetime) -> None: ...
    async def delete_credentials(self, user_id: str) -> None: ...
    async def list_credentials_to_recheck(self, older_than: datetime, limit: int) -> list[CredentialRow]: ...
    async def get_settings(self, user_id: str) -> TradingSettings: ...
    async def save_settings(self, user_id: str, settings: TradingSettings) -> None: ...
    async def insert_trade(self, trade: TradeRecord) -> None: ...
    async def update_trade(self, user_id: str, client_ref: str, **changes: Any) -> None: ...
    async def open_trades(self, user_id: str) -> list[TradeRecord]: ...
    async def realized_pnl_since(self, user_id: str, since: datetime, mode: str) -> float: ...
    async def recent_trades(self, user_id: str, limit: int) -> list[TradeRecord]: ...
    async def trade_stats(self, user_id: str, since: datetime | None) -> dict[str, Any]: ...
    async def set_bot_state(self, state: BotState) -> None: ...
    async def get_bot_state(self, user_id: str) -> BotState | None: ...
    async def list_desired_running(self) -> list[str]: ...
    async def audit(self, user_id: str | None, event: str, detail: dict[str, Any]) -> None: ...


def summarize(trades: list[TradeRecord]) -> dict[str, Any]:
    settled = [t for t in trades if t.status == "settled"]
    wins = sum(1 for t in settled if t.outcome == "win")
    losses = sum(1 for t in settled if t.outcome == "loss")
    draws = sum(1 for t in settled if t.outcome == "draw")
    pnl = round(sum(t.profit or 0.0 for t in settled), 2)
    decided = wins + losses
    return {
        "trades": len(trades),
        "settled": len(settled),
        "open": sum(1 for t in trades if t.status in ("pending", "open")),
        "failed": sum(1 for t in trades if t.status in ("failed", "unknown")),
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "win_rate": round(wins / decided * 100, 1) if decided else None,
        "net_profit": pnl,
    }


class InMemoryRepository:
    def __init__(self) -> None:
        self.credentials: dict[tuple[str, str], CredentialRow] = {}
        self.settings: dict[str, TradingSettings] = {}
        self.trades: list[TradeRecord] = []
        self.states: dict[str, BotState] = {}
        self.audit_log: list[tuple[str | None, str, dict[str, Any]]] = []

    async def upsert_credential(self, row: CredentialRow) -> None:
        self.credentials[(row.user_id, row.kind)] = copy.deepcopy(row)

    async def get_credential(self, user_id: str, kind: str) -> CredentialRow | None:
        return copy.deepcopy(self.credentials.get((user_id, kind)))

    async def list_credentials(self, user_id: str) -> list[CredentialRow]:
        return [copy.deepcopy(r) for (u, _), r in self.credentials.items() if u == user_id]

    async def mark_credential(self, user_id, kind, status, error, now) -> None:
        row = self.credentials.get((user_id, kind))
        if row:
            row.status, row.last_error, row.last_checked_at = status, error, now

    async def delete_credentials(self, user_id: str) -> None:
        for key in [k for k in self.credentials if k[0] == user_id]:
            del self.credentials[key]

    async def list_credentials_to_recheck(self, older_than, limit):
        rows = [r for r in self.credentials.values()
                if r.status == "active" and (r.last_checked_at is None or r.last_checked_at < older_than)]
        return [copy.deepcopy(r) for r in rows[:limit]]

    async def get_settings(self, user_id: str) -> TradingSettings:
        return self.settings.get(user_id, TradingSettings())

    async def save_settings(self, user_id: str, settings: TradingSettings) -> None:
        self.settings[user_id] = settings

    async def insert_trade(self, trade: TradeRecord) -> None:
        self.trades.append(copy.deepcopy(trade))

    async def update_trade(self, user_id: str, client_ref: str, **changes: Any) -> None:
        for t in self.trades:
            if t.user_id == user_id and t.client_ref == client_ref:
                for k, v in changes.items():
                    setattr(t, k, v)

    async def open_trades(self, user_id: str) -> list[TradeRecord]:
        return [copy.deepcopy(t) for t in self.trades if t.user_id == user_id and t.status in ("pending", "open")]

    async def realized_pnl_since(self, user_id, since, mode) -> float:
        return round(sum(t.profit or 0.0 for t in self.trades
                         if t.user_id == user_id and t.mode == mode and t.status == "settled"
                         and t.settled_at and t.settled_at >= since), 2)

    async def recent_trades(self, user_id, limit):
        rows = [t for t in self.trades if t.user_id == user_id]
        return [copy.deepcopy(t) for t in rows[-limit:]][::-1]

    async def trade_stats(self, user_id, since):
        rows = [t for t in self.trades if t.user_id == user_id and (since is None or (t.opened_at and t.opened_at >= since))]
        return summarize(rows)

    async def set_bot_state(self, state: BotState) -> None:
        self.states[state.user_id] = copy.deepcopy(state)

    async def get_bot_state(self, user_id):
        return copy.deepcopy(self.states.get(user_id))

    async def list_desired_running(self):
        return [u for u, s in self.states.items() if s.desired_running]

    async def audit(self, user_id, event, detail) -> None:
        self.audit_log.append((user_id, event, detail))
