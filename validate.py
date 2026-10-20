"""
validate.py — Walk-Forward Validation & Robustness Testing Suite
================================================================
Comprehensive out-of-sample testing for CryptoBot's breakout strategy.

Usage:
  python validate.py walkforward  -s AVAXUSDT --days 180 --interval 1h
  python validate.py basket       --days 180 --interval 1h
  python validate.py montecarlo   -s AVAXUSDT --days 90  --interval 1h
  python validate.py slippage     -s AVAXUSDT --days 90  --interval 1h
  python validate.py all          -s AVAXUSDT --days 180 --interval 1h

Modes:
  walkforward  — Split history into consecutive non-overlapping test windows;
                 prove the edge is stable across ALL windows, not one lucky period.
  basket       — Run the strategy across the top ~15 USDT pairs; a real edge
                 should survive on most liquid coins, not just cherry-picked ones.
  montecarlo   — Resample the trade sequence 1000× to build a distribution of
                 outcomes; reveals tail risk and luck-dependence.
  slippage     — Re-run with progressively worse slippage (0%–0.30%); finds the
                 break-even slippage where the edge vanishes.
  all          — Run every analysis above in sequence.
"""

import argparse
import json
import math
import sys
import time as _time
from pathlib import Path

import numpy as np
import pandas as pd

from backtest import fetch_historical_candles, StrategyBacktester, random_baseline
import config as cfg

# ──────────────────────────────────────────────────────────────
#  Constants & Helpers
# ──────────────────────────────────────────────────────────────

INTERVAL_MINUTES = {
    '1m': 1, '3m': 3, '5m': 5, '15m': 15, '30m': 30,
    '1h': 60, '2h': 120, '4h': 240, '1d': 1440,
}

DEFAULT_BASKET = [
    'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT',
    'ADAUSDT', 'AVAXUSDT', 'DOTUSDT', 'LINKUSDT', 'POLUSDT',
    'NEARUSDT', 'ATOMUSDT', 'APTUSDT', 'SUIUSDT', 'ARBUSDT',
]

RESULTS_DIR = Path('validation_results')


def _bpd(interval: str) -> int:
    """Bars per day for a given candle interval."""
    return 1440 // INTERVAL_MINUTES.get(interval, 15)


def _save(name: str, data: dict):
    """Persist results to JSON."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    fp = RESULTS_DIR / f'{name}.json'
    with open(fp, 'w') as f:
        json.dump(data, f, indent=2, default=str)
    print(f'[+] Results saved → {fp}')


def _elapsed(start: float) -> str:
    """Format elapsed time."""
    dt = _time.time() - start
    if dt < 60:
        return f'{dt:.1f}s'
    return f'{dt / 60:.1f}m'


# ══════════════════════════════════════════════════════════════
#  1. WALK-FORWARD VALIDATION
# ══════════════════════════════════════════════════════════════

def run_walk_forward(args):
    """Split history into consecutive test windows and verify performance stability.

    Each fold gets `warmup` bars for indicator convergence (EMA50/RSI), then
    `test_bars` bars where trades are counted.  Folds do NOT overlap.
    """
    sym      = args.symbol
    interval = args.interval
    days     = args.days
    test_d   = getattr(args, 'test_days', 30)
    cap      = args.capital
    tsize    = args.trade_size
    mscore   = args.min_score
    refresh  = getattr(args, 'refresh', False)

    t0 = _time.time()
    print(f'\n[*] Fetching {days} days of {interval} data for {sym}...')
    df = fetch_historical_candles(sym, interval=interval, days=days, refresh=refresh)

    bpd       = _bpd(interval)
    warmup    = 100                              # bars for indicator convergence
    test_bars = test_d * bpd

    print(f'[+] {len(df)} bars loaded | {bpd} bars/day | Window: {test_d}d ({test_bars} bars)')

    folds = []
    idx   = warmup                               # first tradeable position

    while idx + test_bars <= len(df):
        window = df.iloc[idx - warmup : idx + test_bars].copy()

        bt = StrategyBacktester(
            initial_capital=cap, trade_size_usdt=tsize, min_score=mscore
        )
        m = bt.run(sym, window, interval=interval, start_bar=warmup)

        s = df.index[idx].strftime('%Y-%m-%d')
        e = df.index[min(idx + test_bars - 1, len(df) - 1)].strftime('%Y-%m-%d')

        folds.append({
            'fold': len(folds) + 1,
            'period': f'{s} → {e}',
            'start': s, 'end': e,
            'metrics': m, 'n_trades': m.get('total_trades', 0),
            'trades': bt.trades,
            'partial': False,
        })
        idx += test_bars

    # ── Evaluate final tail window (if remaining data >= 7 days) ────────
    min_partial_bars = 7 * bpd
    if (len(df) - idx) >= min_partial_bars:
        tail_bars = len(df) - idx
        window = df.iloc[idx - warmup : len(df)].copy()
        bt = StrategyBacktester(
            initial_capital=cap, trade_size_usdt=tsize, min_score=mscore
        )
        m = bt.run(sym, window, interval=interval, start_bar=warmup)

        s = df.index[idx].strftime('%Y-%m-%d')
        e = df.index[-1].strftime('%Y-%m-%d')
        tail_days = round(tail_bars / bpd, 1)

        folds.append({
            'fold': len(folds) + 1,
            'period': f'{s} → {e} ({tail_days}d)',
            'start': s, 'end': e,
            'metrics': m, 'n_trades': m.get('total_trades', 0),
            'trades': bt.trades,
            'partial': True,
        })

    # ── Report ────────────────────────────────────────────────
    W = 85
    print('\n' + '═' * W)
    print(f'  WALK-FORWARD VALIDATION: {sym} ({interval})')
    print('═' * W)
    print(f'  Data : {days}d ({len(df)} bars) | Window: {test_d}d | Folds: {len(folds)} | Time: {_elapsed(t0)}')
    print('─' * W)

    hdr = (f"  {'#':>2}  {'Period':<25} {'Trades':>6}  {'Win%':>6}  "
           f"{'PF':>6}  {'Sharpe':>7}  {'Sortino':>8}  {'Expect':>8}  {'Return':>8}")
    print(hdr)
    print('─' * W)

    agg = dict(pf=[], sh=[], so=[], ex=[], wr=[], ret=[])
    all_oos_trades = []

    for f in folds:
        m  = f['metrics']
        t  = m.get('total_trades', 0)
        wr = m.get('win_rate_pct', 0)
        pf = m.get('profit_factor', 0)
        sh = m.get('sharpe_ratio', 0)
        so = m.get('sortino_ratio', 0)
        ex = m.get('expectancy_pct', 0)
        nr = m.get('net_return_pct', 0)

        flag = '  ✓' if ex > 0 and t > 0 else ('  ✗' if t > 0 else '')
        print(f"  {f['fold']:>2}  {f['period']:<25} {t:>6}  {wr:>5.1f}%  "
              f"{pf:>6.2f}  {sh:>7.2f}  {so:>8.2f}  {ex:>+7.2f}%  {nr:>+7.2f}%{flag}")

        if t > 0:
            for k, v in [('pf', pf), ('sh', sh), ('so', so),
                         ('ex', ex), ('wr', wr), ('ret', nr)]:
                agg[k].append(v)
            all_oos_trades.extend(f.get('trades', []))

    print('─' * W)

    aggregate = {}
    pooled_summary = {}

    if all_oos_trades:
        df_oos = pd.DataFrame(all_oos_trades)
        oos_wins = df_oos[df_oos['pnl_usdt'] > 0]
        oos_losses = df_oos[df_oos['pnl_usdt'] <= 0]
        oos_t = len(df_oos)
        oos_w = len(oos_wins)
        oos_l = len(oos_losses)
        oos_wr = (oos_w / oos_t) * 100.0 if oos_t > 0 else 0.0

        oos_gp = float(oos_wins['pnl_usdt'].sum()) if oos_w > 0 else 0.0
        oos_gl = float(abs(oos_losses['pnl_usdt'].sum())) if oos_l > 0 else 0.0
        oos_net = oos_gp - oos_gl
        oos_pf = (oos_gp / oos_gl) if oos_gl > 0 else float('inf')

        oos_ex_pct = float(df_oos['pnl_pct'].mean())
        oos_ex_usdt = oos_net / oos_t if oos_t > 0 else 0.0

        running_cap = cap
        cap_curve = [cap]
        for tr in all_oos_trades:
            running_cap += tr['pnl_usdt']
            cap_curve.append(running_cap)
        continuous_ret = ((running_cap - cap) / cap) * 100.0

        eq_s = pd.Series(cap_curve)
        eq_peaks = eq_s.cummax()
        oos_max_dd = float(abs(((eq_s - eq_peaks) / eq_peaks.replace(0, 1) * 100.0).min()))

        print('\n  POOLED OUT-OF-SAMPLE TOTALS (Fixed-Notional $100/trade Simulation):')
        print(f'    Total OOS Trades : {oos_t} ({oos_w} Wins / {oos_l} Losses | Win Rate: {oos_wr:.1f}%)')
        print(f'    Pooled Profit Fac: {oos_pf:.2f} (Gross Win: ${oos_gp:.2f} / Gross Loss: ${oos_gl:.2f})')
        print(f'    Pooled Expectancy: {oos_ex_pct:+.2f}% / trade (${oos_ex_usdt:+.2f} / trade)')
        print(f'    Net Realized P&L : ${oos_net:+.2f} ({continuous_ret:+.2f}% cumulative return on ${cap:,.0f} initial capital)')
        print(f'    Max OOS Drawdown : {oos_max_dd:.2f}%')

        pooled_summary = {
            'total_trades': oos_t, 'win_rate_pct': round(oos_wr, 2),
            'profit_factor': round(oos_pf, 2), 'expectancy_pct': round(oos_ex_pct, 2),
            'net_profit_usdt': round(oos_net, 2), 'cumulative_return_pct': round(continuous_ret, 2),
            'max_drawdown_pct': round(oos_max_dd, 2),
        }

    if agg['pf']:
        n          = len(agg['pf'])
        profitable = sum(1 for e in agg['ex'] if e > 0)
        consist    = profitable / n

        print(f'\n  FOLD-BY-FOLD DISPERSION ({n} folds with trades):')
        print(f'    Mean PF (folds)  : {np.mean(agg["pf"]):.2f} ± {np.std(agg["pf"]):.2f}')
        print(f'    Mean Sharpe      : {np.mean(agg["sh"]):.2f} ± {np.std(agg["sh"]):.2f}')
        print(f'    Mean Sortino     : {np.mean(agg["so"]):.2f} ± {np.std(agg["so"]):.2f}')
        print(f'    Mean Expectancy  : {np.mean(agg["ex"]):+.2f}% ± {np.std(agg["ex"]):.2f}%')
        print(f'    Profitable Folds : {profitable}/{n} folds ({consist * 100:.0f}%)')

        if consist >= 0.75 and pooled_summary.get('profit_factor', 0) > 1.0:
            label = 'HIGH ✓  — persistent edge across time windows'
        elif consist >= 0.50:
            label = 'MODERATE — edge exists but inconsistent/regime-dependent'
        else:
            label = 'LOW ✗   — failing out of sample, no consistent edge'
        print(f'    Consistency      : {label}')

        aggregate = {k: {'mean': round(float(np.mean(v)), 4),
                         'std':  round(float(np.std(v)), 4)}
                     for k, v in agg.items() if v}
        aggregate['profitable_folds'] = f'{profitable}/{n}'
    else:
        print('\n  No trades in any fold — the strategy took 0 trades.')

    print('═' * W + '\n')

    _save(f'walkforward_{sym}_{interval}_{days}d', {
        'symbol': sym, 'interval': interval, 'total_days': days,
        'test_days': test_d, 'n_folds': len(folds),
        'folds': [{k: v for k, v in f.items() if k != 'trades'} for f in folds],
        'pooled': pooled_summary,
        'aggregate': aggregate,
    })
    return folds


# ══════════════════════════════════════════════════════════════
#  2. MULTI-SYMBOL BASKET TEST & PORTFOLIO SIMULATION
# ══════════════════════════════════════════════════════════════

def simulate_portfolio(all_trades: list, initial_capital: float = 1000.0,
                       trade_size_usdt: float = 100.0, max_positions: int = 3) -> dict:
    """Chronologically simulate a combined portfolio across multiple coins with shared capital.

    Enforces:
    - Shared cash balance
    - Maximum concurrent open positions (default 3)
    - True chronological event ordering (cash freed only after position exit)
    """
    if not all_trades:
        return {
            'initial_capital': initial_capital, 'final_capital': initial_capital,
            'net_profit_usdt': 0.0, 'portfolio_return_pct': 0.0, 'max_drawdown_pct': 0.0,
            'trades_taken': 0, 'trades_skipped': 0, 'max_concurrent_positions': max_positions,
        }

    # Sort all trades chronologically by entry_time
    sorted_trades = sorted(all_trades, key=lambda t: t['entry_time'])

    cash = initial_capital
    open_positions = []
    trades_taken = 0
    trades_skipped = 0
    equity_curve = [initial_capital]

    for t in sorted_trades:
        entry_time = t['entry_time']

        # Settle any positions that closed at or before this entry in strict chronological order
        exited = [p for p in open_positions if p['exit_time'] <= entry_time]
        open_positions = [p for p in open_positions if p['exit_time'] > entry_time]

        exited.sort(key=lambda p: p['exit_time'])
        for pos in exited:
            cash += pos['pnl_usdt']
            equity_curve.append(cash)

        # Capacity check
        if len(open_positions) < max_positions and cash >= trade_size_usdt:
            open_positions.append(t)
            trades_taken += 1
        else:
            trades_skipped += 1

    # Settle remaining positions in chronological order
    open_positions.sort(key=lambda p: p['exit_time'])
    for pos in open_positions:
        cash += pos['pnl_usdt']
        equity_curve.append(cash)

    net_profit = cash - initial_capital
    portfolio_return_pct = (net_profit / initial_capital) * 100.0

    eq_s = pd.Series(equity_curve)
    eq_peaks = eq_s.cummax()
    max_dd = float(abs(((eq_s - eq_peaks) / eq_peaks.replace(0, 1) * 100.0).min()))

    return {
        'initial_capital': initial_capital,
        'final_capital': round(cash, 2),
        'net_profit_usdt': round(net_profit, 2),
        'portfolio_return_pct': round(portfolio_return_pct, 2),
        'max_drawdown_pct': round(max_dd, 2),
        'trades_taken': trades_taken,
        'trades_skipped': trades_skipped,
        'max_concurrent_positions': max_positions,
    }


def run_basket(args):
    """Run strategy across top coins to verify cross-asset edge.

    For each symbol: run full backtest + random baseline comparison.
    A genuine momentum breakout signal should yield positive expectancy
    on most liquid pairs, not just cherry-picked ones.
    """
    interval = args.interval
    days     = args.days
    cap      = args.capital
    tsize    = args.trade_size
    mscore   = args.min_score
    n_base   = getattr(args, 'baseline', 50)
    refresh  = getattr(args, 'refresh', False)
    symbols  = DEFAULT_BASKET

    t0 = _time.time()
    W  = 100

    print('\n' + '═' * W)
    print(f'  MULTI-SYMBOL BASKET TEST ({interval}, {days}d, {len(symbols)} symbols)')
    print('═' * W)

    hdr = (f"  {'Symbol':<10} {'Trades':>6}  {'Win%':>6}  {'PF':>6}  "
           f"{'Sharpe':>7}  {'Sortino':>8}  {'Expect':>8}  {'Return':>8}  {'BeatRand':>8}")
    print(hdr)
    print('─' * W)

    results = []
    agg = dict(pf=[], sh=[], so=[], ex=[], ret=[])
    all_basket_trades = []

    for i, sym in enumerate(symbols, 1):
        try:
            df = fetch_historical_candles(sym, interval=interval, days=days, refresh=refresh)
        except Exception as e:
            print(f"  {sym:<10} SKIP — {e}")
            continue

        if len(df) < 120:
            print(f"  {sym:<10} SKIP — insufficient data ({len(df)} bars)")
            continue

        bt = StrategyBacktester(initial_capital=cap, trade_size_usdt=tsize, min_score=mscore)
        m  = bt.run(sym, df, interval=interval)

        # Random baseline comparison
        beat_str = ''
        beat_pct = None
        if n_base > 0 and m.get('total_trades', 0) > 0:
            try:
                base = random_baseline(sym, df, bt.trades, n_base, cap, tsize, mscore, interval=interval)
                if base and base.get('runs'):
                    nets    = np.array(base['nets'])
                    actual  = m['net_return_pct']
                    beat_pct = float(np.mean(nets < actual)) * 100.0
                    beat_str = f'{beat_pct:.0f}%'
            except Exception:
                beat_str = 'err'

        t  = m.get('total_trades', 0)
        wr = m.get('win_rate_pct', 0)
        pf = m.get('profit_factor', 0)
        sh = m.get('sharpe_ratio', 0)
        so = m.get('sortino_ratio', 0)
        ex = m.get('expectancy_pct', 0)
        nr = m.get('net_return_pct', 0)

        marker = '  ✓' if ex > 0 and t > 0 else ('  ✗' if t > 0 else '')
        print(f"  {sym:<10} {t:>6}  {wr:>5.1f}%  {pf:>6.2f}  "
              f"{sh:>7.2f}  {so:>8.2f}  {ex:>+7.2f}%  {nr:>+7.2f}%  {beat_str:>8}{marker}")

        results.append({
            'symbol': sym, 'metrics': m,
            'beat_random_pct': beat_pct,
        })

        if t > 0:
            for k, v in [('pf', pf), ('sh', sh), ('so', so), ('ex', ex), ('ret', nr)]:
                agg[k].append(v)
            all_basket_trades.extend(bt.trades)

    print('─' * W)

    basket_summary = {}
    portfolio_summary = {}

    if agg['pf'] and all_basket_trades:
        n       = len(agg['pf'])
        pos_exp = sum(1 for e in agg['ex'] if e > 0)

        df_b = pd.DataFrame(all_basket_trades)
        b_wins = df_b[df_b['pnl_usdt'] > 0]
        b_losses = df_b[df_b['pnl_usdt'] <= 0]
        b_t = len(df_b)
        b_w = len(b_wins)
        b_l = len(b_losses)
        b_gp = float(b_wins['pnl_usdt'].sum()) if b_w > 0 else 0.0
        b_gl = float(abs(b_losses['pnl_usdt'].sum())) if b_l > 0 else 0.0
        b_net = b_gp - b_gl
        b_pf = (b_gp / b_gl) if b_gl > 0 else float('inf')

        # Run true portfolio simulation (shared capital, max 3 positions)
        max_pos = int(getattr(cfg, 'MAX_OPEN_TRADES', 3)) or 3
        port = simulate_portfolio(all_basket_trades, initial_capital=cap,
                                  trade_size_usdt=tsize, max_positions=max_pos)

        print(f'\n  CROSS-ASSET SIGNAL BREADTH ({n} symbols with trades):')
        print(f'    Symbols +Expectancy: {pos_exp}/{n} ({pos_exp / n * 100:.0f}%)')
        print(f'    Mean Expectancy    : {np.mean(agg["ex"]):+.2f}% ± {np.std(agg["ex"]):.2f}%')
        print(f'    Median Expectancy  : {np.median(agg["ex"]):+.2f}%')
        print(f'    Pooled Profit Fac  : {b_pf:.2f} across all {b_t} trades (Gross Win: ${b_gp:.2f} / Gross Loss: ${b_gl:.2f})')
        print(f'    Total Net P&L      : ${b_net:+.2f} (unconstrained sum of all coin trades)')

        if pos_exp / n >= 0.60 and b_pf > 1.0:
            verdict = 'BROAD EDGE ✓ — strategy works across most assets'
        elif pos_exp / n >= 0.40:
            verdict = 'SELECTIVE — edge exists on select assets only'
        else:
            verdict = 'NARROW / FAILING ✗ — edge fails across the liquid market'
        print(f'    Signal Verdict     : {verdict}')

        print(f'\n  PORTFOLIO SIMULATION (Shared ${cap:,.0f} Capital, Max {max_pos} Concurrent Positions):')
        print(f'    Trades Executed    : {port["trades_taken"]} taken ({port["trades_skipped"]} skipped due to capacity limit)')
        print(f'    Portfolio Return   : {port["portfolio_return_pct"]:+.2f}% (${port["net_profit_usdt"]:+.2f})')
        print(f'    Portfolio Max DD   : {port["max_drawdown_pct"]:.2f}%')
        print(f'    Final Capital      : ${port["final_capital"]:,.2f}')

        basket_summary = {
            'symbols_evaluated': n, 'positive_expectancy_symbols': f'{pos_exp}/{n}',
            'mean_expectancy_pct': round(float(np.mean(agg['ex'])), 4),
            'median_expectancy_pct': round(float(np.median(agg['ex'])), 4),
            'pooled_profit_factor': round(b_pf, 2),
            'total_net_pnl_usdt': round(b_net, 2),
            'verdict': verdict,
        }
        portfolio_summary = port

    print(f'\n  Elapsed: {_elapsed(t0)}')
    print('═' * W + '\n')

    _save(f'basket_{interval}_{days}d', {
        'interval': interval, 'days': days, 'n_symbols': len(symbols),
        'results': results, 'signal_breadth': basket_summary,
        'portfolio_simulation': portfolio_summary,
    })
    return results


# ══════════════════════════════════════════════════════════════
#  3. MONTE CARLO SIMULATION
# ══════════════════════════════════════════════════════════════

def run_monte_carlo(args):
    """Resample trade PnLs with replacement to estimate the distribution of outcomes.

    If actual results are just one lucky ordering of trades, Monte Carlo will
    show a wide distribution.  A robust strategy shows narrow, consistently
    profitable outcomes.  Also reveals tail-risk (worst-case drawdowns).
    """
    sym      = args.symbol
    interval = args.interval
    days     = args.days
    cap      = args.capital
    tsize    = args.trade_size
    mscore   = args.min_score
    n_sims   = getattr(args, 'n_sims', 1000)
    refresh  = getattr(args, 'refresh', False)

    print(f'\n[*] Running base backtest for {sym} ({interval}, {days}d)...')
    df = fetch_historical_candles(sym, interval=interval, days=days, refresh=refresh)
    bt = StrategyBacktester(initial_capital=cap, trade_size_usdt=tsize, min_score=mscore)
    metrics = bt.run(sym, df, interval=interval)

    trades = bt.trades
    if not trades:
        print(f'[-] No trades for {sym} — cannot run Monte Carlo.\n')
        return None

    pnls = np.array([t['pnl_usdt'] for t in trades])
    n    = len(pnls)
    rng  = np.random.default_rng(42)

    print(f'[*] Resampling {n} trades × {n_sims:,} simulations...')
    t0 = _time.time()

    finals  = np.empty(n_sims)
    max_dds = np.empty(n_sims)

    for i in range(n_sims):
        sampled = rng.choice(pnls, size=n, replace=True)
        equity  = cap + np.cumsum(sampled)
        finals[i] = equity[-1]

        full_eq = np.concatenate([[cap], equity])
        peaks   = np.maximum.accumulate(full_eq)
        dd      = np.where(peaks > 0, (peaks - full_eq) / peaks * 100, 0)
        max_dds[i] = dd.max()

    prob_profit = float(np.mean(finals > cap)) * 100

    # ── Report ────────────────────────────────────────────────
    W = 70
    print('\n' + '═' * W)
    print(f'  MONTE CARLO SIMULATION: {sym} ({interval}, {days}d)')
    print('═' * W)
    print(f'  Source   : {n} trades resampled {n_sims:,}× with replacement')
    print(f'  Actual   : ${metrics["final_capital"]:.2f} ({metrics["net_return_pct"]:+.2f}%)')
    print(f'  Time     : {_elapsed(t0)}')
    print('─' * W)

    print(f'\n  FINAL CAPITAL DISTRIBUTION:')
    print(f'     5th percentile  : ${np.percentile(finals, 5):.2f}')
    print(f'    25th percentile  : ${np.percentile(finals, 25):.2f}')
    print(f'    Median           : ${np.median(finals):.2f}')
    print(f'    Mean             : ${np.mean(finals):.2f}')
    print(f'    75th percentile  : ${np.percentile(finals, 75):.2f}')
    print(f'    95th percentile  : ${np.percentile(finals, 95):.2f}')
    print(f'    Prob of profit   : {prob_profit:.1f}%')

    print(f'\n  MAX DRAWDOWN DISTRIBUTION:')
    print(f'    Median DD        : {np.median(max_dds):.2f}%')
    print(f'    Mean DD          : {np.mean(max_dds):.2f}%')
    print(f'    95th pct DD      : {np.percentile(max_dds, 95):.2f}%')
    print(f'    Worst-case DD    : {np.max(max_dds):.2f}%')

    ruin_20 = float(np.mean(finals < cap * 0.80)) * 100
    ruin_50 = float(np.mean(finals < cap * 0.50)) * 100
    print(f'\n  RISK OF RUIN:')
    print(f'    P(loss > 20%)    : {ruin_20:.1f}%')
    print(f'    P(loss > 50%)    : {ruin_50:.1f}%')

    if prob_profit >= 75:
        verdict = 'ROBUST ✓ — strategy profits in the vast majority of simulated outcomes'
    elif prob_profit >= 55:
        verdict = 'MARGINAL — edge exists but sensitive to trade ordering'
    else:
        verdict = 'FRAGILE ✗ — results heavily dependent on luck/sequence'
    print(f'\n  VERDICT: {verdict}')

    print('═' * W + '\n')

    result = {
        'symbol': sym, 'interval': interval, 'days': days,
        'n_trades': n, 'n_sims': n_sims,
        'actual_return_pct': metrics['net_return_pct'],
        'prob_profit_pct': round(prob_profit, 2),
        'final_capital': {
            'p5':     round(float(np.percentile(finals, 5)), 2),
            'p25':    round(float(np.percentile(finals, 25)), 2),
            'median': round(float(np.median(finals)), 2),
            'mean':   round(float(np.mean(finals)), 2),
            'p75':    round(float(np.percentile(finals, 75)), 2),
            'p95':    round(float(np.percentile(finals, 95)), 2),
        },
        'max_drawdown': {
            'median': round(float(np.median(max_dds)), 2),
            'mean':   round(float(np.mean(max_dds)), 2),
            'p95':    round(float(np.percentile(max_dds, 95)), 2),
            'worst':  round(float(np.max(max_dds)), 2),
        },
        'risk_of_ruin_20pct': round(ruin_20, 2),
        'risk_of_ruin_50pct': round(ruin_50, 2),
    }

    _save(f'montecarlo_{sym}_{interval}_{days}d', result)
    return result


# ══════════════════════════════════════════════════════════════
#  4. SLIPPAGE & FEE SENSITIVITY
# ══════════════════════════════════════════════════════════════

def run_slippage(args):
    """Re-run the backtest with progressively worse execution to find the break-even point.

    If a small slippage increase destroys the edge, the strategy is too
    tight for live trading (where slippage variance is real).
    """
    sym      = args.symbol
    interval = args.interval
    days     = args.days
    cap      = args.capital
    tsize    = args.trade_size
    mscore   = args.min_score
    refresh  = getattr(args, 'refresh', False)

    print(f'\n[*] Fetching {days} days of {interval} data for {sym}...')
    df = fetch_historical_candles(sym, interval=interval, days=days, refresh=refresh)

    slippages = [0.00, 0.02, 0.05, 0.08, 0.10, 0.15, 0.20, 0.25, 0.30]
    rows = []

    for slip in slippages:
        bt = StrategyBacktester(
            initial_capital=cap, trade_size_usdt=tsize,
            slippage_pct=slip, min_score=mscore,
        )
        m = bt.run(sym, df, interval=interval)
        rows.append({'slippage': slip, 'metrics': m})

    # ── Report ────────────────────────────────────────────────
    W = 85
    print('\n' + '═' * W)
    print(f'  SLIPPAGE SENSITIVITY: {sym} ({interval}, {days}d)')
    print('═' * W)

    hdr = (f"  {'Slip%':>6}  {'Trades':>6}  {'Win%':>6}  {'PF':>6}  "
           f"{'Sharpe':>7}  {'Expect':>8}  {'Return':>8}  {'MaxDD':>7}")
    print(hdr)
    print('─' * W)

    breakeven_slip = None
    prev_exp  = None
    prev_slip = None

    for r in rows:
        m  = r['metrics']
        s  = r['slippage']
        t  = m.get('total_trades', 0)
        wr = m.get('win_rate_pct', 0)
        pf = m.get('profit_factor', 0)
        sh = m.get('sharpe_ratio', 0)
        ex = m.get('expectancy_pct', 0)
        nr = m.get('net_return_pct', 0)
        dd = m.get('max_drawdown_pct', 0)

        marker = '  ← default' if abs(s - 0.05) < 0.001 else ''
        print(f"  {s:>5.2f}%  {t:>6}  {wr:>5.1f}%  {pf:>6.2f}  "
              f"{sh:>7.2f}  {ex:>+7.2f}%  {nr:>+7.2f}%  {dd:>6.2f}%{marker}")

        # Linear interpolation to find where expectancy crosses zero
        if (prev_exp is not None and prev_exp > 0 and ex <= 0
                and breakeven_slip is None):
            breakeven_slip = prev_slip + (0 - prev_exp) / (ex - prev_exp) * (s - prev_slip)

        prev_exp  = ex
        prev_slip = s

    print('─' * W)

    if breakeven_slip is not None:
        print(f'\n  Break-even slippage : ~{breakeven_slip:.3f}%')
        margin = breakeven_slip / 0.05 if 0.05 > 0 else 0
        if breakeven_slip > 0.15:
            print(f'  → ROBUST ✓ : edge survives up to {breakeven_slip:.2f}% slippage '
                  f'({margin:.1f}× the default 0.05%)')
        elif breakeven_slip > 0.08:
            print(f'  → ADEQUATE : edge survives moderate slippage increases ({margin:.1f}× default)')
        else:
            print(f'  → FRAGILE ✗ : small slippage increases destroy the edge')
    elif prev_exp is not None and prev_exp > 0:
        print(f'\n  Edge survives even at 0.30% slippage — very robust ✓')
    else:
        # Check if there's any positive expectancy at all
        any_positive = any(r['metrics'].get('expectancy_pct', 0) > 0 for r in rows)
        if any_positive:
            print(f'\n  Edge only exists at very low slippage — potentially fragile')
        else:
            print(f'\n  No positive expectancy at any slippage level ✗')

    print('═' * W + '\n')

    _save(f'slippage_{sym}_{interval}_{days}d', {
        'symbol': sym, 'interval': interval, 'days': days,
        'breakeven_slippage_pct': breakeven_slip,
        'results': [{'slippage_pct': r['slippage'], **r['metrics']} for r in rows],
    })
    return rows


# ══════════════════════════════════════════════════════════════
#  5. RUN ALL ANALYSES
# ══════════════════════════════════════════════════════════════

def run_all(args):
    """Execute every analysis mode in sequence."""
    banner = '█' * 65
    print(f'\n{banner}')
    print( '  COMPREHENSIVE VALIDATION SUITE — CryptoBot')
    print(f'{banner}')
    print(f'  Symbol   : {args.symbol}')
    print(f'  Interval : {args.interval}')
    print(f'  Days     : {args.days}')
    print(f'  Capital  : ${args.capital:,.0f}')
    print(f'  Trade    : ${args.trade_size:,.0f}')
    print()

    # Ensure all mode-specific attrs exist with sensible defaults
    for attr, default in [('test_days', 30), ('n_sims', 1000), ('baseline', 50)]:
        if not hasattr(args, attr):
            setattr(args, attr, default)

    t0 = _time.time()

    print(f'  [1/4] Walk-Forward Validation...')
    run_walk_forward(args)

    print(f'  [2/4] Multi-Symbol Basket Test...')
    run_basket(args)

    print(f'  [3/4] Monte Carlo Simulation...')
    run_monte_carlo(args)

    print(f'  [4/4] Slippage Sensitivity...')
    run_slippage(args)

    print(f'\n{banner}')
    print(f'  ALL ANALYSES COMPLETE — Total time: {_elapsed(t0)}')
    print(f'  Results saved in: {RESULTS_DIR.resolve()}')
    print(f'{banner}\n')


# ══════════════════════════════════════════════════════════════
#  MAIN — CLI
# ══════════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser(
        description='CryptoBot — Walk-Forward Validation & Robustness Suite',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest='mode', help='Analysis mode to run')

    # ── Shared arguments (inherited by all sub-commands) ──────
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument('-s', '--symbol',     default='AVAXUSDT',
                        help='Trading pair (default: AVAXUSDT)')
    shared.add_argument('--days',    type=int,   default=180,
                        help='Days of historical data (default: 180)')
    shared.add_argument('--interval',            default='1h',
                        help='Candle interval (default: 1h)')
    shared.add_argument('--capital', type=float, default=1000.0,
                        help='Initial simulated capital (default: 1000)')
    shared.add_argument('--trade_size', type=float, default=100.0,
                        help='Trade size per order in USDT (default: 100)')
    shared.add_argument('--min_score', type=float, default=None,
                        help='Entry score threshold (default: from config.py)')
    shared.add_argument('--refresh', action='store_true',
                        help='Re-download data even if cached')

    # ── Walk-forward ──────────────────────────────────────────
    wf = sub.add_parser('walkforward', parents=[shared],
                        help='Rolling out-of-sample windows')
    wf.add_argument('--test_days', type=int, default=30,
                    help='Test window size in days (default: 30)')
    wf.set_defaults(func=run_walk_forward)

    # ── Basket ────────────────────────────────────────────────
    ba = sub.add_parser('basket', parents=[shared],
                        help='Test across top 15 USDT pairs')
    ba.add_argument('--baseline', type=int, default=50,
                    help='Random baseline runs per symbol (default: 50, 0=skip)')
    ba.set_defaults(func=run_basket)

    # ── Monte Carlo ───────────────────────────────────────────
    mc = sub.add_parser('montecarlo', parents=[shared],
                        help='Trade resampling simulation')
    mc.add_argument('--n_sims', type=int, default=1000,
                    help='Number of resample simulations (default: 1000)')
    mc.set_defaults(func=run_monte_carlo)

    # ── Slippage ──────────────────────────────────────────────
    sl = sub.add_parser('slippage', parents=[shared],
                        help='Slippage/fee degradation test')
    sl.set_defaults(func=run_slippage)

    # ── All ───────────────────────────────────────────────────
    al = sub.add_parser('all', parents=[shared],
                        help='Run every analysis in sequence')
    al.add_argument('--test_days', type=int, default=30,
                    help='Walk-forward window (default: 30)')
    al.add_argument('--n_sims', type=int, default=1000,
                    help='Monte Carlo simulations (default: 1000)')
    al.add_argument('--baseline', type=int, default=50,
                    help='Random baseline runs per symbol (default: 50)')
    al.set_defaults(func=run_all)

    args = p.parse_args()
    if not args.mode:
        p.print_help()
        print('\nExample: python validate.py walkforward -s AVAXUSDT --days 180 --interval 1h')
        sys.exit(1)

    args.func(args)


if __name__ == '__main__':
    main()
