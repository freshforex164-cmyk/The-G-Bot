"""Runtime settings for the G Bots engine."""

import json
import os

DEFAULT_MODE = "demo"
MODE = os.environ.get("MODE", DEFAULT_MODE).strip().lower()
VALID_MODES = ("demo", "real")
SSID_ENV_BY_MODE = {
    "demo": "PO_SSID_DEMO",
    "real": "PO_SSID_REAL",
}

# Reserved for future persistence. Each mode gets its own namespace so demo and
# real balances, trades, and settings can never share a storage key.
STATE_KEYS_BY_MODE = {
    "demo": {
        "peak_balance": "g_bots:demo:peak_balance",
        "trades": "g_bots:demo:trades",
        "settings": "g_bots:demo:settings",
    },
    "real": {
        "peak_balance": "g_bots:real:peak_balance",
        "trades": "g_bots:real:trades",
        "settings": "g_bots:real:settings",
    },
}
MIN_PAYOUT = 80

CANDLE_INTERVAL_SECONDS = 10
MAX_CANDLES_PER_PAIR = 300
HISTORY_LOOKBACK_SECONDS = CANDLE_INTERVAL_SECONDS * MAX_CANDLES_PER_PAIR
TOP_PAIRS_COUNT = 3
RUN_DURATION_SECONDS = 120
MAX_PAIRS = 5
SIGNAL_RUN_DEFAULT_MINUTES = 30

# Signal indicator settings. Bollinger Bands must stay enabled because they
# define both the setup and live trigger level.
USE_MOVING_AVERAGE = True
MOVING_AVERAGE_METHOD = "EMA"
MOVING_AVERAGE_PERIOD = 10

USE_BOLLINGER_BANDS = True
BOLLINGER_PERIOD = 20
BOLLINGER_STANDARD_DEVIATIONS = 2.0

USE_STOCHASTIC = True
STOCHASTIC_PERIOD = 14
STOCHASTIC_SMOOTH_K = 3
STOCHASTIC_SMOOTH_D = 3
STOCHASTIC_OVERBOUGHT = 80.0
STOCHASTIC_OVERSOLD = 20.0
STOCHASTIC_USE_ALTERNATE_LEVELS = False
STOCHASTIC_ALTERNATE_OVERBOUGHT = 70.0
STOCHASTIC_ALTERNATE_OVERSOLD = 30.0

USE_KELTNER_CHANNEL = False
KELTNER_EMA_PERIOD = 20
KELTNER_ATR_PERIOD = 10
KELTNER_ATR_MULTIPLIER = 2.0

# Pullback/reversal lookbacks and signal/paper-trading settings.
STOCHASTIC_OVERBOUGHT_LOOKBACK_CANDLES = 5
MA_PRIOR_RISE_LOOKBACK_CANDLES = 6
MA_TURN_LOOKBACK_CANDLES = 3
BB_TREND_LOOKBACK_CANDLES = 3
MA_FLAT_TOLERANCE_PERCENT = 0.05
SIGNAL_COOLDOWN_CANDLES = 3
EXPIRY_SECONDS = 30
PAPER_STAKE = 1.0
TEST_TRADE_STAKE_CAP = 1.0
PAPER_SUMMARY_EVERY_SIGNALS = 10
SIGNALS_CSV_PATH = "signals.csv"

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


def validate_mode(mode: str) -> str:
    normalized = mode.strip().lower()
    if normalized not in VALID_MODES:
        raise ConfigurationError("MODE must be either 'demo' or 'real'.")
    return normalized


def ssid_env_var_for_mode(mode: str) -> str:
    return SSID_ENV_BY_MODE[validate_mode(mode)]


def state_keys_for_mode(mode: str) -> dict[str, str]:
    """Return a copy of the reserved state-key names for one account mode."""
    return dict(STATE_KEYS_BY_MODE[validate_mode(mode)])


def load_mode_ssid(mode: str, *, confirm_real: bool = False) -> str:
    """Load the active mode's full SSID without ever logging its contents."""
    mode = validate_mode(mode)
    secret_name = ssid_env_var_for_mode(mode)

    if mode == "real" and not confirm_real:
        raise ConfigurationError(
            "Real mode requires the --confirm-real CLI flag. "
            "Real mode is read-only in this phase."
        )

    ssid = os.environ.get(secret_name, "").strip()
    if not ssid:
        raise ConfigurationError(
            f"Missing required secret {secret_name}. Add the full "
            f"{mode} Pocket Option SSID in Replit Secrets."
        )

    if not ssid.startswith('42["auth",'):
        raise ConfigurationError(
            f"{secret_name} must contain the full SSID beginning with "
            f'42["auth",...]. Copy it from a Pocket Option {mode} session.'
        )

    try:
        payload = json.loads(ssid[2:])
    except json.JSONDecodeError:
        raise ConfigurationError(
            f"{secret_name} is not valid JSON. Replace it with the complete "
            f"current {mode} SSID."
        ) from None

    if not isinstance(payload, list) or len(payload) < 2 or payload[0] != "auth":
        raise ConfigurationError(
            f"{secret_name} is not a valid Pocket Option auth SSID. "
            f"Replace it with the complete current {mode} SSID."
        )

    return ssid
