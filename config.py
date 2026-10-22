"""Runtime settings for the GBot signal scanner."""

import json
import os

MIN_PAYOUT_OPTIONS = (80, 85, 88, 92)
MIN_PAYOUT = 80
MAX_SCAN_PAIRS = 20
PAYOUT_REFRESH_SECONDS = 60
MAX_ACTIVE_SIGNALS = 5
RECOMMENDED_EXPIRY_SECONDS = 60
EXPIRATION_SECONDS = 35
SIGNAL_VALID_SECONDS = 15
MIN_NOTIFY_STRENGTH = 0
MAX_NOTIFICATIONS_PER_MINUTE = 6
HUMAN_DELAY_SECONDS = 5
PAPER_EXPIRIES_SECONDS = (30, 60, 120)
SIGNALS_CSV_PATH = "signals.csv"

CANDLE_SECONDS = 15
CANDLE_INTERVAL_SECONDS = CANDLE_SECONDS
MAX_CANDLES_PER_PAIR = 300
HISTORY_LOOKBACK_SECONDS = CANDLE_INTERVAL_SECONDS * MAX_CANDLES_PER_PAIR
REPLAY_MAX_CANDLES_PER_PAIR = 3000
REPLAY_HISTORY_LOOKBACK_SECONDS = (
    CANDLE_INTERVAL_SECONDS * REPLAY_MAX_CANDLES_PER_PAIR
)
TOP_PAIRS_COUNT = 3
RUN_DURATION_SECONDS = 120

# Strategy indicator settings.
MOVING_AVERAGE_METHOD = "SMA"
MOVING_AVERAGE_PERIOD = 10

BOLLINGER_PERIOD = 5
BOLLINGER_STANDARD_DEVIATIONS = 1.0

STOCHASTIC_PERIOD = 14
STOCHASTIC_SMOOTH_K = 3
STOCHASTIC_SMOOTH_D = 3
STOCHASTIC_OVERBOUGHT = 80.0
STOCHASTIC_OVERSOLD = 20.0
ATR_PERIOD = 14
MIN_STRATEGY_CANDLES = 30

USE_KELTNER_CHANNEL = False
KELTNER_EMA_PERIOD = 20
KELTNER_ATR_PERIOD = 14
KELTNER_ATR_MULTIPLIER = 2.0

# Pullback/reversal rules use closed candles: index 0 is latest, index 1 previous.
TRIGGER_VARIANT = "A"
STOCHASTIC_LOOKBACK_CANDLES = 5
STOCHASTIC_LOOKBACK_OPTIONS = (3, 5, 8)
MA_RISE_MIN = 0.0005
MA_FLAT_TOL = 0.0005
MID_FLAT_TOL = 0.0005
BB_WIDTH_MEAN_LOOKBACK_CANDLES = 50
RANGE_MAX_RATIO = 1.2
S1_ENABLED = True
S2_ENABLED = True
S3_ENABLED = True
S4_ENABLED = True
SIGNAL_COOLDOWN_CANDLES = 3

# Closed-candle pullback-rejection state machine. Values are in oscillator
# points, ATR multiples, or candle counts as indicated by their names.
PULLBACK_MIN_DROP = 15.0
PULLBACK_MAX_DROP = 35.0
STRENGTH_TARGET_DROP = 22.0
STRENGTH_DROP_RADIUS = 13.0
SELL_STOCHASTIC_FLOOR = 40.0
BUY_STOCHASTIC_CEILING = 60.0
SETUP_WINDOW_CANDLES = 3
MAX_BODY_ATR_RATIO = 0.7
MIN_REJECTION_WICK_RATIO = 0.30
STRENGTH_WICK_FULL_SCORE_RATIO = 0.8
EPS_ATR_RATIO = 0.05
MAX_TOUCH_ATR = 0.6
S1_ENABLED = True
MIDDLE_ENABLED = True
LOWER_ENABLED = True
STRENGTH_WEIGHT_STOCHASTIC = 30
STRENGTH_WEIGHT_REJECTION = 30
STRENGTH_WEIGHT_TOUCH = 15
STRENGTH_WEIGHT_MA = 15
STRENGTH_WEIGHT_TIMING = 10
STRENGTH_LOWER_TOUCH_MULTIPLIER = 0.6

# Strength is confirmation quality only; payout is never part of this score.
STRENGTH_WEIGHTS = {
    "stochastic": 30,
    "moving_average": 25,
    "bollinger": 25,
    "middle_band_touch": 20,
}
STRENGTH_STOCHASTIC_EXTREMITY_REFERENCE = 5.0
STRENGTH_STOCHASTIC_TURN_REFERENCE = 5.0
STRENGTH_MA_TURN_REFERENCE_PERCENT = 0.05
STRENGTH_BB_SLOPE_REFERENCE_PERCENT = 0.05
STRENGTH_BB_CURVATURE_REFERENCE_PERCENT = 0.025
STRENGTH_TOUCH_REFERENCE_RANGE_MULTIPLIER = 0.50
STRENGTH_TARGET_PERCENTILES = ((10, 20), (50, 50), (90, 85))
BREAKEVEN_PAYOUT_PERCENT = 92
DATA_CANDLES_CSV_PATH = "data/candles.csv"

PAYOUTS_TIMEOUT_SECONDS = 20.0
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
