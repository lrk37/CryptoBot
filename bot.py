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

        # F-10/F-11: Thread lock for shared mutable state
        self._state_lock = threading.Lock()
        self._active_symbols = set()
        self._symbol_cooldowns: dict[str, float] = {}

        self._prev_high_scores: dict[str, dict] = {}
        self._curr_high_scores: dict[str, dict] = {}

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
            return

        if self.risk_mgr.open_count() >= getattr(cfg, 'MAX_OPEN_TRADES', 1):
            log.info("[SCANNER ENGINE] Max open trades reached - skipping scan")
            return

        btc_status = self.scanner.check_btc_regime()

        # F-11: Copy active_symbols under lock for safe iteration
        with self._state_lock:
            active_copy = list(self._active_symbols)
        results = self.scanner.scan_market(active_symbols=active_copy)
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

            # High Score Override (>=80) fast-tracks entry
            if score >= high_thresh and getattr(cfg, 'ENABLE_HIGH_SCORE_OVERRIDE', True):
                log.info(f"[CONFIRMATION BYPASS] {symbol} score {score:.0f} >= {high_thresh:.0f} - fast-tracking entry!")
                self._execute_entry(res)
                break

            # Check BTC Regime if not high score
            if btc_status == "BEARISH":
                log.info(f"[SCANNER ENGINE] BTC regime BEARISH - skipping entry for {symbol} (score {score:.0f})")
                continue

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

        order = self.exchange.market_buy(symbol, usdt_amount=usdt_amount)
        if not order:
            log.error(f"[ENTRY FAILED] Market buy failed for {symbol}")
            return

        fill_price = self.exchange.avg_fill_price(order)
        if fill_price <= 0:
            fill_price = price

        exec_qty = self.exchange.net_executed_qty(order, symbol)  # net of commission paid in the coin
        if exec_qty <= 0:
            exec_qty = usdt_amount / fill_price

        # Extract breakdown scores
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
            'bid_ask_ratio': res.bid_ask_ratio
        }

        pos = self.risk_mgr.open_position(
            symbol=symbol,
            entry_price=fill_price,
            quantity=exec_qty,
            kind="scanner",
            atr_value=res.atr,
            entry_reason=res.reason,
            entry_score=res.score,
            details=scan_details
        )

        if pos:
            # F-11: Add to active symbols under lock
            with self._state_lock:
                self._active_symbols.add(symbol)
            self.notifier.trade_opened(symbol, "BUY", fill_price, exec_qty, pos.stop_loss, pos.take_profit)

            # Submit native exchange-side OCO stop/profit order
            if getattr(cfg, 'USE_EXCHANGE_OCO', True):
                try:
                    oco = self.exchange.create_oco_sell(symbol, exec_qty, pos.take_profit, pos.stop_loss)
                    if oco:
                        pos.details['oco_order_list_id'] = oco.get('orderListId')
                        self.risk_mgr.persist_positions()
                except Exception as e:
                    log.warning(f"[OCO EXCEPTION] Could not place OCO: {e}")
            
            # Save chart snapshot on entry
            try:
                tf = getattr(cfg, 'SCANNER_INTERVAL', '15m')
                df = self.exchange.get_candles(symbol, interval=tf, limit=50)
                save_trade_chart(symbol, df, fill_price, fill_price, 0.0, 0.0, "ENTRY", pos.stop_loss, pos.take_profit)
            except Exception as e:
                log.warning(f"[CHART LOGGER] Entry chart failed: {e}")

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
        symbols = self.risk_mgr.all_position_symbols()
        for symbol in symbols:
            pos = self.risk_mgr.get_position(symbol)
            if not pos or pos.kind != "scanner":
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
                if pos.details.get('oco_order_list_id') is not None:
                    open_orders = self.exchange.get_open_orders_strict(symbol)
                    if open_orders is not None and len(open_orders) == 0:
                        fill = self.exchange.get_last_sell_fill(symbol, int(pos.open_time * 1000) - 60_000)
                        if fill:
                            exit_px = fill['price']
                            reason = "TAKE_PROFIT" if fill['type'] in ('LIMIT_MAKER', 'LIMIT') else "STOP_LOSS"
                            log.info(f"[EXCHANGE FILL] {symbol} OCO leg filled ({reason}) at {exit_px:.6f}")
                            pnl_pct = pos.pnl_pct(exit_px)
                            self.risk_mgr.close_position(symbol, exit_px, f"OCO_{reason}")
                            with self._state_lock:
                                self._active_symbols.discard(symbol)
                                self._symbol_cooldowns[symbol] = time.time() + getattr(cfg, 'SYMBOL_COOLDOWN_SEC', 3600)
                            self.notifier.trade_closed(symbol, f"OCO_{reason}", pos.entry_price, exit_px, pnl_pct)
                            continue
                        log.warning(f"[OCO] {symbol}: OCO is no longer open but no filled SELL was found - "
                                    f"dropping the OCO link; client-side exits stay active")
                        pos.details.pop('oco_order_list_id', None)
                        self.risk_mgr.persist_positions()
            except Exception as e:
                log.warning(f"[MONITOR OCO CHECK] {symbol}: {e}")

            action = self.risk_mgr.evaluate(pos, curr_price)
            if action != "HOLD":
                log.info(f"[MONITOR EXIT TRIGGERED] {symbol} -> {action} at {curr_price:.6f}")
                self._close_position(symbol, pos, curr_price, action)

    def _close_position(self, symbol, pos, curr_price, reason):
        if not self.risk_mgr.start_exit(symbol):
            return

        try:
            # Cancel open orders (OCO) on Binance so balance is unlocked for selling
            self.exchange.cancel_open_orders(symbol)

            actual_qty = self.exchange.get_asset_balance_fresh(symbol)
            # Sell the POSITION, never more: free balance may include unrelated holdings of the same coin
            sell_qty = min(actual_qty, pos.quantity) if actual_qty > 0 else pos.quantity
            order = self.exchange.market_sell(symbol, sell_qty)

            exit_price = self.exchange.avg_fill_price(order) if order else curr_price
            pnl_pct = pos.pnl_pct(exit_price)
            pnl_usdt = pos.pnl_usdt(exit_price)

            self.risk_mgr.close_position(symbol, exit_price, reason)
            # F-11: Remove from active symbols under lock
            with self._state_lock:
                self._active_symbols.discard(symbol)

            # F-10: Set Symbol Cooldown under lock
            cooldown_sec = getattr(cfg, 'SYMBOL_COOLDOWN_SEC', 3600)
            with self._state_lock:
                self._symbol_cooldowns[symbol] = time.time() + cooldown_sec
            log.info(f"[COOLDOWN SET] {symbol} placed on 1h cooldown after trade close")

            self.notifier.trade_closed(symbol, reason, pos.entry_price, exit_price, pnl_pct)

            # Save chart snapshot on exit
            try:
                tf = getattr(cfg, 'SCANNER_INTERVAL', '15m')
                df = self.exchange.get_candles(symbol, interval=tf, limit=50)
                save_trade_chart(symbol, df, pos.entry_price, exit_price, pnl_pct, pnl_usdt, reason, pos.stop_loss, pos.take_profit)
            except Exception as e:
                log.warning(f"[CHART LOGGER] Exit chart failed: {e}")

        except Exception as e:
            log.error(f"[CLOSE ERROR] Failed to close {symbol}: {e}")
            self.risk_mgr.complete_exit(symbol)

    def _save_scanner_state(self):
        try:
            data = {
                'prev_high_scores': self._prev_high_scores,
                'curr_high_scores': self._curr_high_scores,
                'timestamp': time.time()
            }
            with open('scanner_state.json', 'w') as f:
                json.dump(data, f)
        except Exception as e:
            log.error(f"Failed to save scanner state: {e}")

    def _load_scanner_state(self):
        try:
            if Path('scanner_state.json').exists():
                with open('scanner_state.json', 'r') as f:
                    data = json.load(f)
                    self._prev_high_scores = data.get('prev_high_scores', {})
                    self._curr_high_scores = data.get('curr_high_scores', {})
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
