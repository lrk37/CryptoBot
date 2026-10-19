"""
exchange.py — Binance Spot Connector
=========================================
v6 Improvements:
  - API exceptions propagation in verify_asset_holding to prevent startup cleanup deletions
  - quoteOrderQty support — send USDT amount instead of base qty (eliminates LOT_SIZE filter issues for micro accounts)
  - minQty / minNotional validation before placing orders
  - Cached symbol info for faster LOT_SIZE lookups
  - Portfolio value caching to reduce API calls
"""
import logging
import math
import time
import threading
import pandas as pd
import requests as req_lib
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceOrderException
from typing import Optional, Set, Dict

log = logging.getLogger(__name__)

NON_RETRYABLE_CODES = {-1002, -2015, -2014, -2008}

class BinanceConnector:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = Client(cfg.API_KEY, cfg.API_SECRET, testnet=cfg.USE_TESTNET)
        self.client.recvWindow = 10000

        try:
            server_time = self.client.get_server_time()
            local_time = int(time.time() * 1000)
            self.client.timestamp_offset = server_time['serverTime'] - local_time
            offset_ms = self.client.timestamp_offset
            log.info(f"Time offset synced: {offset_ms:+d} ms")
        except Exception as e:
            log.warning(f"Failed to sync server time: {e}")

        adapter = req_lib.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
        self.client.session.mount('https://', adapter)
        mode = 'TESTNET' if cfg.USE_TESTNET else 'LIVE'
        log.info(f"Binance connected [{mode}]")

        self._info_cache = {}
        self._balance_cache = {}
        self._balance_cache_time = 0
        self._balance_lock = threading.Lock()
        self._portfolio_cache = 0
        self._portfolio_cache_time = 0
        self._portfolio_lock = threading.Lock()

    def _retry(self, fn, *args, **kwargs):
        retries = getattr(self.cfg, 'MAX_RETRIES', 3)
        delay = getattr(self.cfg, 'RETRY_DELAY', 2)
        for i in range(retries):
            try:
                return fn(*args, **kwargs)
            except BinanceAPIException as e:
                if e.code in NON_RETRYABLE_CODES:
                    raise
                if i == retries - 1:
                    raise
                time.sleep(delay * (i + 1))
            except Exception as e:
                if i == retries - 1:
                    raise
                time.sleep(delay * (i + 1))

    def get_candles(self, symbol, interval, limit=100):
        raw = self._retry(self.client.get_klines, symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(raw, columns=[
            'open_time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_vol', 'trades', 'tb_base', 'tb_quote', 'ignore'
        ])
        df['open_time'] = pd.to_datetime(df['open_time'], unit='ms', utc=True)
        df.set_index('open_time', inplace=True)
        for c in ('open', 'high', 'low', 'close', 'volume', 'quote_vol'):
            df[c] = df[c].astype(float)
        return df[['open', 'high', 'low', 'close', 'volume', 'quote_vol']]

    def get_price(self, symbol):
        t = self._retry(self.client.get_symbol_ticker, symbol=symbol)
        return float(t['price'])

    def get_spread_pct(self, symbol):
        """Get bid-ask spread as percentage. Used to skip illiquid coins."""
        try:
            book = self._retry(self.client.get_order_book, symbol=symbol, limit=5)
            best_bid = float(book['bids'][0][0]) if book.get('bids') else 0
            best_ask = float(book['asks'][0][0]) if book.get('asks') else 0
            if best_ask <= 0 or best_bid <= 0:
                return 99.0
            return ((best_ask - best_bid) / best_ask) * 100.0
        except Exception as e:
            log.warning(f"Spread check failed for {symbol}: {e}")
            return 99.0

    def get_orderbook_imbalance(self, symbol, depth=10):
        """Calculate the ratio of Bid (Buy) volume to Ask (Sell) volume.
        > 1.0 means more buy pressure. < 1.0 means more sell pressure."""
        try:
            book = self._retry(self.client.get_order_book, symbol=symbol, limit=depth)
            bid_vol = sum(float(b[1]) for b in book.get('bids', []))
            ask_vol = sum(float(a[1]) for a in book.get('asks', []))
            if ask_vol <= 0:
                return 1.0
            return bid_vol / ask_vol
        except Exception as e:
            log.warning(f"Orderbook imbalance check failed for {symbol}: {e}")
            return 1.0

    def get_all_usdt_symbols(self):
        """Return all currently tradeable USDT spot pairs."""
        info = self._retry(self.client.get_exchange_info)
        symbols = []
        for s in info.get('symbols', []):
            if s.get('quoteAsset') == 'USDT' and s.get('status') == 'TRADING' and s.get('isSpotTradingAllowed', False):
                symbols.append(s['symbol'])
        return symbols

    def get_24h_tickers(self) -> Dict[str, dict]:
        """Fetch 24hr ticker data for ALL symbols in a single API call (cost: 40-80 weight)."""
        try:
            tickers = self._retry(self.client.get_ticker)
            if isinstance(tickers, list):
                return {t['symbol']: t for t in tickers if 'symbol' in t}
            elif isinstance(tickers, dict) and 'symbol' in tickers:
                return {tickers['symbol']: tickers}
            return {}
        except Exception as e:
            log.error(f"Failed to fetch 24h tickers batch: {e}")
            return {}

    def get_balance(self, asset):
        """Get balance for a specific asset (fresh, bypasses cache)."""
        acct = self._retry(self.client.get_account)
        for b in acct.get('balances', []):
            if b['asset'] == asset:
                return float(b['free'])
        return 0.0

    def get_usdt_balance(self):
        """Get cached USDT balance (refreshes every BALANCE_CACHE_TTL sec)."""
        return self._get_cached_balance('USDT')

    def get_asset_balance_fresh(self, symbol):
        """Get fresh balance for the base asset of a trading pair."""
        base_asset = symbol.removesuffix('USDT')
        return self.get_balance(base_asset)

    def get_portfolio_value(self, symbols=None):
        """Estimate total portfolio value in USDT."""
        now = time.time()
        with self._portfolio_lock:
            if now - self._portfolio_cache_time < 30 and self._portfolio_cache > 0:
                return self._portfolio_cache

        usdt = self.get_usdt_balance()
        val = usdt
        if symbols:
            for s in symbols:
                try:
                    qty = self.get_asset_balance_fresh(s)
                    if qty > 0:
                        px = self.get_price(s)
                        val += qty * px
                except Exception:
                    pass

        with self._portfolio_lock:
            self._portfolio_cache = val
            self._portfolio_cache_time = now
        return val

    def verify_asset_holding(self, symbol, expected_qty):
        """Check if we actually hold the expected asset quantity."""
        actual = self.get_asset_balance_fresh(symbol)
        return actual >= expected_qty * 0.9

    def invalidate_balance_cache(self):
        """Force refresh on next balance call (called after trades)."""
        with self._balance_lock:
            self._balance_cache_time = 0

    def _get_cached_balance(self, asset):
        now = time.time()
        with self._balance_lock:
            if now - self._balance_cache_time > getattr(self.cfg, 'BALANCE_CACHE_TTL', 10):
                self._refresh_balances()
            return self._balance_cache.get(asset, 0.0)

    def _refresh_balances(self):
        try:
            acct = self._retry(self.client.get_account)
            bal_map = {}
            for b in acct.get('balances', []):
                bal_map[b['asset']] = float(b['free'])
            self._balance_cache = bal_map
            self._balance_cache_time = time.time()
        except Exception as e:
            log.warning(f"Failed to refresh balances: {e}")

    def _get_symbol_info(self, symbol):
        if symbol not in self._info_cache:
            try:
                info = self._retry(self.client.get_symbol_info, symbol=symbol)
                if info:
                    self._info_cache[symbol] = info
            except Exception as e:
                log.warning(f"Failed to get symbol info for {symbol}: {e}")
                return None
        return self._info_cache.get(symbol)

    def _round_lot(self, symbol, quantity):
        info = self._get_symbol_info(symbol)
        if not info:
            return quantity
        step_size = None
        for f in info.get('filters', []):
            if f.get('filterType') == 'LOT_SIZE':
                step_size = float(f.get('stepSize', 0))
                break
        if step_size and step_size > 0:
            precision = int(round(-math.log10(step_size)))
            precision = max(0, min(precision, 8))  # Clamp to sane range
            if precision <= 0:
                return math.floor(quantity)
            return math.floor(quantity * (10 ** precision)) / (10 ** precision)
        return quantity

    def _round_price(self, symbol, price):
        """Round price according to PRICE_FILTER tickSize."""
        info = self._get_symbol_info(symbol)
        if not info or price <= 0:
            return price
        tick_size = None
        for f in info.get('filters', []):
            if f.get('filterType') == 'PRICE_FILTER':
                tick_size = float(f.get('tickSize', 0))
                break
        if tick_size and tick_size > 0:
            precision = int(round(-math.log10(tick_size)))
            precision = max(0, min(precision, 8))
            return round(price, precision)
        return price

    def _get_min_qty(self, symbol):
        info = self._get_symbol_info(symbol)
        if not info:
            return 0.0
        for f in info.get('filters', []):
            if f.get('filterType') == 'LOT_SIZE':
                return float(f.get('minQty', 0))
        return 0.0

    def _get_min_notional(self, symbol):
        info = self._get_symbol_info(symbol)
        if not info:
            return 11.0
        for f in info.get('filters', []):
            if f.get('filterType') in ('MIN_NOTIONAL', 'NOTIONAL'):
                return float(f.get('minNotional', f.get('notional', 11.0)))
        return 11.0

    def market_buy(self, symbol, quantity=0, usdt_amount=0):
        """Place a market buy order."""
        use_quote = getattr(self.cfg, 'USE_QUOTE_ORDER_QTY', False)
        if use_quote and usdt_amount > 0:
            return self._market_buy_quote(symbol, usdt_amount)
        return self._market_buy_base(symbol, quantity)

    def _market_buy_quote(self, symbol, usdt_amount):
        usdt_amount = math.floor(usdt_amount * 100) / 100
        if usdt_amount < 1:
            log.warning(f"USDT amount too small for {symbol}: ${usdt_amount:.2f}")
            return None
        min_notional = self._get_min_notional(symbol)
        if usdt_amount < min_notional:
            log.warning(f"{symbol}: ${usdt_amount:.2f} below MIN_NOTIONAL ${min_notional}")
            return None
        log.info(f"[BUY]  ${usdt_amount:.2f} USDT of {symbol}")
        try:
            order = self._retry(self.client.order_market_buy, symbol=symbol, quoteOrderQty=usdt_amount)
            self.invalidate_balance_cache()
            return order
        except (BinanceOrderException, BinanceAPIException) as e:
            log.error(f"Buy order failed {symbol}: {e}")
            return None

    def _market_buy_base(self, symbol, quantity):
        qty = self._round_lot(symbol, quantity)
        if qty <= 0:
            log.warning(f"Quantity too small for {symbol}: {quantity}")
            return None
        min_qty = self._get_min_qty(symbol)
        if qty < min_qty:
            log.warning(f"{symbol}: qty {qty} below minQty {min_qty}")
            return None
        log.info(f"[BUY]  {qty} {symbol}")
        try:
            order = self._retry(self.client.order_market_buy, symbol=symbol, quantity=qty)
            self.invalidate_balance_cache()
            return order
        except (BinanceOrderException, BinanceAPIException) as e:
            log.error(f"Buy order failed {symbol}: {e}")
            return None

    def market_sell(self, symbol, quantity):
        qty = self._round_lot(symbol, quantity)
        if qty <= 0:
            return None
        min_qty = self._get_min_qty(symbol)
        if qty < min_qty:
            log.warning(f"{symbol}: sell qty {qty} below minQty {min_qty}, attempting with full available balance")
            actual = self.get_asset_balance_fresh(symbol)
            qty = self._round_lot(symbol, actual)
            if qty < min_qty:
                log.error(f"{symbol}: even actual balance {actual} is below minQty {min_qty}")
                return None
        log.info(f"[SELL] {qty} {symbol}")
        try:
            order = self._retry(self.client.order_market_sell, symbol=symbol, quantity=qty)
            self.invalidate_balance_cache()
            return order
        except (BinanceOrderException, BinanceAPIException) as e:
            log.error(f"Sell order failed {symbol}: {e}")
            return None

    def avg_fill_price(self, order):
        if not order:
            return 0.0
        if 'fills' in order and order['fills']:
            tot_val = sum(float(f['price']) * float(f['qty']) for f in order['fills'])
            tot_qty = sum(float(f['qty']) for f in order['fills'])
            if tot_qty > 0:
                return tot_val / tot_qty
        if 'cummulativeQuoteQty' in order and 'executedQty' in order:
            c_quote = float(order.get('cummulativeQuoteQty', 0))
            e_qty = float(order.get('executedQty', 0))
            if e_qty > 0:
                return c_quote / e_qty
        return float(order.get('price', 0.0))

    def cancel_open_orders(self, symbol):
        """Cancel all open orders for a symbol (e.g. before closing a position)."""
        try:
            res = self._retry(self.client.cancel_open_orders, symbol=symbol)
            log.info(f"[ORDERS] Cancelled open orders for {symbol}")
            return res
        except BinanceAPIException as e:
            if e.code == -2011:  # Unknown order / no open orders
                return None
            log.warning(f"Failed to cancel open orders for {symbol}: {e}")
            return None
        except Exception as e:
            log.warning(f"Failed to cancel open orders for {symbol}: {e}")
            return None

    def get_open_orders(self, symbol):
        """Fetch open orders for a symbol."""
        try:
            return self._retry(self.client.get_open_orders, symbol=symbol)
        except Exception as e:
            log.warning(f"Failed to get open orders for {symbol}: {e}")
            return []

    def create_oco_sell(self, symbol, quantity, take_profit, stop_loss):
        """Place an OCO (One-Cancels-the-Other) sell order on Binance to protect the position."""
        qty = self._round_lot(symbol, quantity)
        tp_price = self._round_price(symbol, take_profit)
        sl_price = self._round_price(symbol, stop_loss)
        # Stop trigger slightly above stopLimitPrice so limit order fills promptly on spot
        stop_trigger = self._round_price(symbol, sl_price * 1.002)

        if qty <= 0 or tp_price <= 0 or sl_price <= 0:
            log.warning(f"[OCO ERROR] Invalid parameters for {symbol}: qty={qty}, tp={tp_price}, sl={sl_price}")
            return None

        min_qty = self._get_min_qty(symbol)
        if qty < min_qty:
            log.warning(f"[OCO ERROR] {symbol} qty {qty} < minQty {min_qty}")
            return None

        log.info(f"[OCO ORDER] Submitting OCO Sell for {symbol} | Qty: {qty} | TP: {tp_price} | Trigger: {stop_trigger} | SL Limit: {sl_price}")
        try:
            oco = self._retry(
                self.client.create_oco_order,
                symbol=symbol,
                side='SELL',
                quantity=qty,
                price=f"{tp_price:.8f}".rstrip('0').rstrip('.'),
                stopPrice=f"{stop_trigger:.8f}".rstrip('0').rstrip('.'),
                stopLimitPrice=f"{sl_price:.8f}".rstrip('0').rstrip('.'),
                stopLimitTimeInForce='GTC'
            )
            log.info(f"[OCO SUCCESS] Placed OCO order for {symbol} (OrderListId: {oco.get('orderListId')})")
            return oco
        except (BinanceAPIException, BinanceOrderException) as e:
            log.warning(f"[OCO FAILED] Could not place OCO for {symbol}: {e}. (Will rely on bot monitor loop)")
            return None

