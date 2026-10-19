"""
chart_logger.py — Automated Trade Chart Snapshot Logger
=======================================================
Generates and saves candlestick charts with Technical Indicators (EMA9, EMA21, EMA50, 20-bar High, RSI, MACD)
and Trade Levels (Entry, Exit, SL, TP) upon trade execution/closure.
"""
import os
import logging
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)

try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    HAS_MATPLOTLIB = True
except Exception as e:
    HAS_MATPLOTLIB = False
    log.warning(f"[CHART LOGGER] Matplotlib unavailable: {e}")

def _calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    close = df['close']
    high = df['high']
    df['ema9'] = close.ewm(span=9, adjust=False).mean()
    df['ema21'] = close.ewm(span=21, adjust=False).mean()
    df['ema50'] = close.ewm(span=50, adjust=False).mean()
    df['high20'] = high.shift(1).rolling(window=20).max()

    # F-21: Use EWM-based RSI to match scanner and strategy calculations
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta.clip(upper=0))
    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df['rsi'] = 100 - 100 / (1 + rs)

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df['macd'] = ema12 - ema26
    df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['macd_hist'] = df['macd'] - df['macd_signal']
    return df

def save_trade_chart(symbol: str, df: pd.DataFrame, entry_price: float, exit_price: float,
                     pnl_pct: float, pnl_usdt: float = 0.0, exit_reason: str = "",
                     stop_loss: Optional[float] = None, take_profit: Optional[float] = None,
                     output_dir: str = "charts") -> Optional[str]:
    if not HAS_MATPLOTLIB:
        log.warning("[CHART LOGGER] Skipped chart logging (Matplotlib not installed)")
        return None

    try:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        df_ind = _calculate_indicators(df)
        
        plt.style.use('dark_background')
        fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(12, 8), sharex=True,
                                            gridspec_kw={'height_ratios': [3, 1, 1]})

        title_color = '#00FF7F' if pnl_pct >= 0 else '#FF4500'
        fig.suptitle(f"{symbol} Trade Result: {pnl_pct:+.2f}% (${pnl_usdt:+.4f}) | Exit: {exit_reason}",
                     color=title_color, fontsize=14, fontweight='bold')

        # Panel 1: Price, EMAs, 20-bar High, SL, TP, Entry, Exit
        ax1.plot(df_ind.index, df_ind['close'], label='Price', color='white', linewidth=1.5)
        ax1.plot(df_ind.index, df_ind['ema9'], label='EMA9', color='#00FFFF', linewidth=1)
        ax1.plot(df_ind.index, df_ind['ema21'], label='EMA21', color='#FF00FF', linewidth=1)
        ax1.plot(df_ind.index, df_ind['ema50'], label='EMA50', color='#FFA500', linewidth=1, linestyle=':')
        ax1.plot(df_ind.index, df_ind['high20'], label='20-bar High', color='#ADFF2F', linewidth=1, linestyle='--')

        ax1.axhline(y=entry_price, color='#FFD700', linestyle='--', label=f'Entry: {entry_price:.4f}')
        ax1.axhline(y=exit_price, color='#00BFFF', linestyle=':', label=f'Exit: {exit_price:.4f}')

        if stop_loss:
            ax1.axhline(y=stop_loss, color='#FF3333', linestyle='-.', label=f'SL: {stop_loss:.4f}')
        if take_profit:
            ax1.axhline(y=take_profit, color='#33FF33', linestyle='-.', label=f'TP: {take_profit:.4f}')

        ax1.set_ylabel('Price (USDT)')
        ax1.legend(loc='upper left', fontsize=8, framealpha=0.3)
        ax1.grid(True, alpha=0.2)

        # Panel 2: RSI
        ax2.plot(df_ind.index, df_ind['rsi'], color='#FF8C00', label='RSI(14)')
        ax2.axhline(y=70, color='red', linestyle='--', alpha=0.5)
        ax2.axhline(y=30, color='green', linestyle='--', alpha=0.5)
        ax2.set_ylabel('RSI')
        ax2.set_ylim(0, 100)
        ax2.legend(loc='upper left', fontsize=8, framealpha=0.3)
        ax2.grid(True, alpha=0.2)

        # Panel 3: MACD
        ax3.plot(df_ind.index, df_ind['macd'], color='#00FFFF', label='MACD')
        ax3.plot(df_ind.index, df_ind['macd_signal'], color='#FF00FF', label='Signal')
        colors = ['#00FF00' if v >= 0 else '#FF0000' for v in df_ind['macd_hist']]
        ax3.bar(df_ind.index, df_ind['macd_hist'], color=colors, alpha=0.5, width=0.005)
        ax3.set_ylabel('MACD')
        ax3.legend(loc='upper left', fontsize=8, framealpha=0.3)
        ax3.grid(True, alpha=0.2)

        plt.xticks(rotation=30)
        plt.tight_layout()

        ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{symbol}_{ts_str}_{pnl_pct:+.2f}pct.png"
        filepath = os.path.join(output_dir, filename)
        plt.savefig(filepath, dpi=120)
        plt.close(fig)

        log.info(f"[CHART SAVED] {filepath}")
        return filepath
    except Exception as e:
        log.error(f"[CHART LOGGER ERROR] Failed to save chart for {symbol}: {e}")
        return None
