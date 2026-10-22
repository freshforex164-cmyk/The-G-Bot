"""Pocket Option asset metadata and OTC eligibility rules."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("gbot.assets")


@dataclass(frozen=True)
class MarketAsset:
    symbol: str
    display_name: str
    payout: float
    is_open: bool


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
        raw_payout = payouts.get(symbol, row.get("payout"))
        try:
            payout = float(raw_payout)
        except (TypeError, ValueError, OverflowError):
            payout = 0.0
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
