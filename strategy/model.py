"""Domain model shared by price-action signals and Telegram delivery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4


@dataclass
class Signal:
    pair: str
    direction: str
    setup: str
    price: float
    time: datetime
    asset_name: str
    payout: float | None = None
    expiry_seconds: int = 60
    valid_until: datetime | None = None
    id: str = ""
    status: str = "ACTIVE"

    @property
    def entry_price(self) -> float:
        return self.price

    def __post_init__(self) -> None:
        if self.expiry_seconds != 60:
            raise ValueError("Signals require 1-minute expiry.")
        if self.direction not in {"BUY", "SELL"}:
            raise ValueError("Signal direction must be BUY or SELL.")
        if not self.id:
            self.id = uuid4().hex
