"""Runtime settings for the GBot signal scanner."""

import json
import os

MIN_PAYOUT_OPTIONS = (80, 85, 88, 92)
MIN_PAYOUT = 80
MAX_SCAN_PAIRS = 20
PAYOUT_REFRESH_SECONDS = 60
MAX_ACTIVE_SIGNALS = 5
RECOMMENDED_EXPIRY_SECONDS = 60
SIGNAL_VALID_SECONDS = 15
MIN_NOTIFY_STRENGTH = 0
MAX_NOTIFICATIONS_PER_MINUTE = 6
HUMAN_DELAY_SECONDS = 5
PAPER_EXPIRIES_SECONDS = (30, 60, 120)
SIGNALS_CSV_PATH = "signals.csv"

CANDLE_INTERVAL_SECONDS = 10
MAX_CANDLES_PER_PAIR = 300
HISTORY_LOOKBACK_SECONDS = CANDLE_INTERVAL_SECONDS * MAX_CANDLES_PER_PAIR
TOP_PAIRS_COUNT = 3
RUN_DURATION_SECONDS = 120

# Strategy indicator settings. Bollinger Bands define the setup and live trigger.
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

# Pullback/reversal lookbacks and per-pair cooldown.
STOCHASTIC_OVERBOUGHT_LOOKBACK_CANDLES = 5
MA_PRIOR_RISE_LOOKBACK_CANDLES = 6
MA_TURN_LOOKBACK_CANDLES = 3
BB_TREND_LOOKBACK_CANDLES = 3
MA_FLAT_TOLERANCE_PERCENT = 0.05
SIGNAL_COOLDOWN_CANDLES = 3

# Strength is confirmation quality only; payout is never part of this score.
STRENGTH_WEIGHTS = {
    "stochastic": 30,
    "moving_average": 25,
    "bollinger": 25,
    "middle_band_touch": 20,
}
STRENGTH_STOCHASTIC_EXTREMITY_REFERENCE = 15.0
STRENGTH_STOCHASTIC_TURN_REFERENCE = 10.0
STRENGTH_MA_TURN_REFERENCE_PERCENT = 0.10
STRENGTH_BB_SLOPE_REFERENCE_PERCENT = 0.10
STRENGTH_BB_CURVATURE_REFERENCE_PERCENT = 0.05
STRENGTH_TOUCH_REFERENCE_RANGE_MULTIPLIER = 0.50

RECONNECT_MAX_ATTEMPTS = 6
RECONNECT_BASE_DELAY_SECONDS = 1.0
RECONNECT_MAX_DELAY_SECONDS = 30.0
RECONNECT_CALL_TIMEOUT_SECONDS = 15.0
CONNECTION_MONITOR_INTERVAL_SECONDS = 0.25
INITIAL_CONNECT_TIMEOUT_SECONDS = 20.0

# BinaryOptionsToolsV2 applies this offset to Pocket Option timestamps.
POCKET_OPTION_TIME_OFFSET_SECONDS = 7200


class ConfigurationError(ValueError):
    """Raised when required project configuration is missing or malformed."""


def load_ssid() -> str:
    """Load and validate the single Pocket Option session without logging it."""
    secret_name = "PO_SSID"
    ssid = os.environ.get(secret_name, "").strip()
    if not ssid:
        raise ConfigurationError(
            "Missing required secret PO_SSID. Add PO_SSID in Replit Secrets."
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
