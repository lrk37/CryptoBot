"""
backtest.py — Quantitative Strategy Backtester for CryptoBot
=============================================================
Uses the SAME stop / take-profit / break-even / trailing rules as the live bot
(shared helpers `compute_stops` and `exit_triggers` in risk_manager.py) and reads the
entry threshold from config.py (override with --min_score).

What it models:   closed-bar signals, entry at the NEXT bar's open, taker fees, slippage,
                  conservative intra-bar ordering (stop is checked before take-profit).
What it CANNOT model (live scoring uses them, history doesn't have them): order-book depth
                  bonus/filter, 24h pump penalty, BTC regime filter, top-N ranking across
                  coins, 2-candle confirmation, the score>=80 fast-track.  Treat results as
                  a test of the core signal + exits, not of the whole live scanner.

Use --baseline N to compare against N runs of RANDOM entries with the same exits and costs:
if random entries do about as well, the signal is not adding anything.
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
from risk_manager import compute_stops, exit_triggers

DATA_DIR = Path("data")

# Map interval strings to minutes for dynamic calculations
INTERVAL_MINUTES = {
    '1m': 1, '3m': 3, '5m': 5, '15m': 15, '30m': 30,
    '1h': 60, '2h': 120, '4h': 240, '1d': 1440,
}


def _bar_minutes(interval: str) -> int:
    """Minutes per bar for a given candle interval."""
    return INTERVAL_MINUTES.get(interval, 15)


def fetch_historical_candles(symbol: str, interval: str = "15m", days: int = 60,
                             refresh: bool = False) -> pd.DataFrame:
    """Fetch historical klines from Binance and cache to data/<symbol>_<interval>_<days>d.csv.

    Set refresh=True (or pass --refresh on the CLI) to re-download even if a
    cached file exists.  The cache file records the data date range; when the
    newest bar in the cache is more than 2 days old the user is warned.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = DATA_DIR / f"{symbol}_{interval}_{days}d.csv"

    if cache_file.exists() and not refresh:
        df = pd.read_csv(cache_file, parse_dates=['open_time'], index_col='open_time')
        # Warn if cache is stale (newest bar > 2 days old)
        try:
            newest = pd.Timestamp(df.index[-1])
            age_days = (pd.Timestamp.now(tz='UTC') - newest).total_seconds() / 86400.0
            if age_days > 2:
                print(f"[!] Cached data for {symbol} is {age_days:.0f} days old "
                      f"(ends {newest.strftime('%Y-%m-%d')}). Use --refresh to re-download.")
        except Exception:
            pass
        print(f"[*] Using cached {len(df)} bars from {cache_file} "
              f"({df.index[0].strftime('%Y-%m-%d')} → {df.index[-1].strftime('%Y-%m-%d')})")
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
    print(f"[+] Saved {len(df)} candles to {cache_file} "
          f"({df.index[0].strftime('%Y-%m-%d')} → {df.index[-1].strftime('%Y-%m-%d')})")
    return df

class StrategyBacktester:
    def __init__(self,
                 initial_capital: float = 1000.0,
                 trade_size_usdt: float = 100.0,
                 fee_pct: float = 0.1,
                 slippage_pct: float = 0.05,
                 min_score: float = None):
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.trade_size_usdt = trade_size_usdt
        self.fee_pct = fee_pct / 100.0
        self.slippage_pct = slippage_pct / 100.0
        # Threshold comes from config.py unless explicitly overridden
        self.min_score = float(min_score) if min_score is not None else float(getattr(cfg, 'MIN_SCORE_TO_TRADE', 60))
        self.trades = []
        self.period_days = None

    def run(self, symbol: str, df: pd.DataFrame, entry_mode: str = 'signal',
            random_prob: float = 0.0, seed: int = None, _check_entry_bar: bool = True,
            interval: str = '15m', start_bar: int = 55) -> dict:
        """entry_mode='signal' -> the strategy's scoring; 'random' -> enter with probability
        `random_prob` on every eligible bar (baseline with identical exits and costs)."""
        self.trades = []
        self.capital = self.initial_capital
        self._bar_minutes = _bar_minutes(interval)
        self._equity_curve = []  # bar-by-bar mark-to-market equity (includes intra-trade)

        close = df['close']
        high = df['high']
        low = df['low']
        open_p = df['open']
        vol = df['volume']

        try:
            self.period_days = max((df.index[-1] - df.index[0]).total_seconds() / 86400.0, 1.0)
        except Exception:
            self.period_days = max((len(df) * self._bar_minutes) / 1440.0, 1.0)

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
        rng = np.random.default_rng(seed) if entry_mode == 'random' else None

        # Exit parameters: identical to the live bot
        trig = exit_triggers(cfg)
        be_trigger = trig['be_trigger_pct'] / 100.0
        trail_trigger = trig['trail_trigger_pct'] / 100.0
        be_stop_pct = trig['fees_pct'] / 100.0
        be_enabled = getattr(cfg, 'ENABLE_BREAK_EVEN_STOP', True)
        trailing_enabled = getattr(cfg, 'SCANNER_TRAILING_STOP', True)
        trailing_offset = getattr(cfg, 'SCANNER_TRAILING_OFFSET', 1.2) / 100.0
        bars_per_hour = 60 / self._bar_minutes
        max_hold_bars = int(getattr(cfg, 'MAX_SCANNER_HOLD_HOURS', 8) * bars_per_hour)

        def try_exit(i, curr_bar, curr_time):
            """Evaluate exits for the open position on bar i (also used for the ENTRY bar itself)."""
            nonlocal position, cooldown_until_idx
            bars_held = i - position['entry_idx']
            high_price = curr_bar['high']
            low_price = curr_bar['low']
            close_price = curr_bar['close']

            if high_price > position['highest']:
                position['highest'] = high_price
            if low_price < position['lowest']:
                position['lowest'] = low_price

            # Break-even (the raised stop applies within the same bar = worst-case ordering)
            peak_gain = (position['highest'] - position['entry_price']) / position['entry_price']
            if be_enabled and peak_gain >= be_trigger and not position['be_activated']:
                min_be = position['entry_price'] * (1 + be_stop_pct)
                if min_be > position['stop_loss']:
                    position['stop_loss'] = min_be
                    position['be_activated'] = True

            exit_price = None
            reason = None

            if low_price <= position['stop_loss']:                      # 1. Stop loss (checked first: conservative)
                exit_price = min(position['stop_loss'], curr_bar['open'])
                reason = "STOP_LOSS"
            elif high_price >= position['take_profit']:                 # 2. Take profit
                exit_price = max(position['take_profit'], curr_bar['open'])
                reason = "TAKE_PROFIT"
            else:
                if trailing_enabled and peak_gain >= trail_trigger:     # 3. Trailing stop
                    trail_level = position['highest'] * (1 - trailing_offset)
                    if low_price <= trail_level:
                        exit_price = min(trail_level, curr_bar['open'])
                        reason = "TRAILING_STOP"
                if exit_price is None and bars_held >= max_hold_bars:   # 4. Trend invalidation / hard timeout
                    if bars_held >= 2 * max_hold_bars:
                        exit_price = close_price
                        reason = "HOLD_TIMEOUT"
                    elif close_price < ema21.iloc[i]:
                        exit_price = close_price
                        reason = "TREND_INVALIDATED"

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
                    'sl_pct': position['sl_pct'],
                    'tp_pct': position['tp_pct'],
                    'capital_after': self.capital
                })

                position = None
                cooldown_until_idx = i + int(60 / self._bar_minutes)  # 1-hour cooldown

        for i in range(max(start_bar, 55), len(df)):
            curr_bar = df.iloc[i]
            curr_time = df.index[i]

            # Bar-by-bar equity: mark-to-market including open position
            if position is not None:
                unrealised = (curr_bar['close'] - position['entry_price_net']) * position['qty']
                self._equity_curve.append(self.capital + unrealised)
            else:
                self._equity_curve.append(self.capital)

            # In trade: check exit
            if position is not None:
                try_exit(i, curr_bar, curr_time)
                continue

            # Looking for entry on closed bar i-1
            if i < cooldown_until_idx:
                continue

            atr_val = atr_series.iloc[i-1]
            if np.isnan(atr_val) or atr_val <= 0:
                continue

            if entry_mode == 'random':
                if rng.random() >= random_prob:
                    continue
                score = 0.0
            else:
                p_close = close.iloc[i-1]
                p_high = high.iloc[i-1]
                p_low = low.iloc[i-1]
                p_open = open_p.iloc[i-1]
                p_vol = vol.iloc[i-1]

                # Hard Block: Doji filter
                c_range = p_high - p_low
                if c_range > 0 and (abs(p_close - p_open) / c_range) < 0.15:
                    continue

                # Hard Block: EMA9 slope
                ema9_val = ema9.iloc[i-1]
                ema9_prev = ema9.iloc[i-3]
                if (ema9_val - ema9_prev) / ema9_prev < -0.001:
                    continue

                # Scoring rules (same as scanner.py, minus order-book / pump-penalty components)
                score = 0.0
                # 1. EMA alignment
                if ema9.iloc[i-1] > ema21.iloc[i-1] > ema50.iloc[i-1]:
                    score += 25.0
                elif ema9.iloc[i-1] > ema21.iloc[i-1]:
                    score += 15.0

                # 2. RSI band: reward true breakout momentum (55-68) instead of sluggish consolidation
                rsi_val = rsi_series.iloc[i-1]
                if 55 <= rsi_val <= 68:
                    score += 25.0
                elif 48 <= rsi_val < 55:
                    score += 15.0
                elif 68 < rsi_val <= 73:
                    score += 10.0
                elif rsi_val > 75:
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

                if score < self.min_score:
                    continue

            # Enter at next bar's open with entry slippage
            entry_market_price = curr_bar['open']
            entry_price_net = entry_market_price * (1 + self.slippage_pct)

            trade_size = min(self.trade_size_usdt, self.capital * 0.95)
            if trade_size < 11.0:
                continue

            qty = trade_size / entry_price_net
            sl, tp, _, _ = compute_stops(cfg, 'scanner', entry_market_price, atr_val)  # same as live

            position = {
                'symbol': symbol,
                'entry_time': curr_time,
                'entry_idx': i,
                'entry_price': entry_market_price,
                'entry_price_net': entry_price_net,
                'qty': qty,
                'stop_loss': sl,
                'take_profit': tp,
                'sl_pct': (entry_market_price - sl) / entry_market_price * 100.0,
                'tp_pct': (tp - entry_market_price) / entry_market_price * 100.0,
                'highest': entry_market_price,
                'lowest': entry_market_price,
                'score': score,
                'be_activated': False
            }

            # The entry bar's own range counts: we buy at its open, so its low can hit the stop.
            # (Older versions skipped this, which silently removed some quick stop-outs.)
            if _check_entry_bar:
                try_exit(i, curr_bar, curr_time)

        # Force close open position at the end of data if any
        if position is not None:
            last_bar = df.iloc[-1]
            last_time = df.index[-1]
            exit_market_price = last_bar['close']
            exit_price_net = exit_market_price * (1 - self.slippage_pct)

            trade_val_entry = position['entry_price_net'] * position['qty']
            trade_val_exit = exit_price_net * position['qty']

            fee_entry = trade_val_entry * self.fee_pct
            fee_exit = trade_val_exit * self.fee_pct
            total_fees = fee_entry + fee_exit

            pnl_usdt = (trade_val_exit - trade_val_entry) - total_fees
            pnl_pct = (pnl_usdt / trade_val_entry) * 100.0

            self.capital += pnl_usdt

            self.trades.append({
                'symbol': symbol,
                'entry_time': position['entry_time'],
                'exit_time': last_time,
                'bars_held': len(df) - 1 - position['entry_idx'],
                'entry_price': position['entry_price'],
                'exit_price': exit_market_price,
                'exit_reason': 'END_OF_DATA',
                'pnl_usdt': pnl_usdt,
                'pnl_pct': pnl_pct,
                'highest_reached_pct': (position['highest'] - position['entry_price']) / position['entry_price'] * 100.0,
                'lowest_reached_pct': (position['lowest'] - position['entry_price']) / position['entry_price'] * 100.0,
                'score': position['score'],
                'sl_pct': position['sl_pct'],
                'tp_pct': position['tp_pct'],
                'capital_after': self.capital
            })
            position = None

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

        gross_profit = float(wins['pnl_usdt'].sum()) if win_count > 0 else 0.0
        gross_loss = float(abs(losses['pnl_usdt'].sum())) if loss_count > 0 else 0.0
        net_profit = gross_profit - gross_loss
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float('inf')

        # Accounting Invariant Assertion: tracked equity must exactly equal initial_capital + net_profit
        expected_capital = round(self.initial_capital + net_profit, 2)
        actual_capital = round(self.capital, 2)
        if abs(actual_capital - expected_capital) > 0.02:
            raise AssertionError(
                f"Accounting mismatch in StrategyBacktester: tracked capital=${actual_capital:.2f} "
                f"does not match initial capital (${self.initial_capital:.2f}) + net P&L (${net_profit:.2f}) "
                f"= expected ${expected_capital:.2f} (delta: ${actual_capital - expected_capital:+.2f})."
            )

        avg_win = float(wins['pnl_pct'].mean()) if win_count > 0 else 0.0
        avg_loss = float(abs(losses['pnl_pct'].mean())) if loss_count > 0 else 0.0
        payoff_ratio = (avg_win / avg_loss) if avg_loss > 0 else 0.0

        # Expectancy = (Win% * AvgWin) - (Loss% * AvgLoss)
        expectancy = ((win_rate / 100.0) * avg_win) - (((100 - win_rate) / 100.0) * avg_loss)

        # Drawdown: use bar-by-bar equity (includes intra-trade mark-to-market)
        # so that every intratrade adverse excursion is captured, not just
        # closed-trade equity snapshots.
        if self._equity_curve:
            eq = pd.Series([self.initial_capital] + self._equity_curve)
        else:
            eq = pd.Series([self.initial_capital] + df_t['capital_after'].tolist())
        peaks = eq.cummax()
        drawdowns = (eq - peaks) / peaks.replace(0, 1) * 100.0
        max_drawdown = abs(drawdowns.min())

        # Sharpe & Sortino (trade-level approximations).
        # These are computed from per-trade returns scaled by sqrt(trades/year).
        # For a proper portfolio-level Sharpe, use periodic (e.g. daily) returns
        # from the mark-to-market equity curve, including flat periods.
        returns = df_t['pnl_pct'] / 100.0
        mean_ret = returns.mean()
        std_ret = returns.std()
        downside_dev = float(np.sqrt(np.mean(np.minimum(returns.values, 0.0) ** 2)))

        trades_per_year = total_trades * 365.0 / (self.period_days or 90.0)
        annual_factor = math.sqrt(trades_per_year)
        sharpe = (mean_ret / std_ret * annual_factor) if (std_ret and std_ret > 0) else 0.0
        sortino = (mean_ret / downside_dev * annual_factor) if downside_dev > 0 else 0.0

        bar_min = getattr(self, '_bar_minutes', 15)

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
            'avg_bars_held': round(df_t['bars_held'].mean(), 1),
            'avg_hold_minutes': round(df_t['bars_held'].mean() * bar_min, 1),
            'bar_minutes': bar_min,
        }


def random_baseline(symbol: str, df: pd.DataFrame, actual_trades: list, n_runs: int,
                    capital: float, trade_size: float, min_score: float = None, seed: int = 42,
                    interval: str = '15m') -> dict:
    """Run `n_runs` backtests with RANDOM entries (same exits, fees, slippage), calibrated to take about as
    many trades as the real strategy did. Tells you whether the signal beats coin-flip entries."""
    n_actual = len(actual_trades)
    if n_actual == 0:
        return {}
    in_pos = sum(t['bars_held'] for t in actual_trades)
    eligible = max(len(df) - 55 - in_pos - 4 * n_actual, 1)
    prob = min(n_actual / eligible, 1.0)

    nets, exps, counts = [], [], []
    for k in range(n_runs):
        bt = StrategyBacktester(initial_capital=capital, trade_size_usdt=trade_size, min_score=min_score)
        m = bt.run(symbol, df, entry_mode='random', random_prob=prob, seed=seed + k, interval=interval)
        if 'error' in m:
            continue
        nets.append(m['net_return_pct'])
        exps.append(float(np.mean([t['pnl_pct'] for t in bt.trades])))
        counts.append(m['total_trades'])
    return {'runs': len(nets), 'prob': prob, 'nets': nets, 'exps': exps, 'counts': counts}


def print_config():
    trig = exit_triggers(cfg)
    print("-" * 65)
    print(" EFFECTIVE SETTINGS (same helpers as the live bot)")
    print(f"  Entry threshold : score >= {getattr(cfg, 'MIN_SCORE_TO_TRADE', 60)} (override with --min_score)")
    if getattr(cfg, 'SCANNER_USE_ATR_STOPS', True):
        print(f"  Stop loss       : ATR x{getattr(cfg, 'SCANNER_ATR_SL_MULTIPLIER', 3.0)}, "
              f"clamped to {getattr(cfg, 'MIN_SL_PCT', 1.5)}%..{getattr(cfg, 'SCANNER_MAX_STOP_LOSS_PCT', 3.5)}% below entry")
        print(f"  Take profit     : ATR x{getattr(cfg, 'SCANNER_ATR_TP_MULTIPLIER', 4.0)}, at least {getattr(cfg, 'MIN_TP_PCT', 0)}% above entry")
    else:
        print(f"  Stop / Take     : fixed -{getattr(cfg, 'SCANNER_STOP_LOSS_PCT', 2.5)}% / +{getattr(cfg, 'SCANNER_TAKE_PROFIT_PCT', 5.0)}%")
    print(f"  Break-even arms : +{trig['be_trigger_pct']:.1f}% (stop moves to entry +{trig['fees_pct']:.1f}%)")
    print(f"  Trailing arms   : +{trig['trail_trigger_pct']:.1f}% peak, offset {getattr(cfg, 'SCANNER_TRAILING_OFFSET', 1.2)}%")
    h = getattr(cfg, 'MAX_SCANNER_HOLD_HOURS', 8)
    print(f"  Timeouts        : after {h}h exit if a candle closes below EMA21; hard exit at {2 * h}h")
    print("-" * 65)


def print_report(symbol: str, metrics: dict):
    print("\n" + "=" * 65)
    print(f"       BACKTEST RESULTS REPORT: {symbol}")
    print("=" * 65)
    if 'error' in metrics:
        print(f"[-] {metrics['error']}")
        print("=" * 65 + "\n")
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
    print(f" Trade Expectancy: {metrics['expectancy_pct']:+.2f}% per trade (net of fees and slippage)")
    bar_min = metrics.get('bar_minutes', 15)
    hold_mins = metrics['avg_bars_held'] * bar_min
    if hold_mins >= 60:
        print(f" Avg Hold Time   : {hold_mins / 60:.1f} hours ({metrics['avg_bars_held']:.1f} bars × {bar_min}m)")
    else:
        print(f" Avg Hold Time   : {hold_mins:.0f} mins ({metrics['avg_bars_held']:.1f} bars × {bar_min}m)")
    print(f" Sharpe / Sortino: {metrics['sharpe_ratio']:.2f} / {metrics['sortino_ratio']:.2f}  (trade-level approx.)")
    print("=" * 65 + "\n")


def print_baseline(metrics: dict, base: dict):
    if not base or not base.get('runs'):
        print("[-] Baseline skipped (no trades).")
        return
    nets = np.array(base['nets'])
    exps = np.array(base['exps'])
    actual = metrics['net_return_pct']
    beat = float(np.mean(nets >= actual)) * 100.0
    print("-" * 65)
    print(f" RANDOM-ENTRY BASELINE ({base['runs']} runs, same exits/costs, ~{np.mean(base['counts']):.0f} trades each)")
    print(f"  Expectancy / trade : mean {exps.mean():+.2f}%   (strategy: {metrics['expectancy_pct']:+.2f}%)")
    print(f"  Net return         : mean {nets.mean():+.1f}%, 5th-95th pct {np.percentile(nets, 5):+.1f}% .. {np.percentile(nets, 95):+.1f}%"
          f"   (strategy: {actual:+.1f}%)")
    print(f"  Random runs that did at least as well as the strategy: {beat:.0f}%")
    print("  -> If that share is large, the entry signal is not adding anything over coin-flip entries.")
    print("-" * 65 + "\n")


def main():
    parser = argparse.ArgumentParser(description="CryptoBot Strategy Backtester")
    parser.add_argument("--symbol", type=str, default="SOLUSDT", help="Binance trading pair (e.g. SOLUSDT, BTCUSDT)")
    parser.add_argument("--days", type=int, default=60, help="Days of historical data to backtest")
    parser.add_argument("--interval", type=str, default="15m", help="Candle interval (15m, 1h, etc.)")
    parser.add_argument("--capital", type=float, default=1000.0, help="Starting simulated capital in USDT")
    parser.add_argument("--trade_size", type=float, default=100.0, help="Trade size in USDT per order")
    parser.add_argument("--min_score", type=float, default=None, help="Entry score threshold (default: MIN_SCORE_TO_TRADE from config.py)")
    parser.add_argument("--baseline", type=int, default=0, help="Also run N random-entry baselines with the same exits/costs")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for the baseline")
    parser.add_argument("--refresh", action='store_true', help="Re-download data even if cached")
    args = parser.parse_args()

    try:
        df = fetch_historical_candles(args.symbol, interval=args.interval, days=args.days,
                                      refresh=args.refresh)
        tester = StrategyBacktester(initial_capital=args.capital, trade_size_usdt=args.trade_size, min_score=args.min_score)
        print_config()
        if args.min_score is not None:
            print(f" (using --min_score {args.min_score})")
        metrics = tester.run(args.symbol, df, interval=args.interval)
        print_report(args.symbol, metrics)
        if args.baseline > 0 and 'error' not in metrics:
            base = random_baseline(args.symbol, df, tester.trades, args.baseline,
                                   args.capital, args.trade_size, args.min_score, args.seed,
                                   interval=args.interval)
            print_baseline(metrics, base)
    except Exception as e:
        print(f"[ERROR] Backtest failed: {e}")

if __name__ == "__main__":
    main()
