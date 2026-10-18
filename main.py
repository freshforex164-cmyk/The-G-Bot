"""Read-only Pocket Option demo account and candle check."""

from __future__ import annotations

import os
import sys
from typing import Any

ASSET = "EURUSD_otc"
CANDLE_PERIOD_SECONDS = 60
CANDLE_COUNT = 50


class BotError(Exception):
    """An actionable error that is safe to show in the console."""


def run_check() -> None:
    ssid = os.environ.get("PO_SSID", "").strip()
    if not ssid:
        raise BotError(
            "Missing required secret PO_SSID. Add your Pocket Option demo "
            "session ID in Replit Secrets, then run this script again."
        )

    try:
        from BinaryOptionsToolsV2.pocketoption import PocketOption
    except ImportError:
        raise BotError(
            "BinaryOptionsToolsV2 is not installed. Install project dependencies "
            "with `uv sync`, then run `uv run main.py`."
        ) from None

    client: Any | None = None
    try:
        client = PocketOption(ssid=ssid)

        if not client.is_demo():
            raise BotError(
                "PO_SSID is not connected to a demo account. Refusing to read "
                "account data; switch Pocket Option to demo and update PO_SSID."
            )

        balance = client.balance()
        print(f"Demo balance: {balance}")

        # One hour of backfill provides extra room for closed candles while
        # max_rows limits the returned history to the requested 50 candles.
        candle_stream = client.get_candles_live(
            ASSET,
            period=CANDLE_PERIOD_SECONDS,
            hours=1.0,
            max_rows=CANDLE_COUNT,
        )
        first_update = next(iter(candle_stream), None)
        if first_update is None:
            raise BotError(f"Pocket Option returned no candles for {ASSET}.")

        closed_candles, _forming_candle = first_update
        candles = list(closed_candles or [])
        if len(candles) < CANDLE_COUNT:
            raise BotError(
                f"Received {len(candles)} closed one-minute candles for {ASSET}; "
                f"expected {CANDLE_COUNT}. Try again when more history is available."
            )

        latest_candle = candles[-1]
        print(f"Retrieved {len(candles)} one-minute candles for {ASSET}.")
        print(f"Latest closed candle: {latest_candle}")
    except BotError:
        raise
    except Exception as exc:
        # Do not print raw provider exceptions: they can contain request details.
        raise BotError(
            f"Pocket Option request failed ({type(exc).__name__}). Check that "
            "PO_SSID is current, belongs to a demo account, and Pocket Option "
            "is reachable. This script does not place trades."
        ) from None
    finally:
        if client is not None:
            try:
                client.shutdown()
            except Exception as exc:
                print(
                    f"Warning: Pocket Option cleanup failed ({type(exc).__name__}).",
                    file=sys.stderr,
                )


def main() -> int:
    try:
        run_check()
    except BotError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
