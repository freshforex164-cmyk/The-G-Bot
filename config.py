"""Runtime settings for the GBot signal scanner."""

import json
import os

from runtime_env import load_environment

load_environment()

from strategy.settings import STRATEGY_SETTINGS

def _positive_int_setting(name: str, default: int) -> int:
    raw_value = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError:
        raise ValueError(f"{name} must be a positive integer.") from None
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return value


MIN_PAYOUT_OPTIONS = (80, 85, 88, 92)
MIN_PAYOUT = 80
MAX_SCAN_PAIRS = _positive_int_setting("MAX_SCAN_PAIRS", 20)
# Upper bound on live symbol subscriptions held at once (selected + grace period).
MAX_SUBSCRIBED = _positive_int_setting("MAX_SUBSCRIBED", 30)
PAYOUT_REFRESH_SECONDS = 60
MAX_ACTIVE_SIGNALS = 5
RECOMMENDED_EXPIRY_SECONDS = STRATEGY_SETTINGS.expiry_seconds
EXPIRATION_SECONDS = STRATEGY_SETTINGS.expiry_seconds
SIGNAL_VALID_SECONDS = STRATEGY_SETTINGS.signal_valid_seconds
MIN_NOTIFY_STRENGTH = 0
MAX_NOTIFICATIONS_PER_MINUTE = 6
HUMAN_DELAY_SECONDS = 5
PAPER_EXPIRIES_SECONDS = (EXPIRATION_SECONDS,)
SIGNALS_CSV_PATH = "data/signals.csv"

CANDLE_SECONDS = STRATEGY_SETTINGS.timeframe_seconds
CANDLE_INTERVAL_SECONDS = CANDLE_SECONDS
MAX_CANDLES_PER_PAIR = 300
HISTORY_LOOKBACK_SECONDS = CANDLE_INTERVAL_SECONDS * MAX_CANDLES_PER_PAIR
REPLAY_MAX_CANDLES_PER_PAIR = 3000
REPLAY_HISTORY_LOOKBACK_SECONDS = (
    CANDLE_INTERVAL_SECONDS * REPLAY_MAX_CANDLES_PER_PAIR
)
TOP_PAIRS_COUNT = 3
RUN_DURATION_SECONDS = 120

# Price-action diagnostics only.
ATR_PERIOD = STRATEGY_SETTINGS.atr_period
MIN_STRATEGY_CANDLES = STRATEGY_SETTINGS.min_regime_candles
BREAKEVEN_PAYOUT_PERCENT = 92
DATA_CANDLES_CSV_PATH = "data/candles.csv"

PAYOUTS_TIMEOUT_SECONDS = 20.0
RECONNECT_MAX_ATTEMPTS = 6
RECONNECT_BASE_DELAY_SECONDS = 1.0
RECONNECT_MAX_DELAY_SECONDS = 30.0
RECONNECT_CALL_TIMEOUT_SECONDS = 15.0
CONNECTION_MONITOR_INTERVAL_SECONDS = 0.25
INITIAL_CONNECT_TIMEOUT_SECONDS = 20.0
WATCHDOG_TICK_MAX_AGE_SECONDS = 5.0
WATCHDOG_GLOBAL_STALL_SECONDS = 20.0
WATCHDOG_CALL_TIMEOUT_SECONDS = 15.0
WATCHDOG_CHECK_INTERVAL_SECONDS = 1.0
WATCHDOG_RETRY_SECONDS = 30.0
WATCHDOG_REBUILD_FAILURE_LIMIT = 3

# BinaryOptionsToolsV2 applies this offset to Pocket Option timestamps.
POCKET_OPTION_TIME_OFFSET_SECONDS = 7200


class ConfigurationError(ValueError):
    """Raised when required project configuration is missing or malformed."""


def load_ssid() -> str:
    """Load and validate the single Pocket Option session without logging it."""
    secret_name = "PO_SSID"
    ssid = (os.getenv(secret_name, "").strip() or os.getenv("AUTOTRADE_FEED_SSID", "").strip())
    if not ssid:
        raise ConfigurationError(
            "Missing PO_SSID (or AUTOTRADE_FEED_SSID). Set it in the environment or repository .env file."
        )
    if not ssid.startswith('42["auth",'):
        raise ConfigurationError(
            "PO_SSID must contain the full Pocket Option auth session beginning "
            'with 42["auth",...].'
        )
    try:
        payload = json.loads(ssid[2:])
    except json.JSONDecodeError:
        raise ConfigurationError(
            "PO_SSID is not valid auth data. Replace it with the complete current "
            "Pocket Option session."
        ) from None
    if not isinstance(payload, list) or len(payload) < 2 or payload[0] != "auth":
        raise ConfigurationError(
            "PO_SSID is not a valid Pocket Option auth session. Replace it with "
            "the complete current session."
        )
    return ssid


def account_type(ssid: str | None = None) -> str:
    """Return DEMO or REAL from the SSID's isDemo flag; never returns SSID data."""
    raw = (ssid if ssid is not None else (os.getenv("PO_SSID", "").strip() or os.getenv("AUTOTRADE_FEED_SSID", "").strip())).strip()
    try:
        payload = json.loads(raw[2:]) if raw.startswith("42[") else None
        flag = payload[1].get("isDemo") if isinstance(payload, list) else None
    except (json.JSONDecodeError, AttributeError, IndexError, TypeError):
        flag = None
    if flag is None:
        return "UNKNOWN"
    return "DEMO" if int(flag) == 1 else "REAL"


PAYOUT_DISCLAIMER = "Payouts may vary."
