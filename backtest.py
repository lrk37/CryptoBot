"""
backtest.py — Quantitative Strategy Backtester for CryptoBot
=============================================================
Features:
  - Fetches and caches historical Binance klines to local CSVs
  - Simulates Scanner Engine strategy on closed 15m candles
  - Realistic trade simulation: taker fees (0.1%), slippage (0.05%)
  - ATR Stop Loss, Take Profit, Break-Even, Trailing Stop, Hold Timeout
  - Calculates institutional quant metrics:
      * Net Return & CAGR
      * Sharpe Ratio (annualized)
      * Sortino Ratio
      * Profit Factor & Win Rate
      * Max Drawdown & Calmar Ratio
      * Payoff Ratio (Avg Win / Avg Loss) & Expectancy
"""

import os
import sys
import math
import argparse
from pathlib import Path
from datetime import datetime, timezone
import pandas as pd
import numpy as np

try:
    from binance.client import Client
except ImportError:
    Client = None

import config as cfg

DATA_DIR = Path("data")

def fetch_historical_candles(symbol: str, interval: str = "15m", days: int = 60) -> pd.DataFrame:
    """Fetch historical klines from Binance and cache to data/<symbol>_<interval>_<days>d.csv."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = DATA_DIR / f"{symbol}_{interval}_{days}d.csv"

    if cache_file.exists():
        df = pd.read_csv(cache_file, parse_dates=['open_time'], index_col='open_time')
        return df

    print(f"[*] Downloading {days} days of {interval} data for {symbol} from Binance...")
    client = Client(getattr(cfg, 'API_KEY', ''), getattr(cfg, 'API_SECRET', ''))

    start_str = f"{days} days ago UTC"
    raw_klines = client.get_historical_klines(symbol, interval, start_str)

    if not raw_klines:
        raise ValueError(f"No kline data returned for {symbol}")

    df = pd.DataFrame(raw_klines, columns=[
        'open_time', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'quote_vol', 'trades', 'tb_base', 'tb_quote', 'ignore'
    ])
    df['open_time'] = pd.to_datetime(df['open_time'], unit='ms', utc=True)
    df.set_index('open_time', inplace=True)

    for col in ('open', 'high', 'low', 'close', 'volume', 'quote_vol'):
        df[col] = df[col].astype(float)

    df = df[['open', 'high', 'low', 'close', 'volume', 'quote_vol']]
    df.to_csv(cache_file)
    print(f"[+] Saved {len(df)} candles to {cache_file}")
    return df

class StrategyBacktester:
    def __init__(self,
                 initial_capital: float = 1000.0,
                 trade_size_usdt: float = 100.0,
                 fee_pct: float = 0.1,
                 slippage_pct: float = 0.05,
                 min_score: float = 60.0):
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.trade_size_usdt = trade_size_usdt
        self.fee_pct = fee_pct / 100.0
        self.slippage_pct = slippage_pct / 100.0
        self.min_score = min_score
        self.trades = []

    def run(self, symbol: str, df: pd.DataFrame) -> dict:
        close = df['close']
        high = df['high']
        low = df['low']
        open_p = df['open']
        vol = df['volume']

        # Technical Indicators
        ema9 = close.ewm(span=9, adjust=False).mean()
        ema21 = close.ewm(span=21, adjust=False).mean()
        ema50 = close.ewm(span=50, adjust=False).mean()

        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs()
        ], axis=1).max(axis=1)
        atr_series = tr.rolling(14).mean()

        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi_series = 100 - (100 / (1 + rs))

        vol_ma = vol.rolling(20).mean()

        position = None
        cooldown_until_idx = 0

        # Configuration parameters
        sl_pct_cfg = getattr(cfg, 'SCANNER_STOP_LOSS_PCT', 2.0) / 100.0
        tp_pct_cfg = getattr(cfg, 'SCANNER_TAKE_PROFIT_PCT', 6.0) / 100.0
        be_trigger_pct = getattr(cfg, 'BREAK_EVEN_TRIGGER_PCT', 2.0) / 100.0
        trailing_offset_pct = getattr(cfg, 'SCANNER_TRAILING_OFFSET', 1.2) / 100.0
        min_trailing_profit_pct = getattr(cfg, 'MIN_TRAILING_PROFIT_PCT', 2.5) / 100.0
        max_hold_bars = getattr(cfg, 'MAX_SCANNER_HOLD_HOURS', 8) * 4  # 15m bars

        for i in range(55, len(df)):
            curr_bar = df.iloc[i]
            prev_bar = df.iloc[i-1]
            curr_time = df.index[i]

            # In trade: check exit
            if position is not None:
                bars_held = i - position['entry_idx']
                high_price = curr_bar['high']
                low_price = curr_bar['low']
                close_price = curr_bar['close']

                if high_price > position['highest']:
                    position['highest'] = high_price
                if low_price < position['lowest']:
                    position['lowest'] = low_price

                # Check Break-Even trigger
                peak_gain = (position['highest'] - position['entry_price']) / position['entry_price']
                if peak_gain >= be_trigger_pct and not position['be_activated']:
                    min_be = position['entry_price'] * (1 + self.fee_pct * 2)
                    if min_be > position['stop_loss']:
                        position['stop_loss'] = min_be
                        position['be_activated'] = True

                exit_price = None
                reason = None

                # 1. Stop Loss check
                if low_price <= position['stop_loss']:
                    exit_price = min(position['stop_loss'], curr_bar['open'])
                    reason = "STOP_LOSS"
                # 2. Take Profit check
                elif high_price >= position['take_profit']:
                    exit_price = max(position['take_profit'], curr_bar['open'])
                    reason = "TAKE_PROFIT"
                # 3. Trailing Stop check
                elif peak_gain >= min_trailing_profit_pct:
                    trail_level = position['highest'] * (1 - trailing_offset_pct)
                    if low_price <= trail_level:
                        exit_price = trail_level
                        reason = "TRAILING_STOP"
                # 4. Invalidation / Max Hold Timeout: check if price broke below EMA21
                elif bars_held >= max_hold_bars:
                    curr_ema21 = ema21.iloc[i]
                    # Exit if candle closed below EMA21, or if held double max_hold (hard timeout)
                    if close_price < curr_ema21 or bars_held >= (max_hold_bars * 2):
                        exit_price = close_price
                        reason = "TREND_INVALIDATED" if close_price < curr_ema21 else "HOLD_TIMEOUT"

                if exit_price is not None:
                    # Apply exit slippage & fees
                    exit_price_net = exit_price * (1 - self.slippage_pct)
                    trade_val_entry = position['entry_price_net'] * position['qty']
                    trade_val_exit = exit_price_net * position['qty']

                    fee_entry = trade_val_entry * self.fee_pct
                    fee_exit = trade_val_exit * self.fee_pct
                    total_fees = fee_entry + fee_exit

                    pnl_usdt = (trade_val_exit - trade_val_entry) - total_fees
                    pnl_pct = (pnl_usdt / trade_val_entry) * 100.0

                    self.capital += pnl_usdt

                    mfe = ((position['highest'] - position['entry_price']) / position['entry_price']) * 100.0
                    mae = ((position['lowest'] - position['entry_price']) / position['entry_price']) * 100.0

                    self.trades.append({
                        'symbol': symbol,
                        'entry_time': position['entry_time'],
                        'exit_time': curr_time,
                        'entry_price': position['entry_price'],
                        'exit_price': exit_price,
                        'pnl_pct': pnl_pct,
                        'pnl_usdt': pnl_usdt,
                        'mfe_pct': mfe,
                        'mae_pct': mae,
                        'bars_held': bars_held,
                        'reason': reason,
                        'score': position['score'],
                        'capital_after': self.capital
                    })

                    position = None
                    cooldown_until_idx = i + 4  # 1 hour cooldown (4 x 15m)
                continue

            # Looking for entry on closed bar prev_bar
            if i < cooldown_until_idx:
                continue

            p_close = close.iloc[i-1]
            p_high = high.iloc[i-1]
            p_low = low.iloc[i-1]
            p_open = open_p.iloc[i-1]
            p_vol = vol.iloc[i-1]

            atr_val = atr_series.iloc[i-1]
            if np.isnan(atr_val) or atr_val <= 0:
                continue

            # Hard Block: Doji filter
            c_range = p_high - p_low
            if c_range > 0 and (abs(p_close - p_open) / c_range) < 0.15:
                continue

            # Hard Block: EMA9 slope
            ema9_val = ema9.iloc[i-1]
            ema9_prev = ema9.iloc[i-3]
            if (ema9_val - ema9_prev) / ema9_prev < -0.001:
                continue

            # Scoring rules
            score = 0.0
            # 1. EMA alignment
            if ema9.iloc[i-1] > ema21.iloc[i-1] > ema50.iloc[i-1]:
                score += 25.0
            elif ema9.iloc[i-1] > ema21.iloc[i-1]:
                score += 15.0

            # 2. RSI band
            rsi_val = rsi_series.iloc[i-1]
            if 45 <= rsi_val <= 55:
                score += 20.0
            elif 55 < rsi_val <= 65:
                score += 15.0
            elif 65 < rsi_val <= 70:
                score += 5.0
            elif rsi_val > 70:
                score -= 20.0
            else:
                score -= 15.0

            # 3. Volume surge
            v_ma = vol_ma.iloc[i-1]
            v_ratio = p_vol / v_ma if (not np.isnan(v_ma) and v_ma > 0) else 1.0
            if v_ratio >= 2.0:
                score += 25.0
            elif v_ratio >= 1.5:
                score += 15.0
            elif v_ratio < 1.0:
                score -= 10.0

            # 4. Breakout
            recent_20_h = high.iloc[i-21:i-1].max()
            if p_close > recent_20_h:
                b_dist = p_close - recent_20_h
                b_ratio = b_dist / atr_val
                if b_ratio >= 0.2:
                    score += 25.0
                else:
                    score += 10.0

            if score >= self.min_score:
                # Enter at next bar's open with entry slippage
                entry_market_price = curr_bar['open']
                entry_price_net = entry_market_price * (1 + self.slippage_pct)

                trade_size = min(self.trade_size_usdt, self.capital * 0.95)
                if trade_size < 11.0:
                    continue

                qty = trade_size / entry_price_net
                sl = entry_market_price * (1 - sl_pct_cfg)
                tp = entry_market_price * (1 + tp_pct_cfg)

                position = {
                    'symbol': symbol,
                    'entry_time': curr_time,
                    'entry_idx': i,
                    'entry_price': entry_market_price,
                    'entry_price_net': entry_price_net,
                    'qty': qty,
                    'stop_loss': sl,
                    'take_profit': tp,
                    'highest': entry_market_price,
                    'lowest': entry_market_price,
                    'score': score,
                    'be_activated': False
                }

        return self.compute_metrics()

    def compute_metrics(self) -> dict:
        if not self.trades:
            return {'total_trades': 0, 'error': 'No trades executed during backtest period.'}

        df_t = pd.DataFrame(self.trades)
        wins = df_t[df_t['pnl_usdt'] > 0]
        losses = df_t[df_t['pnl_usdt'] <= 0]

        total_trades = len(df_t)
        win_count = len(wins)
        loss_count = len(losses)
        win_rate = (win_count / total_trades) * 100.0

        gross_profit = wins['pnl_usdt'].sum() if win_count > 0 else 0.0
        gross_loss = abs(losses['pnl_usdt'].sum()) if loss_count > 0 else 0.0
        net_profit = gross_profit - gross_loss
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float('inf')

        avg_win = wins['pnl_pct'].mean() if win_count > 0 else 0.0
        avg_loss = abs(losses['pnl_pct'].mean()) if loss_count > 0 else 0.0
        payoff_ratio = (avg_win / avg_loss) if avg_loss > 0 else 0.0

        # Expectancy = (Win% * AvgWin) - (Loss% * AvgLoss)
        expectancy = ((win_rate / 100.0) * avg_win) - (((100 - win_rate) / 100.0) * avg_loss)

        # Drawdown calculation
        equity_curve = [self.initial_capital] + df_t['capital_after'].tolist()
        peaks = pd.Series(equity_curve).cummax()
        drawdowns = (pd.Series(equity_curve) - peaks) / peaks * 100.0
        max_drawdown = abs(drawdowns.min())

        # Sharpe & Sortino (per-trade approximated to annualized)
        returns = df_t['pnl_pct'] / 100.0
        mean_ret = returns.mean()
        std_ret = returns.std()
        downside_std = returns[returns < 0].std()

        # Assuming ~350 trades/year scaling
        annual_factor = math.sqrt(min(total_trades * 4, 350))
        sharpe = (mean_ret / std_ret * annual_factor) if std_ret > 0 else 0.0
        sortino = (mean_ret / downside_std * annual_factor) if (downside_std > 0 and not np.isnan(downside_std)) else 0.0

        return {
            'initial_capital': self.initial_capital,
            'final_capital': round(self.capital, 2),
            'net_profit_usdt': round(net_profit, 2),
            'net_return_pct': round(((self.capital - self.initial_capital) / self.initial_capital) * 100.0, 2),
            'total_trades': total_trades,
            'win_rate_pct': round(win_rate, 2),
            'win_count': win_count,
            'loss_count': loss_count,
            'profit_factor': round(profit_factor, 2),
            'max_drawdown_pct': round(max_drawdown, 2),
            'sharpe_ratio': round(sharpe, 2),
            'sortino_ratio': round(sortino, 2),
            'avg_win_pct': round(avg_win, 2),
            'avg_loss_pct': round(avg_loss, 2),
            'payoff_ratio': round(payoff_ratio, 2),
            'expectancy_pct': round(expectancy, 2),
            'avg_bars_held': round(df_t['bars_held'].mean(), 1)
        }

def print_report(symbol: str, metrics: dict):
    print("\n" + "="*65)
    print(f"       BACKTEST RESULTS REPORT: {symbol}")
    print("="*65)
    if 'error' in metrics:
        print(f"[-] {metrics['error']}")
        print("="*65 + "\n")
        return

    print(f" Initial Capital : ${metrics['initial_capital']:,.2f}")
    print(f" Final Capital   : ${metrics['final_capital']:,.2f}")
    print(f" Net Profit      : ${metrics['net_profit_usdt']:+,.2f} ({metrics['net_return_pct']:+.2f}%)")
    print(f" Total Trades    : {metrics['total_trades']} (Wins: {metrics['win_count']}, Losses: {metrics['loss_count']})")
    print(f" Win Rate        : {metrics['win_rate_pct']:.2f}%")
    print(f" Profit Factor   : {metrics['profit_factor']:.2f}")
    print(f" Max Drawdown    : {metrics['max_drawdown_pct']:.2f}%")
    print(f" Sharpe Ratio    : {metrics['sharpe_ratio']:.2f}")
    print(f" Sortino Ratio   : {metrics['sortino_ratio']:.2f}")
    print(f" Avg Win / Loss  : {metrics['avg_win_pct']:+.2f}% / -{metrics['avg_loss_pct']:.2f}%")
    print(f" Payoff Ratio    : {metrics['payoff_ratio']:.2f}")
    print(f" Trade Expectancy: {metrics['expectancy_pct']:+.2f}% per trade")
    print(f" Avg Hold Time   : {metrics['avg_bars_held'] * 15:.0f} mins ({metrics['avg_bars_held']:.1f} bars)")
    print("="*65 + "\n")

def main():
    parser = argparse.ArgumentParser(description="CryptoBot Strategy Backtester")
    parser.add_argument("--symbol", type=str, default="SOLUSDT", help="Binance trading pair (e.g. SOLUSDT, BTCUSDT)")
    parser.add_argument("--days", type=int, default=60, help="Days of historical data to backtest")
    parser.add_argument("--interval", type=str, default="15m", help="Candle interval (15m, 1h, etc.)")
    parser.add_argument("--capital", type=float, default=1000.0, help="Starting simulated capital in USDT")
    parser.add_argument("--trade_size", type=float, default=100.0, help="Trade size in USDT per order")
    args = parser.parse_args()

    try:
        df = fetch_historical_candles(args.symbol, interval=args.interval, days=args.days)
        tester = StrategyBacktester(initial_capital=args.capital, trade_size_usdt=args.trade_size)
        metrics = tester.run(args.symbol, df)
        print_report(args.symbol, metrics)
    except Exception as e:
        print(f"[ERROR] Backtest failed: {e}")

if __name__ == "__main__":
    main()
