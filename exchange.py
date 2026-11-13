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
import uuid
import threading
import pandas as pd
import requests as req_lib
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceOrderException
from typing import Optional, Set, Dict

log = logging.getLogger(__name__)

NON_RETRYABLE_CODES = {-1002, -2015, -2014, -2008, -2010, -1013}
TERMINAL_ORDER_STATUSES = ('FILLED', 'CANCELED', 'EXPIRED', 'REJECTED', 'EXPIRED_IN_MATCH')
NON_TERMINAL_ORDER_STATUSES = ('NEW', 'PENDING_NEW', 'PARTIALLY_FILLED')

class BinanceConnector:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = Client(cfg.API_KEY, cfg.API_SECRET, testnet=cfg.USE_TESTNET)
        self.client.recvWindow = 10000

        self._sync_time()

        adapter = req_lib.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
        self.client.session.mount('https://', adapter)
        if getattr(cfg, 'USE_TESTNET', False):
            try:
                self.market_client = Client('', '', testnet=False)
                self.market_client.session.mount('https://', adapter)
            except Exception:
                self.market_client = self.client
        else:
            self.market_client = self.client
        mode = 'TESTNET (with LIVE Market Data)' if cfg.USE_TESTNET else 'LIVE'
        log.info(f"Binance connected [{mode}]")

        self._info_cache = {}
        self._balance_cache = {}
        self._balance_cache_time = 0
        self._balance_lock = threading.Lock()
        self._portfolio_cache = 0
        self._portfolio_cache_time = 0
        self._portfolio_lock = threading.Lock()
        self._last_prices: Dict[str, float] = {}
        self._last_order_unsent = False

    def _sync_time(self) -> bool:
        """Synchronize local client clock offset with Binance server time."""
        try:
            server_time = self.client.get_server_time()
            local_time = int(time.time() * 1000)
            self.client.timestamp_offset = server_time['serverTime'] - local_time
            log.info(f"Time offset synced: {self.client.timestamp_offset:+d} ms")
            return True
        except Exception as e:
            log.warning(f"Failed to sync server time: {e}")
            return False

    def _retry(self, fn, *args, **kwargs):
        retries = getattr(self.cfg, 'MAX_RETRIES', 3)
        delay = getattr(self.cfg, 'RETRY_DELAY', 2)
        for i in range(retries):
            try:
                return fn(*args, **kwargs)
            except BinanceAPIException as e:
                if e.code in NON_RETRYABLE_CODES:
                    raise
                if e.code == -1021:
                    log.warning("[TIME SYNC] Binance -1021 timestamp error, re-syncing server clock")
                    self._sync_time()
                if i == retries - 1:
                    raise
                time.sleep(delay * (i + 1))
            except Exception as e:
                if i == retries - 1:
                    raise
                time.sleep(delay * (i + 1))

    @property
    def last_order_confirmed_unsent(self) -> bool:
        """True if the most recent market_buy/market_sell failed locally or Binance confirmed -2013/rejection."""
        return bool(self._last_order_unsent)

    @staticmethod
    def is_order_terminal(order) -> bool:
        """Return True only when Binance order status is terminal (not working on the book)."""
        if not order:
            return False
        status = str(order.get('status', '')).upper()
        if status in TERMINAL_ORDER_STATUSES:
            return True
        if status in NON_TERMINAL_ORDER_STATUSES:
            return False
        if not status and (float(order.get('executedQty', 0) or 0) > 0 or order.get('fills')):
            return True
        return False

    def new_client_order_id(self, symbol: str, side: str) -> str:
        return self._new_client_order_id(symbol, side)

    def reconcile_client_order_strict(self, symbol: str, client_oid: str):
        """Query Binance for `origClientOrderId` and return `(state, order)`.

        state:
          - 'FOUND': order dict returned (non-terminal orders undergo one poll + cancel attempt + re-query)
          - 'NOT_FOUND': Binance explicitly returned -2013 (Order does not exist)
          - 'ERROR': API/network failure or client lacks get_order (order state unknown)
        """
        if not client_oid or not hasattr(self.client, 'get_order'):
            return 'ERROR', None
        try:
            existing = self.client.get_order(symbol=symbol, origClientOrderId=client_oid)
        except BinanceAPIException as e:
            if getattr(e, 'code', None) == -2013:
                return 'NOT_FOUND', None
            return 'ERROR', None
        except Exception:
            return 'ERROR', None

        if not existing:
            return 'ERROR', None

        if isinstance(existing, dict):
            existing.setdefault('clientOrderId', client_oid)

        status = str(existing.get('status', '')).upper()
        if status in NON_TERMINAL_ORDER_STATUSES or not status:
            poll_delay = min(max(float(getattr(self.cfg, 'RETRY_DELAY', 0.2)), 0.0), 0.5)
            if poll_delay > 0:
                time.sleep(poll_delay)
            try:
                refreshed = self.client.get_order(symbol=symbol, origClientOrderId=client_oid)
                if refreshed:
                    existing = refreshed
                    existing.setdefault('clientOrderId', client_oid)
                    status = str(existing.get('status', '')).upper()
            except Exception:
                pass

            if status in NON_TERMINAL_ORDER_STATUSES or not status:
                if hasattr(self.client, 'cancel_order'):
                    cancel_resp = None
                    try:
                        cancel_resp = self.client.cancel_order(symbol=symbol, origClientOrderId=client_oid)
                    except Exception:
                        cancel_resp = None
                    try:
                        after_cancel = self.client.get_order(symbol=symbol, origClientOrderId=client_oid)
                        if after_cancel:
                            existing = after_cancel
                            existing.setdefault('clientOrderId', client_oid)
                        elif isinstance(cancel_resp, dict) and cancel_resp.get('status'):
                            cancel_resp.setdefault('clientOrderId', client_oid)
                            existing = cancel_resp
                    except Exception:
                        if isinstance(cancel_resp, dict) and cancel_resp.get('status'):
                            cancel_resp.setdefault('clientOrderId', client_oid)
                            existing = cancel_resp

        if float(existing.get('executedQty', 0) or 0) > 0:
            self.invalidate_balance_cache()
        return 'FOUND', existing

    def reconcile_client_order(self, symbol: str, client_oid: str):
        """Convenience wrapper returning the reconciled order dict or None."""
        state, existing = self.reconcile_client_order_strict(symbol, client_oid)
        return existing if state == 'FOUND' else None

    def _reconcile_order(self, symbol: str, client_oid: str):
        """Query Binance for an order by origClientOrderId and reconcile its execution state.

        - Terminal states (FILLED / CANCELED / EXPIRED / REJECTED / EXPIRED_IN_MATCH) are returned as-is.
        - Non-terminal states (NEW / PENDING_NEW / PARTIALLY_FILLED) undergo a brief poll, remainder
          cancellation attempt, and re-query so working remainders are cancelled before finalizing.
        """
        state, existing = self.reconcile_client_order_strict(symbol, client_oid)
        return existing if state == 'FOUND' else None

    def _new_client_order_id(self, symbol: str, side: str) -> str:
        sym_clean = ''.join(ch for ch in symbol if ch.isalnum())[:10]
        return f"cb-{side.lower()[:1]}-{sym_clean}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:4]}"

    def _submit_order_idempotent(self, fn, symbol: str, side: str, client_oid: str = None, **kwargs):
        """Submit a market order with a unique newClientOrderId and reconcile before retrying.

        Prevents duplicate buys/sells if a network timeout occurs after Binance accepted the order,
        cancels open remainders on NEW/PENDING_NEW/PARTIALLY_FILLED before returning when possible,
        and never submits a second order while an earlier order might still execute.
        """
        self._last_order_unsent = False
        retries = getattr(self.cfg, 'MAX_RETRIES', 3)
        delay = getattr(self.cfg, 'RETRY_DELAY', 2)
        pinned_oid = client_oid is not None
        if not client_oid:
            client_oid = self._new_client_order_id(symbol, side)

        for i in range(retries):
            try:
                order = fn(symbol=symbol, newClientOrderId=client_oid, **kwargs)
                if order and not self.is_order_terminal(order):
                    reconciled = self._reconcile_order(symbol, client_oid)
                    if reconciled is not None:
                        order = reconciled
                if isinstance(order, dict):
                    order.setdefault('clientOrderId', client_oid)
                return order
            except TypeError:
                # Fallback for simplified test doubles that do not accept newClientOrderId
                return fn(symbol=symbol, **kwargs)
            except (BinanceOrderException, BinanceAPIException) as e:
                code = getattr(e, 'code', None)
                if isinstance(e, BinanceOrderException):
                    self._last_order_unsent = True
                    raise
                if code == -1021:
                    log.warning("[TIME SYNC] Binance -1021 during idempotent order, re-syncing server clock")
                    self._sync_time()
                if code in NON_RETRYABLE_CODES:
                    if code == -2010:
                        state, existing = self.reconcile_client_order_strict(symbol, client_oid)
                        if state == 'FOUND' and existing is not None:
                            return existing
                    self._last_order_unsent = True
                    raise
                state, existing = self.reconcile_client_order_strict(symbol, client_oid)
                if state == 'FOUND' and existing is not None:
                    exec_qty = float(existing.get('executedQty', 0) or 0)
                    status = str(existing.get('status', '')).upper()
                    if status in NON_TERMINAL_ORDER_STATUSES or not self.is_order_terminal(existing):
                        # Order still working on Binance and could not be cancelled — do NOT submit another order
                        return existing
                    if exec_qty > 0:
                        log.warning(
                            f"[ORDER RECONCILED] {symbol} {side} {client_oid} executed {exec_qty} "
                            f"(status={status}); returning reconciled order"
                        )
                        return existing
                    # Confirmed terminal with 0 fills
                    if pinned_oid:
                        return existing
                    client_oid = self._new_client_order_id(symbol, side)
                elif state == 'NOT_FOUND' and i == retries - 1:
                    self._last_order_unsent = True
                if i == retries - 1:
                    raise
                time.sleep(delay * (i + 1))
            except Exception:
                state, existing = self.reconcile_client_order_strict(symbol, client_oid)
                if state == 'FOUND' and existing is not None:
                    exec_qty = float(existing.get('executedQty', 0) or 0)
                    status = str(existing.get('status', '')).upper()
                    if status in NON_TERMINAL_ORDER_STATUSES or not self.is_order_terminal(existing):
                        return existing
                    if exec_qty > 0:
                        log.warning(
                            f"[ORDER RECONCILED] {symbol} {side} {client_oid} executed {exec_qty} "
                            f"(status={status}) after timeout; returning reconciled order"
                        )
                        return existing
                    if pinned_oid:
                        return existing
                    client_oid = self._new_client_order_id(symbol, side)
                elif state == 'NOT_FOUND' and i == retries - 1:
                    self._last_order_unsent = True
                if i == retries - 1:
                    raise
                time.sleep(delay * (i + 1))

    def get_candles(self, symbol, interval, limit=100):
        raw = self._retry(self.market_client.get_klines, symbol=symbol, interval=interval, limit=limit)
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
        px = float(t['price'])
        if px > 0:
            self._last_prices[symbol] = px
        return px

    def get_spread_pct(self, symbol):
        """Get bid-ask spread as percentage. Uses live public market data when on Testnet."""
        try:
            book = self._retry(self.market_client.get_order_book, symbol=symbol, limit=5)
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
            book = self._retry(self.market_client.get_order_book, symbol=symbol, limit=depth)
            bid_vol = sum(float(b[1]) for b in book.get('bids', []))
            ask_vol = sum(float(a[1]) for a in book.get('asks', []))
            if ask_vol <= 0 or bid_vol <= 0:
                return 0.0
            return bid_vol / ask_vol
        except Exception as e:
            log.warning(f"Orderbook imbalance check failed for {symbol}: {e}")
            return 0.0

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
            tickers = self._retry(self.market_client.get_ticker)
            if isinstance(tickers, list):
                return {t['symbol']: t for t in tickers if 'symbol' in t}
            elif isinstance(tickers, dict) and 'symbol' in tickers:
                return {tickers['symbol']: tickers}
            return {}
        except Exception as e:
            log.error(f"Failed to fetch 24h tickers batch: {e}")
            return {}

    def get_balance(self, asset, include_locked=False):
        """Get balance for a specific asset (fresh, bypasses cache).

        Default = FREE amount only. Coins reserved by an open order (e.g. an OCO sell) are 'locked',
        not 'free' -- pass include_locked=True to get free + locked.
        """
        acct = self._retry(self.client.get_account)
        for b in acct.get('balances', []):
            if b['asset'] == asset:
                free = float(b['free'])
                if include_locked:
                    return free + float(b.get('locked', 0) or 0)
                return free
        return 0.0

    def get_usdt_balance(self):
        """Get cached USDT balance (refreshes every BALANCE_CACHE_TTL sec)."""
        return self._get_cached_balance('USDT')

    def get_asset_balance_fresh(self, symbol):
        """Get fresh FREE (sellable) balance for the base asset of a trading pair."""
        base_asset = symbol.removesuffix('USDT')
        return self.get_balance(base_asset)

    def get_asset_total(self, symbol):
        """Fresh free + locked balance for the base asset (locked = reserved by open orders such as an OCO)."""
        base_asset = symbol.removesuffix('USDT')
        return self.get_balance(base_asset, include_locked=True)

    def net_executed_qty(self, order, symbol):
        """Executed base quantity minus any commission taken in the base asset = what you actually hold."""
        if not order:
            return 0.0
        executed = float(order.get('executedQty', 0) or 0)
        base = symbol.removesuffix('USDT')
        fee_base = 0.0
        for f in order.get('fills') or []:
            if f.get('commissionAsset') == base:
                fee_base += float(f.get('commission', 0) or 0)
        return max(executed - fee_base, 0.0)

    def get_portfolio_value(self, symbols=None, quantities=None, fallback_prices=None):
        """Estimate total portfolio value in USDT.

        quantities: optional {symbol: qty} of the bot's tracked positions. When given it is used instead of
        the account balance, so locked (OCO) coins and unrelated pre-existing holdings are handled correctly.
        fallback_prices: optional {symbol: price} used if live ticker lookup temporarily fails.
        """
        now = time.time()
        with self._portfolio_lock:
            if now - self._portfolio_cache_time < 30 and self._portfolio_cache > 0:
                return self._portfolio_cache

        usdt = self.get_usdt_balance()
        val = usdt
        degraded = False
        unpriced = False
        if symbols:
            for s in symbols:
                qty = 0.0
                try:
                    if quantities is not None and s in quantities:
                        qty = quantities[s]
                    else:
                        qty = self.get_asset_total(s)  # free + locked
                except Exception as e:
                    log.warning(f"[PORTFOLIO] Quantity lookup failed for {s}: {e}")
                    unpriced = True
                    continue
                if qty <= 0:
                    continue
                px = None
                try:
                    px = self.get_price(s)
                except Exception as e:
                    fallback = self._last_prices.get(s) or (fallback_prices or {}).get(s)
                    if fallback and float(fallback) > 0:
                        px = float(fallback)
                        degraded = True
                        log.warning(f"[PORTFOLIO] Price lookup failed for {s} ({e}); using fallback {px:.6f}")
                    else:
                        unpriced = True
                        log.warning(f"[PORTFOLIO] Price lookup failed for {s} with no fallback ({e})")
                if px and px > 0:
                    val += qty * px

        if unpriced:
            with self._portfolio_lock:
                if self._portfolio_cache > 0:
                    return self._portfolio_cache
            return 0.0

        if not degraded:
            with self._portfolio_lock:
                self._portfolio_cache = val
                self._portfolio_cache_time = now
        return val

    def verify_asset_holding(self, symbol, expected_qty):
        """Check if we actually hold the expected asset quantity."""
        actual = self.get_asset_total(symbol)  # free + locked: coins held by an OCO are still ours
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

    def is_sellable_qty(self, symbol: str, quantity: float, ref_price: float = 0.0) -> bool:
        """Return True if `quantity` satisfies Binance LOT_SIZE stepSize/minQty and MIN_NOTIONAL."""
        rounded = self._round_lot(symbol, quantity)
        if rounded <= 0:
            return False
        if rounded < self._get_min_qty(symbol):
            return False
        if ref_price > 0 and rounded * ref_price < self._get_min_notional(symbol):
            return False
        return True

    def market_buy(self, symbol, quantity=0, usdt_amount=0, client_oid: str = None):
        """Place a market buy order."""
        self._last_order_unsent = False
        use_quote = getattr(self.cfg, 'USE_QUOTE_ORDER_QTY', False)
        if use_quote and usdt_amount > 0:
            return self._market_buy_quote(symbol, usdt_amount, client_oid=client_oid)
        return self._market_buy_base(symbol, quantity, client_oid=client_oid)

    def _market_buy_quote(self, symbol, usdt_amount, client_oid: str = None):
        usdt_amount = math.floor(usdt_amount * 100) / 100
        if usdt_amount < 1:
            log.warning(f"USDT amount too small for {symbol}: ${usdt_amount:.2f}")
            self._last_order_unsent = True
            return None
        min_notional = self._get_min_notional(symbol)
        if usdt_amount < min_notional:
            log.warning(f"{symbol}: ${usdt_amount:.2f} below MIN_NOTIONAL ${min_notional}")
            self._last_order_unsent = True
            return None
        log.info(f"[BUY]  ${usdt_amount:.2f} USDT of {symbol}")
        try:
            order = self._submit_order_idempotent(
                self.client.order_market_buy, symbol=symbol, side='BUY',
                client_oid=client_oid, quoteOrderQty=usdt_amount
            )
            self.invalidate_balance_cache()
            return order
        except (BinanceOrderException, BinanceAPIException) as e:
            log.error(f"Buy order failed {symbol}: {e}")
            return None
        except Exception as e:
            log.error(f"Buy order failed {symbol}: {e}")
            return None

    def _market_buy_base(self, symbol, quantity, client_oid: str = None):
        qty = self._round_lot(symbol, quantity)
        if qty <= 0:
            log.warning(f"Quantity too small for {symbol}: {quantity}")
            self._last_order_unsent = True
            return None
        min_qty = self._get_min_qty(symbol)
        if qty < min_qty:
            log.warning(f"{symbol}: qty {qty} below minQty {min_qty}")
            self._last_order_unsent = True
            return None
        log.info(f"[BUY]  {qty} {symbol}")
        try:
            order = self._submit_order_idempotent(
                self.client.order_market_buy, symbol=symbol, side='BUY',
                client_oid=client_oid, quantity=qty
            )
            self.invalidate_balance_cache()
            return order
        except (BinanceOrderException, BinanceAPIException) as e:
            log.error(f"Buy order failed {symbol}: {e}")
            return None
        except Exception as e:
            log.error(f"Buy order failed {symbol}: {e}")
            return None

    def market_sell(self, symbol, quantity, client_oid: str = None):
        self._last_order_unsent = False
        qty = self._round_lot(symbol, quantity)
        if qty <= 0:
            self._last_order_unsent = True
            return None
        min_qty = self._get_min_qty(symbol)
        if qty < min_qty:
            log.warning(f"{symbol}: sell qty {qty} below minQty {min_qty}, checking actual balance capped to {quantity}")
            actual = min(self.get_asset_balance_fresh(symbol), quantity)
            qty = self._round_lot(symbol, actual)
            if qty < min_qty:
                log.error(f"{symbol}: capped sell balance {actual} (qty={qty}) is below minQty {min_qty}")
                self._last_order_unsent = True
                return None
        log.info(f"[SELL] {qty} {symbol}")
        try:
            order = self._submit_order_idempotent(
                self.client.order_market_sell, symbol=symbol, side='SELL',
                client_oid=client_oid, quantity=qty
            )
            self.invalidate_balance_cache()
            return order
        except (BinanceOrderException, BinanceAPIException) as e:
            log.error(f"Sell order failed {symbol}: {e}")
            return None
        except Exception as e:
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
        """Cancel all open orders for a symbol (e.g. before closing a position or syncing OCO).

        Returns True if open orders were cancelled (or none existed), False on API failure.
        """
        try:
            self._retry(self.client.cancel_open_orders, symbol=symbol)
            log.info(f"[ORDERS] Cancelled open orders for {symbol}")
            return True
        except BinanceAPIException as e:
            if e.code == -2011:  # Unknown order / no open orders
                return True
            log.warning(f"Failed to cancel open orders for {symbol}: {e}")
            return False
        except Exception as e:
            log.warning(f"Failed to cancel open orders for {symbol}: {e}")
            return False

    def get_open_orders(self, symbol):
        """Fetch open orders for a symbol."""
        try:
            return self._retry(self.client.get_open_orders, symbol=symbol)
        except Exception as e:
            log.warning(f"Failed to get open orders for {symbol}: {e}")
            return []

    def get_open_orders_strict(self, symbol):
        """Open orders for a symbol, or None if the API call failed (an outage must never look like 'no orders')."""
        try:
            return self._retry(self.client.get_open_orders, symbol=symbol)
        except Exception as e:
            log.warning(f"Failed to get open orders for {symbol}: {e}")
            return None

    def get_last_sell_fill(self, symbol, since_ms):
        """Most recent FILLED SELL order for `symbol` updated at/after since_ms (epoch ms).

        Returns {'price': avg fill price, 'qty', 'type', 'order_id', 'time'} or None.
        """
        try:
            orders = self._retry(self.client.get_all_orders, symbol=symbol, limit=20)
        except Exception as e:
            log.warning(f"Failed to fetch order history for {symbol}: {e}")
            return None
        best = None
        for o in orders or []:
            if o.get('side') != 'SELL' or o.get('status') != 'FILLED':
                continue
            ts = int(o.get('updateTime') or o.get('time') or 0)
            if ts < since_ms:
                continue
            if best is None or ts > best[0]:
                best = (ts, o)
        if best is None:
            return None
        best_ts, o = best
        executed = float(o.get('executedQty', 0) or 0)
        quote = float(o.get('cummulativeQuoteQty', 0) or 0)
        if executed <= 0 or quote <= 0:
            return None
        return {
            'price': quote / executed,
            'qty': executed,
            'type': o.get('type'),
            'order_id': o.get('orderId'),
            'time': best_ts / 1000.0 if best_ts > 0 else None,
        }

    def get_oco_fill(self, symbol: str, order_list_id, since_ms: int = 0) -> dict:
        """Reconcile a specific Binance OCO order list by `orderListId`.

        Strictly ignores unrelated SELL orders on the symbol that do not belong to `order_list_id`.
        Returns a status dict:
          - {'status': 'OPEN', 'order_list_id': ..., 'qty': 0.0}
          - {'status': 'FILLED', 'price', 'qty', 'type', 'order_id', 'order_list_id', 'time'}
          - {'status': 'CANCELED', 'order_list_id': ..., 'qty': 0.0}
          - {'status': 'NOT_FOUND', 'order_list_id': ..., 'qty': 0.0}
          - {'status': 'UNKNOWN', 'order_list_id': ...}
        """
        try:
            target_lid = int(order_list_id)
        except (TypeError, ValueError):
            return {'status': 'UNKNOWN', 'order_list_id': order_list_id}

        child_orders: Dict[int, dict] = {}
        child_ids = set()
        list_status = ''
        had_api_success = False

        if hasattr(self.client, 'get_order_list'):
            try:
                ol = self._retry(self.client.get_order_list, orderListId=target_lid)
                had_api_success = True
                if isinstance(ol, dict):
                    list_status = str(ol.get('listOrderStatus', '')).upper()
                    for ref in ol.get('orders') or []:
                        if ref.get('orderId') is not None:
                            child_ids.add(int(ref['orderId']))
                    for rep in ol.get('orderReports') or []:
                        oid = rep.get('orderId')
                        if oid is not None:
                            oid_int = int(oid)
                            child_ids.add(oid_int)
                            child_orders[oid_int] = rep
            except BinanceAPIException as e:
                if getattr(e, 'code', None) == -2013:
                    had_api_success = True
                else:
                    log.warning(f"Failed to get OCO order list {target_lid} for {symbol}: {e}")
            except Exception as e:
                log.warning(f"Failed to get OCO order list {target_lid} for {symbol}: {e}")

        need_history = (not child_orders) or any(
            not o.get('cummulativeQuoteQty') and float(o.get('executedQty', 0) or 0) > 0
            for o in child_orders.values()
        )
        if need_history and hasattr(self.client, 'get_all_orders'):
            try:
                all_orders = self._retry(self.client.get_all_orders, symbol=symbol, limit=50)
                had_api_success = True
                for o in all_orders or []:
                    try:
                        o_lid = int(o.get('orderListId')) if o.get('orderListId') is not None else -1
                    except (TypeError, ValueError):
                        o_lid = -1
                    oid = o.get('orderId')
                    oid_int = int(oid) if oid is not None else None
                    if o_lid == target_lid or (oid_int is not None and oid_int in child_ids):
                        if oid_int is not None:
                            child_orders[oid_int] = o
            except Exception as e:
                log.warning(f"Failed to fetch order history for OCO {target_lid} on {symbol}: {e}")

        if child_ids and hasattr(self.client, 'get_order'):
            for cid in child_ids:
                existing = child_orders.get(cid)
                if not existing or not existing.get('status'):
                    try:
                        fetched = self._retry(self.client.get_order, symbol=symbol, orderId=cid)
                        had_api_success = True
                        if fetched:
                            child_orders[cid] = fetched
                    except Exception as e:
                        log.warning(f"Failed to get child order {cid} for OCO {target_lid}: {e}")

        if not had_api_success and not child_orders:
            return {'status': 'UNKNOWN', 'order_list_id': order_list_id}

        matched = list(child_orders.values())
        if matched:
            any_open = any(
                str(o.get('status', '')).upper() in NON_TERMINAL_ORDER_STATUSES
                for o in matched
            )
            if any_open or list_status == 'EXECUTING':
                return {'status': 'OPEN', 'order_list_id': order_list_id, 'qty': 0.0}

            tot_qty = 0.0
            tot_quote = 0.0
            best_ts = 0
            primary_qty = -1.0
            primary_type = None
            primary_oid = None
            for o in matched:
                if o.get('side', 'SELL') != 'SELL':
                    continue
                eq = float(o.get('executedQty', 0) or 0)
                cq = float(o.get('cummulativeQuoteQty', 0) or 0)
                ts = int(o.get('updateTime') or o.get('transactTime') or o.get('time') or 0)
                if since_ms and ts and ts < since_ms:
                    continue
                if eq > 0:
                    if cq <= 0 and float(o.get('price', 0) or 0) > 0:
                        cq = eq * float(o.get('price'))
                    tot_qty += eq
                    tot_quote += cq
                    if ts > best_ts:
                        best_ts = ts
                    if eq > primary_qty:
                        primary_qty = eq
                        primary_type = o.get('type')
                        primary_oid = o.get('orderId')

            if tot_qty > 0 and tot_quote > 0:
                return {
                    'status': 'FILLED',
                    'price': tot_quote / tot_qty,
                    'qty': tot_qty,
                    'type': primary_type,
                    'order_id': primary_oid,
                    'order_list_id': order_list_id,
                    'time': best_ts / 1000.0 if best_ts > 0 else None,
                }
            return {'status': 'CANCELED', 'order_list_id': order_list_id, 'qty': 0.0}

        if list_status == 'EXECUTING':
            return {'status': 'OPEN', 'order_list_id': order_list_id, 'qty': 0.0}

        open_orders = self.get_open_orders_strict(symbol)
        if open_orders is None:
            return {'status': 'UNKNOWN', 'order_list_id': order_list_id}
        for o in open_orders:
            try:
                if int(o.get('orderListId', -1)) == target_lid:
                    return {'status': 'OPEN', 'order_list_id': order_list_id, 'qty': 0.0}
            except (TypeError, ValueError):
                pass
        return {'status': 'NOT_FOUND', 'order_list_id': order_list_id, 'qty': 0.0}

    def create_oco_sell(self, symbol, quantity, take_profit, stop_loss):
        """Place an OCO (One-Cancels-the-Other) sell order on Binance to protect the position."""
        # Never ask for more than we hold free (commission may have been taken in the base asset)
        try:
            free = self.get_balance(symbol.removesuffix('USDT'))
            if 0 < free < quantity:
                quantity = free
        except Exception as e:
            log.warning(f"[OCO] could not check free balance for {symbol}: {e}")
        qty = self._round_lot(symbol, quantity)
        tp_price = self._round_price(symbol, take_profit)
        sl_price = self._round_price(symbol, stop_loss)
        # Trigger at sl_price and place stopLimitPrice below trigger so fast wicks still fill
        offset_pct = float(getattr(self.cfg, 'OCO_STOP_LIMIT_OFFSET_PCT', 0.8) or 0.8)
        stop_trigger = sl_price
        sl_limit = self._round_price(symbol, stop_trigger * (1.0 - offset_pct / 100.0))
        if sl_limit <= 0 or sl_limit > stop_trigger:
            sl_limit = stop_trigger

        if qty <= 0 or tp_price <= 0 or sl_price <= 0:
            log.warning(f"[OCO ERROR] Invalid parameters for {symbol}: qty={qty}, tp={tp_price}, sl={sl_price}")
            return None

        min_qty = self._get_min_qty(symbol)
        if qty < min_qty:
            log.warning(f"[OCO ERROR] {symbol} qty {qty} < minQty {min_qty}")
            return None

        log.info(f"[OCO ORDER] Submitting OCO Sell for {symbol} | Qty: {qty} | TP: {tp_price} | Trigger: {stop_trigger} | SL Limit: {sl_limit}")
        try:
            oco = self._retry(
                self.client.create_oco_order,
                symbol=symbol,
                side='SELL',
                quantity=qty,
                price=f"{tp_price:.8f}".rstrip('0').rstrip('.'),
                stopPrice=f"{stop_trigger:.8f}".rstrip('0').rstrip('.'),
                stopLimitPrice=f"{sl_limit:.8f}".rstrip('0').rstrip('.'),
                stopLimitTimeInForce='GTC'
            )
            log.info(f"[OCO SUCCESS] Placed OCO order for {symbol} (OrderListId: {oco.get('orderListId')})")
            return oco
        except (BinanceAPIException, BinanceOrderException) as e:
            log.warning(f"[OCO FAILED] Could not place OCO for {symbol}: {e}. (Will rely on bot monitor loop)")
            return None

