"""
strategy.py — RSI + EMA + MACD + Volume Strategy
=====================================================
Updates: Quality entry filters, raised exit thresholds.
  - EMA 9/21 instead of 50/200 (much faster signals)
  - RSI sell raised to 75 (let winners run)
  - SELL requires MACD confirmation (no premature exits)
  - Volume multiplier lowered to 1.0 (more trades)
"""
import numpy as np
import pandas as pd
import logging

log = logging.getLogger(__name__)

def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def rsi(series, period):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)

def macd(series, fast=12, slow=26, signal=9):
    """Returns (macd_line, signal_line, histogram)."""
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_ln = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_ln
    return macd_line, signal_ln, histogram

def atr(df, period=14):
    """Average True Range — measures current volatility."""
    high = df['high'].astype(float)
    low = df['low'].astype(float)
    close = df['close'].astype(float)
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()

class RSIEMAStrategy:
    def __init__(self, ema_fast=9, ema_slow=21, rsi_period=14, rsi_buy=35, rsi_sell=75,
                 rsi_confirm_buy=38, rsi_confirm_sell=70, rsi_lookback=6, vol_ma_period=20,
                 vol_multiplier=1.0, macd_fast=12, macd_slow=26, macd_signal=9, atr_period=14):
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.rsi_period = rsi_period
        self.rsi_buy = rsi_buy
        self.rsi_sell = rsi_sell
        self.rsi_confirm_buy = rsi_confirm_buy
        self.rsi_confirm_sell = rsi_confirm_sell
        self.rsi_lookback = rsi_lookback
        self.vol_ma_period = vol_ma_period
        self.vol_multiplier = vol_multiplier
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal
        self.atr_period = atr_period

    @classmethod
    def standard(cls, cfg):
        return cls(
            ema_fast=cfg.EMA_FAST, ema_slow=cfg.EMA_SLOW,
            rsi_period=cfg.RSI_PERIOD, rsi_buy=cfg.RSI_BUY, rsi_sell=cfg.RSI_SELL,
            rsi_confirm_buy=cfg.RSI_CONFIRM_BUY, rsi_confirm_sell=cfg.RSI_CONFIRM_SELL,
            rsi_lookback=cfg.RSI_LOOKBACK, vol_ma_period=cfg.VOLUME_MA_PERIOD,
            vol_multiplier=cfg.VOLUME_MULTIPLIER, macd_fast=cfg.MACD_FAST,
            macd_slow=cfg.MACD_SLOW, macd_signal=cfg.MACD_SIGNAL, atr_period=cfg.ATR_PERIOD
        )

    @classmethod
    def meme(cls, cfg):
        return cls(
            ema_fast=cfg.MEME_EMA_FAST, ema_slow=cfg.MEME_EMA_SLOW,
            rsi_period=cfg.MEME_RSI_PERIOD, rsi_buy=cfg.MEME_RSI_BUY, rsi_sell=cfg.MEME_RSI_SELL,
            rsi_confirm_buy=cfg.MEME_RSI_CONFIRM_BUY, rsi_confirm_sell=cfg.MEME_RSI_CONFIRM_SELL,
            rsi_lookback=cfg.MEME_RSI_LOOKBACK, vol_ma_period=cfg.VOLUME_MA_PERIOD,
            vol_multiplier=cfg.VOLUME_MULTIPLIER, macd_fast=cfg.MACD_FAST,
            macd_slow=cfg.MACD_SLOW, macd_signal=cfg.MACD_SIGNAL, atr_period=cfg.ATR_PERIOD
        )

    def _result(self, signal, close, rsi_val, ema_fast_val, ema_slow_val, macd_hist, volume_ok, atr_val, reason):
        return {
            'signal': signal,
            'close': round(float(close), 6),
            'rsi': round(float(rsi_val), 2),
            'ema_fast': round(float(ema_fast_val), 6),
            'ema_slow': round(float(ema_slow_val), 6),
            'macd_hist': round(float(macd_hist), 6),
            'volume_ok': bool(volume_ok),
            'atr': round(float(atr_val), 6),
            'reason': reason
        }

    def compute(self, df):
        min_bars = max(self.ema_slow, self.rsi_period, self.vol_ma_period, self.macd_slow) + 5
        if len(df) < min_bars:
            return self._result('HOLD', df['close'].iloc[-1], 50, 0, 0, 0, False, 0, f"Insufficient data ({len(df)} < {min_bars})")

        df = df.copy()
        df['ema_fast'] = ema(df['close'], self.ema_fast)
        df['ema_slow'] = ema(df['close'], self.ema_slow)
        df['rsi'] = rsi(df['close'], self.rsi_period)
        df['macd'], df['macd_sig'], df['macd_hist'] = macd(df['close'], self.macd_fast, self.macd_slow, self.macd_signal)
        df['vol_ma'] = df['volume'].rolling(self.vol_ma_period).mean()
        df['atr'] = atr(df, self.atr_period)

        curr = df.iloc[-1]
        prev = df.iloc[-2]

        rsi_val = curr['rsi']
        ema_f = curr['ema_fast']
        ema_s = curr['ema_slow']
        m_hist = curr['macd_hist']
        vol = curr['volume']
        vol_ma = curr['vol_ma']
        atr_val = curr['atr'] if not np.isnan(curr['atr']) else 0.0

        volume_ok = vol >= vol_ma * self.vol_multiplier if not np.isnan(vol_ma) and vol_ma > 0 else True
        uptrend = ema_f > ema_s
        downtrend = ema_f < ema_s

        lookback_slice = df['rsi'].iloc[-self.rsi_lookback:]
        recent_oversold = (lookback_slice <= self.rsi_buy).any()
        rsi_recovering = recent_oversold and rsi_val > self.rsi_confirm_buy
        macd_crossed_up = prev['macd_hist'] <= 0 < m_hist

        buy_signal = uptrend and (rsi_recovering or macd_crossed_up) and volume_ok
        recent_overbought = (lookback_slice >= self.rsi_sell).any()
        rsi_dropping = recent_overbought and rsi_val < self.rsi_confirm_sell
        macd_crossed_down = prev['macd_hist'] >= 0 > m_hist
        sell_signal = downtrend and (rsi_dropping or macd_crossed_down)

        if buy_signal:
            reason = "Uptrend + RSI/MACD recovery + Volume" if (rsi_recovering and macd_crossed_up) else ("Uptrend + RSI recovery" if rsi_recovering else "Uptrend + MACD cross")
            return self._result('BUY', curr['close'], rsi_val, ema_f, ema_s, m_hist, volume_ok, atr_val, reason)
        elif sell_signal:
            return self._result('SELL', curr['close'], rsi_val, ema_f, ema_s, m_hist, volume_ok, atr_val, "Downtrend + Exit signal")
        else:
            return self._result('HOLD', curr['close'], rsi_val, ema_f, ema_s, m_hist, volume_ok, atr_val, "No setup")
