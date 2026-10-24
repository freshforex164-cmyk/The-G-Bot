"""Pocket Option asset metadata and OTC eligibility rules."""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("gbot.assets")


@dataclass(frozen=True)
class MarketAsset:
    symbol: str
    display_name: str
    payout: float
    is_open: bool


@dataclass(frozen=True)
class MarketAssetSnapshot:
    captured_at: datetime
    otc_pair_count: int
    assets: dict[str, MarketAsset]


@dataclass(frozen=True)
class LiveTestCandidate:
    signal: Any
    asset: MarketAsset
    live_price: float


async def read_market_asset_snapshot(
    broker: Any, *, timeout_seconds: float = 15.0
) -> MarketAssetSnapshot:
    """Read current broker payouts and market status through one shared path."""
    payouts = await asyncio.wait_for(broker.payouts(), timeout=timeout_seconds)
    rows = await asyncio.wait_for(broker.active_assets(), timeout=timeout_seconds)
    otc_pair_count = sum(
        1
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("symbol"), str)
        and row["symbol"].endswith("_otc")
    )
    return MarketAssetSnapshot(
        captured_at=datetime.now(timezone.utc),
        otc_pair_count=otc_pair_count,
        assets=normalize_assets(rows, payouts),
    )


def normalize_assets(
    rows: list[dict[str, Any]],
    payouts: dict[str, float],
) -> dict[str, MarketAsset]:
    """Return only OTC assets with a provider-supplied app display name."""
    result: dict[str, MarketAsset] = {}
    for row in rows:
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol.endswith("_otc"):
            continue
        name = row.get("name")
        if not isinstance(name, str) or not name.strip():
            logger.warning("Skipping OTC pair %s: Pocket Option provided no display name.", symbol)
            continue
        if row.get("is_otc") is False:
            continue
        if symbol not in payouts:
            logger.warning("Skipping OTC pair %s: current payout is unavailable.", symbol)
            continue
        raw_payout = payouts[symbol]
        try:
            payout = float(raw_payout)
        except (TypeError, ValueError, OverflowError):
            logger.warning("Skipping OTC pair %s: current payout is invalid.", symbol)
            continue
        if not math.isfinite(payout) or not 0 <= payout <= 100:
            logger.warning("Skipping OTC pair %s: current payout is invalid.", symbol)
            continue
        result[symbol] = MarketAsset(
            symbol=symbol,
            display_name=name.strip(),
            payout=payout,
            is_open=bool(row.get("is_active", False)),
        )
    return result


def eligible_assets(
    assets: dict[str, MarketAsset], min_payout: float, max_pairs: int
) -> list[MarketAsset]:
    return sorted(
        (
            asset
            for asset in assets.values()
            if asset.is_open and asset.payout >= min_payout
        ),
        key=lambda asset: (-asset.payout, asset.symbol),
    )[:max_pairs]
