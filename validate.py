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
    'ADAUSDT', 'AVAXUSDT', 'DOTUSDT', 'LINKUSDT', 'MATICUSDT',
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

    t0 = _time.time()
    print(f'\n[*] Fetching {days} days of {interval} data for {sym}...')
    df = fetch_historical_candles(sym, interval=interval, days=days)

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
        m = bt.run(sym, window)

        s = df.index[idx].strftime('%Y-%m-%d')
        e = df.index[min(idx + test_bars - 1, len(df) - 1)].strftime('%Y-%m-%d')

        folds.append({
            'fold': len(folds) + 1,
            'period': f'{s} → {e}',
            'start': s, 'end': e,
            'metrics': m, 'n_trades': m.get('total_trades', 0),
        })
        idx += test_bars

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

    print('─' * W)

    aggregate = {}
    if agg['pf']:
        n          = len(agg['pf'])
        profitable = sum(1 for e in agg['ex'] if e > 0)
        consist    = profitable / n

        print(f'\n  AGGREGATE ({n} folds with trades):')
        print(f'    Profit Factor  : {np.mean(agg["pf"]):.2f} ± {np.std(agg["pf"]):.2f}')
        print(f'    Sharpe Ratio   : {np.mean(agg["sh"]):.2f} ± {np.std(agg["sh"]):.2f}')
        print(f'    Sortino Ratio  : {np.mean(agg["so"]):.2f} ± {np.std(agg["so"]):.2f}')
        print(f'    Expectancy     : {np.mean(agg["ex"]):+.2f}% ± {np.std(agg["ex"]):.2f}%')
        print(f'    Win Rate       : {np.mean(agg["wr"]):.1f}% ± {np.std(agg["wr"]):.1f}%')
        print(f'    Net Return     : {np.mean(agg["ret"]):+.2f}% ± {np.std(agg["ret"]):.2f}%')
        print(f'    Profitable     : {profitable}/{n} folds ({consist * 100:.0f}%)')

        if consist >= 0.75:
            label = 'HIGH ✓  — edge is stable across time periods'
        elif consist >= 0.50:
            label = 'MODERATE — edge exists but inconsistent'
        else:
            label = 'LOW ✗   — likely overfitting or regime-dependent'
        print(f'    Consistency    : {label}')

        aggregate = {k: {'mean': round(float(np.mean(v)), 4),
                         'std':  round(float(np.std(v)), 4)}
                     for k, v in agg.items() if v}
        aggregate['profitable_folds'] = f'{profitable}/{n}'
    else:
        print('\n  No trades in any fold — the strategy may need a lower score threshold.')

    print('═' * W + '\n')

    _save(f'walkforward_{sym}_{interval}_{days}d', {
        'symbol': sym, 'interval': interval, 'total_days': days,
        'test_days': test_d, 'n_folds': len(folds),
        'folds': folds, 'aggregate': aggregate,
    })
    return folds


# ══════════════════════════════════════════════════════════════
#  2. MULTI-SYMBOL BASKET TEST
# ══════════════════════════════════════════════════════════════

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

    for i, sym in enumerate(symbols, 1):
        try:
            df = fetch_historical_candles(sym, interval=interval, days=days)
        except Exception as e:
            print(f"  {sym:<10} SKIP — {e}")
            continue

        if len(df) < 120:
            print(f"  {sym:<10} SKIP — insufficient data ({len(df)} bars)")
            continue

        bt = StrategyBacktester(initial_capital=cap, trade_size_usdt=tsize, min_score=mscore)
        m  = bt.run(sym, df)

        # Random baseline comparison
        beat_str = ''
        beat_pct = None
        if n_base > 0 and m.get('total_trades', 0) > 0:
            try:
                base = random_baseline(sym, df, bt.trades, n_base, cap, tsize, mscore)
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

    print('─' * W)

    basket_summary = {}
    if agg['pf']:
        n       = len(agg['pf'])
        pos_exp = sum(1 for e in agg['ex'] if e > 0)

        print(f'\n  BASKET SUMMARY ({n} symbols with trades):')
        print(f'    Mean PF          : {np.mean(agg["pf"]):.2f} ± {np.std(agg["pf"]):.2f}')
        print(f'    Mean Sharpe      : {np.mean(agg["sh"]):.2f} ± {np.std(agg["sh"]):.2f}')
        print(f'    Mean Sortino     : {np.mean(agg["so"]):.2f} ± {np.std(agg["so"]):.2f}')
        print(f'    Mean Expectancy  : {np.mean(agg["ex"]):+.2f}% ± {np.std(agg["ex"]):.2f}%')
        print(f'    Symbols +expect  : {pos_exp}/{n} ({pos_exp / n * 100:.0f}%)')
        print(f'    Cumulative Ret   : {sum(agg["ret"]):+.2f}% across {n} symbols')

        if pos_exp / n >= 0.60:
            print(f'    Verdict          : BROAD EDGE ✓ — strategy works across most assets')
        elif pos_exp / n >= 0.40:
            print(f'    Verdict          : SELECTIVE — edge exists but coin-dependent')
        else:
            print(f'    Verdict          : NARROW ✗ — edge limited to few assets')

        basket_summary = {k: {'mean': round(float(np.mean(v)), 4),
                              'std':  round(float(np.std(v)), 4)}
                          for k, v in agg.items() if v}
        basket_summary['positive_symbols'] = f'{pos_exp}/{n}'

    print(f'\n  Elapsed: {_elapsed(t0)}')
    print('═' * W + '\n')

    _save(f'basket_{interval}_{days}d', {
        'interval': interval, 'days': days, 'n_symbols': len(symbols),
        'results': results, 'summary': basket_summary,
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

    print(f'\n[*] Running base backtest for {sym} ({interval}, {days}d)...')
    df = fetch_historical_candles(sym, interval=interval, days=days)
    bt = StrategyBacktester(initial_capital=cap, trade_size_usdt=tsize, min_score=mscore)
    metrics = bt.run(sym, df)

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

    print(f'\n[*] Fetching {days} days of {interval} data for {sym}...')
    df = fetch_historical_candles(sym, interval=interval, days=days)

    slippages = [0.00, 0.02, 0.05, 0.08, 0.10, 0.15, 0.20, 0.25, 0.30]
    rows = []

    for slip in slippages:
        bt = StrategyBacktester(
            initial_capital=cap, trade_size_usdt=tsize,
            slippage_pct=slip, min_score=mscore,
        )
        m = bt.run(sym, df)
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
