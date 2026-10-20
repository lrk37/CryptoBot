"""
scan_logger.py — Empirical Forward Return Tracker for CryptoBot
================================================================
As recommended by institutional review:
At each 5m market scan, records top candidates with scores and orderbook depth.
Tracks actual market price 1 hour and 4 hours later to measure real-world
predictive edge and statistical validity without risking capital.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)
SCAN_LOG_FILE = Path("scan_history.jsonl")

def log_scan_candidates(candidates: list, exchange=None):
    """
    Append top scan candidates to scan_history.jsonl with a timestamp,
    initial entry price, and score breakdown.
    """
    if not candidates:
        return

    now = time.time()
    records = []
    for c in candidates[:5]:  # Top 5 candidates per scan
        record = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'ts': now,
            'symbol': c.symbol,
            'score': c.score,
            'raw_score': getattr(c, 'raw_score', c.score),
            'ema_score': getattr(c, 'ema_score', 0),
            'rsi_score': getattr(c, 'rsi_score', 0),
            'vol_score': getattr(c, 'vol_score', 0),
            'breakout_score': getattr(c, 'breakout_score', 0),
            'ob_score': getattr(c, 'ob_score', 0),
            'rsi': getattr(c, 'rsi', 0),
            'volume_ratio': getattr(c, 'volume_ratio', 0),
            'bid_ask_ratio': getattr(c, 'bid_ask_ratio', 1.0),
            'price_at_scan': c.close_price,
            'price_1h': None,
            'pnl_1h_pct': None,
            'price_4h': None,
            'pnl_4h_pct': None,
        }
        records.append(record)

    try:
        with open(SCAN_LOG_FILE, 'a', encoding='utf-8') as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
    except Exception as e:
        log.warning(f"[SCAN LOGGER] Failed to append scan history: {e}")

def update_forward_returns(exchange):
    """
    Inspect previous scan logs and fill in price_1h and price_4h once enough
    time has elapsed, computing the empirical forward return.
    """
    if not SCAN_LOG_FILE.exists() or not exchange:
        return

    now = time.time()
    updated = False
    new_lines = []

    try:
        with open(SCAN_LOG_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    age = now - r.get('ts', now)
                    sym = r['symbol']
                    base_price = r.get('price_at_scan', 0)

                    # Update 1h return if age >= 3600s
                    if r.get('price_1h') is None and age >= 3600 and base_price > 0:
                        curr_p = exchange.get_current_price(sym)
                        if curr_p and curr_p > 0:
                            r['price_1h'] = curr_p
                            r['pnl_1h_pct'] = round(((curr_p - base_price) / base_price) * 100.0, 3)
                            updated = True

                    # Update 4h return if age >= 14400s
                    if r.get('price_4h') is None and age >= 14400 and base_price > 0:
                        curr_p = exchange.get_current_price(sym)
                        if curr_p and curr_p > 0:
                            r['price_4h'] = curr_p
                            r['pnl_4h_pct'] = round(((curr_p - base_price) / base_price) * 100.0, 3)
                            updated = True

                    new_lines.append(json.dumps(r) + "\n")
                except Exception:
                    new_lines.append(line + "\n")

        if updated:
            with open(SCAN_LOG_FILE, 'w', encoding='utf-8') as f:
                f.writelines(new_lines)
    except Exception as e:
        log.warning(f"[SCAN LOGGER] Failed updating forward returns: {e}")
