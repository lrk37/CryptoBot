"""
risk_manager.py — Shared Risk Manager
==========================================
CryptoBot Overview:
  - Concurrent exit safety locks (race condition protection)
  - Safe startup verification that preserves positions on API failure
  - Trade logging with MFE, MAE, auditable score math (raw_score, pump_penalty, final_score), and fee/slippage tracking
  - Daily circuit breaker & consecutive loss protection
  - ATR stop losses, fee-aware trailing stops, dynamic break-even
  - Smart momentum-aware 8h hold timeout
"""
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, Optional, List, Set

log = logging.getLogger(__name__)

@dataclass
class Position:
    symbol: str
    entry_price: float
    quantity: float
    stop_loss: float
    take_profit: float
    trailing: bool = False
    trailing_offset: float = 1.0
    highest_price: float = 0.0
    lowest_price: float = 0.0
    kind: str = "scanner"
    open_time: float = field(default_factory=time.time)
    entry_reason: str = ""
    entry_score: float = 0.0
    min_be_price: float = 0.0
    details: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.highest_price == 0.0:
            self.highest_price = self.entry_price
        if self.lowest_price == 0.0:
            self.lowest_price = self.entry_price

    def pnl_pct(self, current_price: float) -> float:
        if self.entry_price <= 0:
            return 0.0
        return ((current_price - self.entry_price) / self.entry_price) * 100.0

    def pnl_usdt(self, current_price: float) -> float:
        return (current_price - self.entry_price) * self.quantity

    def mfe_pct(self) -> float:
        """Max Favorable Excursion percentage."""
        if self.entry_price <= 0:
            return 0.0
        return ((self.highest_price - self.entry_price) / self.entry_price) * 100.0

    def mae_pct(self) -> float:
        """Max Adverse Excursion percentage."""
        if self.entry_price <= 0:
            return 0.0
        return ((self.lowest_price - self.entry_price) / self.entry_price) * 100.0


class RiskManager:
    """Thread-safe risk manager shared across all trading threads."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._positions: Dict[str, Position] = {}
        self._daily_start_bal = None
        self._today = date.today()
        self._halted = False
        self._exiting_symbols: Set[str] = set()
        self.exchange = None
        self._consecutive_losses = 0
        self._loss_halt_until = 0
        self._trade_log: List[dict] = []

        self._load_trade_log()
        self._load_loss_state()
        self._load_positions()

    def set_exchange(self, exchange):
        """Set exchange instance reference for fetching candle data."""
        self.exchange = exchange

    def validate_positions(self, exchange):
        """Validate persisted positions against actual Binance holdings."""
        cleaned = 0
        with self._lock:
            stale = []
            for symbol, pos in list(self._positions.items()):
                try:
                    if not exchange.verify_asset_holding(symbol, pos.quantity):
                        log.warning(f"[CLEANUP] Stale position found: {symbol} (expected {pos.quantity}, not held) - removing")
                        stale.append(symbol)
                except Exception as e:
                    log.error(f"[CLEANUP ERROR] Failed to verify holdings for {symbol}: {e}. Keeping position.")
                    continue
            for sym in stale:
                del self._positions[sym]
                cleaned += 1
            if cleaned > 0:
                self._save_positions()
                log.info(f"[CLEANUP] Removed {cleaned} stale position(s)")

    def start_exit(self, symbol: str) -> bool:
        """Mark a symbol as in-progress for exit."""
        with self._lock:
            if symbol in self._exiting_symbols or symbol not in self._positions:
                return False
            self._exiting_symbols.add(symbol)
            return True

    def complete_exit(self, symbol: str):
        """Release exit lock on a symbol."""
        with self._lock:
            self._exiting_symbols.discard(symbol)

    def _risk_params(self, kind: str, entry_price: float, atr_value: float = 0.0):
        # F-04: Read kind-specific ATR flag instead of generic USE_ATR_STOPS
        if kind == 'scanner':
            use_atr = getattr(self.cfg, 'SCANNER_USE_ATR_STOPS', True)
            sl_pct = getattr(self.cfg, 'SCANNER_STOP_LOSS_PCT', 2.5)
            tp_pct = getattr(self.cfg, 'SCANNER_TAKE_PROFIT_PCT', 5.0)
            trail = getattr(self.cfg, 'SCANNER_TRAILING_STOP', True)
            trail_off = getattr(self.cfg, 'SCANNER_TRAILING_OFFSET', 1.5)
            atr_sl_mult = getattr(self.cfg, 'SCANNER_ATR_SL_MULTIPLIER', 3.0)
            atr_tp_mult = getattr(self.cfg, 'SCANNER_ATR_TP_MULTIPLIER', 4.0)
            max_sl_pct = getattr(self.cfg, 'SCANNER_MAX_STOP_LOSS_PCT', 3.5)
        elif kind == 'meme':
            use_atr = getattr(self.cfg, 'MEME_USE_ATR_STOPS', True)
            sl_pct = getattr(self.cfg, 'MEME_STOP_LOSS_PCT', 4.0)
            tp_pct = getattr(self.cfg, 'MEME_TAKE_PROFIT_PCT', 12.0)
            trail = getattr(self.cfg, 'MEME_TRAILING_STOP', True)
            trail_off = getattr(self.cfg, 'MEME_TRAILING_OFFSET', 2.5)
            atr_sl_mult = getattr(self.cfg, 'MEME_ATR_SL_MULTIPLIER', 4.0)
            atr_tp_mult = getattr(self.cfg, 'MEME_ATR_TP_MULTIPLIER', 8.0)
            max_sl_pct = getattr(self.cfg, 'MEME_STOP_LOSS_PCT', 4.0)
        else:
            use_atr = getattr(self.cfg, 'USE_ATR_STOPS', True)
            sl_pct = getattr(self.cfg, 'STOP_LOSS_PCT', 2.5)
            tp_pct = getattr(self.cfg, 'TAKE_PROFIT_PCT', 5.0)
            trail = getattr(self.cfg, 'TRAILING_STOP', True)
            trail_off = getattr(self.cfg, 'TRAILING_OFFSET', 1.0)
            atr_sl_mult = getattr(self.cfg, 'ATR_SL_MULTIPLIER', 3.0)
            atr_tp_mult = getattr(self.cfg, 'ATR_TP_MULTIPLIER', 6.0)
            max_sl_pct = getattr(self.cfg, 'STOP_LOSS_PCT', 2.5)

        if use_atr and atr_value > 0:
            sl_dist = atr_value * atr_sl_mult
            tp_dist = atr_value * atr_tp_mult
            sl = entry_price - sl_dist
            tp = entry_price + tp_dist
            # F-05: Floor — SL must be at least MIN_SL_PCT away (use max to keep SL closer to entry)
            min_sl_pct = getattr(self.cfg, 'MIN_SL_PCT', 1.5)
            min_sl_price = entry_price * (1 - min_sl_pct / 100.0)
            sl = max(sl, min_sl_price)
            # F-06: Cap — SL must not be wider than max_sl_pct
            max_sl_price = entry_price * (1 - max_sl_pct / 100.0)
            sl = max(sl, max_sl_price)
        else:
            sl = entry_price * (1 - sl_pct / 100.0)
            tp = entry_price * (1 + tp_pct / 100.0)

        return sl, tp, trail, trail_off

    def open_position(self, symbol: str, entry_price: float, quantity: float,
                      kind: str = "standard", atr_value: float = 0.0,
                      entry_reason: str = "", entry_score: float = 0.0,
                      details: dict = None) -> Optional[Position]:
        with self._lock:
            if symbol in self._positions:
                log.warning(f"Position already open for {symbol}, skipping.")
                return None
            if len(self._positions) >= getattr(self.cfg, 'MAX_OPEN_TRADES', 1):
                log.warning(f"Max open trades ({getattr(self.cfg, 'MAX_OPEN_TRADES', 1)}) reached.")
                return None

            sl, tp, trail, trail_off = self._risk_params(kind, entry_price, atr_value)
            pos = Position(
                symbol=symbol, entry_price=entry_price, quantity=quantity,
                stop_loss=sl, take_profit=tp, trailing=trail, trailing_offset=trail_off,
                kind=kind, open_time=time.time(), entry_reason=entry_reason, entry_score=entry_score,
                details=details or {}
            )
            self._positions[symbol] = pos
            self._save_positions()
            log.info(f"[OPENED] {symbol} ({kind}) | entry={entry_price:.6f} qty={quantity} | SL={sl:.6f} TP={tp:.6f} Score={entry_score:.0f} Reason: {entry_reason}")
            return pos

    def close_position(self, symbol: str, exit_price: float, exit_reason: str):
        with self._lock:
            pos = self._positions.pop(symbol, None)
            self._save_positions()
            self._exiting_symbols.discard(symbol)
            if pos and exit_price > 0:
                pnl = pos.pnl_pct(exit_price)
                # F-20: Account for round-trip fees when determining if trade is a net loss
                fees_pct = getattr(self.cfg, 'ROUND_TRIP_FEE_PCT', 0.2)
                is_loss = pnl < fees_pct or 'SL' in exit_reason or 'Stop-Loss' in exit_reason
                if is_loss:
                    self._consecutive_losses += 1
                    max_consec = getattr(self.cfg, 'MAX_CONSECUTIVE_LOSSES', 2)
                    if self._consecutive_losses >= max_consec:
                        cooldown = getattr(self.cfg, 'CONSECUTIVE_LOSS_COOLDOWN_SEC', 1800)
                        self._loss_halt_until = time.time() + cooldown
                        log.error(f"[CONSECUTIVE LOSSES] {self._consecutive_losses} losses in a row - pausing {cooldown}s")
                else:
                    self._consecutive_losses = 0
                self._save_loss_state()
                self._log_trade(pos, exit_price, exit_reason)

    def get_position(self, symbol: str) -> Optional[Position]:
        with self._lock:
            return self._positions.get(symbol)

    def has_position(self, symbol: str) -> bool:
        with self._lock:
            return symbol in self._positions

    def open_count(self) -> int:
        with self._lock:
            return len(self._positions)

    def all_position_symbols(self) -> List[str]:
        with self._lock:
            return list(self._positions.keys())

    def is_loss_halted(self) -> bool:
        if time.time() < self._loss_halt_until:
            remaining = int(self._loss_halt_until - time.time())
            log.info(f"[LOSS HALT] Paused due to {self._consecutive_losses} consecutive losses - {remaining}s remaining")
            return True
        return False

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses

    def evaluate(self, position: Position, current_price: float) -> str:
        """Evaluate position against SL, TP, Trailing Stop, and Smart Momentum-Aware Max Hold."""
        if not position or current_price <= 0:
            return "HOLD"

        # F-09: Acquire lock before mutating shared Position state
        with self._lock:
            if current_price > position.highest_price:
                position.highest_price = current_price
            if current_price < position.lowest_price or position.lowest_price == 0.0:
                position.lowest_price = current_price

            fees_pct = getattr(self.cfg, 'ROUND_TRIP_FEE_PCT', 0.2)
            min_profit_pct = getattr(self.cfg, 'MIN_TRAILING_PROFIT_PCT', 0.5)
            fee_adjusted_trigger = fees_pct + min_profit_pct

            if getattr(self.cfg, 'ENABLE_BREAK_EVEN_STOP', True) and position.min_be_price == 0.0:
                be_trigger_pct = getattr(self.cfg, 'BREAK_EVEN_TRIGGER_PCT', 1.5)
                if position.pnl_pct(current_price) >= max(be_trigger_pct, fee_adjusted_trigger):
                    min_be = position.entry_price * (1 + (fees_pct / 100.0))
                    if min_be > position.stop_loss:
                        position.min_be_price = min_be
                        position.stop_loss = min_be
                        log.info(f"[BREAK-EVEN] {position.symbol} raised SL to fee-aware break-even {min_be:.6f}")

            # Read position fields under lock for evaluation
            sl = position.stop_loss
            tp = position.take_profit
            highest = position.highest_price
            trailing = position.trailing
            trailing_offset = position.trailing_offset
            entry_price = position.entry_price
            kind = position.kind
            open_time = position.open_time
            symbol = position.symbol

        # 1. Take Profit
        if current_price >= tp:
            return "TAKE_PROFIT"

        # 2. Stop Loss
        if current_price <= sl:
            return "STOP_LOSS"

        # 3. Trailing Stop
        if trailing:
            drop_pct = ((highest - current_price) / highest) * 100.0
            peak_profit_pct = ((highest - entry_price) / entry_price) * 100.0 if entry_price > 0 else 0.0

            if peak_profit_pct >= fee_adjusted_trigger and drop_pct >= trailing_offset:
                return "TRAILING_STOP"

        # 4. Smart Momentum-Aware Max Hold Timeout
        if kind == "scanner":
            max_hold = getattr(self.cfg, 'MAX_SCANNER_HOLD_HOURS', 8) * 3600
            hold_time = time.time() - open_time

            # Hard timeout: close if held > 2x max_hold (16h) without hitting TP/SL
            if hold_time > max_hold * 2:
                log.info(f"[HOLD TIMEOUT] {symbol} exceeded maximum hard hold limit ({hold_time / 3600:.1f}h > {max_hold * 2 / 3600:.0f}h) -> closing")
                return "MAX_HOLD_TIMEOUT"

            # Soft timeout: close if held > 8h and losing below EMA9
            if hold_time > max_hold:
                exit_loss_pct = getattr(self.cfg, 'HOLD_EXIT_LOSS_PCT', -1.0)
                pnl_now = ((current_price - entry_price) / entry_price) * 100.0 if entry_price > 0 else 0.0
                if pnl_now < exit_loss_pct:
                    if self.exchange:
                        try:
                            df = self.exchange.get_candles(symbol, interval='15m', limit=20)
                            if df is not None and len(df) >= 10:
                                ema9 = df['close'].ewm(span=9, adjust=False).mean().iloc[-1]
                                if current_price < ema9:
                                    log.info(f"[HOLD TIMEOUT] {symbol} held >8h, PnL {pnl_now:.2f}% < {exit_loss_pct}%, and below EMA9 -> closing")
                                    return "MAX_HOLD_TIMEOUT"
                        except Exception:
                            return "MAX_HOLD_TIMEOUT"
                    else:
                        return "MAX_HOLD_TIMEOUT"

        return "HOLD"

    def check_circuit_breaker(self, current_balance: float) -> bool:
        """Check daily loss circuit breaker. Pass total portfolio value, not just USDT cash."""
        today = date.today()
        if today != self._today:
            self._today = today
            self._daily_start_bal = current_balance
            self._halted = False
            self._save_loss_state()  # F-19: Persist daily balance across restarts

        if self._daily_start_bal is None:
            self._daily_start_bal = current_balance
            self._save_loss_state()
            return False

        if self._daily_start_bal > 0:
            loss_pct = ((self._daily_start_bal - current_balance) / self._daily_start_bal) * 100.0
            max_loss = getattr(self.cfg, 'MAX_DAILY_LOSS', 6.0)
            if loss_pct >= max_loss:
                if not self._halted:
                    log.error(f"[CIRCUIT BREAKER] Daily loss {loss_pct:.2f}% >= {max_loss}%. HALTING.")
                    self._halted = True
                return True

        return False

    def _save_positions(self):
        # F-30: Atomic write — write to tmp then rename
        try:
            fn = getattr(self.cfg, 'POSITIONS_FILE', 'positions.json')
            data = {k: asdict(v) for k, v in self._positions.items()}
            tmp_fn = fn + '.tmp'
            with open(tmp_fn, 'w') as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_fn, fn)
        except Exception as e:
            log.error(f"Failed to save positions: {e}")

    def _load_positions(self):
        try:
            fn = getattr(self.cfg, 'POSITIONS_FILE', 'positions.json')
            if Path(fn).exists():
                with open(fn, 'r') as f:
                    data = json.load(f)
                    for k, v in data.items():
                        self._positions[k] = Position(**v)
        except Exception as e:
            log.error(f"Failed to load positions: {e}")

    def _save_loss_state(self):
        # F-19: Also persist daily start balance and date. F-30: Atomic write.
        try:
            data = {
                'losses': self._consecutive_losses,
                'halt_until': self._loss_halt_until,
                'daily_start_bal': self._daily_start_bal,
                'today': str(self._today)
            }
            tmp_fn = 'loss_state.json.tmp'
            with open(tmp_fn, 'w') as f:
                json.dump(data, f)
            os.replace(tmp_fn, 'loss_state.json')
        except Exception as e:
            log.error(f"Failed to save loss state: {e}")

    def _load_loss_state(self):
        try:
            if Path('loss_state.json').exists():
                with open('loss_state.json', 'r') as f:
                    data = json.load(f)
                    self._consecutive_losses = data.get('losses', 0)
                    self._loss_halt_until = data.get('halt_until', 0)
                    # F-19: Restore daily start balance if still same day
                    saved_today = data.get('today', '')
                    if saved_today == str(date.today()):
                        self._daily_start_bal = data.get('daily_start_bal')
                        self._today = date.today()
        except Exception as e:
            log.error(f"Failed to load loss state: {e}")

    def _save_trade_log(self):
        # F-30: Atomic write
        try:
            fn = getattr(self.cfg, 'TRADE_LOG_FILE', 'trade_log.json')
            tmp_fn = fn + '.tmp'
            with open(tmp_fn, 'w') as f:
                json.dump(self._trade_log, f, indent=2)
            os.replace(tmp_fn, fn)
        except Exception as e:
            log.error(f"Failed to save trade log: {e}")

    def _load_trade_log(self):
        try:
            fn = getattr(self.cfg, 'TRADE_LOG_FILE', 'trade_log.json')
            if Path(fn).exists():
                with open(fn, 'r') as f:
                    self._trade_log = json.load(f)
        except Exception as e:
            log.error(f"Failed to load trade log: {e}")

    def _log_trade(self, pos: Position, exit_price: float, reason: str):
        pnl_pct = pos.pnl_pct(exit_price)
        pnl_usdt = pos.pnl_usdt(exit_price)
        hold_duration = int(time.time() - pos.open_time)

        # F-22: Use Position methods instead of inline calc
        mfe = round(pos.mfe_pct(), 2)
        mae = round(pos.mae_pct(), 2)

        # F-23: Fee estimate uses both entry and exit values
        fees_pct = getattr(self.cfg, 'ROUND_TRIP_FEE_PCT', 0.2)
        entry_val = pos.entry_price * pos.quantity
        exit_val = exit_price * pos.quantity
        est_fee_usdt = round((entry_val + exit_val) * (fees_pct / 200.0), 4)

        dt = pos.details or {}
        raw_score = dt.get('raw_score', pos.entry_score)
        pump_penalty = dt.get('pump_penalty', 0.0)

        entry = {
            # F-32: Use timezone-aware UTC datetime
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'symbol': pos.symbol,
            'kind': pos.kind,
            'entry_price': pos.entry_price,
            'exit_price': exit_price,
            'quantity': pos.quantity,
            'pnl_pct': round(pnl_pct, 4),
            'pnl_usdt': round(pnl_usdt, 4),
            'mfe_pct': mfe,
            'mae_pct': mae,
            'raw_score': raw_score,
            'pump_penalty': pump_penalty,
            'final_score': pos.entry_score,
            'ema_score': dt.get('ema_score', 0),
            'rsi_score': dt.get('rsi_score', 0),
            'vol_score': dt.get('vol_score', 0),
            'breakout_score': dt.get('breakout_score', 0),
            'ob_score': dt.get('ob_score', 0),
            'rsi': dt.get('rsi', 0),
            'volume_ratio': dt.get('volume_ratio', 0),
            'breakout_atr_ratio': dt.get('breakout_atr_ratio', 0),
            'bid_ask_ratio': dt.get('bid_ask_ratio', 0),
            'est_fees_usdt': est_fee_usdt,
            'reason': reason,
            'hold_duration_sec': hold_duration,
            'entry_reason': pos.entry_reason
        }
        self._trade_log.append(entry)
        self._save_trade_log()

        log.info(f"[TRADE LOGGED] {pos.symbol} | Exit: {exit_price:.6f} | PnL: {pnl_pct:+.2f}% (${pnl_usdt:+.4f}) | Final Score: {pos.entry_score:.0f} (Raw: {raw_score:.0f}, Pen: {pump_penalty:.0f}) | MFE: {mfe:+.2f}% | MAE: {mae:+.2f}% | Reason: {reason}")
