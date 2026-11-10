"""
=============================================================
  CRYPTOBOT — Configuration
=============================================================
  ⚠️  WARNING: Crypto trading carries significant risk.
      Start on TESTNET. Only risk what you can afford to lose.
=============================================================
  STRATEGY: Dual-Timeframe Capitulation Mean-Reversion
  - Smart Dual-Timeframe: 1h for Mega-Caps (BTC, ETH, SOL, XRP),
    15m + 1h fallback for agile altcoins
  - Smart BTC Regime Gate (1h EMA200): blocks weak dips in
    bearish regimes while allowing Extreme Capitulation (Score >= 80)
  - $10M 24h volume floor & spread/orderbook depth filters
  - Dynamic 3-Stage Exit: ATR Catastrophe Stop -> Break-Even
    Lock (+2.2%) -> 0.7% Trailing Stop & EMA21 Reversion Exit
  - 6% daily circuit breaker & consecutive loss protection
=============================================================
"""
import os
from dotenv import load_dotenv

load_dotenv()

# --- 1. API & Environment Credentials ---
USE_TESTNET = os.getenv('USE_TESTNET', 'False').lower() in ('true', '1', 't')
if USE_TESTNET:
    API_KEY = os.getenv('BINANCE_TESTNET_API_KEY', '') or os.getenv('BINANCE_API_KEY', '')
    API_SECRET = os.getenv('BINANCE_TESTNET_API_SECRET', '') or os.getenv('BINANCE_API_SECRET', '')
else:
    API_KEY = os.getenv('BINANCE_API_KEY', '')
    API_SECRET = os.getenv('BINANCE_API_SECRET', '')

# --- 2. Strategy & Dual-Timeframe Scanner Settings ---
ENABLE_SCANNER = True
STRATEGY_MODE = 'capitulation'  # 'capitulation' (Dual-Regime Mean Reversion) or 'breakout'
ENABLE_DUAL_TIMEFRAME = True
ONE_HOUR_ONLY_SYMBOLS = [
    'BTCUSDT',
    'ETHUSDT',
    'SOLUSDT',
    'XRPUSDT',
]
SCANNER_INTERVAL = '15m'
SCANNER_CANDLE_LIMIT = 250
CANDLE_INTERVAL = '15m'
CANDLE_LIMIT = 250
SCANNER_CACHE_SEC = 60
SCANNER_TOP_N = 3
SCANNER_RESCAN_INTERVAL = 300
MIN_SCAN_VOLUME_USDT = 10000000
MAX_SCAN_CANDIDATES = 50
MIN_SCORE_TO_TRADE = 75
MIN_VOLUME_RATIO = 1.5
MIN_24H_CHANGE_ABS = 0.5

# --- 3. BTC Market Regime & Confirmation Gate ---
ENABLE_BTC_REGIME_FILTER = True
BTC_REGIME_EMA_PERIOD = 200
BTC_REGIME_INTERVAL = '1h'
BTC_REGIME_CACHE_SEC = 300
ENABLE_HIGH_SCORE_OVERRIDE = True
HIGH_SCORE_THRESHOLD = 80
REQUIRE_CONFIRMATION = False
CONFIRMATION_SCANS = 2

# --- 4. Position Sizing & Order Execution ---
TRADE_BALANCE_PCT = 99
SCANNER_TRADE_PCT = 99
MAX_TRADE_USDT = 20
MIN_NOTIONAL_USDT = 11
USE_QUOTE_ORDER_QTY = True
USE_EXCHANGE_OCO = True
ROUND_TRIP_FEE_PCT = 0.2

# --- 5. Dynamic Stop-Loss, Take-Profit & Trailing Parameters ---
MIN_SL_PCT = 1.5
MIN_TP_PCT = 1.8
SCANNER_USE_ATR_STOPS = True
SCANNER_ATR_SL_MULTIPLIER = 4.8
SCANNER_ATR_TP_MULTIPLIER = 2.0
SCANNER_STOP_LOSS_PCT = 4.5
SCANNER_MAX_STOP_LOSS_PCT = 7.0
SCANNER_TAKE_PROFIT_PCT = 2.2
SCANNER_TRAILING_STOP = True
SCANNER_TRAILING_OFFSET = 0.7
MIN_TRAILING_PROFIT_PCT = 1.6
ENABLE_BREAK_EVEN_STOP = True
BREAK_EVEN_TRIGGER_PCT = 2.2
MAX_SCANNER_HOLD_HOURS = 8
HOLD_EXIT_LOSS_PCT = -1

# --- 6. Technical Indicator & Safety Filter Thresholds ---
EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
VOLUME_MA_PERIOD = 20
ATR_PERIOD = 14
STABLECOIN_ATR_THRESHOLD = 0.3
DOJI_MAX_BODY_RATIO = 0.15
MIN_ORDERBOOK_RATIO = 0.70
PARABOLIC_PUMP_MAX_PCT = 25.0
MAX_24H_PUMP_PCT = 8

# Stablecoins & wrapped tokens excluded from scanning
SNIPE_BLACKLIST = [
    'USDCUSDT',
    'BUSDUSDT',
    'TUSDUSDT',
    'USDTUSDT',
    'DAIUSDT',
    'FDUSDUSDT',
    'RLUSDUSDT',
    'USDSUSDT',
    'USDPUSDT',
    'EURIUSDT',
    'AEURUSDT',
    'EURUSDT',
    'GBPUSDT',
    'WBTCUSDT',
    'WBETHUSDT',
]

# --- 7. Portfolio Risk & Circuit Breaker Limits ---
MAX_DAILY_LOSS = 6
MAX_OPEN_TRADES = 1
MAX_CONSECUTIVE_LOSSES = 2
CONSECUTIVE_LOSS_COOLDOWN_SEC = 1800
COOLDOWN_AFTER_SL_SEC = 1800
SYMBOL_COOLDOWN_SEC = 3600
POLL_INTERVAL = 30
MAX_RETRIES = 3
RETRY_DELAY = 5
BALANCE_CACHE_TTL = 10
POSITIONS_FILE = 'positions.json'
TRADE_LOG_FILE = 'trade_log.json'

# --- 8. Telegram & Logging ---
ENABLE_TELEGRAM = os.getenv('ENABLE_TELEGRAM', 'False').lower() in ('true', '1', 't')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
LOG_FILE = os.getenv('LOG_FILE', 'bot.log')
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
