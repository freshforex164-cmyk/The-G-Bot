"""Runtime settings for the G Bots engine."""

import json
import os

PO_SSID_ENV_VAR = "PO_SSID_DEMO"
MIN_PAYOUT = 80

CANDLE_INTERVAL_SECONDS = 10
MAX_CANDLES_PER_PAIR = 300
HISTORY_LOOKBACK_SECONDS = CANDLE_INTERVAL_SECONDS * MAX_CANDLES_PER_PAIR
TOP_PAIRS_COUNT = 3
RUN_DURATION_SECONDS = 120

RECONNECT_MAX_ATTEMPTS = 6
RECONNECT_BASE_DELAY_SECONDS = 1.0
RECONNECT_MAX_DELAY_SECONDS = 30.0
RECONNECT_CALL_TIMEOUT_SECONDS = 15.0
CONNECTION_MONITOR_INTERVAL_SECONDS = 0.25
INITIAL_CONNECT_TIMEOUT_SECONDS = 20.0

# BinaryOptionsToolsV2 applies this same offset to Pocket Option timestamps
# while compiling live candles.
POCKET_OPTION_TIME_OFFSET_SECONDS = 7200


class ConfigurationError(ValueError):
    """Raised when required project configuration is missing or malformed."""


def load_demo_ssid() -> str:
    """Load and validate the full demo SSID without ever logging its contents."""
    ssid = os.environ.get(PO_SSID_ENV_VAR, "").strip()
    if not ssid:
        raise ConfigurationError(
            f"Missing required secret {PO_SSID_ENV_VAR}. Add the full demo "
            "SSID in Replit Secrets."
        )

    if not ssid.startswith('42["auth",'):
        raise ConfigurationError(
            f"{PO_SSID_ENV_VAR} must contain the full SSID beginning with "
            '42["auth",...]. Copy it from a Pocket Option demo session.'
        )

    try:
        payload = json.loads(ssid[2:])
    except json.JSONDecodeError:
        raise ConfigurationError(
            f"{PO_SSID_ENV_VAR} is not valid JSON. Replace it with the complete "
            "current demo SSID."
        ) from None

    if not isinstance(payload, list) or len(payload) < 2 or payload[0] != "auth":
        raise ConfigurationError(
            f"{PO_SSID_ENV_VAR} is not a valid Pocket Option auth SSID. "
            "Replace it with the complete current demo SSID."
        )

    return ssid
