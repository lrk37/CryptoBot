"""
bot.py — Main Entry Point for CryptoBot
=========================================
Features:
  - Scanner Engine with Candle-Based 2-Scan Confirmation & High-Score Override
  - Automatic Symbol Cooldowns (1h per symbol after close)
  - Risk Manager integration with daily circuit breaker & consecutive loss protection
  - Automated dark-themed chart snapshots on trade entry/exit
  - Fully Auditable Score Logging (raw_score, pump_penalty, final_score)
"""
import os
import sys
import time
import json
import logging
from logging.handlers import RotatingFileHandler
import threading
from datetime import datetime, timezone
from pathlib import Path

import config as cfg
from exchange import BinanceConnector
from risk_manager import RiskManager
from scanner import MarketScanner, ScanResult
from notifier import TelegramNotifier
from chart_logger import save_trade_chart
from scan_logger import log_scan_candidates, update_forward_returns

# Configure Logging
log_format = '%(asctime)s  %(levelname)-8s  [%(threadName)-12s]  %(message)s'
logging.basicConfig(
    level=getattr(logging, getattr(cfg, 'LOG_LEVEL', 'INFO'), logging.INFO),
    format=log_format,
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(
            getattr(cfg, 'LOG_FILE', 'bot.log'),
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding='utf-8',
        ),
    ]
)
log = logging.getLogger('MainThread')

_INTERVAL_SECONDS = {
    '1m': 60, '3m': 180, '5m': 300, '15m': 900, '30m': 1800,
    '1h': 3600, '2h': 7200, '4h': 14400, '1d': 86400,
}

class ScannerEngine:
    def __init__(self, exchange: BinanceConnector, risk_mgr: RiskManager, notifier: TelegramNotifier):
        self.exchange = exchange
        self.risk_mgr = risk_mgr
        self.notifier = notifier
        self.scanner = MarketScanner(exchange, cfg)
        self._running = False

        # F-10/F-11: Reentrant locks for shared mutable state & pending order lifecycle
        self._state_lock = threading.RLock()
        self._order_reconcile_lock = threading.RLock()
        self._active_symbols = set()
        self._symbol_cooldowns: dict[str, float] = {}
        self._last_alerts: dict[str, float] = {}
        self._breaker_alerted_date: str | None = None
        self._heartbeat_sent_date: str | None = str(datetime.now(timezone.utc).date())

        self._prev_high_scores: dict[str, dict] = {}
        self._curr_high_scores: dict[str, dict] = {}
        self._pending_buys: dict[str, dict] = {}
        self._in_flight_buys: set[str] = set()

        self._load_scanner_state()
        self.sync_active_symbols()

    def sync_active_symbols(self):
        """Synchronize _active_symbols with RiskManager's currently open positions."""
        with self._state_lock:
            self._active_symbols = set(self.risk_mgr.all_position_symbols())

    def _check_bnb_reserve(self):
        """Warn in logs and Telegram (throttled to once/12h) if Live Spot BNB fee reserve is depleted or low."""
        if getattr(cfg, 'USE_TESTNET', False) or not getattr(cfg, 'WARN_ZERO_BNB', True):
            return
        if not hasattr(self.exchange, 'check_bnb_fee_reserve'):
            return
        try:
            min_bnb = float(getattr(cfg, 'MIN_BNB_FEE_RESERVE_USDT', 1.0) or 1.0)
            status = self.exchange.check_bnb_fee_reserve(min_bnb_usdt=min_bnb)
            if not status.get('ok', True):
                rsn = status.get('reason', 'LOW_BNB')
                bnb_free = float(status.get('bnb_free', 0.0) or 0.0)
                bnb_usdt = float(status.get('bnb_usdt', 0.0) or 0.0)
                if rsn == 'ZERO_BNB':
                    msg = (
                        "BNB fee reserve is 0.00 BNB! Binance will deduct 0.10% fees from bought "
                        "altcoins (creating unsellable LOT_SIZE dust). Top up ~$2-$5 BNB in Spot."
                    )
                else:
                    msg = (
                        f"BNB fee reserve is low ({bnb_free:.5f} BNB ~ ${bnb_usdt:.2f} USDT < ${min_bnb:.2f}). "
                        f"Top up BNB soon to keep the 25% fee discount and avoid altcoin dust."
                    )
                log.warning(f"[BNB FEE WARNING] {msg}")
                self._alert_throttled("bnb_fee_reserve", msg, min_interval=43200.0)
        except Exception as e:
            log.debug(f"[BNB CHECK] Skipped: {e}")

    def _alert_throttled(self, key: str, message: str, min_interval: float = 900.0):
        """Send a Telegram operational alert at most once per `min_interval` seconds per key."""
        if not hasattr(self.notifier, 'alert'):
            return
        now = time.time()
        with self._state_lock:
            last = self._last_alerts.get(key, 0.0)
            if now - last < min_interval:
                return
            self._last_alerts[key] = now
        try:
            self.notifier.alert(message)
        except Exception as e:
            log.warning(f"[ALERT ERROR] {key}: {e}")

    def _record_cooldown(self, symbol: str, reason: str, net_pnl_pct: float = None) -> int:
        """Apply and persist per-symbol post-exit cooldown (12h after losing SL/invalidation, 1h after wins/BE)."""
        cooldown_sec = getattr(cfg, 'SYMBOL_COOLDOWN_SEC', 3600)
        if getattr(cfg, 'STRATEGY_MODE', 'capitulation') == 'capitulation':
            rsn_str = str(reason or '')
            if net_pnl_pct is None:
                is_lockout = "STOP_LOSS" in rsn_str
            else:
                is_lockout = (net_pnl_pct < 0) and (
                    "STOP_LOSS" in rsn_str or rsn_str in ("TREND_INVALIDATED", "MAX_HOLD_TIMEOUT")
                )
            if is_lockout:
                cooldown_sec = max(cooldown_sec, getattr(cfg, 'COOLDOWN_AFTER_SL_SEC', 43200))
        with self._state_lock:
            self._symbol_cooldowns[symbol] = time.time() + cooldown_sec
        self._save_scanner_state()
        return cooldown_sec

    def _is_signal_stale(self, res) -> bool:
        """Return True if a real-epoch signal's closed candle ended longer ago than MAX_SIGNAL_AGE_SEC."""
        lct = getattr(res, 'last_closed_time', 0) or 0
        if lct < 1_000_000_000:
            return False
        tf = getattr(res, 'timeframe', getattr(cfg, 'SCANNER_INTERVAL', '15m'))
        bar_sec = _INTERVAL_SECONDS.get(tf, 900)
        base_max_age = float(getattr(cfg, 'MAX_SIGNAL_AGE_SEC', 480))
        if base_max_age <= 0:
            return False
        max_age = max(base_max_age, int(bar_sec * 0.5)) if bar_sec > 900 else base_max_age
        age = time.time() - (float(lct) + bar_sec)
        if age > max_age:
            log.warning(
                f"[STALE SIGNAL] {res.symbol} ({tf}) closed {age:.0f}s ago "
                f"(> max {max_age:.0f}s) - skipping entry"
            )
            return True
        return False

    def start(self):
        self._running = True
        threading.Thread(target=self._scan_loop, name='scanner-scan', daemon=True).start()
        threading.Thread(target=self._monitor_loop, name='scanner-monitor', daemon=True).start()
        log.info("[SCANNER ENGINE] Started - scan + monitor threads running")

    def stop(self):
        self._running = False

    def _scan_loop(self):
        rescan_sec = int(getattr(cfg, 'SCANNER_RESCAN_INTERVAL', 300) or 300)
        while self._running:
            try:
                self._scan_tick()
            except Exception as e:
                log.error(f"[SCANNER ENGINE ERROR] {e}", exc_info=True)

            if getattr(cfg, 'ALIGN_SCANS_TO_CANDLE', True) and rescan_sec > 0:
                offset_sec = int(getattr(cfg, 'SCAN_ALIGN_OFFSET_SEC', 3))
                now_s = int(time.time())
                next_slot = ((now_s // rescan_sec) + 1) * rescan_sec + offset_sec
                wait_sec = max(1, next_slot - now_s)
            else:
                wait_sec = max(1, rescan_sec)

            for _ in range(wait_sec):
                if not self._running:
                    break
                time.sleep(1)

    def _scan_tick(self):
        if self.risk_mgr.is_loss_halted():
            halt_until = getattr(self.risk_mgr, '_loss_halt_until', 0)
            self._alert_throttled(
                f"loss_halt:{int(halt_until)}",
                f"⏸️ Consecutive loss halt active ({getattr(self.risk_mgr, 'consecutive_losses', 0)} consecutive losses); scanning paused.",
                min_interval=3600.0,
            )
            return

        # F-07: Use portfolio value (including open positions) for circuit breaker
        pos_symbols = self.risk_mgr.all_position_symbols()
        pos_qty = {}
        pos_fallback_prices = {}
        for s in pos_symbols:
            p_ = self.risk_mgr.get_position(s)
            if p_:
                pos_qty[s] = p_.quantity
                if p_.entry_price > 0:
                    pos_fallback_prices[s] = p_.entry_price
        try:
            portfolio_val = self.exchange.get_portfolio_value(
                symbols=pos_symbols, quantities=pos_qty, fallback_prices=pos_fallback_prices
            )
        except TypeError:
            portfolio_val = self.exchange.get_portfolio_value(symbols=pos_symbols, quantities=pos_qty)
        if portfolio_val > 0:
            self._maybe_send_daily_heartbeat(portfolio_val)
        self._check_bnb_reserve()

        if self.risk_mgr.check_circuit_breaker(portfolio_val):
            today_str = str(datetime.now(timezone.utc).date())
            if self._breaker_alerted_date != today_str:
                self._breaker_alerted_date = today_str
                if hasattr(self.notifier, 'alert'):
                    halt_rsn = getattr(self.risk_mgr, 'last_halt_reason', '') or 'Daily loss limit reached'
                    self.notifier.alert(f"🛑 Circuit breaker tripped! {halt_rsn}. Trading halted.")
            return

        self._reconcile_pending_buys()

        if self.risk_mgr.open_count() + self._pending_buy_count() >= getattr(cfg, 'MAX_OPEN_TRADES', 1):
            log.info("[SCANNER ENGINE] Max open trades (or unresolved entry) reached - skipping scan")
            return

        btc_status = self.scanner.check_btc_regime()

        # F-11: Copy active_symbols under lock for safe iteration
        with self._state_lock:
            active_copy = list(self._active_symbols)
        results = self.scanner.scan_market(active_symbols=active_copy)

        if not results:
            log.info("[SCANNER ENGINE] No candidates passed scoring threshold")
            try:
                update_forward_returns(self.exchange, price_map=getattr(self.scanner, '_last_tickers_24h', None))
            except TypeError:
                update_forward_returns(self.exchange)
            except Exception as e:
                log.warning(f"[SCAN LOGGER] Forward tracking exception: {e}")
            self._rotate_scores()
            return

        now = time.time()
        high_thresh = getattr(cfg, 'HIGH_SCORE_THRESHOLD', 80)
        require_confirm = getattr(cfg, 'REQUIRE_CONFIRMATION', True)
        expired_removed = False

        try:
            for res in results:
                symbol = res.symbol
                score = res.score
                closed_time = res.last_closed_time

                # F-10: Check symbol cooldown under lock
                with self._state_lock:
                    if symbol in self._symbol_cooldowns:
                        if now < self._symbol_cooldowns[symbol]:
                            remaining = int(self._symbol_cooldowns[symbol] - now)
                            log.info(f"[SCANNER ENGINE] {symbol} is in cooldown ({remaining}s remaining) - skipping")
                            continue
                        else:
                            del self._symbol_cooldowns[symbol]
                            expired_removed = True

                # 1. Enforce BTC Market Regime: in BEARISH regime, only allow Extreme Capitulation (>= high_thresh)
                if btc_status == "BEARISH" and not (
                    getattr(cfg, 'STRATEGY_MODE', 'capitulation') == 'capitulation' and score >= high_thresh
                ):
                    log.info(f"[SCANNER ENGINE] BTC regime BEARISH - skipping entry for {symbol} (score {score:.0f})")
                    continue

                if self._is_signal_stale(res):
                    continue

                # 2. High Score Override (>=80) fast-tracks entry
                if score >= high_thresh and getattr(cfg, 'ENABLE_HIGH_SCORE_OVERRIDE', True):
                    log.info(f"[CONFIRMATION BYPASS] {symbol} score {score:.0f} >= {high_thresh:.0f} - fast-tracking entry!")
                    if self._execute_entry(res):
                        break
                    continue

                # 2-Scan Candle-Based Confirmation System
                if require_confirm:
                    self._curr_high_scores[symbol] = {
                        'score': score,
                        'last_closed_time': closed_time,
                        'recorded_at': now,
                    }

                    if symbol in self._prev_high_scores:
                        prev_info = self._prev_high_scores[symbol]
                        prev_score = prev_info.get('score', 0)
                        prev_time = prev_info.get('last_closed_time', 0)

                        if closed_time > prev_time:
                            log.info(f"[CONFIRMATION CONFIRMED] {symbol} scored high on distinct candles ({prev_score:.0f} -> {score:.0f}) - executing!")
                            if self._execute_entry(res):
                                self._curr_high_scores.pop(symbol, None)
                                self._prev_high_scores.pop(symbol, None)
                                break
                        else:
                            log.info(f"[CONFIRMATION WAIT] {symbol} same candle as previous scan ({closed_time}) - waiting for next closed candle")
                    else:
                        log.info(f"[CONFIRMATION WAIT] {symbol} score {score:.0f} >= 60 - waiting for 2nd scan confirmation")
                else:
                    if self._execute_entry(res):
                        break
        finally:
            if expired_removed:
                self._save_scanner_state()
            # Log candidates & update historical forward returns AFTER entry execution (avoids retry lag on buys)
            try:
                log_scan_candidates(results, self.exchange)
                try:
                    update_forward_returns(self.exchange, price_map=getattr(self.scanner, '_last_tickers_24h', None))
                except TypeError:
                    update_forward_returns(self.exchange)
            except Exception as e:
                log.warning(f"[SCAN LOGGER] Forward tracking exception: {e}")

        self._rotate_scores()

    def _pending_buy_count(self) -> int:
        with self._state_lock:
            return len(self._pending_buys)

    @staticmethod
    def _extract_order_exec_qty(order) -> float:
        if not order:
            return 0.0
        if 'executedQty' in order:
            return float(order.get('executedQty') or 0)
        if order.get('fills'):
            return sum(float(f.get('qty', 0) or 0) for f in order['fills'])
        return 0.0

    def _order_is_terminal(self, order) -> bool:
        if hasattr(self.exchange, 'is_order_terminal'):
            return bool(self.exchange.is_order_terminal(order))
        return BinanceConnector.is_order_terminal(order)

    def _call_market_buy(self, symbol: str, usdt_amount: float = 0.0, client_oid: str = None, on_oid_update=None):
        if client_oid is not None:
            if on_oid_update is not None:
                try:
                    return self.exchange.market_buy(
                        symbol, usdt_amount=usdt_amount, client_oid=client_oid, on_oid_update=on_oid_update
                    )
                except TypeError:
                    pass
            try:
                return self.exchange.market_buy(symbol, usdt_amount=usdt_amount, client_oid=client_oid)
            except TypeError:
                pass
        return self.exchange.market_buy(symbol, usdt_amount=usdt_amount)

    def _call_market_sell(self, symbol: str, sell_qty: float, client_oid: str = None):
        if client_oid is not None:
            try:
                return self.exchange.market_sell(symbol, sell_qty, client_oid=client_oid)
            except TypeError:
                pass
        return self.exchange.market_sell(symbol, sell_qty)

    def _clear_pending_buy(self, symbol: str) -> bool:
        with self._state_lock:
            self._pending_buys.pop(symbol, None)
        return self._save_scanner_state()

    def _reconcile_pending_buys(self):
        """Reconcile any persisted unresolved entry clientOrderIds before scanning or allowing new entries."""
        if not hasattr(self.exchange, 'reconcile_client_order_strict'):
            return
        with self._order_reconcile_lock:
            with self._state_lock:
                pending_items = list(self._pending_buys.items())
            for symbol, meta in pending_items:
                with self._state_lock:
                    if symbol in self._in_flight_buys:
                        log.info(f"[PENDING BUY] {symbol} initial submission in progress; skipping reconciliation")
                        continue
                client_oid = (meta or {}).get('client_oid')
                if not client_oid:
                    self._clear_pending_buy(symbol)
                    continue
                if self.risk_mgr.has_position(symbol):
                    existing_pos = self.risk_mgr.get_position(symbol)
                    if existing_pos and existing_pos.details.get('oco_order_list_id') is None and getattr(cfg, 'USE_EXCHANGE_OCO', True):
                        existing_pos.details['oco_needs_sync'] = True
                        self.risk_mgr.persist_positions()
                    with self._state_lock:
                        self._active_symbols.add(symbol)
                    self._clear_pending_buy(symbol)
                    continue

                state, order = self.exchange.reconcile_client_order_strict(symbol, client_oid)
                if state == 'NOT_FOUND':
                    if (meta or {}).get('state') == 'SUBMITTING':
                        now_ts = time.time()
                        created_at = float((meta or {}).get('created_at') or 0.0)
                        if created_at <= 0.0:
                            created_at = now_ts
                            with self._state_lock:
                                if symbol in self._pending_buys:
                                    self._pending_buys[symbol]['created_at'] = created_at
                        age = max(0.0, now_ts - created_at)
                        grace_sec = float(getattr(cfg, 'SUBMITTING_RECOVERY_GRACE_SEC', 15.0))
                        min_checks = max(1, int(getattr(cfg, 'SUBMITTING_NOT_FOUND_CONFIRMATIONS', 2)))
                        checks = int((meta or {}).get('not_found_checks') or 0) + 1
                        if checks < min_checks or age < grace_sec:
                            with self._state_lock:
                                if symbol in self._pending_buys:
                                    self._pending_buys[symbol]['not_found_checks'] = checks
                            self._save_scanner_state()
                            log.warning(
                                f"[PENDING BUY] {symbol} {client_oid} was SUBMITTING before restart and "
                                f"returned -2013 (check {checks}/{min_checks}, age {age:.1f}s/{grace_sec:.1f}s); keeping lock"
                            )
                            continue
                    log.info(f"[PENDING BUY] {symbol} {client_oid} confirmed not found (-2013); clearing pending entry")
                    self._clear_pending_buy(symbol)
                elif state == 'FOUND' and order is not None:
                    if not self._order_is_terminal(order):
                        with self._state_lock:
                            if symbol in self._pending_buys:
                                self._pending_buys[symbol]['state'] = 'PENDING'
                        self._save_scanner_state()
                        log.warning(
                            f"[PENDING BUY] {symbol} {client_oid} still non-terminal "
                            f"(status={order.get('status')}); keeping pending lock"
                        )
                        continue
                    # Finalize and persist RiskManager position FIRST before clearing pending_buys
                    self._finalize_buy_order(symbol, order, meta, clear_pending=True)
                else:
                    log.warning(f"[PENDING BUY] {symbol} {client_oid} could not be verified ({state}); keeping lock")

    def _finalize_buy_order(self, symbol: str, order: dict, meta: dict, clear_pending: bool = False) -> bool:
        """Persist position in RiskManager first, clear pending_buy only once durable, then notify/OCO/chart."""
        fill_price = self.exchange.avg_fill_price(order)
        if fill_price <= 0:
            fill_price = float(meta.get('price') or 0.0)

        exec_qty = self.exchange.net_executed_qty(order, symbol)  # net of commission paid in the coin
        if exec_qty <= 0:
            log.error(
                f"[ENTRY ABORTED] Market buy for {symbol} returned 0 executed qty "
                f"(status={order.get('status', 'UNKNOWN')}); refusing to open phantom position."
            )
            if clear_pending:
                self._clear_pending_buy(symbol)
            return True

        if self.risk_mgr.has_position(symbol):
            log.warning(f"[ENTRY SKIPPED] Position already tracked for {symbol}")
            if clear_pending:
                self._clear_pending_buy(symbol)
            return True

        tx_ms = order.get('transactTime') or order.get('updateTime') or order.get('time')
        entry_ts = (float(tx_ms) / 1000.0) if tx_ms else None

        details = dict(meta.get('scan_details') or {})
        if getattr(cfg, 'USE_EXCHANGE_OCO', True):
            details.setdefault('oco_needs_sync', True)
        if meta.get('client_oid'):
            details['entry_client_oid'] = meta['client_oid']

        pos = self.risk_mgr.open_position(
            symbol=symbol,
            entry_price=fill_price,
            quantity=exec_qty,
            kind="scanner",
            atr_value=float(meta.get('atr') or 0.0),
            entry_reason=str(meta.get('reason') or "SCANNER"),
            entry_score=float(meta.get('score') or 0.0),
            details=details,
            open_time=entry_ts if (entry_ts and entry_ts > 0) else None,
        )

        if not pos and not self.risk_mgr.has_position(symbol):
            log.error(f"[ENTRY PERSIST ERROR] RiskManager refused/failed opening position for {symbol}; retaining pending buy record")
            return False

        if pos and entry_ts and entry_ts > 0 and abs(pos.open_time - entry_ts) > 1e-6:
            pos.open_time = entry_ts
            if not self.risk_mgr.persist_positions():
                log.error(f"[ENTRY PERSIST ERROR] Failed updating open_time for {symbol}; retaining pending buy record")
                return False

        # Position is now durably persisted in positions.json — safe to clear pending buy lock
        if clear_pending:
            self._clear_pending_buy(symbol)

        with self._state_lock:
            self._active_symbols.add(symbol)
        tf = meta.get('timeframe', getattr(cfg, 'SCANNER_INTERVAL', '15m'))
        max_hold_h = getattr(cfg, 'MAX_SCANNER_HOLD_HOURS', 8)
        target_pos = pos or self.risk_mgr.get_position(symbol)
        if target_pos:
            self.notifier.trade_opened(
                symbol, "BUY", fill_price, exec_qty, target_pos.stop_loss, target_pos.take_profit,
                timeframe=tf, max_hold_hours=max_hold_h, open_time=target_pos.open_time
            )

            # Submit native exchange-side OCO stop/profit order
            if getattr(cfg, 'USE_EXCHANGE_OCO', True):
                try:
                    oco = self.exchange.create_oco_sell(symbol, exec_qty, target_pos.take_profit, target_pos.stop_loss)
                    if oco is None and getattr(self.exchange, 'last_oco_filter_rejected', False):
                        target_pos.details.pop('oco_order_list_id', None)
                        target_pos.details.pop('oco_stop_loss', None)
                        target_pos.details['oco_needs_sync'] = False
                        target_pos.details['oco_filter_blocked_qty'] = round(exec_qty, 8)
                        self.risk_mgr.persist_positions()
                        log.warning(
                            f"[OCO ENTRY SKIPPED] {symbol} qty={exec_qty} rejected by exchange filters (-1013/minQty); "
                            f"relying on client-side monitor exits."
                        )
                    else:
                        self._record_or_rollback_oco(symbol, target_pos, oco, target_pos.stop_loss, tag="OCO ENTRY")
                except Exception as e:
                    target_pos.details['oco_needs_sync'] = True
                    self.risk_mgr.persist_positions()
                    log.warning(f"[OCO EXCEPTION] Could not place OCO: {e}")

            # Save chart snapshot on entry (fetch 100 bars so 50-bar chart has warmed-up indicators)
            try:
                df = self.exchange.get_candles(symbol, interval=tf, limit=100)
                chart_path = save_trade_chart(
                    symbol, df, fill_price, fill_price, 0.0, 0.0,
                    "ENTRY", target_pos.stop_loss, target_pos.take_profit
                )
                if chart_path and hasattr(self.notifier, 'send_photo'):
                    self.notifier.send_photo(chart_path, caption=f"📈 {symbol} [{tf}] — ENTRY @ {fill_price:.6g}")
            except Exception as e:
                log.warning(f"[CHART LOGGER] Entry chart failed: {e}")
        return True

    def _maybe_send_daily_heartbeat(self, portfolio_val: float):
        if not getattr(cfg, 'ENABLE_DAILY_HEARTBEAT', False):
            return
        today_str = str(datetime.now(timezone.utc).date())
        if self._heartbeat_sent_date == today_str:
            return
        try:
            stats = self.risk_mgr.get_expectancy_stats() if hasattr(self.risk_mgr, 'get_expectancy_stats') else {}
            peak_val = getattr(self.risk_mgr, 'peak_balance', None) or portfolio_val
            open_cnt = self.risk_mgr.open_count()
            log.info(
                f"[HEARTBEAT] Equity=${portfolio_val:.2f} (Peak=${peak_val:.2f}) | Open={open_cnt} | "
                f"Trades={stats.get('total_trades', 0)} ({stats.get('win_rate_pct', 0.0):.1f}% WR, "
                f"Exp=${stats.get('expectancy_usdt', 0.0):+.2f})"
            )
            sent_ok = True
            if hasattr(self.notifier, 'daily_heartbeat'):
                res = self.notifier.daily_heartbeat(portfolio_val, peak_val, open_cnt, stats)
                if getattr(self.notifier, 'enabled', False) and res is False:
                    sent_ok = False
            if sent_ok:
                self._heartbeat_sent_date = today_str
                self._save_scanner_state()
        except Exception as e:
            log.warning(f"[HEARTBEAT ERROR] {e}")

    def _execute_entry(self, res: ScanResult) -> bool:
        symbol = res.symbol
        price = res.close_price

        bal = self.exchange.get_usdt_balance()
        max_usdt = float(getattr(cfg, 'MAX_TRADE_USDT', 20.0) or 0.0)
        # F-18: Use SCANNER_TRADE_PCT config instead of hardcoded 0.99; MAX_TRADE_USDT<=0 auto-scales with balance
        trade_pct = float(getattr(cfg, 'SCANNER_TRADE_PCT', 99)) / 100.0
        if max_usdt <= 0:
            usdt_amount = bal * trade_pct
        else:
            usable_bal = bal * trade_pct if bal < max_usdt else max_usdt
            usdt_amount = min(usable_bal, max_usdt)
        min_notional = getattr(cfg, 'MIN_NOTIONAL_USDT', 11.0)

        if getattr(cfg, 'USE_RISK_BASED_SIZING', False) and price > 0 and usdt_amount >= min_notional:
            from risk_manager import compute_stops
            sl_px, _, _, _ = compute_stops(cfg, 'scanner', price, float(getattr(res, 'atr', 0.0) or 0.0))
            sl_dist_pct = ((price - sl_px) / price) * 100.0 if sl_px < price else 0.0
            risk_pct = float(getattr(cfg, 'MAX_RISK_PER_TRADE_PCT', 2.5) or 2.5)
            if sl_dist_pct > 0 and risk_pct > 0:
                risk_budget_usdt = usdt_amount * (risk_pct / 100.0)
                risk_sized_usdt = risk_budget_usdt / (sl_dist_pct / 100.0)
                usdt_amount = max(min_notional, min(usdt_amount, risk_sized_usdt))

        if usdt_amount < min_notional:
            log.warning(f"[ENTRY FAILED] Insufficient balance for {symbol}: ${usdt_amount:.2f}")
            return False

        tf = getattr(res, 'timeframe', getattr(cfg, 'SCANNER_INTERVAL', '15m'))
        scan_details = {
            'raw_score': res.raw_score,
            'pump_penalty': res.pump_penalty,
            'ema_score': res.ema_score,
            'rsi_score': res.rsi_score,
            'vol_score': res.vol_score,
            'breakout_score': res.breakout_score,
            'ob_score': res.ob_score,
            'rsi': res.rsi,
            'volume_ratio': res.volume_ratio,
            'breakout_atr_ratio': res.breakout_atr_ratio,
            'bid_ask_ratio': res.bid_ask_ratio,
            'timeframe': tf,
        }
        meta = {
            'symbol': symbol,
            'price': price,
            'atr': res.atr,
            'score': res.score,
            'reason': res.reason,
            'timeframe': tf,
            'scan_details': scan_details,
            'created_at': time.time(),
        }

        buy_oid = None
        with self._order_reconcile_lock:
            with self._state_lock:
                if symbol in self._pending_buys or symbol in self._in_flight_buys:
                    log.warning(f"[ENTRY DEFERRED] Unresolved buy order already tracked for {symbol}")
                    return False
                if self.risk_mgr.has_position(symbol):
                    log.warning(f"[ENTRY DEFERRED] Position already tracked for {symbol}")
                    return False
                if hasattr(self.exchange, 'new_client_order_id'):
                    buy_oid = self.exchange.new_client_order_id(symbol, 'BUY')
                    meta['client_oid'] = buy_oid
                    meta['state'] = 'SUBMITTING'
                    self._pending_buys[symbol] = meta
                    self._in_flight_buys.add(symbol)
                    if not self._save_scanner_state():
                        self._pending_buys.pop(symbol, None)
                        self._in_flight_buys.discard(symbol)
                        log.error(
                            f"[ENTRY ABORTED] Failed to persist scanner state for {symbol}; "
                            f"refusing unrecorded market buy."
                        )
                        return False

        def _rotate_buy_oid(new_oid: str) -> bool:
            nonlocal buy_oid
            with self._state_lock:
                if symbol in self._pending_buys:
                    self._pending_buys[symbol]['client_oid'] = new_oid
                    self._pending_buys[symbol]['state'] = 'SUBMITTING'
            if self._save_scanner_state():
                buy_oid = new_oid
                meta['client_oid'] = new_oid
                return True
            return False

        try:
            try:
                order = self._call_market_buy(
                    symbol, usdt_amount=usdt_amount, client_oid=buy_oid,
                    on_oid_update=_rotate_buy_oid if buy_oid else None,
                )
            except TypeError:
                order = self._call_market_buy(symbol, usdt_amount=usdt_amount, client_oid=buy_oid)

            with self._order_reconcile_lock:
                if not order:
                    if (
                        buy_oid
                        and not getattr(self.exchange, 'last_order_confirmed_unsent', False)
                        and hasattr(self.exchange, 'reconcile_client_order_strict')
                    ):
                        state, rec = self.exchange.reconcile_client_order_strict(symbol, buy_oid)
                        if state == 'NOT_FOUND':
                            self._clear_pending_buy(symbol)
                            log.error(f"[ENTRY FAILED] Market buy failed for {symbol}")
                            return False
                        if state == 'FOUND' and rec is not None:
                            order = rec
                        else:
                            with self._state_lock:
                                if symbol in self._pending_buys:
                                    self._pending_buys[symbol]['state'] = 'PENDING'
                                    self._save_scanner_state()
                            log.warning(
                                f"[ENTRY UNCERTAIN] Market buy {buy_oid} for {symbol} could not be confirmed; "
                                f"keeping persisted pending_buy for next reconciliation tick."
                            )
                            return True
                    else:
                        if buy_oid:
                            self._clear_pending_buy(symbol)
                        log.error(f"[ENTRY FAILED] Market buy failed for {symbol}")
                        return False

                if not self._order_is_terminal(order):
                    if buy_oid:
                        with self._state_lock:
                            if symbol in self._pending_buys:
                                self._pending_buys[symbol]['state'] = 'PENDING'
                                self._save_scanner_state()
                        log.warning(
                            f"[ENTRY UNRESOLVED] Market buy {buy_oid} for {symbol} still non-terminal "
                            f"(status={order.get('status')}, executedQty={order.get('executedQty', 0)}); "
                            f"deferring position open until reconciled terminal."
                        )
                    else:
                        log.error(
                            f"[ENTRY ABORTED] Market buy for {symbol} returned non-terminal status="
                            f"{order.get('status', 'UNKNOWN')} without reconciliation support."
                        )
                    return True

                return self._finalize_buy_order(symbol, order, meta, clear_pending=bool(buy_oid))
        finally:
            if buy_oid:
                with self._order_reconcile_lock:
                    with self._state_lock:
                        self._in_flight_buys.discard(symbol)
                        if symbol in self._pending_buys and self._pending_buys[symbol].get('state') == 'SUBMITTING':
                            self._pending_buys[symbol]['state'] = 'PENDING'
                            self._save_scanner_state()

    def _rotate_scores(self):
        now = time.time()
        max_keep_sec = 7200.0
        merged = {
            sym: info for sym, info in self._prev_high_scores.items()
            if isinstance(info, dict) and (now - float(info.get('recorded_at', now) or now)) <= max_keep_sec
        }
        merged.update(self._curr_high_scores)
        self._prev_high_scores = merged
        self._curr_high_scores.clear()
        self._save_scanner_state()

    def _monitor_loop(self):
        poll_sec = getattr(cfg, 'POLL_INTERVAL', 30)
        while self._running:
            try:
                self._monitor_tick()
            except Exception as e:
                log.error(f"[SCANNER MONITOR ERROR] {e}", exc_info=True)

            for _ in range(poll_sec):
                if not self._running:
                    break
                time.sleep(1)

    def _process_oco_fill(self, symbol: str, pos, oco_id, fill: dict) -> str:
        """Finalize a full or partial OCO fill dict detected from Binance.

        Returns one of: 'FULL_CLOSED', 'FULL_DEFERRED', 'PARTIAL_CLOSED', 'PARTIAL_DEFERRED', 'NO_FILL'.
        """
        fill_qty = (
            float(fill['qty'])
            if fill and 'qty' in fill and fill['qty'] is not None
            else (pos.quantity if fill else 0.0)
        )
        if not fill or fill_qty <= 0:
            return 'NO_FILL'

        exit_px = float(fill['price'])
        fill_ts = fill.get('time') or time.time()
        reason = "TAKE_PROFIT" if fill.get('type') in ('LIMIT_MAKER', 'LIMIT') else "STOP_LOSS"
        oco_fill_id = f"OCO:{oco_id}:{fill.get('order_id') or fill.get('time') or ''}"
        remaining = pos.quantity - fill_qty
        if fill_qty < pos.quantity * 0.99 and remaining > 1e-8:
            ref_px = min(exit_px, pos.stop_loss) if pos.stop_loss > 0 else exit_px
            sellable_rem = (
                self.exchange.is_sellable_qty(symbol, remaining, ref_px)
                if hasattr(self.exchange, 'is_sellable_qty')
                else True
            )
            if sellable_rem:
                log.info(
                    f"[EXCHANGE PARTIAL FILL] {symbol} OCO leg partially filled "
                    f"{fill_qty} / {pos.quantity} ({reason}) at {exit_px:.6f}; "
                    f"retaining remaining {remaining:.8f}"
                )
                if not self.risk_mgr.partial_close_position(
                    symbol, fill_qty, exit_px, f"OCO_{reason}",
                    clear_oco=True, fill_id=oco_fill_id,
                ):
                    self._alert_throttled(
                        f"oco_partial_fail:{symbol}",
                        f"⚠️ Failed persisting OCO partial fill for {symbol}; retaining state for retry.",
                    )
                    return 'PARTIAL_DEFERRED'
                return 'PARTIAL_CLOSED'
            log.info(
                f"[DUST REMAINDER] {symbol} OCO filled {fill_qty} / {pos.quantity}; "
                f"remainder {remaining:.8f} below exchange minQty/minNotional — finalizing close."
            )

        log.info(f"[EXCHANGE FILL] {symbol} OCO leg filled ({reason}) qty={fill_qty} at {exit_px:.6f}")
        fees_pct = float(getattr(cfg, 'ROUND_TRIP_FEE_PCT', 0.2) or 0.0)
        gross_pnl_pct = pos.pnl_pct(exit_px)
        gross_pnl_usdt = (exit_px - pos.entry_price) * fill_qty
        est_fee_usdt = (pos.entry_price * fill_qty + exit_px * fill_qty) * (fees_pct / 200.0)
        pnl_pct = gross_pnl_pct - fees_pct
        pnl_usdt = gross_pnl_usdt - est_fee_usdt
        dur_sec = max(0.0, fill_ts - pos.open_time)
        if not self.risk_mgr.close_position(
            symbol, exit_px, f"OCO_{reason}",
            fill_id=oco_fill_id, executed_qty=fill_qty,
        ):
            log.error(
                f"[CLOSE DEFERRED] Failed persisting OCO fill close for {symbol}; "
                f"retaining position and OCO id for retry."
            )
            self._alert_throttled(
                f"close_defer_oco:{symbol}",
                f"⚠️ Failed persisting OCO close for {symbol}; will retry on next monitor tick.",
            )
            return 'FULL_DEFERRED'

        with self._state_lock:
            self._active_symbols.discard(symbol)
        self._record_cooldown(symbol, reason, net_pnl_pct=pnl_pct)
        self.notifier.trade_closed(
            symbol, f"OCO_{reason}", pos.entry_price, exit_px, pnl_pct,
            duration_sec=dur_sec, pnl_usdt=pnl_usdt, open_time=pos.open_time, close_time=fill_ts
        )
        try:
            tf = (pos.details or {}).get('timeframe', getattr(cfg, 'SCANNER_INTERVAL', '15m'))
            df = self.exchange.get_candles(symbol, interval=tf, limit=100)
            chart_path = save_trade_chart(
                symbol, df, pos.entry_price, exit_px, pnl_pct, pnl_usdt,
                f"OCO_{reason}", pos.stop_loss, pos.take_profit
            )
            if chart_path and hasattr(self.notifier, 'send_photo'):
                self.notifier.send_photo(
                    chart_path,
                    caption=f"📊 {symbol} — OCO_{reason} ({pnl_pct:+.2f}% | {pnl_usdt:+.2f} USDT)"
                )
        except Exception as e:
            log.warning(f"[CHART LOGGER] Exit chart failed: {e}")
        return 'FULL_CLOSED'

    def _check_position_oco_status(self, symbol: str, pos) -> bool:
        """Inspect active exchange OCO for `pos` and handle fills/vanished OCOs.

        Returns True if the caller should stop processing further exit/sync actions for `pos`
        this tick (e.g. closed, partially closed, or persistence deferred).
        """
        oco_id = (pos.details or {}).get('oco_order_list_id')
        if oco_id is None:
            return False

        try:
            fill = None
            oco_gone = False
            if hasattr(self.exchange, 'get_oco_fill'):
                oco_res = self.exchange.get_oco_fill(
                    symbol, oco_id, since_ms=int(pos.open_time * 1000) - 60_000
                )
                oco_status = (oco_res or {}).get('status', 'UNKNOWN')
                if oco_status == 'FILLED':
                    fill = oco_res
                    oco_gone = True
                elif oco_status in ('CANCELED', 'NOT_FOUND'):
                    oco_gone = True
            else:
                open_orders = self.exchange.get_open_orders_strict(symbol)
                if open_orders is not None and len(open_orders) == 0:
                    oco_gone = True
                    fill = self.exchange.get_last_sell_fill(symbol, int(pos.open_time * 1000) - 60_000)

            if oco_gone:
                if fill and float(fill.get('qty', pos.quantity) if fill.get('qty') is not None else pos.quantity) > 0:
                    outcome = self._process_oco_fill(symbol, pos, oco_id, fill)
                    if outcome != 'NO_FILL':
                        return True
                log.warning(
                    f"[OCO] {symbol}: OCO is no longer open but no filled SELL was found - "
                    f"clearing stale OCO link and marking oco_needs_sync; client-side exits stay active"
                )
                pos.details.pop('oco_order_list_id', None)
                pos.details.pop('oco_stop_loss', None)
                if getattr(cfg, 'USE_EXCHANGE_OCO', True):
                    pos.details['oco_needs_sync'] = True
                self.risk_mgr.persist_positions()
        except Exception as e:
            log.warning(f"[MONITOR OCO CHECK] {symbol}: {e}")
        return False

    def _check_oco_before_replacing_or_selling(self, symbol: str, pos, oco_id, tag: str) -> str:
        """Right after cancel_open_orders(symbol) succeeds (which treats -2011 as True), check
        whether the OCO actually executed just before cancellation.

        Returns one of: 'CLEAR', 'DEFER', 'FULL_CLOSED', 'FULL_DEFERRED', 'PARTIAL_CLOSED', 'PARTIAL_DEFERRED'.
        """
        since_ms = int(pos.open_time * 1000) - 60_000
        if hasattr(self.exchange, 'get_oco_fill'):
            oco_res = self.exchange.get_oco_fill(symbol, oco_id, since_ms=since_ms)
            st = (oco_res or {}).get('status', 'UNKNOWN')
            if st in ('OPEN', 'UNKNOWN'):
                self.risk_mgr.persist_positions()
                log.warning(
                    f"[{tag}] {symbol} OCO {oco_id} status={st} after cancel; "
                    f"preserving oco_order_list_id and deferring action."
                )
                self._alert_throttled(
                    f"oco_post_cancel:{tag}:{symbol}",
                    f"⚠️ {symbol} OCO {oco_id} status={st} after cancel; preserving OCO and deferring.",
                )
                return 'DEFER'
            if st == 'FILLED' and oco_res and float(oco_res.get('qty') or 0) > 0:
                return self._process_oco_fill(symbol, pos, oco_id, oco_res)
            return 'CLEAR'

        fill = self.exchange.get_last_sell_fill(symbol, since_ms)
        if fill and float(fill.get('qty', pos.quantity) if fill.get('qty') is not None else pos.quantity) > 0:
            return self._process_oco_fill(symbol, pos, oco_id, fill)
        return 'CLEAR'

    def _reconcile_startup_exits(self):
        """Reconcile any pending sells or exchange OCO fills before startup stale-position cleanup."""
        for symbol in self.risk_mgr.all_position_symbols():
            pos = self.risk_mgr.get_position(symbol)
            if not pos:
                continue
            if (pos.details or {}).get('pending_sell_client_oid'):
                try:
                    fallback_px = self.exchange.get_price(symbol)
                except Exception:
                    fallback_px = pos.entry_price
                self._reconcile_pending_sell(symbol, pos, fallback_px)
            pos = self.risk_mgr.get_position(symbol)
            if pos and (pos.details or {}).get('oco_order_list_id') is not None:
                self._check_position_oco_status(symbol, pos)

    def _should_defer_exit_to_active_oco(self, symbol: str, pos, curr_price: float, action: str, prev_sl: float) -> bool:
        """Return True on the FIRST monitor tick when `action` is an SL/TP hit already covered by
        a synced active exchange OCO within its execution band, avoiding cancel/market-sell races
        mid-trigger. If price breaches past the OCO stopLimitPrice floor (or if the OCO still has
        not filled on the next monitor tick), returns False so the client-side market sell executes."""
        if not getattr(cfg, 'USE_EXCHANGE_OCO', True):
            return False
        if pos.details is None:
            pos.details = {}
        details = pos.details
        oco_id = details.get('oco_order_list_id')
        if oco_id is None or details.get('oco_needs_sync'):
            return False

        oco_sl = float(details.get('oco_stop_loss', 0.0) or 0.0)
        if pos.stop_loss > prev_sl or (oco_sl > 0 and abs(pos.stop_loss - oco_sl) > 1e-9):
            return False
        if action == "STOP_LOSS" and oco_sl <= 0 and float(getattr(pos, 'min_be_price', 0.0) or 0.0) > 0:
            return False

        offset_pct = float(getattr(cfg, 'OCO_STOP_LIMIT_OFFSET_PCT', 0.8) or 0.8)
        in_band = False
        if action == "STOP_LOSS":
            sl_floor = pos.stop_loss * (1.0 - offset_pct / 100.0)
            if curr_price >= sl_floor:
                in_band = True
        elif action == "TAKE_PROFIT" and pos.take_profit > 0 and curr_price >= pos.take_profit:
            tp_ceil = pos.take_profit * (1.0 + offset_pct / 100.0)
            if curr_price <= tp_ceil:
                in_band = True

        if not in_band:
            details.pop('oco_band_defer_action', None)
            return False

        # Defer at most once per consecutive band entry so a stuck/untriggered OCO never stalls exit
        if details.get('oco_band_defer_action') == action:
            details.pop('oco_band_defer_action', None)
            log.warning(
                f"[MONITOR OCO FALLBACK] {symbol} OCO {oco_id} still open on second tick at {action} "
                f"({curr_price:.6f}); proceeding with client-side close"
            )
            return False

        details['oco_band_defer_action'] = action
        log.info(
            f"[MONITOR OCO ACTIVE] {symbol} price {curr_price:.6f} within active OCO {oco_id} "
            f"{action} band; deferring 1 tick to let exchange OCO fill"
        )
        return True

    def _monitor_tick(self):
        self._reconcile_pending_buys()

        symbols = self.risk_mgr.all_position_symbols()
        for symbol in symbols:
            try:
                pos = self.risk_mgr.get_position(symbol)
                if not pos or pos.kind != "scanner":
                    continue

                if pos.details.get('pending_sell_client_oid'):
                    try:
                        fallback_px = self.exchange.get_price(symbol)
                    except Exception:
                        fallback_px = pos.entry_price
                    rec_state = self._reconcile_pending_sell(symbol, pos, fallback_px)
                    if rec_state in ('FINALIZED', 'BLOCKED'):
                        continue

                try:
                    curr_price = self.exchange.get_price(symbol)
                except Exception as e:
                    # F-27: Log warning and skip — hold timeout is based on open_time,
                    # not consecutive poll failures, so brief outages are safe.
                    log.warning(f"[MONITOR] Failed to get price for {symbol}: {e}")
                    continue

                # Did the exchange-side OCO already close or partially fill this position?
                if self._check_position_oco_status(symbol, pos):
                    continue

                prev_sl = pos.stop_loss
                action = self.risk_mgr.evaluate(pos, curr_price)
                if action != "HOLD":
                    if self._should_defer_exit_to_active_oco(symbol, pos, curr_price, action, prev_sl):
                        continue
                    log.info(f"[MONITOR EXIT TRIGGERED] {symbol} -> {action} at {curr_price:.6f}")
                    self._close_position(symbol, pos, curr_price, action)
                else:
                    if pos.details:
                        pos.details.pop('oco_band_defer_action', None)
                    if getattr(cfg, 'USE_EXCHANGE_OCO', True):
                        oco_id = pos.details.get('oco_order_list_id')
                        oco_sl = float(pos.details.get('oco_stop_loss', 0.0) or 0.0)
                        stop_changed = (pos.stop_loss > prev_sl) or (
                            oco_id is not None and oco_sl > 0 and abs(pos.stop_loss - oco_sl) > 1e-9
                        )
                        if stop_changed and (oco_id is not None or pos.details.get('oco_needs_sync')):
                            pos.details['oco_needs_sync'] = True
                        if pos.details.get('oco_needs_sync'):
                            self._sync_oco_stop(symbol, pos, curr_price)
            except Exception as e:
                log.error(f"[MONITOR SYMBOL ERROR] {symbol}: {e}", exc_info=True)

    def _record_or_rollback_oco(self, symbol: str, pos, oco, stop_loss_px: float, tag: str = "OCO") -> bool:
        """Persist a newly created OCO orderListId; if persistence fails, cancel the exchange OCO
        so an untracked protective order is never left silently orphaned."""
        lid = oco.get('orderListId') if oco else None
        if lid is None:
            pos.details.pop('oco_order_list_id', None)
            pos.details.pop('oco_stop_loss', None)
            pos.details['oco_needs_sync'] = True
            self.risk_mgr.persist_positions()
            return False

        pos.details['oco_order_list_id'] = lid
        pos.details['oco_stop_loss'] = stop_loss_px
        pos.details['oco_needs_sync'] = False
        if self.risk_mgr.persist_positions():
            return True

        log.error(
            f"[{tag} PERSIST ERROR] Failed saving OCO orderListId={lid} for {symbol}; "
            f"cancelling exchange OCO to avoid untracked protective order."
        )
        try:
            cancelled = self.exchange.cancel_open_orders(symbol)
        except Exception as exc:
            log.warning(f"[{tag} ROLLBACK CANCEL ERROR] {symbol}: {exc}")
            cancelled = False

        if cancelled is not False:
            pos.details.pop('oco_order_list_id', None)
            pos.details.pop('oco_stop_loss', None)
            pos.details['oco_needs_sync'] = True
            self.risk_mgr.persist_positions()
            log.warning(f"[{tag} ROLLBACK] Cancelled unpersisted OCO {lid} for {symbol}; will re-sync on next tick.")
        else:
            pos.details['oco_order_list_id'] = lid
            pos.details['oco_stop_loss'] = stop_loss_px
            pos.details['oco_needs_sync'] = True
            log.error(
                f"[{tag} ROLLBACK FAILED] Could not cancel unpersisted OCO {lid} for {symbol}; "
                f"retaining in-memory oco_order_list_id and oco_needs_sync."
            )
            self._alert_throttled(
                f"oco_rollback_fail:{symbol}",
                f"🚨 Critical: {symbol} failed both persisting OCO {lid} and cancelling it; manual verification advised.",
            )
        return False

    def _sync_oco_stop(self, symbol, pos, curr_price):
        """Re-submit exchange-side OCO when local stop_loss moves up (e.g. Break-Even), retrying across ticks until confirmed."""
        if curr_price <= pos.stop_loss * 1.002 or curr_price >= pos.take_profit * 0.998:
            pos.details['oco_needs_sync'] = True
            self.risk_mgr.persist_positions()
            log.info(f"[OCO SYNC DEFERRED] {symbol} price {curr_price:.6f} too close to SL/TP; will retry on next monitor tick")
            return
        try:
            old_oco_id = pos.details.get('oco_order_list_id')
            if old_oco_id is not None:
                cancelled = self.exchange.cancel_open_orders(symbol)
                if cancelled is False:
                    pos.details['oco_needs_sync'] = True
                    self.risk_mgr.persist_positions()
                    log.warning(f"[OCO SYNC] {symbol} cancellation failed; keeping existing OCO id and retrying next tick")
                    return
                oco_check = self._check_oco_before_replacing_or_selling(symbol, pos, old_oco_id, tag="OCO SYNC")
                if oco_check in ('DEFER', 'FULL_CLOSED', 'FULL_DEFERRED', 'PARTIAL_DEFERRED'):
                    return
                if oco_check == 'CLEAR':
                    pos.details.pop('oco_order_list_id', None)
                    pos.details.pop('oco_stop_loss', None)
                    pos.details['oco_needs_sync'] = True
                    if not self.risk_mgr.persist_positions():
                        log.warning(f"[OCO SYNC] {symbol} failed persisting cleared OCO state after cancel; deferring replacement")
                        return
            elif hasattr(self.exchange, 'get_open_orders_strict'):
                existing_open = self.exchange.get_open_orders_strict(symbol)
                if existing_open is None:
                    pos.details['oco_needs_sync'] = True
                    self.risk_mgr.persist_positions()
                    log.warning(f"[OCO SYNC DEFERRED] Could not verify open orders for {symbol}; deferring OCO placement")
                    return
                if existing_open:
                    cancelled = self.exchange.cancel_open_orders(symbol)
                    if cancelled is False:
                        for ord_obj in existing_open:
                            ol_id = (ord_obj or {}).get('orderListId')
                            if ol_id not in (None, -1):
                                pos.details['oco_order_list_id'] = ol_id
                                break
                        pos.details['oco_needs_sync'] = True
                        self.risk_mgr.persist_positions()
                        log.warning(f"[OCO SYNC] Untracked open orders exist for {symbol} and cancel failed; avoiding duplicate OCO")
                        return

            actual_qty = self.exchange.get_asset_balance_fresh(symbol)
            if actual_qty <= 0:
                pos.details['oco_needs_sync'] = True
                self.risk_mgr.persist_positions()
                log.warning(f"[OCO SYNC] {symbol} zero free balance ({actual_qty}) for {pos.quantity}; deferring OCO placement")
                return
            oco_qty = min(actual_qty, pos.quantity)
            oco = self.exchange.create_oco_sell(symbol, oco_qty, pos.take_profit, pos.stop_loss)
            if oco is None and getattr(self.exchange, 'last_oco_filter_rejected', False):
                pos.details.pop('oco_order_list_id', None)
                pos.details.pop('oco_stop_loss', None)
                pos.details['oco_needs_sync'] = False
                pos.details['oco_filter_blocked_qty'] = round(oco_qty, 8)
                self.risk_mgr.persist_positions()
                log.warning(
                    f"[OCO SYNC SKIPPED] {symbol} qty={oco_qty} rejected by exchange filters (-1013/minQty); "
                    f"disabling OCO re-sync loop and relying on client-side monitor."
                )
            elif self._record_or_rollback_oco(symbol, pos, oco, pos.stop_loss, tag="OCO SYNC"):
                log.info(f"[OCO SYNC] {symbol} exchange OCO stop updated to {pos.stop_loss:.6f}")
            elif not oco or oco.get('orderListId') is None:
                log.warning(f"[OCO SYNC] {symbol} OCO replacement returned None; will retry on next monitor tick")
        except Exception as e:
            pos.details['oco_needs_sync'] = True
            self.risk_mgr.persist_positions()
            log.warning(f"[OCO SYNC ERROR] {symbol}: {e}")

    def _reconcile_pending_sell(self, symbol: str, pos, curr_price: float, default_reason: str = "PENDING_SELL") -> str:
        """Reconcile a persisted unresolved sell clientOrderId before ever placing a second market_sell.

        Returns:
          - 'FINALIZED': filled (partially or fully) and accounted for; caller must stop processing this exit
          - 'BLOCKED': still non-terminal or unverified on Binance; caller must NOT place another sell
          - 'CLEARED': confirmed dead (0 fill terminal or -2013 NOT_FOUND); safe to submit a fresh exit
        """
        oid = pos.details.get('pending_sell_client_oid')
        if not oid:
            return 'CLEARED'
        if not hasattr(self.exchange, 'reconcile_client_order_strict'):
            return 'CLEARED'

        sell_reason = pos.details.get('pending_sell_reason') or default_reason
        state, order = self.exchange.reconcile_client_order_strict(symbol, oid)

        if state == 'NOT_FOUND':
            pos.details.pop('pending_sell_client_oid', None)
            pos.details.pop('pending_sell_reason', None)
            pos.details.pop('pending_sell_qty', None)
            self.risk_mgr.persist_positions()
            return 'CLEARED'

        if state != 'FOUND' or not order:
            log.warning(
                f"[PENDING SELL UNVERIFIED] {symbol} {oid} could not be confirmed ({state}); "
                f"retaining pending lock and refusing duplicate sell."
            )
            return 'BLOCKED'

        if not self._order_is_terminal(order):
            log.warning(
                f"[PENDING SELL OPEN] {symbol} {oid} still non-terminal "
                f"(status={order.get('status')}, executedQty={order.get('executedQty', 0)}); "
                f"refusing duplicate sell."
            )
            return 'BLOCKED'

        exec_qty = self._extract_order_exec_qty(order)
        if exec_qty <= 0:
            pos.details.pop('pending_sell_client_oid', None)
            pos.details.pop('pending_sell_reason', None)
            pos.details.pop('pending_sell_qty', None)
            if pos.details.get('oco_order_list_id') is None and getattr(cfg, 'USE_EXCHANGE_OCO', True):
                pos.details['oco_needs_sync'] = True
            self.risk_mgr.persist_positions()
            log.info(f"[PENDING SELL] {symbol} {oid} terminated with 0 executed qty; cleared lock")
            return 'CLEARED'

        had_oco_cleared = pos.details.get('oco_order_list_id') is None
        self._finalize_sell_order(symbol, pos, order, exec_qty, curr_price, sell_reason, had_oco=had_oco_cleared)
        return 'FINALIZED'

    def _finalize_sell_order(self, symbol: str, pos, order: dict, exec_qty: float, curr_price: float, reason: str, had_oco: bool = False):
        exit_price = self.exchange.avg_fill_price(order) or curr_price
        sell_oid = order.get('clientOrderId') or pos.details.get('pending_sell_client_oid')
        order_id = order.get('orderId')
        fill_id = str(sell_oid or (f"ORD:{order_id}" if order_id is not None else "")) or None

        # Reconcile partial fills: keep remaining position open when it is still sellable on Binance
        remaining = pos.quantity - exec_qty
        if exec_qty < pos.quantity * 0.99 and remaining > 1e-8:
            ref_px = min(exit_price, pos.stop_loss) if pos.stop_loss > 0 else exit_price
            sellable_rem = (
                self.exchange.is_sellable_qty(symbol, remaining, ref_px)
                if hasattr(self.exchange, 'is_sellable_qty')
                else True
            )
            if sellable_rem:
                if not self.risk_mgr.partial_close_position(
                    symbol, exec_qty, exit_price, reason, clear_oco=had_oco, fill_id=fill_id
                ):
                    log.error(
                        f"[PARTIAL CLOSE DEFERRED] Failed to persist partial fill for {symbol} "
                        f"(fill_id={fill_id}); retaining recoverable order record."
                    )
                    self._alert_throttled(
                        f"partial_close_defer:{symbol}",
                        f"⚠️ Failed persisting partial sell close for {symbol}; retaining state for retry.",
                    )
                return
            log.info(
                f"[DUST REMAINDER] {symbol}: sold {exec_qty} / {pos.quantity}; "
                f"remainder {remaining:.8f} below exchange minQty/minNotional — finalizing close."
            )

        # Full fill (or dust remainder): compute net realized P&L (after estimated round-trip fees)
        fees_pct = float(getattr(cfg, 'ROUND_TRIP_FEE_PCT', 0.2) or 0.0)
        gross_pnl_pct = pos.pnl_pct(exit_price)
        gross_pnl_usdt = (exit_price - pos.entry_price) * exec_qty
        est_fee_usdt = (pos.entry_price * exec_qty + exit_price * exec_qty) * (fees_pct / 200.0)
        pnl_pct = gross_pnl_pct - fees_pct
        pnl_usdt = gross_pnl_usdt - est_fee_usdt

        tx_ms = order.get('transactTime') or order.get('updateTime') or order.get('time')
        close_ts = (float(tx_ms) / 1000.0) if tx_ms else time.time()
        dur_sec = max(0.0, close_ts - pos.open_time)
        if not self.risk_mgr.close_position(
            symbol, exit_price, reason, fill_id=fill_id, executed_qty=exec_qty
        ):
            log.error(
                f"[CLOSE DEFERRED] Failed to persist sell close for {symbol} "
                f"(fill_id={fill_id}); retaining recoverable position lock."
            )
            self._alert_throttled(
                f"close_defer_persist:{symbol}",
                f"⚠️ Failed persisting close for {symbol}; retaining state for retry.",
            )
            return

        with self._state_lock:
            self._active_symbols.discard(symbol)

        cooldown_sec = self._record_cooldown(symbol, reason, net_pnl_pct=pnl_pct)
        log.info(f"[COOLDOWN SET] {symbol} placed on {cooldown_sec // 60}m cooldown after trade close ({reason})")

        self.notifier.trade_closed(
            symbol, reason, pos.entry_price, exit_price, pnl_pct,
            duration_sec=dur_sec, pnl_usdt=pnl_usdt, open_time=pos.open_time, close_time=close_ts
        )

        # Save chart snapshot on exit (fetch 100 bars so 50-bar chart has warmed-up indicators)
        try:
            tf = (pos.details or {}).get('timeframe', getattr(cfg, 'SCANNER_INTERVAL', '15m'))
            df = self.exchange.get_candles(symbol, interval=tf, limit=100)
            chart_path = save_trade_chart(symbol, df, pos.entry_price, exit_price, pnl_pct, pnl_usdt, reason, pos.stop_loss, pos.take_profit)
            if chart_path and hasattr(self.notifier, 'send_photo'):
                self.notifier.send_photo(
                    chart_path,
                    caption=f"📊 {symbol} — {reason} ({pnl_pct:+.2f}% | {pnl_usdt:+.2f} USDT)"
                )
        except Exception as e:
            log.warning(f"[CHART LOGGER] Exit chart failed: {e}")

    def _close_position(self, symbol, pos, curr_price, reason):
        if not self.risk_mgr.start_exit(symbol):
            return

        try:
            if pos.details.get('pending_sell_client_oid'):
                rec_state = self._reconcile_pending_sell(symbol, pos, curr_price, default_reason=reason)
                if rec_state in ('FINALIZED', 'BLOCKED'):
                    self.risk_mgr.complete_exit(symbol)
                    return

            # Cancel open orders (OCO) on Binance so balance is unlocked for selling.
            # NEVER proceed with market_sell if OCO cancellation failed or if the OCO already filled (-2011 race).
            old_oco_id = pos.details.get('oco_order_list_id')
            had_oco = old_oco_id is not None
            if had_oco:
                cancelled = self.exchange.cancel_open_orders(symbol)
                if cancelled is False:
                    self.risk_mgr.persist_positions()
                    log.warning(
                        f"[CLOSE DEFERRED] Failed to cancel open OCO for {symbol}; "
                        f"preserving oco_order_list_id and deferring market sell until next tick."
                    )
                    self._alert_throttled(
                        f"close_defer_oco_cancel:{symbol}",
                        f"⚠️ Failed cancelling open OCO for {symbol}; close deferred until next tick.",
                    )
                    self.risk_mgr.complete_exit(symbol)
                    return
                oco_check = self._check_oco_before_replacing_or_selling(symbol, pos, old_oco_id, tag="CLOSE OCO CHECK")
                if oco_check in ('DEFER', 'FULL_CLOSED', 'FULL_DEFERRED', 'PARTIAL_DEFERRED'):
                    self.risk_mgr.complete_exit(symbol)
                    return
                if oco_check == 'CLEAR':
                    pos.details.pop('oco_order_list_id', None)
                    pos.details.pop('oco_stop_loss', None)

            actual_qty = self.exchange.get_asset_balance_fresh(symbol)
            if actual_qty <= 0:
                total_qty = self.exchange.get_asset_total(symbol) if hasattr(self.exchange, 'get_asset_total') else 0.0
                if had_oco and total_qty > 0 and getattr(cfg, 'USE_EXCHANGE_OCO', True):
                    pos.details['oco_needs_sync'] = True
                self.risk_mgr.persist_positions()
                log.warning(
                    f"[CLOSE DEFERRED] {symbol} has zero free balance (free={actual_qty}, total={total_qty}); "
                    f"refusing phantom/duplicate market sell."
                )
                self._alert_throttled(
                    f"close_zero_bal:{symbol}",
                    f"⚠️ {symbol} exit deferred: free balance={actual_qty}, total={total_qty}.",
                )
                self.risk_mgr.complete_exit(symbol)
                return

            # Sell the POSITION, never more: free balance may include unrelated holdings of the same coin
            sell_qty = min(actual_qty, pos.quantity)

            sell_oid = (
                self.exchange.new_client_order_id(symbol, 'SELL')
                if hasattr(self.exchange, 'new_client_order_id')
                else None
            )
            if sell_oid:
                pos.details['pending_sell_client_oid'] = sell_oid
                pos.details['pending_sell_reason'] = reason
                pos.details['pending_sell_qty'] = sell_qty
            if not self.risk_mgr.persist_positions():
                if sell_oid:
                    pos.details.pop('pending_sell_client_oid', None)
                    pos.details.pop('pending_sell_reason', None)
                    pos.details.pop('pending_sell_qty', None)
                if had_oco:
                    pos.details['oco_needs_sync'] = True
                log.error(
                    f"[CLOSE ABORTED] Failed to persist positions state before market sell for {symbol}; "
                    f"refusing unrecorded sell."
                )
                self._alert_throttled(
                    f"close_abort_persist:{symbol}",
                    f"⚠️ Failed persisting positions.json before exit for {symbol}; market sell aborted.",
                )
                self.risk_mgr.complete_exit(symbol)
                return

            order = self._call_market_sell(symbol, sell_qty, client_oid=sell_oid)

            if not order:
                if (
                    sell_oid
                    and not getattr(self.exchange, 'last_order_confirmed_unsent', False)
                    and hasattr(self.exchange, 'reconcile_client_order_strict')
                ):
                    state, rec = self.exchange.reconcile_client_order_strict(symbol, sell_oid)
                    if state == 'NOT_FOUND':
                        pos.details.pop('pending_sell_client_oid', None)
                        pos.details.pop('pending_sell_reason', None)
                        pos.details.pop('pending_sell_qty', None)
                    elif state == 'FOUND' and rec is not None:
                        order = rec
                    else:
                        if had_oco:
                            pos.details['oco_needs_sync'] = True
                        self.risk_mgr.persist_positions()
                        log.warning(
                            f"[CLOSE UNCERTAIN] Market sell {sell_oid} status unverified for {symbol}; "
                            f"retaining pending_sell_client_oid to reconcile before any retry."
                        )
                        self._alert_throttled(
                            f"close_uncertain:{symbol}",
                            f"⚠️ Market sell {sell_oid} for {symbol} is unverified; holding pending sell lock.",
                        )
                        self.risk_mgr.complete_exit(symbol)
                        return
                else:
                    if sell_oid:
                        pos.details.pop('pending_sell_client_oid', None)
                        pos.details.pop('pending_sell_reason', None)
                        pos.details.pop('pending_sell_qty', None)

            if order and not self._order_is_terminal(order):
                if had_oco:
                    pos.details['oco_needs_sync'] = True
                self.risk_mgr.persist_positions()
                log.warning(
                    f"[CLOSE UNRESOLVED] Sell order {sell_oid} for {symbol} is non-terminal "
                    f"(status={order.get('status')}, executedQty={order.get('executedQty', 0)}); "
                    f"keeping pending_sell_client_oid and waiting for terminal reconciliation."
                )
                self.risk_mgr.complete_exit(symbol)
                return

            exec_qty = self._extract_order_exec_qty(order)
            if not order or exec_qty <= 0:
                if sell_oid:
                    pos.details.pop('pending_sell_client_oid', None)
                    pos.details.pop('pending_sell_reason', None)
                    pos.details.pop('pending_sell_qty', None)
                if had_oco:
                    pos.details['oco_needs_sync'] = True
                self.risk_mgr.persist_positions()
                log.error(
                    f"[CLOSE ABORTED] Market sell failed or executed 0 qty for {symbol}; "
                    f"keeping position open for retry on next monitor tick."
                )
                self.risk_mgr.complete_exit(symbol)
                return

            self._finalize_sell_order(symbol, pos, order, exec_qty, curr_price, reason, had_oco=had_oco)

        except Exception as e:
            log.error(f"[CLOSE ERROR] Failed to close {symbol}: {e}")
            self.risk_mgr.complete_exit(symbol)

    def _save_scanner_state(self) -> bool:
        try:
            now = time.time()
            with self._state_lock:
                active_cooldowns = {
                    k: float(v) for k, v in self._symbol_cooldowns.items()
                    if float(v) > now
                }
                self._symbol_cooldowns = dict(active_cooldowns)
                data = {
                    'prev_high_scores': dict(self._prev_high_scores),
                    'curr_high_scores': dict(self._curr_high_scores),
                    'pending_buys': dict(self._pending_buys),
                    'symbol_cooldowns': active_cooldowns,
                    'heartbeat_sent_date': self._heartbeat_sent_date,
                    'timestamp': now,
                }
            tmp_path = f"scanner_state.json.{os.getpid()}.{threading.get_ident()}.tmp"
            try:
                with open(tmp_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f)
                os.replace(tmp_path, 'scanner_state.json')
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
            return True
        except Exception as e:
            log.error(f"Failed to save scanner state: {e}")
            return False

    def _load_scanner_state(self):
        try:
            if Path('scanner_state.json').exists():
                now = time.time()
                with open('scanner_state.json', 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    with self._state_lock:
                        self._prev_high_scores = data.get('prev_high_scores', {})
                        self._curr_high_scores = data.get('curr_high_scores', {})
                        self._pending_buys = data.get('pending_buys', {})
                        saved_hb = data.get('heartbeat_sent_date')
                        if saved_hb:
                            self._heartbeat_sent_date = str(saved_hb)
                        raw_cd = data.get('symbol_cooldowns', {}) or {}
                        self._symbol_cooldowns = {
                            k: float(v) for k, v in raw_cd.items()
                            if float(v) > now
                        }
        except Exception as e:
            log.error(f"Failed to load scanner state: {e}")


def print_banner():
    max_t = float(getattr(cfg, 'MAX_TRADE_USDT', 20.0) or 0.0)
    size_str = f"${max_t:.2f} USDT" if max_t > 0 else f"AUTO ({getattr(cfg, 'SCANNER_TRADE_PCT', 99)}% of USDT)"
    rbs_str = f"ON ({getattr(cfg, 'MAX_RISK_PER_TRADE_PCT', 2.5)}% max risk)" if getattr(cfg, 'USE_RISK_BASED_SIZING', False) else "OFF"
    lm_str = f"LIMIT_MAKER ({getattr(cfg, 'LIMIT_MAKER_WAIT_SEC', 6.0):.0f}s bid -> Market)" if getattr(cfg, 'USE_LIMIT_MAKER_ENTRY', False) else "MARKET"
    log.info("==============================================================")
    log.info("  CRYPTOBOT - Automated Spot Scanner & Trading Engine")
    log.info(f"  Testnet      : {getattr(cfg, 'USE_TESTNET', False)}")
    log.info(f"  Max Trade    : {size_str} | Risk-Sizing: {rbs_str}")
    log.info(f"  Entry Mode   : {lm_str} | Max Open: {getattr(cfg, 'MAX_OPEN_TRADES', 1)}")
    log.info(f"  Min 24h Vol  : ${float(getattr(cfg, 'MIN_SCAN_VOLUME_USDT', 20000000)):,.0f} | Min Age: {getattr(cfg, 'MIN_LISTING_AGE_DAYS', 60)}d")
    log.info(f"  Futures Gate : {'ON (Public OI + Funding)' if getattr(cfg, 'ENABLE_FUTURES_CONFIRMATION', True) else 'OFF'}")
    log.info(f"  BTC Filter   : {'ON' if getattr(cfg, 'ENABLE_BTC_REGIME_FILTER', True) else 'OFF'} | BNB Warn: {'ON' if getattr(cfg, 'WARN_ZERO_BNB', True) else 'OFF'}")
    log.info(f"  Daily / DD   : {getattr(cfg, 'MAX_DAILY_LOSS', 6.0)}% daily | {getattr(cfg, 'MAX_TOTAL_DRAWDOWN_PCT', 12.0)}% peak DD")
    log.info(f"  Stops / BE   : Max SL {getattr(cfg, 'SCANNER_MAX_STOP_LOSS_PCT', 4.8)}% | Min TP {getattr(cfg, 'MIN_TP_PCT', 2.0)}% | BE +{getattr(cfg, 'BREAK_EVEN_TRIGGER_PCT', 1.8)}%")
    log.info(f"  Cooldown     : {getattr(cfg, 'SYMBOL_COOLDOWN_SEC', 3600)}s normal / {getattr(cfg, 'COOLDOWN_AFTER_SL_SEC', 43200)}s after losing SL")
    log.info("==============================================================")

def main():
    print_banner()

    connector = BinanceConnector(cfg)
    risk_mgr = RiskManager(cfg)
    risk_mgr.set_exchange(connector)

    if '--reset-breaker' in sys.argv:
        pos_syms = risk_mgr.all_position_symbols()
        pos_qty = {s: risk_mgr.get_position(s).quantity for s in pos_syms if risk_mgr.get_position(s)}
        cur_eq = connector.get_portfolio_value(symbols=pos_syms, quantities=pos_qty)
        risk_mgr.reset_circuit_breaker(cur_eq if cur_eq > 0 else None)
        log.info(f"[CIRCUIT BREAKER RESET] Baselines reset to current equity (${cur_eq:.2f} USDT). Exiting.")
        return

    notifier = TelegramNotifier(cfg)

    engine = ScannerEngine(connector, risk_mgr, notifier)
    # Reconcile pending buys, pending sells, and offline OCO fills before cleaning stale ghost positions
    try:
        engine._reconcile_pending_buys()
        engine._reconcile_startup_exits()
    except Exception as e:
        log.warning(f"[STARTUP RECONCILE] Failed startup reconciliation pass: {e}")

    risk_mgr.validate_positions(connector)
    engine.sync_active_symbols()
    engine._check_bnb_reserve()

    engine.start()

    log.info("Bot running - Scanner Engine active. Press Ctrl+C to exit.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutting down bot...")
        engine.stop()
        log.info("Bot stopped.")

if __name__ == '__main__':
    main()
