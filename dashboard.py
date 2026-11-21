"""
dashboard.py — CryptoBot Read-Only Live Web Command Center
===========================================================
A standalone, zero-dependency, read-only web dashboard that visualizes:
  - Live Account Equity, Peak High-Water Mark, Expectancy, Win Rate & Profit Factor
  - Active Open Position with live public Binance price & SL <-> TP progress gauge
  - Cumulative Realized PnL Equity Curve (SVG)
  - Full Closed Trade History with Score, MFE/MAE, Duration & Exit Reason badges
  - Saved Entry & Exit Chart PNG Gallery (from charts/) with click-to-zoom lightbox
  - Live tail of bot.log

Safety Guarantee:
  Strictly READ-ONLY. Never imports or mutates the running bot, never places or cancels orders,
  and requires zero extra pip packages (uses Python standard library http.server).

Usage:
  python dashboard.py --port 8050
"""
import argparse
import base64
import hmac
import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

import config as cfg

_PRICE_CACHE = {}
_PRICE_CACHE_TTL = 5.0


def _fetch_public_price(symbol: str) -> float:
    """Fetch public live ticker price from Binance without API keys (cached for 5s)."""
    now = time.time()
    cached = _PRICE_CACHE.get(symbol)
    if cached and (now - cached[1]) < _PRICE_CACHE_TTL:
        return cached[0]
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
        req = urllib.request.Request(url, headers={"User-Agent": "CryptoBot-Dashboard/1.0"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            px = float(data.get("price", 0.0) or 0.0)
            if px > 0:
                _PRICE_CACHE[symbol] = (px, now)
                return px
    except Exception:
        pass
    return cached[0] if cached else 0.0


def _read_json_safe(path: str, default):
    try:
        p = Path(path)
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default


def _tail_log(path: str, max_lines: int = 45) -> list:
    try:
        p = Path(path)
        if not p.exists():
            return []
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = min(size, 24576)
            f.seek(max(0, size - block))
            raw = f.read().decode("utf-8", errors="replace")
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        return lines[-max_lines:]
    except Exception:
        return []


def collect_dashboard_state(base_dir: str = ".") -> dict:
    base = Path(base_dir)
    pos_file = str(base / getattr(cfg, "POSITIONS_FILE", "positions.json"))
    trade_file = str(base / getattr(cfg, "TRADE_LOG_FILE", "trade_log.json"))
    loss_file = str(base / "loss_state.json")
    scan_file = str(base / "scanner_state.json")
    log_file = str(base / getattr(cfg, "LOG_FILE", "bot.log"))
    charts_dir = base / "charts"

    raw_positions = _read_json_safe(pos_file, {}) or {}
    trades = _read_json_safe(trade_file, []) or []
    loss_state = _read_json_safe(loss_file, {}) or {}
    scan_state = _read_json_safe(scan_file, {}) or {}

    # Compute Expectancy & Performance Metrics
    total_trades = len(trades)
    pnls = [float(t.get("net_pnl_usdt", t.get("pnl_usdt", 0.0)) or 0.0) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    net_pnl = sum(pnls)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    win_rate = (len(wins) / total_trades * 100.0) if total_trades > 0 else 0.0
    avg_win = (gross_win / len(wins)) if wins else 0.0
    avg_loss = (-gross_loss / len(losses)) if losses else 0.0
    expectancy = (net_pnl / total_trades) if total_trades > 0 else 0.0
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (99.9 if gross_win > 0 else 0.0)

    # Cumulative PnL curve points
    cum = 0.0
    equity_curve = [{"idx": 0, "label": "Start", "cum_pnl": 0.0}]
    for i, t in enumerate(trades, start=1):
        p = float(t.get("net_pnl_usdt", t.get("pnl_usdt", 0.0)) or 0.0)
        cum += p
        equity_curve.append({
            "idx": i,
            "symbol": t.get("symbol", ""),
            "label": f"#{i} {t.get('symbol', '')}",
            "pnl": round(p, 4),
            "cum_pnl": round(cum, 4),
            "timestamp": t.get("timestamp", ""),
        })

    # Enrich active open positions with live public price
    now = time.time()
    open_positions = []
    for sym, p in raw_positions.items():
        entry_px = float(p.get("entry_price", 0.0) or 0.0)
        qty = float(p.get("quantity", 0.0) or 0.0)
        sl = float(p.get("stop_loss", 0.0) or 0.0)
        tp = float(p.get("take_profit", 0.0) or 0.0)
        live_px = _fetch_public_price(sym) or entry_px
        unreal_pct = ((live_px - entry_px) / entry_px * 100.0) if entry_px > 0 else 0.0
        unreal_usdt = (live_px - entry_px) * qty
        open_ts = float(p.get("open_time", now) or now)
        held_sec = max(0, int(now - open_ts))
        band = (tp - sl) if (tp > sl) else 1.0
        progress_pct = max(0.0, min(100.0, ((live_px - sl) / band) * 100.0)) if tp > sl else 50.0
        entry_marker_pct = max(0.0, min(100.0, ((entry_px - sl) / band) * 100.0)) if tp > sl else 50.0
        dt = p.get("details") or {}
        open_positions.append({
            "symbol": sym,
            "timeframe": dt.get("timeframe", getattr(cfg, "SCANNER_INTERVAL", "15m")),
            "entry_price": entry_px,
            "live_price": live_px,
            "quantity": qty,
            "notional_usdt": round(entry_px * qty, 2),
            "stop_loss": sl,
            "take_profit": tp,
            "highest_price": float(p.get("highest_price", entry_px) or entry_px),
            "unrealized_pct": round(unreal_pct, 2),
            "unrealized_usdt": round(unreal_usdt, 4),
            "held_sec": held_sec,
            "score": float(p.get("entry_score", 0.0) or 0.0),
            "oco_id": dt.get("oco_order_list_id"),
            "progress_pct": round(progress_pct, 1),
            "entry_marker_pct": round(entry_marker_pct, 1),
        })

    # Collect recent chart images
    charts = []
    if charts_dir.exists():
        pngs = sorted(charts_dir.glob("*.png"), key=lambda x: x.stat().st_mtime, reverse=True)[:24]
        for img in pngs:
            charts.append({
                "name": img.name,
                "url": f"/charts/{img.name}",
                "mtime": datetime.fromtimestamp(img.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            })

    # Active cooldowns
    raw_cd = (scan_state.get("symbol_cooldowns") or {})
    active_cooldowns = [
        {"symbol": k, "remaining_min": max(1, int((float(v) - now) // 60))}
        for k, v in raw_cd.items()
        if float(v) > now
    ]

    halt_until = float(loss_state.get("halt_until", 0) or 0)
    loss_halt_rem = max(0, int(halt_until - now)) if halt_until > now else 0

    return {
        "server_time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "mode": "TESTNET" if getattr(cfg, "USE_TESTNET", False) else "LIVE SPOT",
        "config": {
            "max_trade_usdt": getattr(cfg, "MAX_TRADE_USDT", 20),
            "max_sl_pct": getattr(cfg, "SCANNER_MAX_STOP_LOSS_PCT", 5.0),
            "max_daily_loss_pct": getattr(cfg, "MAX_DAILY_LOSS", 6.0),
            "max_total_dd_pct": getattr(cfg, "MAX_TOTAL_DRAWDOWN_PCT", 12.0),
            "min_score": getattr(cfg, "MIN_SCORE_TO_TRADE", 75),
            "high_score": getattr(cfg, "HIGH_SCORE_THRESHOLD", 85),
        },
        "account": {
            "daily_start_bal": loss_state.get("daily_start_bal"),
            "peak_bal": loss_state.get("peak_bal"),
            "consecutive_losses": int(loss_state.get("losses", 0) or 0),
            "max_consecutive_losses": int(getattr(cfg, "MAX_CONSECUTIVE_LOSSES", 2)),
            "loss_halt_remaining_sec": loss_halt_rem,
        },
        "stats": {
            "total_trades": total_trades,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(win_rate, 1),
            "net_pnl_usdt": round(net_pnl, 2),
            "avg_win_usdt": round(avg_win, 2),
            "avg_loss_usdt": round(avg_loss, 2),
            "expectancy_usdt": round(expectancy, 2),
            "profit_factor": round(profit_factor, 2),
        },
        "open_positions": open_positions,
        "equity_curve": equity_curve,
        "trades": list(reversed(trades[-50:])),
        "charts": charts,
        "cooldowns": active_cooldowns,
        "logs": _tail_log(log_file, 40),
    }


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CryptoBot — Live Command Center</title>
<style>
  :root {
    --bg: #080b12;
    --panel: rgba(16, 22, 36, 0.85);
    --border: rgba(255, 255, 255, 0.08);
    --text: #e6edf3;
    --muted: #8b949e;
    --green: #00e676;
    --green-dim: rgba(0, 230, 118, 0.14);
    --red: #ff5252;
    --red-dim: rgba(255, 82, 82, 0.14);
    --cyan: #00e5ff;
    --gold: #ffd740;
    --purple: #b388ff;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: radial-gradient(circle at 15% 0%, #111b2e 0%, var(--bg) 55%);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Inter, sans-serif;
    min-height: 100vh;
    padding: 20px 24px 40px;
  }
  .header {
    display: flex; align-items: center; justify-content: space-between;
    flex-wrap: wrap; gap: 12px;
    padding-bottom: 18px; margin-bottom: 20px;
    border-bottom: 1px solid var(--border);
  }
  .brand { display: flex; align-items: center; gap: 12px; }
  .pulse {
    width: 11px; height: 11px; border-radius: 50%;
    background: var(--green);
    box-shadow: 0 0 12px var(--green);
    animation: pulse 2s infinite;
  }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
  .brand h1 { font-size: 1.35rem; font-weight: 700; letter-spacing: 0.4px; }
  .badge {
    padding: 4px 10px; border-radius: 999px; font-size: 0.74rem;
    font-weight: 700; letter-spacing: 0.6px; text-transform: uppercase;
  }
  .badge-live { background: var(--green-dim); color: var(--green); border: 1px solid rgba(0,230,118,0.35); }
  .badge-test { background: rgba(255,215,64,0.14); color: var(--gold); border: 1px solid rgba(255,215,64,0.35); }
  .meta-right { color: var(--muted); font-size: 0.84rem; display: flex; align-items: center; gap: 14px; }
  .grid-kpi {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
    gap: 14px; margin-bottom: 20px;
  }
  .card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 16px 18px;
    backdrop-filter: blur(12px);
    box-shadow: 0 8px 24px rgba(0,0,0,0.35);
  }
  .kpi-title { color: var(--muted); font-size: 0.77rem; text-transform: uppercase; letter-spacing: 0.7px; margin-bottom: 6px; }
  .kpi-val { font-size: 1.65rem; font-weight: 800; line-height: 1.15; }
  .kpi-sub { color: var(--muted); font-size: 0.80rem; margin-top: 6px; }
  .pos { color: var(--green); }
  .neg { color: var(--red); }
  .cyan { color: var(--cyan); }
  .gold { color: var(--gold); }
  .grid-2 {
    display: grid; grid-template-columns: 1.15fr 1fr;
    gap: 16px; margin-bottom: 20px;
  }
  @media (max-width: 980px) { .grid-2 { grid-template-columns: 1fr; } }
  .section-title {
    font-size: 0.92rem; font-weight: 700; text-transform: uppercase;
    letter-spacing: 0.7px; color: var(--muted); margin-bottom: 14px;
    display: flex; align-items: center; justify-content: space-between;
  }
  .pos-box {
    border: 1px solid rgba(0,229,255,0.25);
    background: rgba(0, 229, 255, 0.04);
    border-radius: 12px; padding: 16px;
  }
  .pos-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .pos-sym { font-size: 1.3rem; font-weight: 800; color: var(--cyan); }
  .pos-stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 14px; font-size: 0.85rem; }
  .pos-stats div span { display: block; color: var(--muted); font-size: 0.74rem; }
  .gauge-track {
    position: relative; height: 12px; border-radius: 6px;
    background: linear-gradient(90deg, rgba(255,82,82,0.45) 0%, rgba(255,215,64,0.35) 50%, rgba(0,230,118,0.5) 100%);
    margin: 18px 0 8px;
  }
  .gauge-fill {
    position: absolute; top: -4px; width: 10px; height: 20px;
    border-radius: 4px; background: #fff;
    box-shadow: 0 0 10px var(--cyan);
    transform: translateX(-50%);
  }
  .gauge-entry {
    position: absolute; top: -3px; width: 2px; height: 18px;
    background: var(--gold);
  }
  .gauge-labels { display: flex; justify-content: space-between; font-size: 0.76rem; color: var(--muted); }
  table { width: 100%; border-collapse: collapse; font-size: 0.84rem; }
  th { text-align: left; color: var(--muted); font-weight: 600; padding: 10px 8px; border-bottom: 1px solid var(--border); font-size: 0.76rem; text-transform: uppercase; }
  td { padding: 10px 8px; border-bottom: 1px solid rgba(255,255,255,0.04); font-variant-numeric: tabular-nums; }
  tr:hover td { background: rgba(255,255,255,0.02); }
  .pill { padding: 3px 8px; border-radius: 6px; font-size: 0.72rem; font-weight: 700; }
  .pill-win { background: var(--green-dim); color: var(--green); }
  .pill-loss { background: var(--red-dim); color: var(--red); }
  .charts-grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
    gap: 12px;
  }
  .chart-thumb {
    border: 1px solid var(--border); border-radius: 10px; overflow: hidden;
    cursor: pointer; transition: transform 0.15s, border-color 0.15s;
    background: #05070c;
  }
  .chart-thumb:hover { transform: translateY(-2px); border-color: var(--cyan); }
  .chart-thumb img { width: 100%; height: 145px; object-fit: cover; display: block; }
  .chart-cap { padding: 7px 10px; font-size: 0.74rem; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .log-box {
    background: #05070b; border: 1px solid var(--border); border-radius: 10px;
    padding: 12px 14px; font-family: 'Cascadia Code', Consolas, monospace;
    font-size: 0.76rem; line-height: 1.55; max-height: 280px; overflow-y: auto; color: #b0bec5;
  }
  .modal {
    display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.88);
    z-index: 1000; align-items: center; justify-content: center; padding: 24px;
  }
  .modal.open { display: flex; }
  .modal img { max-width: 95vw; max-height: 90vh; border-radius: 10px; border: 1px solid var(--border); }
</style>
</head>
<body>
  <div class="header">
    <div class="brand">
      <div class="pulse"></div>
      <h1>CRYPTOBOT // COMMAND CENTER</h1>
      <span id="mode-badge" class="badge badge-live">LIVE SPOT</span>
    </div>
    <div class="meta-right">
      <span id="cfg-pill"></span>
      <span id="clock">Loading...</span>
    </div>
  </div>

  <div class="grid-kpi">
    <div class="card">
      <div class="kpi-title">Account Benchmark / Peak</div>
      <div class="kpi-val cyan" id="kpi-peak">$--</div>
      <div class="kpi-sub" id="kpi-daily">Daily Start: $--</div>
    </div>
    <div class="card">
      <div class="kpi-title">Total Realized PnL</div>
      <div class="kpi-val" id="kpi-pnl">$0.00</div>
      <div class="kpi-sub" id="kpi-exp">Expectancy: $0.00 / trade</div>
    </div>
    <div class="card">
      <div class="kpi-title">Win Rate & Record</div>
      <div class="kpi-val" id="kpi-wr">0.0%</div>
      <div class="kpi-sub" id="kpi-rec">0W — 0L (0 trades)</div>
    </div>
    <div class="card">
      <div class="kpi-title">Profit Factor</div>
      <div class="kpi-val gold" id="kpi-pf">0.00</div>
      <div class="kpi-sub" id="kpi-avg">Avg W: $0.00 | Avg L: $0.00</div>
    </div>
    <div class="card">
      <div class="kpi-title">Risk Shield & Guards</div>
      <div class="kpi-val pos" id="kpi-shield">ARMED</div>
      <div class="kpi-sub" id="kpi-consec">Loss Streak: 0/2 | Cooldowns: 0</div>
    </div>
  </div>

  <div class="grid-2">
    <div class="card">
      <div class="section-title">
        <span>Active Position Monitor</span>
        <span id="open-count" style="color:var(--cyan)">0 / 1 Open</span>
      </div>
      <div id="active-pos-container"></div>
    </div>
    <div class="card">
      <div class="section-title">
        <span>Cumulative Realized Profit Curve (USDT)</span>
      </div>
      <div id="curve-container" style="height:210px; width:100%;"></div>
    </div>
  </div>

  <div class="card" style="margin-bottom:20px;">
    <div class="section-title"><span>Closed Trade Ledger</span></div>
    <div style="overflow-x:auto;">
      <table>
        <thead>
          <tr>
            <th>Time (UTC)</th><th>Pair</th><th>Entry → Exit</th><th>Net PnL</th>
            <th>Score</th><th>MFE / MAE</th><th>Duration</th><th>Exit Reason</th>
          </tr>
        </thead>
        <tbody id="trades-tbody"></tbody>
      </table>
    </div>
  </div>

  <div class="card" style="margin-bottom:20px;">
    <div class="section-title"><span>Entry & Exit Chart Snapshots (Click to Enlarge)</span></div>
    <div id="charts-container" class="charts-grid"></div>
  </div>

  <div class="card">
    <div class="section-title"><span>Live Scanner & Engine Log Tail</span></div>
    <div id="log-container" class="log-box"></div>
  </div>

  <div id="modal" class="modal" onclick="this.classList.remove('open')">
    <img id="modal-img" src="" alt="Trade Chart">
  </div>

<script>
function fmtPx(px) {
  const v = Number(px || 0);
  const a = Math.abs(v);
  if (a === 0) return '0.0000';
  if (a >= 1.0) return v.toFixed(4).replace(/\.?0+$/, '');
  if (a >= 0.01) return v.toFixed(6).replace(/\.?0+$/, '');
  return v.toFixed(8).replace(/\.?0+$/, '');
}

function fmtDur(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  if (sec < 60) return sec + 's';
  if (sec < 3600) return Math.floor(sec/60) + 'm ' + (sec%60) + 's';
  return Math.floor(sec/3600) + 'h ' + Math.floor((sec%3600)/60) + 'm';
}

function renderCurve(points) {
  const el = document.getElementById('curve-container');
  if (!points || points.length < 2) {
    el.innerHTML = '<div style="height:100%;display:flex;align-items:center;justify-content:center;color:var(--muted);">Waiting for closed trades...</div>';
    return;
  }
  const w = el.clientWidth || 460, h = 200, pad = 28;
  const vals = points.map(p => p.cum_pnl);
  const minV = Math.min(0, ...vals), maxV = Math.max(0.5, ...vals);
  const span = (maxV - minV) || 1;
  const coords = points.map((p, i) => {
    const x = pad + (i / (points.length - 1)) * (w - pad * 2);
    const y = h - pad - ((p.cum_pnl - minV) / span) * (h - pad * 2);
    return [x, y, p];
  });
  const poly = coords.map(c => `${c[0].toFixed(1)},${c[1].toFixed(1)}`).join(' ');
  const lastVal = vals[vals.length - 1];
  const stroke = lastVal >= 0 ? '#00e676' : '#ff5252';
  const zeroY = h - pad - ((0 - minV) / span) * (h - pad * 2);
  const dots = coords.map(c => `<circle cx="${c[0].toFixed(1)}" cy="${c[1].toFixed(1)}" r="3.5" fill="${stroke}"><title>${c[2].label}: ${c[2].cum_pnl >= 0 ? '+' : ''}$${c[2].cum_pnl.toFixed(2)}</title></circle>`).join('');
  el.innerHTML = `
    <svg width="100%" height="${h}" viewBox="0 0 ${w} ${h}">
      <line x1="${pad}" y1="${zeroY}" x2="${w-pad}" y2="${zeroY}" stroke="rgba(255,255,255,0.12)" stroke-dasharray="4 4"/>
      <polyline fill="none" stroke="${stroke}" stroke-width="2.5" points="${poly}"/>
      ${dots}
    </svg>`;
}

async function refreshState() {
  try {
    const res = await fetch('/api/state');
    const d = await res.json();
    document.getElementById('clock').textContent = d.server_time_utc;
    const mb = document.getElementById('mode-badge');
    mb.textContent = d.mode;
    mb.className = 'badge ' + (d.mode === 'LIVE SPOT' ? 'badge-live' : 'badge-test');
    document.getElementById('cfg-pill').textContent = `Max Trade: $${d.config.max_trade_usdt} | Max SL: ${d.config.max_sl_pct}% | Daily SL: ${d.config.max_daily_loss_pct}% | Peak DD: ${d.config.max_total_dd_pct}%`;

    const peak = d.account.peak_bal ? `$${Number(d.account.peak_bal).toFixed(2)}` : 'Tracking';
    const dstart = d.account.daily_start_bal ? `$${Number(d.account.daily_start_bal).toFixed(2)}` : 'Tracking';
    document.getElementById('kpi-peak').textContent = peak;
    document.getElementById('kpi-daily').textContent = `Daily Start Equity: ${dstart}`;

    const net = d.stats.net_pnl_usdt;
    const pnlEl = document.getElementById('kpi-pnl');
    pnlEl.textContent = (net >= 0 ? '+$' : '-$') + Math.abs(net).toFixed(2) + ' USDT';
    pnlEl.className = 'kpi-val ' + (net >= 0 ? 'pos' : 'neg');
    const exp = d.stats.expectancy_usdt;
    document.getElementById('kpi-exp').textContent = `Expectancy: ${exp >= 0 ? '+' : ''}$${exp.toFixed(2)} / trade`;

    const wrEl = document.getElementById('kpi-wr');
    wrEl.textContent = d.stats.win_rate_pct.toFixed(1) + '%';
    wrEl.className = 'kpi-val ' + (d.stats.win_rate_pct >= 60 ? 'pos' : 'gold');
    document.getElementById('kpi-rec').textContent = `${d.stats.wins}W — ${d.stats.losses}L (${d.stats.total_trades} closed)`;

    document.getElementById('kpi-pf').textContent = d.stats.profit_factor.toFixed(2);
    document.getElementById('kpi-avg').textContent = `Avg W: +$${d.stats.avg_win_usdt.toFixed(2)} | Avg L: $${d.stats.avg_loss_usdt.toFixed(2)}`;

    const shEl = document.getElementById('kpi-shield');
    if (d.account.loss_halt_remaining_sec > 0) {
      shEl.textContent = `PAUSED (${fmtDur(d.account.loss_halt_remaining_sec)})`;
      shEl.className = 'kpi-val gold';
    } else {
      shEl.textContent = 'PROTECTED';
      shEl.className = 'kpi-val pos';
    }
    document.getElementById('kpi-consec').textContent = `Loss Streak: ${d.account.consecutive_losses}/${d.account.max_consecutive_losses} | Cooldowns: ${d.cooldowns.length}`;

    // Active Position
    const posCont = document.getElementById('active-pos-container');
    document.getElementById('open-count').textContent = `${d.open_positions.length} / 1 Open`;
    if (d.open_positions.length === 0) {
      posCont.innerHTML = `<div style="padding:36px 16px;text-align:center;color:var(--muted);border:1px dashed var(--border);border-radius:12px;">
        🔍 Scanner active — waiting for next high-probability capitulation setup (Score &ge; ${d.config.min_score})
      </div>`;
    } else {
      posCont.innerHTML = d.open_positions.map(p => {
        const pCls = p.unrealized_pct >= 0 ? 'pos' : 'neg';
        const sign = p.unrealized_pct >= 0 ? '+' : '';
        return `<div class="pos-box">
          <div class="pos-header">
            <div><span class="pos-sym">${p.symbol}</span> <span class="badge badge-test">${p.timeframe}</span></div>
            <div class="${pCls}" style="font-size:1.25rem;font-weight:800;">${sign}${p.unrealized_pct.toFixed(2)}% (${sign}$${p.unrealized_usdt.toFixed(2)})</div>
          </div>
          <div class="pos-stats">
            <div><span>Entry Price</span><b>${fmtPx(p.entry_price)}</b></div>
            <div><span>Live Price</span><b class="cyan">${fmtPx(p.live_price)}</b></div>
            <div><span>Position Size</span><b>$${p.notional_usdt.toFixed(2)} (${p.quantity})</b></div>
            <div><span>Stop-Loss (OCO)</span><b class="neg">${fmtPx(p.stop_loss)}</b></div>
            <div><span>Take-Profit (OCO)</span><b class="pos">${fmtPx(p.take_profit)}</b></div>
            <div><span>Time Held / Score</span><b>${fmtDur(p.held_sec)} (Score ${p.score})</b></div>
          </div>
          <div class="gauge-track">
            <div class="gauge-entry" style="left:${p.entry_marker_pct}%;" title="Entry"></div>
            <div class="gauge-fill" style="left:${p.progress_pct}%;" title="Live Price"></div>
          </div>
          <div class="gauge-labels">
            <span>SL: ${fmtPx(p.stop_loss)}</span>
            <span>Entry: ${fmtPx(p.entry_price)}</span>
            <span>TP: ${fmtPx(p.take_profit)}</span>
          </div>
        </div>`;
      }).join('');
    }

    renderCurve(d.equity_curve);

    // Trades Table
    const tbody = document.getElementById('trades-tbody');
    if (d.trades.length === 0) {
      tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:20px;">No closed trades logged in trade_log.json yet.</td></tr>';
    } else {
      tbody.innerHTML = d.trades.map(t => {
        const netU = Number(t.net_pnl_usdt !== undefined ? t.net_pnl_usdt : t.pnl_usdt || 0);
        const netP = Number(t.net_pnl_pct !== undefined ? t.net_pnl_pct : t.pnl_pct || 0);
        const cls = netU >= 0 ? 'pos' : 'neg';
        const pillCls = netU >= 0 ? 'pill-win' : 'pill-loss';
        const ts = (t.timestamp || '').replace('T', ' ').slice(0, 19);
        return `<tr>
          <td style="color:var(--muted)">${ts}</td>
          <td><b>${t.symbol}</b></td>
          <td>${fmtPx(t.entry_price)} &rarr; ${fmtPx(t.exit_price)}</td>
          <td class="${cls}"><b>${netP >= 0 ? '+' : ''}${netP.toFixed(2)}% (${netU >= 0 ? '+$' : '-$'}${Math.abs(netU).toFixed(2)})</b></td>
          <td>${Number(t.final_score || 0).toFixed(0)}</td>
          <td><span class="pos">+${Number(t.mfe_pct || 0).toFixed(2)}%</span> / <span class="neg">${Number(t.mae_pct || 0).toFixed(2)}%</span></td>
          <td>${fmtDur(t.hold_duration_sec)}</td>
          <td><span class="pill ${pillCls}">${t.reason || 'EXIT'}</span></td>
        </tr>`;
      }).join('');
    }

    // Charts Gallery
    const chCont = document.getElementById('charts-container');
    if (d.charts.length === 0) {
      chCont.innerHTML = '<div style="color:var(--muted);font-size:0.85rem;">No chart snapshots found in charts/ directory yet.</div>';
    } else {
      chCont.innerHTML = d.charts.map(c => `
        <div class="chart-thumb" onclick="document.getElementById('modal-img').src='${c.url}';document.getElementById('modal').classList.add('open');">
          <img src="${c.url}" loading="lazy" alt="${c.name}">
          <div class="chart-cap" title="${c.name}">${c.name}</div>
        </div>
      `).join('');
    }

    // Logs
    const logCont = document.getElementById('log-container');
    logCont.innerHTML = (d.logs || []).map(ln => {
      let col = '#b0bec5';
      if (ln.includes('ERROR') || ln.includes('CRITICAL')) col = '#ff5252';
      else if (ln.includes('WARNING')) col = '#ffd740';
      else if (ln.includes('[BUY]') || ln.includes('TAKE_PROFIT') || ln.includes('[CHART SAVED]')) col = '#00e676';
      else if (ln.includes('[BTC REGIME]')) col = '#00e5ff';
      const esc = ln.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      return `<div style="color:${col}">${esc}</div>`;
    }).join('');
  } catch (e) {
    console.error('Dashboard refresh failed:', e);
  }
}

refreshState();
setInterval(refreshState, 10000);
</script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    base_dir = "."
    auth_password = ""

    def log_message(self, format, *args):
        return  # Keep console clean

    def _is_authorized(self) -> bool:
        expected = self.auth_password or os.getenv("DASHBOARD_PASSWORD", "")
        if not expected:
            return True
        auth_hdr = self.headers.get("Authorization", "")
        if not auth_hdr.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(auth_hdr.split(" ", 1)[1].strip()).decode("utf-8", errors="replace")
            _, pwd = decoded.split(":", 1) if ":" in decoded else ("", decoded)
            return hmac.compare_digest(pwd, expected)
        except Exception:
            return False

    def do_GET(self):
        if not self._is_authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="CryptoBot Command Center"')
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Authentication required")
            return

        path = unquote(self.path.split("?")[0])
        if path in ("/", "/index.html"):
            body = DASHBOARD_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/state":
            state = collect_dashboard_state(self.base_dir)
            body = json.dumps(state).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path.startswith("/charts/"):
            fname = os.path.basename(path)
            img_path = Path(self.base_dir) / "charts" / fname
            if img_path.exists() and img_path.suffix.lower() == ".png":
                data = img_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

        self.send_response(404)
        self.end_headers()


def main():
    parser = argparse.ArgumentParser(description="CryptoBot Live Web Dashboard (Read-Only)")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8050, help="Bind port (default: 8050)")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print("=" * 62)
    print(f"  CRYPTOBOT LIVE COMMAND CENTER — http://localhost:{args.port}")
    print("  Mode: Strictly Read-Only (Safe alongside running bot.py)")
    print("=" * 62)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down dashboard...")
        server.server_close()


if __name__ == "__main__":
    main()
