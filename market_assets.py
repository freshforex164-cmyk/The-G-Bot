"""Catalogue loading and runtime Pocket Option symbol matching."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CatalogAsset:
    id: str
    category: str
    name: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class MarketAsset:
    catalog_id: str
    category: str
    display_name: str
    symbol: str
    payout: float | None
    is_open: bool


def normalize_asset_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]", "", value.casefold())
    return normalized[:-3] if normalized.endswith("otc") else normalized


def load_catalog(path: str | Path = "assets.json") -> tuple[CatalogAsset, ...]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    result: list[CatalogAsset] = []
    for category in payload["categories"]:
        for item in category["assets"]:
            result.append(
                CatalogAsset(
                    id=str(item["id"]),
                    category=str(category["name"]),
                    name=str(item["name"]),
                    aliases=tuple(map(str, item.get("aliases", ()))),
                )
            )
    return tuple(result)


CATALOG = load_catalog()
CATALOG_BY_ID = {asset.id: asset for asset in CATALOG}
CATEGORIES = tuple(dict.fromkeys(asset.category for asset in CATALOG))


def match_runtime_assets(
    rows: list[dict[str, Any]], payouts: dict[str, float] | None = None
) -> dict[str, MarketAsset]:
    """Map exact catalogue names/aliases to live broker symbols without filtering."""
    payouts = payouts or {}
    result: dict[str, MarketAsset] = {}
    for row in rows:
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            continue
        provider_name = row.get("name")
        candidates = {normalize_asset_name(symbol)}
        if isinstance(provider_name, str) and provider_name:
            candidates.add(normalize_asset_name(provider_name))
        for asset in CATALOG:
            names = (asset.name, *asset.aliases)
            if not candidates.intersection(map(normalize_asset_name, names)):
                continue
            payout_value = payouts.get(symbol)
            try:
                payout = float(payout_value) if payout_value is not None else None
            except (TypeError, ValueError, OverflowError):
                payout = None
            if payout is not None and not 0 <= payout <= 100:
                payout = None
            result[asset.id] = MarketAsset(
                catalog_id=asset.id,
                category=asset.category,
                display_name=asset.name,
                symbol=symbol,
                payout=payout,
                is_open=bool(row.get("is_active", row.get("is_open", False))),
            )
            break
    return result


def asset_table(
    matches: dict[str, MarketAsset], *, include_closed: bool = True
) -> list[tuple[str, str | None]]:
    return [
        (
            asset.name,
            matches[asset.id].symbol if asset.id in matches else None,
        )
        for asset in CATALOG
        if include_closed or (asset.id in matches and matches[asset.id].is_open)
    ]
