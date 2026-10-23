"""
scanner.py — Dynamic All-Market Scanner
========================================
Features & Improvements:
  - Scans all tradeable USDT pairs on Binance Spot
  - BTC Market Regime Filter (1h EMA20 check)
  - Fully Auditable Score Math (raw_score, pump_penalty, final_score)
  - Option B Graduated Orderbook Scoring (<0.8 block, 1.0-1.2:+3, 1.2-1.5:+6, 1.5-2.0:+8, >2.0:+10)
  - Tiered 24h Pump Penalty (-5, -10, -20, >20% hard block)
  - Last-closed-candle analysis for Doji & pattern checks
  - Volatility-adjusted breakout quality (Breakout / ATR >= 0.2)
"""
import logging
import time
from dataclasses import dataclass
from typing import List, Optional, Dict
import pandas as pd
import numpy as np

log = logging.getLogger(__name__)

@dataclass
class ScanResult:
    symbol: str
    score: float
    raw_score: float
    pump_penalty: float
    reason: str
    close_price: float
    rsi: float
    volume_ratio: float
    pct_change_24h: float
    atr: float
    breakout_atr_ratio: float
    bid_ask_ratio: float
    last_closed_time: int
    ema_score: float = 0.0
    rsi_score: float = 0.0
    vol_score: float = 0.0
    breakout_score: float = 0.0
    ob_score: float = 0.0
    candidate_type: str = "standard"


class MarketScanner:
    def __init__(self, exchange, cfg):
        self.exchange = exchange
        self.cfg = cfg
        self._btc_regime_cache = None
        self._btc_cache_time = 0

    def check_btc_regime(self) -> str:
        """Returns 'BULLISH' if BTC >= 1h EMA20, otherwise 'BEARISH'."""
        if not getattr(self.cfg, 'ENABLE_BTC_REGIME_FILTER', True):
            return "BULLISH"

        now = time.time()
        ttl = getattr(self.cfg, 'BTC_REGIME_CACHE_SEC', 300)
        if self._btc_regime_cache and (now - self._btc_cache_time < ttl):
            return self._btc_regime_cache

        try:
            interval = getattr(self.cfg, 'BTC_REGIME_INTERVAL', '1h')
            limit = getattr(self.cfg, 'BTC_REGIME_EMA_PERIOD', 20) + 10
            df = self.exchange.get_candles('BTCUSDT', interval=interval, limit=limit)
            if df is None or len(df) < 25:
                log.warning("[BTC REGIME] Could not fetch BTC candles - assuming BULLISH")
                return "BULLISH"

            ema_period = getattr(self.cfg, 'BTC_REGIME_EMA_PERIOD', 20)
            df['ema'] = df['close'].ewm(span=ema_period, adjust=False).mean()

            btc_price = df['close'].iloc[-1]
            ema_val = df['ema'].iloc[-1]

            status = "BULLISH" if btc_price >= ema_val else "BEARISH"
            self._btc_regime_cache = status
            self._btc_cache_time = now

            log.info(f"[BTC REGIME] BTC={btc_price:.0f} EMA{ema_period}={ema_val:.0f} -> {status}")
            return status
        except Exception as e:
            log.warning(f"[BTC REGIME] Check failed: {e} - assuming BULLISH")
            return "BULLISH"

    def scan_market(self, active_symbols: List[str] = None) -> List[ScanResult]:
        """Scan all tradeable USDT pairs and return top candidates ordered by score."""
        active_symbols = set(active_symbols or [])
        start_time = time.time()
        log.info("[SCANNER] Starting full market scan...")

        try:
            all_symbols = self.exchange.get_all_usdt_symbols()
        except Exception as e:
            log.error(f"[SCANNER] Failed to fetch symbol list: {e}")
            return []

        blacklist = set(getattr(self.cfg, 'SNIPE_BLACKLIST', []))
        known_stable_prefixes = {'USD', 'EUR', 'GBP', 'DAI', 'BUSD', 'USDC', 'USDT', 'TUSD', 'FDUSD', 'RLUSD', 'USDS', 'USDP', 'AEUR', 'EURI'}
        candidates = []
        for s in all_symbols:
            if s in blacklist or s in active_symbols:
                continue
            base = s.removesuffix('USDT')
            if base in known_stable_prefixes or base.startswith('USD') or base.endswith('USD'):
                continue
            candidates.append(s)

        log.info(f"[SCANNER] {len(candidates)} tradeable USDT pairs (after blacklist/stablecoin filter)")

        # Step 1: Pre-filter illiquid coins using a single batch API call
        passed_prefilter = []
        min_vol = getattr(self.cfg, 'MIN_SCAN_VOLUME_USDT', 5000000)
        tickers_24h = self.exchange.get_24h_tickers()

        for sym in candidates:
            try:
                t = tickers_24h.get(sym)
                if not t:
                    continue
                vol_24h = float(t.get('quoteVolume', 0))
                if vol_24h < min_vol:
                    continue
                passed_prefilter.append(sym)
            except Exception:
                continue

        log.info(f"[SCANNER] {len(passed_prefilter)} passed volume pre-filter (>= ${min_vol:,.0f} USDT)")

        # Keep the MOST LIQUID pairs when capping. (Previously the cap kept whichever pairs Binance listed first.)
        passed_prefilter.sort(key=lambda s: float((tickers_24h.get(s) or {}).get('quoteVolume', 0) or 0), reverse=True)
        max_scan = getattr(self.cfg, 'MAX_SCAN_CANDIDATES', 60)
        passed_prefilter = passed_prefilter[:max_scan]

        results = []
        for sym in passed_prefilter:
            res = self._analyze_symbol(sym)
            if res and res.score >= getattr(self.cfg, 'MIN_SCORE_TO_TRADE', 60):
                results.append(res)

        results.sort(key=lambda x: x.score, reverse=True)
        top_n = getattr(self.cfg, 'SCANNER_TOP_N', 3)
        final_results = results[:top_n]

        elapsed = time.time() - start_time
        log.info(f"[SCANNER] Scan complete in {elapsed:.1f}s - {len(final_results)} candidates")
        for r in final_results:
            log.info(f"  -> {r.symbol}: Score={r.score:.0f} (Raw={r.raw_score:.0f}, Pen={r.pump_penalty:.0f}) | Price={r.close_price:.6f} | 24h={r.pct_change_24h:+.2f}% - {r.reason}")

        return final_results

    def _analyze_symbol(self, symbol: str) -> Optional[ScanResult]:
        try:
            tf = getattr(self.cfg, 'SCANNER_INTERVAL', '15m')
            limit = getattr(self.cfg, 'SCANNER_CANDLE_LIMIT', 100)
            df = self.exchange.get_candles(symbol, interval=tf, limit=limit)

            if df is None or len(df) < 50:
                return None

            # Current live market price
            curr_market_price = float(df['close'].iloc[-1])

            # IMPORTANT: All indicators and patterns are calculated on CLOSED candles only to eliminate repainting
            df_closed = df.iloc[:-1] if len(df) >= 2 else df
            if len(df_closed) < 40:
                return None

            close = df_closed['close']
            high = df_closed['high']
            low = df_closed['low']
            open_p = df_closed['open']
            vol = df_closed['volume']

            last_close = close.iloc[-1]
            last_open = open_p.iloc[-1]
            last_high = high.iloc[-1]
            last_low = low.iloc[-1]
            last_closed_time = int(df_closed.index[-1].timestamp())

            # 24h Percentage Change calculation
            if len(close) >= 96:
                pct_24h = ((last_close - close.iloc[-96]) / close.iloc[-96]) * 100.0
            else:
                pct_24h = 0.0

            # --- TIERED 24h PUMP PENALTY ---
            parabolic_max = getattr(self.cfg, 'PARABOLIC_PUMP_MAX_PCT', 25.0)
            pump_penalty = 0.0
            if pct_24h > parabolic_max:
                log.info(f"[SCANNER] {symbol} hard-blocked (parabolic 24h pump = {pct_24h:+.1f}% > {parabolic_max:.0f}%)")
                return None
            elif pct_24h > 15.0:
                pump_penalty = -20.0
            elif pct_24h > 10.0:
                pump_penalty = -10.0
            elif pct_24h > 6.0:
                pump_penalty = -5.0

            # Calculate indicators on closed bars
            ema9 = close.ewm(span=9, adjust=False).mean()
            ema21 = close.ewm(span=21, adjust=False).mean()
            ema50 = close.ewm(span=50, adjust=False).mean()

            # --- HARD BLOCK 1: EMA Slope Check (EMA9 must not be falling steeply) ---
            if len(ema9) >= 3:
                ema9_slope = (ema9.iloc[-1] - ema9.iloc[-3]) / ema9.iloc[-3]
                if ema9_slope < -0.001:  # Allow flat, block falling
                    log.info(f"[SCANNER] {symbol} skipped - EMA9 slope is falling ({ema9_slope:.4f})")
                    return None

            # --- HARD BLOCK 2: Doji Filter on last closed candle ---
            closed_range = last_high - last_low
            if closed_range > 0:
                body_size = abs(last_close - last_open)
                body_ratio = body_size / closed_range
                doji_thresh = getattr(self.cfg, 'DOJI_MAX_BODY_RATIO', 0.15)
                if body_ratio < doji_thresh:
                    log.info(f"[SCANNER] {symbol} skipped - doji on last closed candle (body={body_ratio:.0%} < {doji_thresh:.0%})")
                    return None

            # --- HARD BLOCK 3: Orderbook ratio ---
            min_ob = getattr(self.cfg, 'MIN_ORDERBOOK_RATIO', 0.70)
            try:
                ob_ratio = self.exchange.get_orderbook_imbalance(symbol, depth=10)
                if ob_ratio < min_ob:
                    log.info(f"[SCANNER] {symbol} skipped - orderbook ratio ({ob_ratio:.2f} < {min_ob:.2f})")
                    return None
            except Exception:
                ob_ratio = 1.0

            ob_score = 0.0
            if ob_ratio >= 2.0:
                ob_score = 10.0
            elif ob_ratio >= 1.5:
                ob_score = 8.0
            elif ob_ratio >= 1.2:
                ob_score = 6.0
            elif ob_ratio >= 1.0:
                ob_score = 3.0

            # Calculate ATR & Volatility
            tr = pd.concat([
                high - low,
                (high - close.shift()).abs(),
                (low - close.shift()).abs()
            ], axis=1).max(axis=1)
            atr_val = tr.rolling(14).mean().iloc[-1]

            # --- HARD BLOCK 4: Stablecoin & Low-Volatility Filter ---
            min_atr_pct = getattr(self.cfg, 'STABLECOIN_ATR_THRESHOLD', 0.3)
            atr_pct = ((atr_val / last_close) * 100.0) if (last_close > 0 and not np.isnan(atr_val)) else 0.0
            if atr_pct < min_atr_pct:
                log.info(f"[SCANNER] {symbol} skipped - ATR volatility too low ({atr_pct:.3f}% < {min_atr_pct}%)")
                return None

            # RSI Calculation (EWM)
            delta = close.diff()
            gain = delta.clip(lower=0)
            loss = (-delta.clip(upper=0))
            avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
            avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
            rs = avg_gain / avg_loss.replace(0, np.nan)
            rsi = (100 - 100 / (1 + rs)).iloc[-1]

            # Volume Ratio vs 20-candle MA
            vol_ma = vol.rolling(20).mean().iloc[-1]
            vol_ratio = vol.iloc[-1] / vol_ma if vol_ma > 0 else 1.0

            # --- COMPONENT SCORING BREAKDOWN ---
            ema_score = 0.0
            rsi_score = 0.0
            vol_score = 0.0
            breakout_score = 0.0
            reasons = []

            # 1. EMA Alignment
            if ema9.iloc[-1] > ema21.iloc[-1] > ema50.iloc[-1]:
                ema_score = 25.0
                reasons.append("Perfect EMA alignment (9>21>50)")
            elif ema9.iloc[-1] > ema21.iloc[-1]:
                ema_score = 15.0
                reasons.append("EMA9 > EMA21")

            # 2. Smooth Dynamic RSI Scoring Band: reward true breakout momentum (55-68)
            if 55 <= rsi <= 68:
                rsi_score = 25.0
                reasons.append(f"RSI momentum sweet spot ({rsi:.1f})")
            elif 48 <= rsi < 55:
                rsi_score = 15.0
                reasons.append(f"RSI building ({rsi:.1f})")
            elif 68 < rsi <= 73:
                rsi_score = 10.0
                reasons.append(f"RSI strong but extended ({rsi:.1f})")
            elif 73 < rsi <= 75:
                rsi_score = 0.0
                reasons.append(f"RSI approaching overbought ({rsi:.1f})")
            elif rsi > 75:
                rsi_score = -20.0
                reasons.append(f"RSI overbought ({rsi:.1f})")
            else:
                rsi_score = -15.0
                reasons.append(f"RSI weak ({rsi:.1f})")

            # 3. Volume Surge
            if vol_ratio >= 2.0:
                vol_score = 25.0
                reasons.append(f"Huge volume surge ({vol_ratio:.1f}x)")
            elif vol_ratio >= 1.5:
                vol_score = 15.0
                reasons.append(f"Solid volume ({vol_ratio:.1f}x)")
            elif vol_ratio < 1.0:
                vol_score = -10.0
                reasons.append(f"Low volume ({vol_ratio:.1f}x)")

            # 4. Volatility-Adjusted Breakout Quality (20 bars before the breakout candle)
            recent_20_high = high.iloc[-21:-1].max() if len(high) >= 22 else high.max()
            breakout_atr_ratio = 0.0

            if last_close > recent_20_high:
                breakout_dist = last_close - recent_20_high
                breakout_atr_ratio = (breakout_dist / atr_val) if atr_val > 0 else 0.0

                if breakout_atr_ratio >= 0.2:
                    breakout_score = 25.0
                    reasons.append(f"Clean 20-bar breakout (dist={breakout_atr_ratio:.2f} ATR)")
                else:
                    breakout_score = 10.0
                    reasons.append("Minor breakout")

            # 5. Orderbook Boost
            if ob_score > 0:
                reasons.append(f"Orderbook boost +{ob_score:.0f} (ratio={ob_ratio:.2f})")

            # 6. 24h Pump Penalty
            if pump_penalty < 0:
                reasons.append(f"24h pump penalty ({pump_penalty:+.0f})")

            raw_score = ema_score + rsi_score + vol_score + breakout_score + ob_score
            final_score = raw_score + pump_penalty
            reason_str = ", ".join(reasons)

            return ScanResult(
                symbol=symbol,
                score=final_score,
                raw_score=raw_score,
                pump_penalty=pump_penalty,
                reason=reason_str,
                close_price=curr_market_price,
                rsi=float(rsi),
                volume_ratio=float(vol_ratio),
                pct_change_24h=float(pct_24h),
                atr=float(atr_val) if not np.isnan(atr_val) else 0.0,
                breakout_atr_ratio=round(breakout_atr_ratio, 3),
                bid_ask_ratio=round(ob_ratio, 2),
                last_closed_time=last_closed_time,
                ema_score=ema_score,
                rsi_score=rsi_score,
                vol_score=vol_score,
                breakout_score=breakout_score,
                ob_score=ob_score
            )

        except Exception as e:
            log.warning(f"[SCANNER ERROR] Failed to analyze {symbol}: {e}")
            return None
