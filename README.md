# CryptoBot — Quantitative Spot Mean-Reversion & Momentum Engine for Binance

**CryptoBot** is a high-performance, quality-focused automated trading bot designed for Binance Spot. It continuously scans the most liquid USDT trading pairs, identifies high-volume **Dual-Regime Capitulation Reversion** setups (`STRATEGY_MODE = 'capitulation'`) on closed 15m/1h candles, and protects capital using native exchange-side OCO stop-loss orders, dynamic `EMA21` reversion profit-taking, and fee-aware trailing stops.

---

## ⚡ Highlights & Key Upgrades

- **Dual-Regime Capitulation Strategy**: Buys high-volume oversold flushes below the Lower Bollinger Band (`20, 2.0`) when stretched below `EMA21`, using an `EMA200` regime filter (`Score = 78` in Bull Regime, `Score = 85` for Extreme Panic Capitulation in Bear Regime).
- **Instant Market Scanning**: Uses Binance's batch 24h ticker endpoint to pre-filter the top 50 most liquid USDT pairs (`>= $10M` 24h volume) in **< 0.5 seconds**.
- **No Mid-Candle Repainting**: All indicator logic (`EMA 9/21/50/200`, `Bollinger Bands 20,2`, `RSI 14`, `Volume MA 20`, `ATR 14`) is calculated exclusively on **fully closed candles**, preventing false triggers on intra-candle wicks.
- **Native Exchange-Side OCO Protection**: Immediately upon entry, the bot submits a server-side **OCO (One-Cancels-the-Other)** bracket order directly to Binance. If the bot crashes, loses internet, or restarts, your capital remains 100% protected on the exchange.
- **Calibrated Mean-Reversion Exits**: ATR-scaled Stop-Loss (`4.8x ATR`, clamped `1.5%–7.0%`) and Take-Profit (`2.0x ATR`, floored at `1.8%`), plus automatic `EMA21` reclamation profit-taking (`>= +1.2%`), trailing stop (`+1.8%` trigger, `0.7%` offset), and fee-aware break-even (`+2.2%` trigger).
- **Quantitative Validation Suite (`backtest.py` & `validate.py`)**: Includes walk-forward out-of-sample testing, 15-coin basket validation, Monte Carlo trade resampling, slippage stress-testing, and random-entry baselines.
- **Production-Ready VPS & Docker Support**: Includes a ready-to-use `Dockerfile`, `docker-compose.yml`, Linux `systemd` service, and 1-click VPS installation script.

---

## 🏗️ Architecture & How It Works

```mermaid
flowchart TD
    A["Binance Spot 24h Ticker Batch (<0.5s)"] --> B["Pre-Filter (Top 50 Liquid Pairs >= $10M Vol, Stablecoin Filter)"]
    B --> C["BTC Market Regime Check (1h EMA200)"]
    C --> D["Closed Candle Scoring (Lower BB 20,2 + EMA21 Stretch + RSI 14 + Volume Surge)"]
    D --> E{"Score >= 75?"}
    E -- No --> F["Skip / Log Filter Reason"]
    E -- Yes --> G{"BTC Bullish OR Score >= 80?"}
    G -- No --> F
    G -- Yes --> H["Market Buy via quoteOrderQty"]
    H --> I["Submit Native OCO Sell Order on Binance (TP + SL)"]
    I --> J["Monitor Position (EMA21 Reversion TP + Trailing Stop + Hold Timeout)"]
    J --> K["Save Dark-Themed Technical Chart to charts/ & MFE/MAE to trade_log.json"]
```

---

## 🚀 Quick Start (Local)

### 1. Clone & Set Up Environment
```bash
git clone https://github.com/lrk37/CryptoBot.git
cd CryptoBot

python -m venv venv
# On Windows:
.\venv\Scripts\activate
# On Linux/macOS:
source venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure Credentials
Copy `.env.example` to `.env` and fill in your Binance API keys:
```bash
cp .env.example .env
```
Edit `.env`:
```ini
BINANCE_API_KEY=your_api_key_here
BINANCE_API_SECRET=your_api_secret_here
USE_TESTNET=False
```

### 3. Check Permissions & Test Order
Run the diagnostic tools to verify connectivity and spot permissions:
```bash
python check_perms.py
python test_order.py
```

### 4. Run Backtest
Validate the strategy on historical data before trading:
```bash
python backtest.py --symbol SOLUSDT --days 60 --interval 15m
```

### 5. Launch Bot
```bash
python bot.py
```

---

## ☁️ Cloud VPS Deployment (24/7 Hosting)

For unattended 24/7 operation on an Ubuntu Cloud VPS (e.g., Hetzner, DigitalOcean, Linode, AWS):

```bash
# Automated 1-Click Setup:
curl -sSL https://raw.githubusercontent.com/lrk37/CryptoBot/main/deploy/vps_setup.sh | bash
```

See [`VPS_GUIDE.md`](VPS_GUIDE.md) for full instructions covering `systemd` service management, Docker deployment, and API security.

---

## 📊 Quantitative Metrics & Trade Log

Every closed trade is saved with auditable metrics to `trade_log.json`:

```json
{
  "timestamp": "2026-10-19T14:30:00+00:00",
  "symbol": "SOLUSDT",
  "kind": "scanner",
  "entry_price": 162.40,
  "exit_price": 172.14,
  "quantity": 0.12,
  "pnl_pct": 6.00,
  "pnl_usdt": 1.17,
  "mfe_pct": 6.15,
  "mae_pct": -0.42,
  "raw_score": 90.0,
  "pump_penalty": 0.0,
  "final_score": 90.0,
  "reason": "TAKE_PROFIT",
  "hold_duration_sec": 3600
}
```

---

## 🛡️ Risk Management Matrix

| Rule | Configuration | Action Taken |
| :--- | :--- | :--- |
| **Stop Loss** | `4.8x ATR` (clamped `1.5%–7.0%`) | Server-side OCO stop order on Binance |
| **Take Profit** | `2.0x ATR` (floored at `1.8%`) + `EMA21` Reversion | Server-side OCO limit order + client-side `EMA21` profit-take |
| **Break-Even Stop** | `BREAK_EVEN_TRIGGER_PCT = 2.2%` | Raises SL to entry + `0.2%` round-trip fees |
| **Trailing Stop** | `MIN_TRAILING_PROFIT_PCT = 1.6%` (`+1.8%` trigger) | Trailing offset `0.7%` behind peak price |
| **Consecutive Losses** | `MAX_CONSECUTIVE_LOSSES = 2` | Pauses new scans for 30 minutes |
| **Daily Circuit Breaker** | `MAX_DAILY_LOSS = 6.0%` | Halts all trading if portfolio drops 6% in a day |
| **Symbol Cooldown** | `3600s` (`1h`) normal / `14400s` (`4h`) after SL | Cooldown per coin after trade close |

---

## 📜 Disclaimer
*Cryptocurrency trading involves substantial risk of loss. Always backtest strategies thoroughly and start with small position sizes or testnet funds. Never risk capital you cannot afford to lose.*
