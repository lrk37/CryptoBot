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

# --- 1. API & Environment Credentials (Safe Default: Testnet) ---
USE_TESTNET = os.getenv('USE_TESTNET', 'True').lower() in ('true', '1', 't')
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
SCANNER_CANDLE_LIMIT = 500  # Sufficient bars for converged EMA200 warmup
SCANNER_TOP_N = 3
SCANNER_RESCAN_INTERVAL = 300
ALIGN_SCANS_TO_CANDLE = True
SCAN_ALIGN_OFFSET_SEC = 3
MAX_SIGNAL_AGE_SEC = 480  # Skip 15m signals > 8m after closed bar (1h signals scale proportionally)
MIN_SCAN_VOLUME_USDT = float(os.getenv('MIN_SCAN_VOLUME_USDT', '20000000'))
MAX_SCAN_CANDIDATES = int(os.getenv('MAX_SCAN_CANDIDATES', '50'))
MIN_SCORE_TO_TRADE = float(os.getenv('MIN_SCORE_TO_TRADE', '75'))
ENABLE_LISTING_AGE_FILTER = os.getenv('ENABLE_LISTING_AGE_FILTER', 'True').lower() in ('true', '1', 't')
MIN_LISTING_AGE_DAYS = int(os.getenv('MIN_LISTING_AGE_DAYS', '60'))
ENABLE_FUTURES_CONFIRMATION = os.getenv('ENABLE_FUTURES_CONFIRMATION', 'True').lower() in ('true', '1', 't')
FUTURES_MAX_OI_SPIKE_PCT = float(os.getenv('FUTURES_MAX_OI_SPIKE_PCT', '5.0'))
FUTURES_MAX_FUNDING_PCT = float(os.getenv('FUTURES_MAX_FUNDING_PCT', '0.05'))
FUTURES_OI_FLUSH_PCT = float(os.getenv('FUTURES_OI_FLUSH_PCT', '-1.5'))

# --- 3. BTC Market Regime & Confirmation Gate ---
ENABLE_BTC_REGIME_FILTER = True
BTC_REGIME_EMA_PERIOD = 200
BTC_REGIME_INTERVAL = '1h'
BTC_REGIME_CACHE_SEC = 300
ENABLE_HIGH_SCORE_OVERRIDE = True
HIGH_SCORE_THRESHOLD = 85
REQUIRE_CONFIRMATION = False

# --- 4. Position Sizing & Order Execution ---
SCANNER_TRADE_PCT = float(os.getenv('SCANNER_TRADE_PCT', '99'))
MAX_TRADE_USDT = float(os.getenv('MAX_TRADE_USDT', '20'))  # Set 0 in .env to scale dynamically with USDT balance
MIN_NOTIONAL_USDT = float(os.getenv('MIN_NOTIONAL_USDT', '11'))
USE_RISK_BASED_SIZING = os.getenv('USE_RISK_BASED_SIZING', 'False').lower() in ('true', '1', 't')
MAX_RISK_PER_TRADE_PCT = float(os.getenv('MAX_RISK_PER_TRADE_PCT', '2.5'))
USE_QUOTE_ORDER_QTY = True
USE_LIMIT_MAKER_ENTRY = os.getenv('USE_LIMIT_MAKER_ENTRY', 'True').lower() in ('true', '1', 't')
LIMIT_MAKER_WAIT_SEC = float(os.getenv('LIMIT_MAKER_WAIT_SEC', '6.0'))
USE_EXCHANGE_OCO = True
OCO_STOP_LIMIT_OFFSET_PCT = 0.8  # Place OCO stopLimitPrice 0.8% below stopPrice trigger
ROUND_TRIP_FEE_PCT = 0.2
WARN_ZERO_BNB = os.getenv('WARN_ZERO_BNB', 'True').lower() in ('true', '1', 't')
MIN_BNB_FEE_RESERVE_USDT = float(os.getenv('MIN_BNB_FEE_RESERVE_USDT', '1.0'))

# --- 5. Dynamic Stop-Loss, Take-Profit & Trailing Parameters ---
# Validated across 90d Dual-TF (93 trades, 87.1% WR, PF 3.23) & 365d Full-Year 1h (99 trades, 71.7% WR, PF 1.94):
# Break-Even & Trailing arm together at +1.8% while OCO TP sits strictly higher (>= +2.0% / 2.2x ATR).
MIN_SL_PCT = 1.5
MIN_TP_PCT = 2.0
SCANNER_USE_ATR_STOPS = True
SCANNER_ATR_SL_MULTIPLIER = 4.8
SCANNER_ATR_TP_MULTIPLIER = 2.2
SCANNER_STOP_LOSS_PCT = 4.5
SCANNER_MAX_STOP_LOSS_PCT = 4.8
SCANNER_TAKE_PROFIT_PCT = 2.2
SCANNER_TRAILING_STOP = True
SCANNER_TRAILING_OFFSET = 0.7
MIN_TRAILING_PROFIT_PCT = 1.6    # +0.2% RT fees => Trailing & Break-Even arm together at +1.8%
ENABLE_BREAK_EVEN_STOP = True
BREAK_EVEN_TRIGGER_PCT = 1.8
BREAK_EVEN_SLIPPAGE_BUFFER_PCT = 0.10
MAX_SCANNER_HOLD_HOURS = 8

# --- 6. Technical Indicator & Safety Filter Thresholds ---
EMA_SLOW = 21
STABLECOIN_ATR_THRESHOLD = 0.3
DOJI_MAX_BODY_RATIO = 0.15
MAX_SPREAD_PCT = 0.25
MIN_ORDERBOOK_RATIO = 0.70
PARABOLIC_PUMP_MAX_PCT = 25.0
MAX_24H_DUMP_PCT = -18.0
MAX_24H_HIGH_DUMP_PCT = -18.0    # Also block intraday crashes >18% off the rolling 24h high
MAX_FORMING_BOUNCE_PCT = 0.45    # Skip if forming bar already bounced >0.45% above signal close
MAX_FORMING_HIGH_PCT = 0.85      # Skip if forming bar wick already ran >=0.85% above signal close
MAX_FORMING_DUMP_PCT = -1.20     # Skip if forming bar is actively collapsing >1.2% below signal close
SCANNER_MAX_WORKERS = 6          # Parallel kline fetch threads (cuts 50-symbol scan from ~25s to ~3s)

# Stablecoins, wrapped tokens & fee token (BNB) excluded from scanning
SNIPE_BLACKLIST = [
    'BNBUSDT',
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
MAX_DAILY_LOSS = float(os.getenv('MAX_DAILY_LOSS', '6'))
MAX_TOTAL_DRAWDOWN_PCT = float(os.getenv('MAX_TOTAL_DRAWDOWN_PCT', '12.0'))  # Multi-day kill-switch: halt if equity drops 12% below peak balance
MAX_OPEN_TRADES = int(os.getenv('MAX_OPEN_TRADES', '1'))
MAX_CONSECUTIVE_LOSSES = 2
CONSECUTIVE_LOSS_COOLDOWN_SEC = 1800
COOLDOWN_AFTER_SL_SEC = 43200
SYMBOL_COOLDOWN_SEC = 3600
POLL_INTERVAL = 30
MAX_RETRIES = 3
RETRY_DELAY = 5
SUBMITTING_RECOVERY_GRACE_SEC = 15
SUBMITTING_NOT_FOUND_CONFIRMATIONS = 2
BALANCE_CACHE_TTL = 10
POSITIONS_FILE = 'positions.json'
TRADE_LOG_FILE = 'trade_log.json'

# --- 8. Telegram & Logging ---
ENABLE_TELEGRAM = os.getenv('ENABLE_TELEGRAM', 'False').lower() in ('true', '1', 't')
ENABLE_DAILY_HEARTBEAT = True    # Send once-daily Telegram status & expectancy summary
TELEGRAM_SEND_CHARTS = True      # Automatically attach Entry/Exit PNG chart snapshots to Telegram
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
DISPLAY_TIMEZONE = os.getenv('DISPLAY_TIMEZONE', 'Europe/Belgrade')  # DST-aware CET/CEST (UTC+1/UTC+2)
DISPLAY_TZ_OFFSET_HOURS = int(os.getenv('DISPLAY_TZ_OFFSET_HOURS', '1'))  # Fallback fixed offset (UTC+1)
LOG_FILE = os.getenv('LOG_FILE', 'bot.log')
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
