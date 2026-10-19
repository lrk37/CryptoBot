"""
sniper.py — New Listing Sniper
====================================
Updates: quoteOrderQty support, fee-safe sells, improved monitoring (disabled by default).
"""
import logging
import time
import threading

log = logging.getLogger(__name__)

class ListingSniper:
    def __init__(self, cfg, exchange, risk_mgr, notifier):
        self.cfg = cfg
        self.exchange = exchange
        self.risk_mgr = risk_mgr
        self.notifier = notifier
        self._known = set()
        self._running = False
        self._thread = None

    def start(self):
        log.info("[SNIPER] Initialising known symbols list...")
        try:
            self._known = set(self.exchange.get_all_usdt_symbols())
        except Exception as e:
            log.error(f"[SNIPER] Failed to initialize symbols: {e}")
            self._known = set()

        self._running = True
        self._thread = threading.Thread(target=self._loop, name="sniper", daemon=True)
        self._thread.start()
        log.info(f"[SNIPER] Watching {len(self._known)} symbols. Scan every {getattr(self.cfg, 'SNIPE_SCAN_INTERVAL', 30)}s")

    def stop(self):
        self._running = False

    def _loop(self):
        interval = getattr(self.cfg, 'SNIPE_SCAN_INTERVAL', 30)
        while self._running:
            try:
                self._scan()
            except Exception as e:
                log.error(f"[SNIPER] Loop error: {e}")
            for _ in range(interval):
                if not self._running:
                    break
                time.sleep(1)

    def _scan(self):
        try:
            current = set(self.exchange.get_all_usdt_symbols())
        except Exception as e:
            log.warning(f"[SNIPER] Could not fetch market symbols: {e}")
            return

        new_symbols = current - self._known
        blacklist = set(getattr(self.cfg, 'SNIPE_BLACKLIST', []))

        for symbol in new_symbols:
            if symbol in blacklist:
                log.info(f"[SNIPER] {symbol} is blacklisted - skipping.")
                continue

            log.info(f"[SNIPER] NEW LISTING DETECTED: {symbol}")
            self.notifier.alert(f"New listing detected: *{symbol}* - sniping in {self.cfg.SNIPE_BUY_DELAY_SEC}s!")
            t = threading.Thread(target=self._snipe, args=(symbol,), name=f"snipe-{symbol}", daemon=True)
            t.start()

        self._known = current

    def _snipe(self, symbol):
        delay = getattr(self.cfg, 'SNIPE_BUY_DELAY_SEC', 3)
        log.info(f"[SNIPER] Waiting {delay}s before buying {symbol}...")
        time.sleep(delay)

        usdt = self.exchange.get_usdt_balance()
        if self.risk_mgr.check_circuit_breaker(usdt):
            log.warning(f"[SNIPER] Circuit breaker active - skipping {symbol}.")
            return

        price = self.exchange.get_price(symbol)
        if price <= 0:
            log.warning(f"[SNIPER] Could not get price for {symbol}.")
            return

        max_trade = getattr(self.cfg, 'MAX_TRADE_USDT', 20.0)
        usdt_amount = min(usdt * 0.99, max_trade)

        if usdt_amount < getattr(self.cfg, 'MIN_NOTIONAL_USDT', 11.0):
            log.warning(f"[SNIPER] Not enough balance to snipe {symbol}.")
            return

        order = self.exchange.market_buy(symbol, usdt_amount=usdt_amount)
        if not order:
            return

        fill_price = self.exchange.avg_fill_price(order)
        fill_qty = float(order.get('executedQty', 0))
        if fill_qty <= 0:
            fill_qty = usdt_amount / fill_price

        pos = self.risk_mgr.open_position(symbol, fill_price, fill_qty, kind="snipe")
        if not pos:
            self.exchange.market_sell(symbol, fill_qty)
            return

        self.notifier.trade_opened(symbol, "SNIPE BUY", fill_price, fill_qty, pos.stop_loss, pos.take_profit)
        log.info(f"[SNIPER] Sniped {symbol} @ {fill_price:.6f} | SL={pos.stop_loss:.6f} TP={pos.take_profit:.6f}")
        self._monitor_snipe(symbol, pos)

    def _monitor_snipe(self, symbol, pos):
        timeout = getattr(self.cfg, 'SNIPE_SELL_AFTER_MIN', 60) * 60
        start_t = time.time()

        while self._running:
            try:
                curr_price = self.exchange.get_price(symbol)
                action = self.risk_mgr.evaluate(pos, curr_price)
                if action != "HOLD":
                    self._close_snipe(symbol, pos, curr_price, action)
                    break

                if time.time() - start_t > timeout:
                    self._close_snipe(symbol, pos, curr_price, "CLOSE_TIME")
                    break
            except Exception as e:
                log.warning(f"[SNIPER MONITOR] Error checking {symbol}: {e}")

            time.sleep(10)

    def _close_snipe(self, symbol, pos, price, reason):
        # F-34: Use start_exit/complete_exit race guard
        if not self.risk_mgr.start_exit(symbol):
            return

        try:
            actual_qty = self.exchange.get_asset_balance_fresh(symbol)
            # F-14: Use actual balance as ground truth, fall back to recorded qty
            sell_qty = actual_qty if actual_qty > 0 else pos.quantity
            order = self.exchange.market_sell(symbol, sell_qty)

            fill = self.exchange.avg_fill_price(order) if order else price
            pnl = pos.pnl_pct(fill)

            self.notifier.trade_closed(symbol, reason, pos.entry_price, fill, pnl)
            self.risk_mgr.close_position(symbol, exit_price=fill, exit_reason=reason)
        except Exception as e:
            log.error(f"[SNIPER CLOSE ERROR] Failed to close {symbol}: {e}")
            self.risk_mgr.complete_exit(symbol)
