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
import threading
from datetime import datetime
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
        logging.FileHandler(getattr(cfg, 'LOG_FILE', 'bot.log'), encoding='utf-8')
    ]
)
log = logging.getLogger('MainThread')

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

        self._prev_high_scores: dict[str, dict] = {}
        self._curr_high_scores: dict[str, dict] = {}
        self._pending_buys: dict[str, dict] = {}
        self._in_flight_buys: set[str] = set()

        self._load_scanner_state()

    def start(self):
        self._running = True
        threading.Thread(target=self._scan_loop, name='scanner-scan', daemon=True).start()
        threading.Thread(target=self._monitor_loop, name='scanner-monitor', daemon=True).start()
        log.info("[SCANNER ENGINE] Started - scan + monitor threads running")

    def stop(self):
        self._running = False

    def _scan_loop(self):
        rescan_sec = getattr(cfg, 'SCANNER_RESCAN_INTERVAL', 300)
        while self._running:
            try:
                self._scan_tick()
            except Exception as e:
                log.error(f"[SCANNER ENGINE ERROR] {e}", exc_info=True)

            for _ in range(rescan_sec):
                if not self._running:
                    break
                time.sleep(1)

    def _scan_tick(self):
        if self.risk_mgr.is_loss_halted():
            return

        # F-07: Use portfolio value (including open positions) for circuit breaker
        pos_symbols = self.risk_mgr.all_position_symbols()
        pos_qty = {}
        for s in pos_symbols:
            p_ = self.risk_mgr.get_position(s)
            if p_:
                pos_qty[s] = p_.quantity
        portfolio_val = self.exchange.get_portfolio_value(symbols=pos_symbols, quantities=pos_qty)
        if self.risk_mgr.check_circuit_breaker(portfolio_val):
            self.notifier.alert("🛑 Circuit breaker tripped! Daily loss limit reached. Trading halted.")
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
        
        # Log candidates & check empirical 1h/4h forward returns (no capital risk)
        try:
            log_scan_candidates(results, self.exchange)
            update_forward_returns(self.exchange)
        except Exception as e:
            log.warning(f"[SCAN LOGGER] Forward tracking exception: {e}")

        if not results:
            log.info("[SCANNER ENGINE] No candidates passed scoring threshold")
            self._rotate_scores()
            return

        now = time.time()
        cooldown_sec = getattr(cfg, 'SYMBOL_COOLDOWN_SEC', 3600)
        high_thresh = getattr(cfg, 'HIGH_SCORE_THRESHOLD', 80)
        require_confirm = getattr(cfg, 'REQUIRE_CONFIRMATION', True)

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

            # 1. Enforce BTC Market Regime: in BEARISH regime, only allow Extreme Capitulation (>= high_thresh)
            if btc_status == "BEARISH" and not (
                getattr(cfg, 'STRATEGY_MODE', 'capitulation') == 'capitulation' and score >= high_thresh
            ):
                log.info(f"[SCANNER ENGINE] BTC regime BEARISH - skipping entry for {symbol} (score {score:.0f})")
                continue

            # 2. High Score Override (>=80) fast-tracks entry (only in Bullish BTC regime)
            if score >= high_thresh and getattr(cfg, 'ENABLE_HIGH_SCORE_OVERRIDE', True):
                log.info(f"[CONFIRMATION BYPASS] {symbol} score {score:.0f} >= {high_thresh:.0f} - fast-tracking entry!")
                self._execute_entry(res)
                break

            # 2-Scan Candle-Based Confirmation System
            if require_confirm:
                self._curr_high_scores[symbol] = {'score': score, 'last_closed_time': closed_time}

                if symbol in self._prev_high_scores:
                    prev_info = self._prev_high_scores[symbol]
                    prev_score = prev_info.get('score', 0)
                    prev_time = prev_info.get('last_closed_time', 0)

                    if closed_time > prev_time:
                        log.info(f"[CONFIRMATION CONFIRMED] {symbol} scored high on distinct 15m candles ({prev_score:.0f} -> {score:.0f}) - executing!")
                        self._execute_entry(res)
                        break
                    else:
                        log.info(f"[CONFIRMATION WAIT] {symbol} same 15m candle as previous scan ({closed_time}) - waiting for next closed candle")
                else:
                    log.info(f"[CONFIRMATION WAIT] {symbol} score {score:.0f} >= 60 - waiting for 2nd scan confirmation")
            else:
                self._execute_entry(res)
                break

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

    def _call_market_buy(self, symbol: str, usdt_amount: float = 0.0, client_oid: str = None):
        if client_oid is not None:
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
                    if oco and oco.get('orderListId') is not None:
                        target_pos.details['oco_order_list_id'] = oco.get('orderListId')
                        target_pos.details['oco_stop_loss'] = target_pos.stop_loss
                        target_pos.details['oco_needs_sync'] = False
                        self.risk_mgr.persist_positions()
                    else:
                        target_pos.details['oco_needs_sync'] = True
                        self.risk_mgr.persist_positions()
                except Exception as e:
                    target_pos.details['oco_needs_sync'] = True
                    self.risk_mgr.persist_positions()
                    log.warning(f"[OCO EXCEPTION] Could not place OCO: {e}")

            # Save chart snapshot on entry (fetch 100 bars so 50-bar chart has warmed-up indicators)
            try:
                df = self.exchange.get_candles(symbol, interval=tf, limit=100)
                save_trade_chart(
                    symbol, df, fill_price, fill_price, 0.0, 0.0,
                    "ENTRY", target_pos.stop_loss, target_pos.take_profit
                )
            except Exception as e:
                log.warning(f"[CHART LOGGER] Entry chart failed: {e}")
        return True

    def _execute_entry(self, res: ScanResult):
        symbol = res.symbol
        price = res.close_price

        bal = self.exchange.get_usdt_balance()
        max_usdt = getattr(cfg, 'MAX_TRADE_USDT', 20.0)
        # F-18: Use SCANNER_TRADE_PCT config instead of hardcoded 0.99
        trade_pct = getattr(cfg, 'SCANNER_TRADE_PCT', 99) / 100.0
        usable_bal = bal * trade_pct if bal < max_usdt else max_usdt
        usdt_amount = min(usable_bal, max_usdt)

        if usdt_amount < getattr(cfg, 'MIN_NOTIONAL_USDT', 11.0):
            log.warning(f"[ENTRY FAILED] Insufficient balance for {symbol}: ${usdt_amount:.2f}")
            return

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
                    return
                if self.risk_mgr.has_position(symbol):
                    log.warning(f"[ENTRY DEFERRED] Position already tracked for {symbol}")
                    return
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
                        return

        try:
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
                            return
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
                            return
                    else:
                        if buy_oid:
                            self._clear_pending_buy(symbol)
                        log.error(f"[ENTRY FAILED] Market buy failed for {symbol}")
                        return

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
                    return

                self._finalize_buy_order(symbol, order, meta, clear_pending=bool(buy_oid))
        finally:
            if buy_oid:
                with self._order_reconcile_lock:
                    with self._state_lock:
                        self._in_flight_buys.discard(symbol)
                        if symbol in self._pending_buys and self._pending_buys[symbol].get('state') == 'SUBMITTING':
                            self._pending_buys[symbol]['state'] = 'PENDING'
                            self._save_scanner_state()

    def _rotate_scores(self):
        self._prev_high_scores = self._curr_high_scores.copy()
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

    def _monitor_tick(self):
        self._reconcile_pending_buys()

        symbols = self.risk_mgr.all_position_symbols()
        for symbol in symbols:
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

            # Did the exchange-side OCO already close this position?
            # Decided from ORDER state, not balances: coins locked in an OCO are not 'free', and
            # unrelated pre-existing holdings of the same coin would confuse a balance check.
            try:
                oco_id = pos.details.get('oco_order_list_id')
                if oco_id is not None:
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
                        fill_qty = (
                            float(fill['qty'])
                            if fill and 'qty' in fill and fill['qty'] is not None
                            else (pos.quantity if fill else 0.0)
                        )
                        if fill and fill_qty > 0:
                            exit_px = float(fill['price'])
                            fill_ts = fill.get('time') or time.time()
                            reason = "TAKE_PROFIT" if fill.get('type') in ('LIMIT_MAKER', 'LIMIT') else "STOP_LOSS"
                            remaining = pos.quantity - fill_qty
                            if fill_qty < pos.quantity * 0.99 and remaining > 1e-8:
                                sellable_rem = (
                                    self.exchange.is_sellable_qty(symbol, remaining, exit_px)
                                    if hasattr(self.exchange, 'is_sellable_qty')
                                    else True
                                )
                                if sellable_rem:
                                    log.info(
                                        f"[EXCHANGE PARTIAL FILL] {symbol} OCO leg partially filled "
                                        f"{fill_qty} / {pos.quantity} ({reason}) at {exit_px:.6f}; "
                                        f"retaining remaining {remaining:.8f}"
                                    )
                                    pos.details['oco_needs_sync'] = True
                                    self.risk_mgr.partial_close_position(
                                        symbol, fill_qty, exit_px, f"OCO_{reason}", clear_oco=True
                                    )
                                    continue
                                log.info(
                                    f"[DUST REMAINDER] {symbol} OCO filled {fill_qty} / {pos.quantity}; "
                                    f"remainder {remaining:.8f} below exchange minQty/minNotional — finalizing close."
                                )

                            log.info(f"[EXCHANGE FILL] {symbol} OCO leg filled ({reason}) qty={fill_qty} at {exit_px:.6f}")
                            pos.quantity = fill_qty
                            pnl_pct = pos.pnl_pct(exit_px)
                            pnl_usdt = pos.pnl_usdt(exit_px)
                            dur_sec = max(0.0, fill_ts - pos.open_time)
                            self.risk_mgr.close_position(symbol, exit_px, f"OCO_{reason}")
                            with self._state_lock:
                                self._active_symbols.discard(symbol)
                                cd_sec = getattr(cfg, 'SYMBOL_COOLDOWN_SEC', 3600)
                                if reason == "STOP_LOSS" and getattr(cfg, 'STRATEGY_MODE', 'capitulation') == 'capitulation':
                                    cd_sec = max(cd_sec, 43200)
                                self._symbol_cooldowns[symbol] = time.time() + cd_sec
                            self.notifier.trade_closed(
                                symbol, f"OCO_{reason}", pos.entry_price, exit_px, pnl_pct,
                                duration_sec=dur_sec, pnl_usdt=pnl_usdt, open_time=pos.open_time, close_time=fill_ts
                            )
                            continue
                        log.warning(f"[OCO] {symbol}: OCO is no longer open but no filled SELL was found - "
                                    f"dropping the OCO link; client-side exits stay active")
                        pos.details.pop('oco_order_list_id', None)
                        pos.details.pop('oco_stop_loss', None)
                        self.risk_mgr.persist_positions()
            except Exception as e:
                log.warning(f"[MONITOR OCO CHECK] {symbol}: {e}")

            prev_sl = pos.stop_loss
            action = self.risk_mgr.evaluate(pos, curr_price)
            if action != "HOLD":
                log.info(f"[MONITOR EXIT TRIGGERED] {symbol} -> {action} at {curr_price:.6f}")
                self._close_position(symbol, pos, curr_price, action)
            elif getattr(cfg, 'USE_EXCHANGE_OCO', True):
                oco_id = pos.details.get('oco_order_list_id')
                oco_sl = float(pos.details.get('oco_stop_loss', 0.0) or 0.0)
                stop_changed = (pos.stop_loss > prev_sl) or (
                    oco_id is not None and oco_sl > 0 and abs(pos.stop_loss - oco_sl) > 1e-9
                )
                if stop_changed and (oco_id is not None or pos.details.get('oco_needs_sync')):
                    pos.details['oco_needs_sync'] = True
                if pos.details.get('oco_needs_sync'):
                    self._sync_oco_stop(symbol, pos, curr_price)

    def _sync_oco_stop(self, symbol, pos, curr_price):
        """Re-submit exchange-side OCO when local stop_loss moves up (e.g. Break-Even), retrying across ticks until confirmed."""
        if curr_price <= pos.stop_loss * 1.002 or curr_price >= pos.take_profit * 0.998:
            pos.details['oco_needs_sync'] = True
            self.risk_mgr.persist_positions()
            log.info(f"[OCO SYNC DEFERRED] {symbol} price {curr_price:.6f} too close to SL/TP; will retry on next monitor tick")
            return
        try:
            if pos.details.get('oco_order_list_id') is not None:
                cancelled = self.exchange.cancel_open_orders(symbol)
                if cancelled is False:
                    pos.details['oco_needs_sync'] = True
                    self.risk_mgr.persist_positions()
                    log.warning(f"[OCO SYNC] {symbol} cancellation failed; keeping existing OCO id and retrying next tick")
                    return
                pos.details.pop('oco_order_list_id', None)
                pos.details['oco_needs_sync'] = True
                self.risk_mgr.persist_positions()

            actual_qty = self.exchange.get_asset_balance_fresh(symbol)
            oco_qty = min(actual_qty, pos.quantity) if actual_qty > 0 else pos.quantity
            oco = self.exchange.create_oco_sell(symbol, oco_qty, pos.take_profit, pos.stop_loss)
            if oco and oco.get('orderListId') is not None:
                pos.details['oco_order_list_id'] = oco.get('orderListId')
                pos.details['oco_stop_loss'] = pos.stop_loss
                pos.details['oco_needs_sync'] = False
                log.info(f"[OCO SYNC] {symbol} exchange OCO stop updated to {pos.stop_loss:.6f}")
            else:
                pos.details.pop('oco_order_list_id', None)
                pos.details['oco_needs_sync'] = True
                log.warning(f"[OCO SYNC] {symbol} OCO replacement returned None; will retry on next monitor tick")
            self.risk_mgr.persist_positions()
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

        # Reconcile partial fills: keep remaining position open when it is still sellable on Binance
        remaining = pos.quantity - exec_qty
        if exec_qty < pos.quantity * 0.99 and remaining > 1e-8:
            sellable_rem = (
                self.exchange.is_sellable_qty(symbol, remaining, exit_price)
                if hasattr(self.exchange, 'is_sellable_qty')
                else True
            )
            if sellable_rem:
                if had_oco:
                    pos.details['oco_needs_sync'] = True
                self.risk_mgr.partial_close_position(
                    symbol, exec_qty, exit_price, reason, clear_oco=had_oco
                )
                return
            log.info(
                f"[DUST REMAINDER] {symbol}: sold {exec_qty} / {pos.quantity}; "
                f"remainder {remaining:.8f} below exchange minQty/minNotional — finalizing close."
            )

        # Full fill (or dust remainder): compute realized P&L on actual executed quantity
        pos.quantity = exec_qty
        pnl_pct = pos.pnl_pct(exit_price)
        pnl_usdt = pos.pnl_usdt(exit_price)

        tx_ms = order.get('transactTime') or order.get('updateTime') or order.get('time')
        close_ts = (float(tx_ms) / 1000.0) if tx_ms else time.time()
        dur_sec = max(0.0, close_ts - pos.open_time)
        self.risk_mgr.close_position(symbol, exit_price, reason)
        with self._state_lock:
            self._active_symbols.discard(symbol)

        cooldown_sec = getattr(cfg, 'SYMBOL_COOLDOWN_SEC', 3600)
        if "STOP_LOSS" in str(reason) and getattr(cfg, 'STRATEGY_MODE', 'capitulation') == 'capitulation':
            cooldown_sec = max(cooldown_sec, 43200)
        with self._state_lock:
            self._symbol_cooldowns[symbol] = time.time() + cooldown_sec
        log.info(f"[COOLDOWN SET] {symbol} placed on {cooldown_sec // 60}m cooldown after trade close ({reason})")

        self.notifier.trade_closed(
            symbol, reason, pos.entry_price, exit_price, pnl_pct,
            duration_sec=dur_sec, pnl_usdt=pnl_usdt, open_time=pos.open_time, close_time=close_ts
        )

        # Save chart snapshot on exit (fetch 100 bars so 50-bar chart has warmed-up indicators)
        try:
            tf = (pos.details or {}).get('timeframe', getattr(cfg, 'SCANNER_INTERVAL', '15m'))
            df = self.exchange.get_candles(symbol, interval=tf, limit=100)
            save_trade_chart(symbol, df, pos.entry_price, exit_price, pnl_pct, pnl_usdt, reason, pos.stop_loss, pos.take_profit)
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
            # NEVER proceed with market_sell if OCO cancellation failed (OCO could still execute concurrently).
            had_oco = pos.details.get('oco_order_list_id') is not None
            if had_oco:
                cancelled = self.exchange.cancel_open_orders(symbol)
                if cancelled is False:
                    self.risk_mgr.persist_positions()
                    log.warning(
                        f"[CLOSE DEFERRED] Failed to cancel open OCO for {symbol}; "
                        f"preserving oco_order_list_id and deferring market sell until next tick."
                    )
                    self.risk_mgr.complete_exit(symbol)
                    return
                pos.details.pop('oco_order_list_id', None)
                pos.details.pop('oco_stop_loss', None)

            actual_qty = self.exchange.get_asset_balance_fresh(symbol)
            # Sell the POSITION, never more: free balance may include unrelated holdings of the same coin
            sell_qty = min(actual_qty, pos.quantity) if actual_qty > 0 else pos.quantity

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
            with self._state_lock:
                data = {
                    'prev_high_scores': dict(self._prev_high_scores),
                    'curr_high_scores': dict(self._curr_high_scores),
                    'pending_buys': dict(self._pending_buys),
                    'timestamp': time.time()
                }
            tmp_path = 'scanner_state.json.tmp'
            with open(tmp_path, 'w') as f:
                json.dump(data, f)
            os.replace(tmp_path, 'scanner_state.json')
            return True
        except Exception as e:
            log.error(f"Failed to save scanner state: {e}")
            return False

    def _load_scanner_state(self):
        try:
            if Path('scanner_state.json').exists():
                with open('scanner_state.json', 'r') as f:
                    data = json.load(f)
                    with self._state_lock:
                        self._prev_high_scores = data.get('prev_high_scores', {})
                        self._curr_high_scores = data.get('curr_high_scores', {})
                        self._pending_buys = data.get('pending_buys', {})
        except Exception as e:
            log.error(f"Failed to load scanner state: {e}")


def print_banner():
    log.info("==============================================================")
    log.info("  CRYPTOBOT - Automated Spot Scanner & Trading Engine")
    log.info(f"  Testnet      : {getattr(cfg, 'USE_TESTNET', False)}")
    log.info(f"  Scanner      : ON [ALL COINS]")
    log.info(f"  Top-N        : {getattr(cfg, 'SCANNER_TOP_N', 3)}")
    log.info(f"  Min Score    : {getattr(cfg, 'MIN_SCORE_TO_TRADE', 60)}")
    log.info(f"  Rescan       : {getattr(cfg, 'SCANNER_RESCAN_INTERVAL', 300)}s")
    log.info(f"  Timeframe    : {getattr(cfg, 'SCANNER_INTERVAL', '15m')}")
    log.info(f"  BTC Filter   : {'ON' if getattr(cfg, 'ENABLE_BTC_REGIME_FILTER', True) else 'OFF'}")
    log.info(f"  Confirmation : {'ON (2 closed 15m candles)' if getattr(cfg, 'REQUIRE_CONFIRMATION', True) else 'OFF'}")
    log.info(f"  Max trades   : {getattr(cfg, 'MAX_OPEN_TRADES', 1)}")
    log.info(f"  Daily SL     : {getattr(cfg, 'MAX_DAILY_LOSS', 6.0)}%")
    log.info(f"  Consec loss  : {getattr(cfg, 'MAX_CONSECUTIVE_LOSSES', 2)} -> {getattr(cfg, 'CONSECUTIVE_LOSS_COOLDOWN_SEC', 1800)}s pause")
    log.info(f"  ATR stops    : {getattr(cfg, 'SCANNER_USE_ATR_STOPS', True)}")
    log.info(f"  Trailing     : {getattr(cfg, 'SCANNER_TRAILING_OFFSET', 1.5)}% offset")
    log.info(f"  Cooldown     : {getattr(cfg, 'SYMBOL_COOLDOWN_SEC', 3600)}s per symbol after close")
    log.info("==============================================================")

def main():
    print_banner()

    connector = BinanceConnector(cfg)
    risk_mgr = RiskManager(cfg)
    risk_mgr.set_exchange(connector)
    notifier = TelegramNotifier(cfg)

    risk_mgr.validate_positions(connector)

    engine = ScannerEngine(connector, risk_mgr, notifier)
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
