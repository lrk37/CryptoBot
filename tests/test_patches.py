"""Offline tests for the fixes in this patch set. Run from the repo root:

    python -m unittest discover -s tests -v

No network / API keys needed: Binance is replaced by small fakes."""
import os
import sys
import json
import time
import types
import shutil
import tempfile
import logging
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# --- stubs for third-party modules that may not be installed ------------------------------------
try:
    import dotenv  # noqa
except ImportError:
    m = types.ModuleType("dotenv"); m.load_dotenv = lambda *a, **k: None; sys.modules["dotenv"] = m
try:
    import binance  # noqa
except ImportError:
    b = types.ModuleType("binance"); bc = types.ModuleType("binance.client"); be = types.ModuleType("binance.exceptions")
    class BinanceAPIException(Exception):
        def __init__(self, *a, **k):
            super().__init__(*a); self.code = k.get("code", 0)
    class BinanceOrderException(Exception):
        pass
    class Client:  # replaced in tests that need it
        def __init__(self, *a, **k): pass
    bc.Client = Client; be.BinanceAPIException = BinanceAPIException; be.BinanceOrderException = BinanceOrderException
    sys.modules.update({"binance": b, "binance.client": bc, "binance.exceptions": be})

logging.disable(logging.CRITICAL)

import numpy as np
import pandas as pd
import config as real_cfg
from synthetic import make_series


def read_json(path):
    with open(path) as f:
        return json.load(f)


def make_cfg(**over):
    ns = types.SimpleNamespace(**{k: v for k, v in vars(real_cfg).items() if not k.startswith("_")})
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


class InTempDir(unittest.TestCase):
    def setUp(self):
        self._old = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self._old)
        shutil.rmtree(self.tmp, ignore_errors=True)


# =============================================================== stops / triggers
class TestStops(unittest.TestCase):
    def setUp(self):
        from risk_manager import compute_stops
        self.cs = compute_stops
        self.cfg = make_cfg(SCANNER_ATR_SL_MULTIPLIER=2.5, SCANNER_MAX_STOP_LOSS_PCT=3.0, MIN_SL_PCT=1.5,
                            MIN_TP_PCT=3, SCANNER_ATR_TP_MULTIPLIER=5.0, SCANNER_USE_ATR_STOPS=True)

    def dist(self, atr, **over):
        cfg = make_cfg(**{**vars(self.cfg), **over}) if over else self.cfg
        sl, tp, *_ = self.cs(cfg, "scanner", 100.0, atr)
        return round(100 - sl, 4), round(tp - 100, 4)

    def test_floor_cap_and_tp_floor(self):
        self.assertEqual(self.dist(0.3), (1.5, 3.0))    # SL floored at 1.5 (was 0.75), TP floored at 3.0
        self.assertEqual(self.dist(0.6), (1.5, 3.0))
        self.assertEqual(self.dist(1.0), (2.5, 5.0))    # ATR math now honoured (was capped at 1.5)
        self.assertEqual(self.dist(1.5), (3.0, 7.5))    # cap at 3.0

    def test_wider_settings_take_effect(self):
        self.assertEqual(self.dist(1.0, SCANNER_ATR_SL_MULTIPLIER=3.2, SCANNER_MAX_STOP_LOSS_PCT=4.0)[0], 3.2)
        self.assertEqual(self.dist(1.5, SCANNER_ATR_SL_MULTIPLIER=3.2, SCANNER_MAX_STOP_LOSS_PCT=4.0)[0], 4.0)

    def test_fixed_mode_without_atr(self):
        sl, tp, *_ = self.cs(self.cfg, "scanner", 100.0, 0.0)
        self.assertAlmostEqual(100 - sl, self.cfg.SCANNER_STOP_LOSS_PCT)
        self.assertAlmostEqual(tp - 100, self.cfg.SCANNER_TAKE_PROFIT_PCT)

    def test_exit_triggers_include_fees(self):
        from risk_manager import exit_triggers
        t = exit_triggers(make_cfg(ROUND_TRIP_FEE_PCT=0.2, MIN_TRAILING_PROFIT_PCT=4.0, BREAK_EVEN_TRIGGER_PCT=3.5))
        self.assertAlmostEqual(t["trail_trigger_pct"], 4.2)
        self.assertAlmostEqual(t["be_trigger_pct"], 4.2)    # 3.5 is dead config while min-profit is 4.0
        t = exit_triggers(make_cfg(ROUND_TRIP_FEE_PCT=0.2, MIN_TRAILING_PROFIT_PCT=3.0, BREAK_EVEN_TRIGGER_PCT=3.5))
        self.assertAlmostEqual(t["be_trigger_pct"], 3.5)


# =============================================================== risk manager
class FakeCandlesExchange:
    def __init__(self, df=None, boom=False):
        self.df, self.boom = df, boom
    def get_candles(self, symbol, interval="15m", limit=100):
        if self.boom:
            raise RuntimeError("api down")
        return self.df


def candles(trend):
    """80 closed 15m candles + 1 forming candle. trend='down' -> last closed close is below EMA21."""
    n = 80
    base = np.linspace(100, 90, n) if trend == "down" else np.linspace(90, 100, n)
    df = pd.DataFrame({"open": base, "high": base * 1.001, "low": base * 0.999, "close": base,
                       "volume": 1.0, "quote_vol": 1.0})
    forming = df.iloc[[-1]].copy()
    forming["close"] = 1e9          # must be ignored (forming candle)
    return pd.concat([df, forming], ignore_index=True)


class TestRiskManager(InTempDir):
    def make_rm(self, **over):
        from risk_manager import RiskManager
        cfg = make_cfg(POSITIONS_FILE=os.path.join(self.tmp, "positions.json"),
                       TRADE_LOG_FILE=os.path.join(self.tmp, "trade_log.json"), **over)
        return RiskManager(cfg), cfg

    def pos(self, age_h):
        from risk_manager import Position
        return Position(symbol="AAAUSDT", entry_price=100.0, quantity=1.0, stop_loss=90.0, take_profit=120.0,
                        trailing=True, trailing_offset=1.8, open_time=time.time() - age_h * 3600)

    def test_trend_invalidation(self):
        rm, _ = self.make_rm()
        rm.exchange = FakeCandlesExchange(candles("down"))
        self.assertEqual(rm.evaluate(self.pos(7), 100.0), "HOLD")                 # before 8h: never
        self.assertEqual(rm.evaluate(self.pos(9), 100.0), "TREND_INVALIDATED")    # after 8h + below EMA21
        self.assertEqual(rm.evaluate(self.pos(17), 100.0), "MAX_HOLD_TIMEOUT")    # hard cap at 16h
        rm.exchange = FakeCandlesExchange(candles("up"))
        self.assertEqual(rm.evaluate(self.pos(9), 100.0), "HOLD")                 # above EMA21: keep holding
        rm.exchange = FakeCandlesExchange(boom=True)
        self.assertEqual(rm.evaluate(self.pos(9), 100.0), "HOLD")                 # data problem: hold, don't panic-sell

    def test_break_even_arms_at_effective_trigger(self):
        from risk_manager import exit_triggers
        rm, cfg = self.make_rm()
        trig = exit_triggers(cfg)["be_trigger_pct"]
        p = self.pos(0.1)
        rm.evaluate(p, 100.0 + trig - 0.2)
        self.assertEqual(p.min_be_price, 0.0)
        rm.evaluate(p, 100.0 + trig + 0.2)
        expected_be = 100.0 * (1 + (cfg.ROUND_TRIP_FEE_PCT + getattr(cfg, "BREAK_EVEN_SLIPPAGE_BUFFER_PCT", 0.10)) / 100.0)
        self.assertAlmostEqual(p.min_be_price, expected_be, places=4)
        self.assertAlmostEqual(p.stop_loss, expected_be, places=4)

    def test_trade_log_has_net_fields(self):
        rm, cfg = self.make_rm()
        rm.open_position("AAAUSDT", 100.0, 1.0, kind="scanner", atr_value=1.0, entry_score=80)
        rm.close_position("AAAUSDT", 102.0, "TEST")
        row = read_json(cfg.TRADE_LOG_FILE)[-1]
        self.assertAlmostEqual(row["pnl_pct"], 2.0, places=3)
        self.assertAlmostEqual(row["net_pnl_pct"], 2.0 - cfg.ROUND_TRIP_FEE_PCT, places=3)
        self.assertLess(row["net_pnl_usdt"], row["pnl_usdt"])

    def test_persist_positions_roundtrip(self):
        rm, cfg = self.make_rm()
        pos = rm.open_position("AAAUSDT", 100.0, 1.0, kind="scanner", atr_value=1.0)
        pos.details["oco_order_list_id"] = 123
        rm.persist_positions()
        self.assertEqual(read_json(cfg.POSITIONS_FILE)["AAAUSDT"]["details"]["oco_order_list_id"], 123)


# =============================================================== exchange helpers
class FakeClient:
    def __init__(self):
        self.session = types.SimpleNamespace(mount=lambda *a, **k: None)
        self.balances = [{"asset": "USDT", "free": "1000", "locked": "0"},
                         {"asset": "AAA", "free": "0.1", "locked": "0.9"}]
        self.orders = []
        self.open_orders = []
        self.fail_open_orders = False
        self.oco_calls = []
        self.price = 10.0
    def get_server_time(self): return {"serverTime": int(time.time() * 1000)}
    def get_account(self): return {"balances": self.balances}
    def get_symbol_ticker(self, symbol): return {"price": str(self.price)}
    def get_symbol_info(self, symbol):
        return {"filters": [{"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                            {"filterType": "PRICE_FILTER", "tickSize": "0.01"}]}
    def get_open_orders(self, symbol):
        if self.fail_open_orders:
            raise RuntimeError("boom")
        return self.open_orders
    def get_all_orders(self, symbol, limit=20): return self.orders
    def create_oco_order(self, **kw):
        self.oco_calls.append(kw); return {"orderListId": 7}


class TestExchange(unittest.TestCase):
    def setUp(self):
        import exchange
        self.fake = FakeClient()
        exchange.Client = lambda *a, **k: self.fake
        cfg = make_cfg(MAX_RETRIES=1, RETRY_DELAY=0)
        self.ex = exchange.BinanceConnector(cfg)

    def test_free_vs_total_balance(self):
        self.assertAlmostEqual(self.ex.get_asset_balance_fresh("AAAUSDT"), 0.1)
        self.assertAlmostEqual(self.ex.get_asset_total("AAAUSDT"), 1.0)
        self.assertTrue(self.ex.verify_asset_holding("AAAUSDT", 1.0))      # locked coins still count

    def test_net_executed_qty(self):
        order = {"executedQty": "1.0", "fills": [{"commission": "0.001", "commissionAsset": "AAA"}]}
        self.assertAlmostEqual(self.ex.net_executed_qty(order, "AAAUSDT"), 0.999)
        order = {"executedQty": "1.0", "fills": [{"commission": "0.0005", "commissionAsset": "BNB"}]}
        self.assertAlmostEqual(self.ex.net_executed_qty(order, "AAAUSDT"), 1.0)

    def test_open_orders_strict(self):
        self.fake.open_orders = [{"orderId": 1}]
        self.assertEqual(len(self.ex.get_open_orders_strict("AAAUSDT")), 1)
        self.fake.fail_open_orders = True
        self.assertIsNone(self.ex.get_open_orders_strict("AAAUSDT"))      # outage != "no orders"

    def test_last_sell_fill(self):
        t0 = 1_000_000
        self.fake.orders = [
            {"side": "BUY", "status": "FILLED", "updateTime": t0 + 5, "executedQty": "1", "cummulativeQuoteQty": "100", "type": "MARKET"},
            {"side": "SELL", "status": "FILLED", "updateTime": t0 - 1, "executedQty": "1", "cummulativeQuoteQty": "50", "type": "LIMIT_MAKER"},
            {"side": "SELL", "status": "CANCELED", "updateTime": t0 + 9, "executedQty": "0", "cummulativeQuoteQty": "0", "type": "STOP_LOSS_LIMIT"},
            {"side": "SELL", "status": "FILLED", "updateTime": t0 + 10, "executedQty": "2", "cummulativeQuoteQty": "210", "type": "LIMIT_MAKER", "orderId": 9},
        ]
        fill = self.ex.get_last_sell_fill("AAAUSDT", t0)
        self.assertAlmostEqual(fill["price"], 105.0)
        self.assertEqual(fill["type"], "LIMIT_MAKER")
        self.assertIsNone(self.ex.get_last_sell_fill("AAAUSDT", t0 + 11))

    def test_oco_quantity_clamped_to_free_balance(self):
        self.fake.balances = [{"asset": "AAA", "free": "0.9995", "locked": "0"}]
        self.ex.create_oco_sell("AAAUSDT", 1.0, 11.0, 9.5)
        self.assertEqual(len(self.fake.oco_calls), 1)
        self.assertAlmostEqual(float(self.fake.oco_calls[0]["quantity"]), 0.999)

    def test_portfolio_value_uses_tracked_quantity(self):
        self.fake.balances = [{"asset": "USDT", "free": "980", "locked": "0"},
                              {"asset": "AAA", "free": "5000", "locked": "0"}]   # unrelated pre-existing holdings
        val = self.ex.get_portfolio_value(symbols=["AAAUSDT"], quantities={"AAAUSDT": 2.0})
        self.assertAlmostEqual(val, 980 + 2.0 * 10.0)

    def test_idempotent_order_reconciles_on_timeout(self):
        """If order_market_buy times out after Binance accepted the order, _submit_order_idempotent
        must reconcile via get_order(origClientOrderId=...) instead of sending a duplicate buy."""
        buy_calls = []
        def fake_market_buy(**kw):
            buy_calls.append(kw)
            raise RuntimeError("Read timed out")
        def fake_get_order(symbol, origClientOrderId):
            return {
                "symbol": symbol,
                "clientOrderId": origClientOrderId,
                "status": "FILLED",
                "executedQty": "2.0",
                "cummulativeQuoteQty": "20.0"
            }
        self.fake.order_market_buy = fake_market_buy
        self.fake.get_order = fake_get_order
        self.ex.max_retries = 2
        order = self.ex.market_buy("AAAUSDT", 20.0)
        self.assertIsNotNone(order)
        self.assertEqual(len(buy_calls), 1)
        self.assertEqual(order["status"], "FILLED")
        self.assertEqual(order["clientOrderId"], buy_calls[0]["newClientOrderId"])

    def test_idempotent_order_reconciles_expired_with_partial_fill(self):
        """If an order times out and Binance reports status=EXPIRED with executedQty > 0,
        _submit_order_idempotent must return that partial fill and NOT submit a second order."""
        buy_calls = []
        def fake_market_buy(**kw):
            buy_calls.append(kw)
            raise RuntimeError("Read timed out")
        def fake_get_order(symbol, origClientOrderId):
            return {
                "symbol": symbol,
                "clientOrderId": origClientOrderId,
                "status": "EXPIRED",
                "executedQty": "0.6",
                "cummulativeQuoteQty": "12.0"
            }
        self.fake.order_market_buy = fake_market_buy
        self.fake.get_order = fake_get_order
        self.ex.max_retries = 3
        order = self.ex.market_buy("AAAUSDT", 20.0)
        self.assertIsNotNone(order)
        self.assertEqual(len(buy_calls), 1)
        self.assertEqual(order["status"], "EXPIRED")
        self.assertAlmostEqual(float(order["executedQty"]), 0.6)

    def test_reconcile_cancels_partially_filled_remainder_before_returning(self):
        """PARTIALLY_FILLED market order must poll/cancel remaining quantity and confirm terminal state."""
        buy_calls = []
        cancel_calls = []
        state = {"status": "PARTIALLY_FILLED", "executedQty": "0.4", "cummulativeQuoteQty": "8.0"}

        def fake_market_buy(**kw):
            buy_calls.append(kw)
            return dict(state, symbol=kw["symbol"], clientOrderId=kw["newClientOrderId"])

        def fake_get_order(symbol, origClientOrderId):
            return dict(state, symbol=symbol, clientOrderId=origClientOrderId)

        def fake_cancel_order(symbol, origClientOrderId):
            cancel_calls.append((symbol, origClientOrderId))
            state["status"] = "CANCELED"
            return dict(state, symbol=symbol, clientOrderId=origClientOrderId)

        self.fake.order_market_buy = fake_market_buy
        self.fake.get_order = fake_get_order
        self.fake.cancel_order = fake_cancel_order

        order = self.ex.market_buy("AAAUSDT", 20.0)
        self.assertEqual(len(buy_calls), 1)
        self.assertEqual(len(cancel_calls), 1)
        self.assertEqual(order["status"], "CANCELED")
        self.assertAlmostEqual(float(order["executedQty"]), 0.4)

    def test_last_sell_fill_uses_selected_best_order_timestamp(self):
        """get_last_sell_fill must return best[0]'s timestamp even when trailing orders have later timestamps."""
        t0 = 1_000_000
        self.fake.orders = [
            {"side": "SELL", "status": "FILLED", "updateTime": t0 + 10_000,
             "executedQty": "1", "cummulativeQuoteQty": "105", "type": "LIMIT_MAKER", "orderId": 9},
            {"side": "SELL", "status": "CANCELED", "updateTime": t0 + 50_000,
             "executedQty": "0", "cummulativeQuoteQty": "0", "type": "STOP_LOSS_LIMIT", "orderId": 10},
        ]
        fill = self.ex.get_last_sell_fill("AAAUSDT", t0)
        self.assertIsNotNone(fill)
        self.assertAlmostEqual(fill["time"], (t0 + 10_000) / 1000.0)

    def test_get_oco_fill_scopes_to_order_list_id(self):
        """get_oco_fill must ignore unrelated SELL fills and respect EXECUTING vs ALL_DONE orderListId."""
        t0 = 2_000_000
        self.fake.orders = [
            # Unrelated manual SELL on same symbol
            {"side": "SELL", "status": "FILLED", "orderListId": 999, "orderId": 100,
             "updateTime": t0 + 5000, "executedQty": "5", "cummulativeQuoteQty": "999", "type": "MARKET"},
            # Target OCO order list 7: partial/full TP leg + expired SL leg
            {"side": "SELL", "status": "FILLED", "orderListId": 7, "orderId": 11,
             "updateTime": t0 + 2000, "executedQty": "0.6", "cummulativeQuoteQty": "63.0", "type": "LIMIT_MAKER"},
            {"side": "SELL", "status": "EXPIRED", "orderListId": 7, "orderId": 12,
             "updateTime": t0 + 2000, "executedQty": "0", "cummulativeQuoteQty": "0", "type": "STOP_LOSS_LIMIT"},
        ]
        ol_status = {"listOrderStatus": "ALL_DONE"}
        self.fake.get_order_list = lambda orderListId: {
            "orderListId": orderListId,
            "listOrderStatus": ol_status["listOrderStatus"],
            "orders": [{"orderId": 11}, {"orderId": 12}],
        }
        res = self.ex.get_oco_fill("AAAUSDT", 7, since_ms=t0)
        self.assertEqual(res["status"], "FILLED")
        self.assertAlmostEqual(res["price"], 105.0)
        self.assertAlmostEqual(res["qty"], 0.6)
        self.assertEqual(res["type"], "LIMIT_MAKER")
        self.assertAlmostEqual(res["time"], (t0 + 2000) / 1000.0)

        ol_status["listOrderStatus"] = "EXECUTING"
        res_open = self.ex.get_oco_fill("AAAUSDT", 7, since_ms=t0)
        self.assertEqual(res_open["status"], "OPEN")


# =============================================================== scanner
class TestScanner(unittest.TestCase):
    def test_cap_keeps_most_liquid_pairs(self):
        from scanner import MarketScanner
        syms = [f"C{i:02d}USDT" for i in range(30)]
        tick = {s: {"quoteVolume": str(20_000_000 + i * 1_000_000)} for i, s in enumerate(syms)}   # later = more liquid
        ex = types.SimpleNamespace(get_all_usdt_symbols=lambda: syms, get_24h_tickers=lambda: tick)
        cfg = make_cfg(MAX_SCAN_CANDIDATES=5, MIN_SCAN_VOLUME_USDT=10_000_000, SNIPE_BLACKLIST=[])
        sc = MarketScanner(ex, cfg)
        seen = []
        sc._analyze_symbol = lambda s: seen.append(s)
        sc.scan_market()
        self.assertEqual(sorted(seen), sorted(syms[-5:]))

    def test_btc_regime_uses_closed_candle_and_fails_closed(self):
        from scanner import MarketScanner
        # 210 downward candles (close=90 < EMA200) + 1 forming candle spiking to 200
        n = 210
        base = np.linspace(110.0, 90.0, n)
        df = pd.DataFrame({
            "open": base, "high": base + 1.0, "low": base - 1.0, "close": base,
            "volume": 100.0, "quote_vol": 10000.0
        })
        forming = df.iloc[[-1]].copy()
        forming["close"] = 200.0  # unclosed forming spike must be ignored
        df_with_forming = pd.concat([df, forming], ignore_index=True)

        ex = types.SimpleNamespace(get_candles=lambda *a, **k: df_with_forming)
        cfg = make_cfg(ENABLE_BTC_REGIME_FILTER=True)
        sc = MarketScanner(ex, cfg)
        self.assertEqual(sc.check_btc_regime(), "BEARISH")

        # API failure must also fail closed ("BEARISH")
        ex_fail = types.SimpleNamespace(get_candles=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("api down")))
        sc_fail = MarketScanner(ex_fail, cfg)
        self.assertEqual(sc_fail.check_btc_regime(), "BEARISH")

    def test_scanner_rejects_wide_spread(self):
        from scanner import MarketScanner
        n = 210
        base = np.full(n, 100.0)
        # Construct an extreme capitulation bar at iloc[-2]
        df = pd.DataFrame({
            "open": base, "high": base + 0.5, "low": base - 0.5, "close": base,
            "volume": 100.0, "quote_vol": 10000.0
        })
        df.iloc[-2, df.columns.get_loc("close")] = 90.0
        df.iloc[-2, df.columns.get_loc("low")] = 89.5
        df.iloc[-2, df.columns.get_loc("volume")] = 500.0
        df.iloc[-1, df.columns.get_loc("close")] = 90.2

        ex = types.SimpleNamespace(
            get_candles=lambda *a, **k: df,
            get_spread_pct=lambda s: 0.45,  # > 0.25% max spread
            get_orderbook_imbalance=lambda s, limit=20: 1.5,
        )
        cfg = make_cfg(MAX_SPREAD_PCT=0.25, STRATEGY_MODE="capitulation")
        sc = MarketScanner(ex, cfg)
        self.assertIsNone(sc._analyze_symbol_tf("AAAUSDT", "15m"))


# =============================================================== bot monitor / OCO handling
class FakeNotifier:
    def __init__(self): self.closed = []
    def trade_opened(self, *a, **k): pass
    def trade_closed(self, *a, **k): self.closed.append(a)


class FakeBotExchange:
    def __init__(self):
        self.open_orders = []          # list, or None to simulate an API failure
        self.fill = None
        self.free = 5.0                # free balance includes unrelated holdings
        self.sold = []
        self.strict_calls = 0
        self.price = 100.0
        self.sell_return = "default"
        self.buy_return = "default"
        self.cancel_return = True
        self.oco_return = "default"
        self.oco_created = []
    def get_usdt_balance(self): return 100.0
    def get_price(self, s): return self.price
    def get_open_orders_strict(self, s):
        self.strict_calls += 1
        return self.open_orders
    def get_last_sell_fill(self, s, since_ms): return self.fill
    def cancel_open_orders(self, s): return self.cancel_return
    def get_asset_balance_fresh(self, s): return self.free
    def create_oco_sell(self, s, q, tp, sl):
        self.oco_created.append((s, q, tp, sl))
        if self.oco_return != "default":
            return self.oco_return
        return {"orderListId": 99}
    def market_buy(self, s, quantity=0, usdt_amount=0):
        if self.buy_return != "default":
            return self.buy_return
        return {"status": "FILLED", "executedQty": "1.0", "cummulativeQuoteQty": "20.0", "fills": []}
    def net_executed_qty(self, order, symbol):
        return float(order.get("executedQty", 0) or 0) if order else 0.0
    def market_sell(self, s, q):
        self.sold.append(q)
        if self.sell_return != "default":
            return self.sell_return
        return {"fills": [{"price": "100", "qty": str(q)}], "executedQty": str(q)}
    def avg_fill_price(self, o): return 100.0
    def get_candles(self, *a, **k): raise RuntimeError("no charts in tests")


class TestBotMonitor(InTempDir):
    def setUp(self):
        super().setUp()
        sys.modules.setdefault("chart_logger", types.SimpleNamespace(save_trade_chart=lambda *a, **k: None))
        import bot
        from risk_manager import RiskManager
        self.bot = bot
        bot.cfg = make_cfg(POSITIONS_FILE=os.path.join(self.tmp, "positions.json"),
                           TRADE_LOG_FILE=os.path.join(self.tmp, "trade_log.json"))
        self.ex = FakeBotExchange()
        self.rm = RiskManager(bot.cfg)
        self.rm.set_exchange(self.ex)
        self.nt = FakeNotifier()
        self.eng = bot.ScannerEngine(self.ex, self.rm, self.nt)
        self.pos = self.rm.open_position("AAAUSDT", 100.0, 1.0, kind="scanner", atr_value=1.0, entry_score=80)

    def tick(self):
        self.eng._monitor_tick()

    def test_oco_still_open_position_stays(self):
        self.pos.details["oco_order_list_id"] = 5
        self.ex.open_orders = [{"orderId": 1}, {"orderId": 2}]
        self.tick()
        self.assertTrue(self.rm.has_position("AAAUSDT"))

    def test_oco_take_profit_uses_real_fill(self):
        self.pos.details["oco_order_list_id"] = 5
        self.ex.open_orders = []
        self.ex.fill = {"price": 105.0, "qty": 1.0, "type": "LIMIT_MAKER"}
        self.tick()
        self.assertFalse(self.rm.has_position("AAAUSDT"))
        row = read_json(self.bot.cfg.TRADE_LOG_FILE)[-1]
        self.assertEqual(row["reason"], "OCO_TAKE_PROFIT")
        self.assertAlmostEqual(row["exit_price"], 105.0)
        self.assertIn("AAAUSDT", self.eng._symbol_cooldowns)

    def test_oco_stop_loss_reason(self):
        self.pos.details["oco_order_list_id"] = 5
        self.ex.open_orders = []
        self.ex.fill = {"price": 98.5, "qty": 1.0, "type": "STOP_LOSS_LIMIT"}
        self.tick()
        self.assertEqual(read_json(self.bot.cfg.TRADE_LOG_FILE)[-1]["reason"], "OCO_STOP_LOSS")

    def test_api_failure_is_not_a_fill(self):
        self.pos.details["oco_order_list_id"] = 5
        self.ex.open_orders = None
        self.tick()
        self.assertTrue(self.rm.has_position("AAAUSDT"))

    def test_no_oco_means_no_inference(self):
        self.ex.open_orders = []
        self.tick()
        self.assertTrue(self.rm.has_position("AAAUSDT"))
        self.assertEqual(self.ex.strict_calls, 0)

    def test_oco_gone_without_fill_drops_link_keeps_position(self):
        self.pos.details["oco_order_list_id"] = 5
        self.ex.open_orders = []
        self.ex.fill = None
        self.tick()
        self.assertTrue(self.rm.has_position("AAAUSDT"))
        self.assertNotIn("oco_order_list_id", self.rm.get_position("AAAUSDT").details)

    def test_close_sells_only_the_position(self):
        self.eng._close_position("AAAUSDT", self.pos, 100.0, "TEST")
        self.assertEqual(self.ex.sold, [1.0])      # not the 5.0 free balance

    def test_failed_sell_keeps_position_open(self):
        """If market_sell returns None (failed order), position must stay open in RiskManager."""
        self.ex.sell_return = None
        self.eng._close_position("AAAUSDT", self.pos, 100.0, "STOP_LOSS")
        self.assertTrue(self.rm.has_position("AAAUSDT"))
        self.assertEqual(len(self.nt.closed), 0)
        self.assertNotIn("AAAUSDT", self.eng._symbol_cooldowns)

    def test_oco_resynced_when_break_even_arms(self):
        """When price crosses Break-Even trigger while holding an OCO, _monitor_tick updates the OCO on Binance."""
        self.pos.details["oco_order_list_id"] = 5
        self.pos.take_profit = 110.0
        self.ex.open_orders = [{"orderId": 1}, {"orderId": 2}]
        self.ex.price = 103.0  # +3.0% gain triggers break-even (+2.2% threshold), still below TP (110.0)
        self.tick()
        self.assertEqual(len(self.ex.oco_created), 1)
        sym, qty, tp, sl = self.ex.oco_created[0]
        self.assertEqual(sym, "AAAUSDT")
        self.assertAlmostEqual(sl, 100.3, places=3)
        self.assertEqual(self.pos.details["oco_order_list_id"], 99)

    def test_partial_sell_retains_remaining_position_and_logs_realized_slice(self):
        """If market_sell partially executes (0.6 of 1.0), keep 0.4 open and log PARTIAL_ realized PnL."""
        self.ex.sell_return = {"fills": [{"price": "100", "qty": "0.6"}], "executedQty": "0.6"}
        self.eng._close_position("AAAUSDT", self.pos, 100.0, "STOP_LOSS")
        self.assertTrue(self.rm.has_position("AAAUSDT"))
        self.assertAlmostEqual(self.rm.get_position("AAAUSDT").quantity, 0.4, places=6)
        self.assertEqual(len(self.nt.closed), 0)
        row = read_json(self.bot.cfg.TRADE_LOG_FILE)[-1]
        self.assertEqual(row["reason"], "PARTIAL_STOP_LOSS")
        self.assertAlmostEqual(row["quantity"], 0.6, places=6)

    def test_partial_sell_dust_remainder_finalizes_cleanly(self):
        """If LOT_SIZE rounding leaves unsellable dust (e.g. 0.00986 out of 0.13986 with stepSize=0.01),
        is_sellable_qty returns False so the position closes cleanly without an infinite dust loop."""
        self.pos.quantity = 0.13986
        self.ex.is_sellable_qty = lambda s, q, px: q >= 0.01
        self.ex.sell_return = {"fills": [{"price": "100", "qty": "0.13"}], "executedQty": "0.13"}
        self.eng._close_position("AAAUSDT", self.pos, 100.0, "TAKE_PROFIT")
        self.assertFalse(self.rm.has_position("AAAUSDT"))
        row = read_json(self.bot.cfg.TRADE_LOG_FILE)[-1]
        self.assertEqual(row["reason"], "TAKE_PROFIT")
        self.assertAlmostEqual(row["quantity"], 0.13, places=6)

    def test_entry_aborts_on_unfilled_new_order(self):
        """Market buy returning status=NEW with executedQty=0 must never create a phantom position."""
        from scanner import ScanResult
        self.ex.buy_return = {"status": "NEW", "executedQty": "0.0", "cummulativeQuoteQty": "0.0", "fills": []}
        res = ScanResult(
            symbol="BBBUSDT", score=85.0, raw_score=80.0, pump_penalty=0.0,
            ema_score=25.0, rsi_score=25.0, vol_score=25.0, breakout_score=5.0, ob_score=5.0,
            close_price=50.0, rsi=18.0, volume_ratio=2.5, breakout_atr_ratio=0.0,
            bid_ask_ratio=1.4, atr=1.0, pct_change_24h=-5.0,
            reason="CAPITULATION", last_closed_time=1000, timeframe="15m"
        )
        self.eng._execute_entry(res)
        self.assertFalse(self.rm.has_position("BBBUSDT"))

    def test_oco_replacement_and_cancellation_failure_retries_next_tick(self):
        """If OCO cancellation or replacement fails, oco_needs_sync stays True and retries on the next tick."""
        self.pos.details["oco_order_list_id"] = 5
        self.pos.take_profit = 110.0
        self.ex.open_orders = [{"orderId": 1}, {"orderId": 2}]
        self.ex.price = 103.0  # triggers Break-Even

        # Tick 1: cancel_open_orders fails -> keeps old OCO id 5 and marks oco_needs_sync=True
        self.ex.cancel_return = False
        self.tick()
        self.assertEqual(self.pos.details.get("oco_order_list_id"), 5)
        self.assertTrue(self.pos.details.get("oco_needs_sync"))
        self.assertEqual(len(self.ex.oco_created), 0)

        # Tick 2: cancel succeeds, but create_oco_sell returns None -> oco_needs_sync stays True
        self.ex.cancel_return = True
        self.ex.oco_return = None
        self.tick()
        self.assertIsNone(self.pos.details.get("oco_order_list_id"))
        self.assertTrue(self.pos.details.get("oco_needs_sync"))
        self.assertEqual(len(self.ex.oco_created), 1)

        # Tick 3: create_oco_sell succeeds -> oco_order_list_id=99 and oco_needs_sync=False
        self.ex.oco_return = {"orderListId": 99}
        self.tick()
        self.assertEqual(self.pos.details.get("oco_order_list_id"), 99)
        self.assertFalse(self.pos.details.get("oco_needs_sync"))
        self.assertEqual(len(self.ex.oco_created), 2)

    def test_oco_sync_deferred_near_stop_or_tp_retries_next_tick(self):
        """If price is too close to SL/TP when Break-Even arms, OCO sync is deferred and retried on the next tick."""
        self.pos.details["oco_order_list_id"] = 5
        self.pos.take_profit = 103.1
        self.ex.open_orders = [{"orderId": 1}, {"orderId": 2}]

        # Tick 1: price is 103.0 (within 0.2% of TP 103.1, below TP so action=="HOLD") -> Break-Even arms, OCO sync deferred
        self.ex.price = 103.0
        self.tick()
        self.assertEqual(len(self.ex.oco_created), 0)
        self.assertTrue(self.pos.details.get("oco_needs_sync"))
        self.assertEqual(self.pos.details.get("oco_order_list_id"), 5)

        # Tick 2: price settles to 102.6 (safe distance from TP 103.1 and above trailing stop 102.28; stop_loss doesn't change on tick 2)
        self.ex.price = 102.6
        self.tick()
        self.assertEqual(len(self.ex.oco_created), 1)
        self.assertEqual(self.pos.details.get("oco_order_list_id"), 99)
        self.assertFalse(self.pos.details.get("oco_needs_sync"))

    def test_telegram_timestamps_and_execution_times(self):
        """Verify TelegramNotifier formats timestamps in Europe/Belgrade (CET UTC+1 / CEST UTC+2)
        and uses actual Binance execution timestamps."""
        from notifier import TelegramNotifier
        cfg = make_cfg(ENABLE_TELEGRAM=True, TELEGRAM_TOKEN="t", TELEGRAM_CHAT_ID="c",
                       DISPLAY_TIMEZONE="", DISPLAY_TZ_OFFSET_HOURS=1)
        nt = TelegramNotifier(cfg)
        sent = []
        nt.send = lambda msg, parse_mode=None: sent.append(msg) or True
        nt.trade_opened("AAAUSDT", "BUY", 100.0, 1.0, 95.0, 105.0, timeframe="15m", open_time=0.0)
        self.assertIn("1970-01-01 01:00:00 (UTC+1)", sent[-1])
        nt.trade_closed("AAAUSDT", "TAKE_PROFIT", 100.0, 102.0, 2.0, duration_sec=3600, pnl_usdt=2.0,
                        open_time=0.0, close_time=3600.0)
        self.assertIn("1970-01-01 01:00:00 (UTC+1)", sent[-1])
        self.assertIn("1970-01-01 02:00:00 (UTC+1)", sent[-1])

        # Verify DST-aware Europe/Belgrade (2026-07-01 12:00:00 UTC -> 14:00:00 CEST / UTC+2;
        # 2026-11-12 12:00:00 UTC -> 13:00:00 CET / UTC+1)
        nt_dst = TelegramNotifier(make_cfg(ENABLE_TELEGRAM=True, TELEGRAM_TOKEN="t", TELEGRAM_CHAT_ID="c",
                                           DISPLAY_TIMEZONE="Europe/Belgrade", DISPLAY_TZ_OFFSET_HOURS=1))
        summer_ts = pd.Timestamp("2026-07-01 12:00:00", tz="UTC").timestamp()
        winter_ts = pd.Timestamp("2026-11-12 12:00:00", tz="UTC").timestamp()
        self.assertIn("2026-07-01 14:00:00 (UTC+2)", nt_dst._format_ts(summer_ts))
        self.assertIn("2026-11-12 13:00:00 (UTC+1)", nt_dst._format_ts(winter_ts))

    def _make_live_stack(self, client_overrides=None):
        """Wire real ScannerEngine + real BinanceConnector + real RiskManager against an offline FakeClient."""
        import exchange
        from risk_manager import RiskManager

        sub_dir = tempfile.mkdtemp(dir=self.tmp)
        os.chdir(sub_dir)

        fc = FakeClient()
        fc.balances = [
            {"asset": "USDT", "free": "100", "locked": "0"},
            {"asset": "AAA", "free": "1.0", "locked": "0"},
            {"asset": "BBB", "free": "0.5", "locked": "0"},
        ]
        fc.get_symbol_info = lambda s: {
            "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "MIN_NOTIONAL", "minNotional": "10.0"},
            ]
        }
        def _fake_cancel_open(symbol):
            fc.open_orders = []
            return []
        def _fake_create_oco(**kw):
            fc.oco_calls.append(kw)
            fc.open_orders.append({"orderListId": 7, "orderId": 701, "status": "NEW"})
            return {"orderListId": 7}
        fc.cancel_open_orders = _fake_cancel_open
        fc.create_oco_order = _fake_create_oco
        if client_overrides:
            for k, v in client_overrides.items():
                setattr(fc, k, v)

        cfg = make_cfg(
            POSITIONS_FILE=os.path.join(sub_dir, "positions.json"),
            TRADE_LOG_FILE=os.path.join(sub_dir, "trade_log.json"),
            MAX_RETRIES=1, RETRY_DELAY=0, USE_QUOTE_ORDER_QTY=True, USE_EXCHANGE_OCO=True,
        )
        orig_client = exchange.Client
        exchange.Client = lambda *a, **k: fc
        try:
            conn = exchange.BinanceConnector(cfg)
        finally:
            exchange.Client = orig_client
        self.bot.cfg = cfg
        rm = RiskManager(cfg)
        rm.set_exchange(conn)
        nt = FakeNotifier()
        eng = self.bot.ScannerEngine(conn, rm, nt)
        return fc, conn, rm, nt, eng, cfg

    def test_close_defers_when_oco_cancel_fails_and_preserves_oco_id(self):
        """If cancel_open_orders returns False, _close_position must preserve oco_order_list_id and NOT call market_sell."""
        self.pos.details["oco_order_list_id"] = 5
        self.ex.cancel_return = False
        self.eng._close_position("AAAUSDT", self.pos, 95.0, "STOP_LOSS")
        self.assertEqual(self.ex.sold, [])
        self.assertTrue(self.rm.has_position("AAAUSDT"))
        self.assertEqual(self.rm.get_position("AAAUSDT").details.get("oco_order_list_id"), 5)

        # Next attempt when OCO cancellation succeeds should sell cleanly
        self.ex.cancel_return = True
        self.eng._close_position("AAAUSDT", self.pos, 95.0, "STOP_LOSS")
        self.assertEqual(self.ex.sold, [1.0])
        self.assertFalse(self.rm.has_position("AAAUSDT"))

    def test_unresolved_sell_persisted_and_reconciled_without_duplicate_sell(self):
        """A NEW sell whose cancel fails must persist pending_sell_client_oid and never issue a 2nd market_sell across ticks/restarts."""
        sell_calls = []
        order_state = {"status": "NEW", "executedQty": "0.0", "cummulativeQuoteQty": "0.0"}

        def fake_market_sell(**kw):
            sell_calls.append(kw)
            return dict(order_state, symbol=kw["symbol"], clientOrderId=kw["newClientOrderId"])

        def fake_get_order(symbol, origClientOrderId):
            return dict(order_state, symbol=symbol, clientOrderId=origClientOrderId)

        def failing_cancel(symbol, origClientOrderId):
            raise RuntimeError("cancel failed")

        fc, conn, rm, nt, eng, cfg = self._make_live_stack({
            "order_market_sell": fake_market_sell,
            "get_order": fake_get_order,
            "cancel_order": failing_cancel,
        })
        pos = rm.open_position("AAAUSDT", 100.0, 1.0, kind="scanner", atr_value=1.0, entry_score=80)
        fc.price = 95.0

        # Tick 1 / Exit attempt 1: submits 1 market_sell which stays NEW; persists pending_sell_client_oid
        eng._close_position("AAAUSDT", pos, 95.0, "STOP_LOSS")
        self.assertEqual(len(sell_calls), 1)
        self.assertTrue(rm.has_position("AAAUSDT"))
        persisted = read_json(cfg.POSITIONS_FILE)["AAAUSDT"]["details"].get("pending_sell_client_oid")
        self.assertEqual(persisted, sell_calls[0]["newClientOrderId"])

        # Simulate process restart loading positions.json while order is still NEW
        from risk_manager import RiskManager
        rm2 = RiskManager(cfg)
        rm2.set_exchange(conn)
        eng2 = self.bot.ScannerEngine(conn, rm2, nt)
        eng2._monitor_tick()
        self.assertEqual(len(sell_calls), 1)  # MUST NOT submit a second sell!
        self.assertTrue(rm2.has_position("AAAUSDT"))

        # Now order settles FILLED on Binance -> monitor tick reconciles without another sell
        order_state.update({
            "status": "FILLED", "executedQty": "1.0",
            "cummulativeQuoteQty": "95.0", "updateTime": 1_700_000_000_000,
        })
        eng2._monitor_tick()
        self.assertEqual(len(sell_calls), 1)
        self.assertFalse(rm2.has_position("AAAUSDT"))
        self.assertEqual(len(nt.closed), 1)
        self.assertAlmostEqual(read_json(cfg.TRADE_LOG_FILE)[-1]["exit_price"], 95.0)

    def test_unresolved_buy_persisted_and_reconciled_before_duplicate_entry(self):
        """A PARTIALLY_FILLED/NEW buy whose cancel fails must persist in scanner_state.json,
        refuse duplicate entry on subsequent scans, and finalize once reconciled terminal."""
        from scanner import ScanResult
        buy_calls = []
        order_state = {"status": "PARTIALLY_FILLED", "executedQty": "0.4", "cummulativeQuoteQty": "8.0"}

        def fake_market_buy(**kw):
            buy_calls.append(kw)
            return dict(order_state, symbol=kw["symbol"], clientOrderId=kw["newClientOrderId"])

        def fake_get_order(symbol, origClientOrderId):
            return dict(order_state, symbol=symbol, clientOrderId=origClientOrderId)

        def failing_cancel(symbol, origClientOrderId):
            raise RuntimeError("cancel timeout")

        fc, conn, rm, nt, eng, cfg = self._make_live_stack({
            "price": 20.0,
            "order_market_buy": fake_market_buy,
            "get_order": fake_get_order,
            "cancel_order": failing_cancel,
        })
        res = ScanResult(
            symbol="BBBUSDT", score=85.0, raw_score=80.0, pump_penalty=0.0,
            ema_score=25.0, rsi_score=25.0, vol_score=25.0, breakout_score=5.0, ob_score=5.0,
            close_price=20.0, rsi=18.0, volume_ratio=2.5, breakout_atr_ratio=0.0,
            bid_ask_ratio=1.4, atr=0.5, pct_change_24h=-5.0,
            reason="CAPITULATION", last_closed_time=1000, timeframe="15m"
        )

        # Entry attempt leaves unresolved PARTIALLY_FILLED buy persisted in scanner_state.json
        eng._execute_entry(res)
        self.assertEqual(len(buy_calls), 1)
        self.assertFalse(rm.has_position("BBBUSDT"))
        self.assertEqual(eng._pending_buy_count(), 1)
        self.assertIn("BBBUSDT", read_json("scanner_state.json")["pending_buys"])

        # Subsequent direct entry, scan tick, or restart must NOT issue a duplicate buy while unresolved
        eng2 = self.bot.ScannerEngine(conn, rm, nt)
        self.assertEqual(eng2._pending_buy_count(), 1)
        eng2._execute_entry(res)
        eng2.scanner.check_btc_regime = lambda: (_ for _ in ()).throw(AssertionError("must not scan while buy is pending"))
        eng2._scan_tick()
        self.assertEqual(len(buy_calls), 1)

        # Binance later confirms CANCELED remainder with 0.4 filled -> monitor tick reconciles and opens position
        order_state.update({"status": "CANCELED", "executedQty": "0.4", "cummulativeQuoteQty": "8.0"})
        eng2._monitor_tick()
        self.assertEqual(len(buy_calls), 1)
        self.assertEqual(eng2._pending_buy_count(), 0)
        self.assertTrue(rm.has_position("BBBUSDT"))
        self.assertAlmostEqual(rm.get_position("BBBUSDT").quantity, 0.4, places=6)
        self.assertEqual(rm.get_position("BBBUSDT").details.get("oco_order_list_id"), 7)

    def test_oco_partial_fill_uses_fill_qty_and_retains_remainder(self):
        """Partial OCO fill (via orderListId) must call partial_close_position with fill['qty'] and retain remainder."""
        t0 = 1_700_000_000_000
        fc, conn, rm, nt, eng, cfg = self._make_live_stack()
        fc.price = 104.0
        pos = rm.open_position("AAAUSDT", 100.0, 1.0, kind="scanner", atr_value=1.0, entry_score=80)
        pos.open_time = (t0 - 60_000) / 1000.0
        pos.take_profit = 105.0
        pos.details["oco_order_list_id"] = 77
        rm.persist_positions()

        fc.orders = [
            # Unrelated manual fill must be ignored
            {"side": "SELL", "status": "FILLED", "orderListId": 999, "orderId": 500,
             "updateTime": t0 + 10_000, "executedQty": "1.0", "cummulativeQuoteQty": "110.0", "type": "MARKET"},
            # Partial fill on our OCO orderListId=77
            {"side": "SELL", "status": "FILLED", "orderListId": 77, "orderId": 771,
             "updateTime": t0 + 5000, "executedQty": "0.6", "cummulativeQuoteQty": "63.0", "type": "LIMIT_MAKER"},
            {"side": "SELL", "status": "EXPIRED", "orderListId": 77, "orderId": 772,
             "updateTime": t0 + 5000, "executedQty": "0", "cummulativeQuoteQty": "0", "type": "STOP_LOSS_LIMIT"},
        ]
        fc.get_order_list = lambda orderListId: {
            "orderListId": orderListId, "listOrderStatus": "ALL_DONE",
            "orders": [{"orderId": 771}, {"orderId": 772}],
        }

        eng._monitor_tick()
        self.assertTrue(rm.has_position("AAAUSDT"))
        rem_pos = rm.get_position("AAAUSDT")
        self.assertAlmostEqual(rem_pos.quantity, 0.4, places=6)
        self.assertTrue(rem_pos.details.get("oco_needs_sync"))
        self.assertIsNone(rem_pos.details.get("oco_order_list_id"))
        row = read_json(cfg.TRADE_LOG_FILE)[-1]
        self.assertEqual(row["reason"], "PARTIAL_OCO_TAKE_PROFIT")
        self.assertAlmostEqual(row["quantity"], 0.6, places=6)
        self.assertAlmostEqual(row["exit_price"], 105.0, places=4)

    def test_monitor_does_not_clear_in_flight_submitting_buy_race(self):
        """Concurrent _reconcile_pending_buys() during in-flight buy submission must never wipe
        pending_buys even if Binance does not yet know the order (-2013)."""
        import threading
        from binance.exceptions import BinanceAPIException
        from scanner import ScanResult

        buy_calls = []
        get_order_calls = []
        state_during_buy = {}

        def make_not_found():
            err = BinanceAPIException.__new__(BinanceAPIException)
            Exception.__init__(err, "Order does not exist")
            err.code = -2013
            return err

        fc, conn, rm, nt, eng, cfg = self._make_live_stack()
        fc.price = 20.0

        def fake_get_order(symbol, origClientOrderId):
            get_order_calls.append(origClientOrderId)
            # Before Binance registers the timed-out order on the very first timeout reconciliation check,
            # simulate -2013 if in-flight; after submission completes, simulate Binance having accepted it!
            if state_during_buy.get("submitted"):
                return {
                    "symbol": symbol, "clientOrderId": origClientOrderId,
                    "status": "FILLED", "executedQty": "1.0",
                    "cummulativeQuoteQty": "20.0", "updateTime": 1_700_000_000_000,
                }
            raise make_not_found()

        def fake_market_buy(**kw):
            buy_calls.append(kw)
            # Persist shows state == 'SUBMITTING' BEFORE HTTP call completes
            state_during_buy["disk_state"] = read_json("scanner_state.json")["pending_buys"]["BBBUSDT"]["state"]
            # Trigger concurrent monitor reconciliation from another thread while buy is STILL in flight
            t = threading.Thread(target=eng._reconcile_pending_buys)
            t.start()
            t.join(timeout=2)
            state_during_buy["after_race_pending"] = eng._pending_buy_count()
            # Simulate order actually reaching Binance and timing out on HTTP response
            state_during_buy["submitted"] = True
            raise RuntimeError("Read timed out after Binance accepted order")

        fc.order_market_buy = fake_market_buy
        fc.get_order = fake_get_order

        res = ScanResult(
            symbol="BBBUSDT", score=85.0, raw_score=80.0, pump_penalty=0.0,
            ema_score=25.0, rsi_score=25.0, vol_score=25.0, breakout_score=5.0, ob_score=5.0,
            close_price=20.0, rsi=18.0, volume_ratio=2.5, breakout_atr_ratio=0.0,
            bid_ask_ratio=1.4, atr=0.5, pct_change_24h=-5.0,
            reason="CAPITULATION", last_closed_time=1000, timeframe="15m"
        )
        eng._execute_entry(res)

        self.assertEqual(state_during_buy["disk_state"], "SUBMITTING")
        self.assertEqual(state_during_buy["after_race_pending"], 1)  # monitor MUST NOT have wiped it mid-flight
        self.assertEqual(len(buy_calls), 1)
        # Reconciled cleanly once fake_market_buy timed out and get_order found the FILLED order
        self.assertEqual(eng._pending_buy_count(), 0)
        self.assertTrue(rm.has_position("BBBUSDT"))

    def test_crash_after_open_position_before_clearing_pending_buy_preserves_oco_sync(self):
        """Position must be persisted in positions.json BEFORE clearing pending_buys; on restart with both
        present and no OCO yet, _reconcile_pending_buys clears pending_buys and preserves oco_needs_sync."""
        from scanner import ScanResult

        fc, conn, rm, nt, eng, cfg = self._make_live_stack()
        fc.price = 20.0
        saw_pending_during_open = []
        orig_open = rm.open_position

        def spy_open_position(*a, **kw):
            # When open_position() executes, scanner_state.json MUST still retain pending_buys["BBBUSDT"]
            saw_pending_during_open.append("BBBUSDT" in read_json("scanner_state.json")["pending_buys"])
            return orig_open(*a, **kw)

        rm.open_position = spy_open_position
        fc.order_market_buy = lambda **kw: {
            "symbol": kw["symbol"], "clientOrderId": kw["newClientOrderId"],
            "status": "FILLED", "executedQty": "1.0", "cummulativeQuoteQty": "20.0",
        }
        # Simulate crash right AFTER open_position saved positions.json, BEFORE _clear_pending_buy cleared disk
        orig_clear = eng._clear_pending_buy
        eng._clear_pending_buy = lambda s: (_ for _ in ()).throw(RuntimeError("simulated power loss"))

        res = ScanResult(
            symbol="BBBUSDT", score=85.0, raw_score=80.0, pump_penalty=0.0,
            ema_score=25.0, rsi_score=25.0, vol_score=25.0, breakout_score=5.0, ob_score=5.0,
            close_price=20.0, rsi=18.0, volume_ratio=2.5, breakout_atr_ratio=0.0,
            bid_ask_ratio=1.4, atr=0.5, pct_change_24h=-5.0,
            reason="CAPITULATION", last_closed_time=1000, timeframe="15m"
        )
        with self.assertRaises(RuntimeError):
            eng._execute_entry(res)

        self.assertEqual(saw_pending_during_open, [True])
        # Both positions.json and scanner_state.json exist on disk after the simulated crash
        self.assertIn("BBBUSDT", read_json(cfg.POSITIONS_FILE))
        self.assertIn("BBBUSDT", read_json("scanner_state.json")["pending_buys"])

        # Restart process cleanly: loads both files, reconciles, clears pending_buys, places protective OCO
        from risk_manager import RiskManager
        rm2 = RiskManager(cfg)
        rm2.set_exchange(conn)
        eng2 = self.bot.ScannerEngine(conn, rm2, nt)
        eng2._monitor_tick()

        self.assertEqual(eng2._pending_buy_count(), 0)
        self.assertTrue(rm2.has_position("BBBUSDT"))
        self.assertEqual(rm2.get_position("BBBUSDT").details.get("oco_order_list_id"), 7)


# =============================================================== backtester
class TestBacktest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import backtest
        cls.bt = backtest
        cls.df = make_series(3)

    def test_threshold_comes_from_config(self):
        self.assertEqual(self.bt.StrategyBacktester().min_score, float(real_cfg.MIN_SCORE_TO_TRADE))
        self.assertEqual(self.bt.StrategyBacktester(min_score=60).min_score, 60.0)

    def test_uses_live_stop_rules(self):
        t = self.bt.StrategyBacktester(initial_capital=1000, trade_size_usdt=1000)
        t.run("SYN", self.df)
        self.assertGreater(len(t.trades), 5)
        lo, hi = real_cfg.MIN_SL_PCT, real_cfg.SCANNER_MAX_STOP_LOSS_PCT
        for tr in t.trades:
            self.assertGreaterEqual(tr["sl_pct"], lo - 1e-9)
            self.assertLessEqual(tr["sl_pct"], hi + 1e-9)
            self.assertGreaterEqual(tr["tp_pct"], real_cfg.MIN_TP_PCT - 1e-9)
            self.assertGreaterEqual(tr["bars_held"], 0)
            self.assertIn(tr["reason"], {"STOP_LOSS", "TAKE_PROFIT", "TRAILING_STOP", "TREND_INVALIDATED", "HOLD_TIMEOUT"})

    def test_entry_bar_range_is_checked(self):
        n = 120
        idx = pd.date_range("2026-07-01", periods=n, freq="15min", tz="UTC")
        base = np.full(n, 100.0)
        df = pd.DataFrame({"open": base, "high": base + 0.1, "low": base - 0.1, "close": base, "volume": 1.0, "quote_vol": 100.0}, index=idx)
        df.iloc[55, df.columns.get_loc("low")] = 98.0          # the entry bar itself dives through a ~1.5% stop
        t = self.bt.StrategyBacktester(initial_capital=1000, trade_size_usdt=1000)
        t.run("FLAT", df, entry_mode="random", random_prob=1.0, seed=1)
        self.assertEqual(t.trades[0]["bars_held"], 0)
        self.assertEqual(t.trades[0]["reason"], "STOP_LOSS")
        t2 = self.bt.StrategyBacktester(initial_capital=1000, trade_size_usdt=1000)
        t2.run("FLAT", df, entry_mode="random", random_prob=1.0, seed=1, _check_entry_bar=False)
        self.assertGreater(t2.trades[0]["bars_held"], 0)       # old behaviour: the dive was silently ignored

    def test_random_baseline_runs(self):
        t = self.bt.StrategyBacktester(initial_capital=1000, trade_size_usdt=1000, min_score=60)
        t.run("SYN", self.df)
        base = self.bt.random_baseline("SYN", self.df, t.trades, n_runs=3, capital=1000, trade_size=1000, min_score=60, seed=1)
        self.assertEqual(base["runs"], 3)
        self.assertGreater(np.mean(base["counts"]), 0.3 * len(t.trades))

    def test_green_candle_rallies_to_tp_without_false_be_stopout(self):
        """A green candle opening at entry ($100) and rallying straight to TP ($107)
        must hit TAKE_PROFIT, not falsely trigger a same-bar break-even STOP_LOSS at $100."""
        n = 120
        idx = pd.date_range("2026-07-01", periods=n, freq="15min", tz="UTC")
        base = np.full(n, 100.0)
        df = pd.DataFrame({
            "open": base, "high": base + 0.5, "low": base - 0.5, "close": base,
            "volume": 100.0, "quote_vol": 10000.0
        }, index=idx)
        # Bar 55: enters at open=100.0, low=100.0 (never drops below entry), rallies to 107.0 (above TP)
        df.iloc[55, df.columns.get_loc("open")] = 100.0
        df.iloc[55, df.columns.get_loc("low")] = 100.0
        df.iloc[55, df.columns.get_loc("high")] = 107.0
        df.iloc[55, df.columns.get_loc("close")] = 106.5

        t = self.bt.StrategyBacktester(initial_capital=1000, trade_size_usdt=100)
        t.run("RALLY", df, entry_mode="random", random_prob=1.0, seed=1)
        self.assertGreater(len(t.trades), 0)
        self.assertEqual(t.trades[0]["reason"], "TAKE_PROFIT")
        self.assertGreater(t.trades[0]["pnl_usdt"], 0.0)


# =============================================================== accounting & portfolio tests
class TestAccountingAndPortfolio(unittest.TestCase):
    def test_end_of_data_exit_accounting(self):
        """Verify that positions open at the final bar are closed cleanly at close,
        and capital reflects only net PnL (not phantom trade size proceeds)."""
        import backtest
        n = 80
        idx = pd.date_range("2026-07-01", periods=n, freq="1h", tz="UTC")
        base = np.full(n, 100.0)
        # Flat series where no SL or TP is hit
        df = pd.DataFrame({
            "open": base, "high": base + 0.1, "low": base - 0.1, "close": base,
            "volume": 100.0, "quote_vol": 10000.0
        }, index=idx)
        # Enter on bar 70 and hold through bar 79 (end of data)
        t = backtest.StrategyBacktester(initial_capital=1000.0, trade_size_usdt=100.0)
        m = t.run("FLAT", df, entry_mode="random", random_prob=1.0, seed=42)

        self.assertGreater(len(t.trades), 0)
        last_trade = t.trades[-1]
        self.assertEqual(last_trade["exit_reason"], "END_OF_DATA")

        # Capital must strictly equal initial_capital + total net PnL
        net_pnl = sum(tr["pnl_usdt"] for tr in t.trades)
        self.assertAlmostEqual(t.capital, 1000.0 + net_pnl, places=2)
        self.assertAlmostEqual(m["final_capital"], 1000.0 + net_pnl, places=2)

        # Confirm the old bug (phantom +$100 proceeds) is absent:
        # PnL on a flat asset after round-trip fees/slippage is negative, so capital < 1000.
        self.assertLess(t.capital, 1000.0)
        self.assertLess(m["net_return_pct"], 0.0)

    def test_accounting_invariant_assertion_fails_on_mismatch(self):
        """Verify that an accounting discrepancy immediately raises an AssertionError."""
        import backtest
        t = backtest.StrategyBacktester(initial_capital=1000.0, trade_size_usdt=100.0)
        t.trades = [{
            "symbol": "BTCUSDT", "pnl_usdt": 10.0, "pnl_pct": 10.0,
            "bars_held": 5, "capital_after": 1010.0
        }]
        # Actual capital is intentionally set incorrectly to 1050.0
        t.capital = 1050.0

        with self.assertRaises(AssertionError) as ctx:
            t.compute_metrics()
        self.assertIn("Accounting mismatch in StrategyBacktester", str(ctx.exception))

    def test_simulate_portfolio_chronological_and_drawdown(self):
        """Verify simulate_portfolio runs without NameError, respects max_positions,
        and computes drawdown properly."""
        from validate import simulate_portfolio
        t0 = pd.Timestamp("2026-08-01 10:00:00", tz="UTC")
        trades = [
            # Trade 1: 10:00 to 12:00, +$5
            {"entry_time": t0, "exit_time": t0 + pd.Timedelta(hours=2), "pnl_usdt": 5.0},
            # Trade 2: 10:30 to 13:00, -$3
            {"entry_time": t0 + pd.Timedelta(minutes=30), "exit_time": t0 + pd.Timedelta(hours=3), "pnl_usdt": -3.0},
            # Trade 3: 11:00 to 11:30, +$2 (skipped under max_positions=2 because Trades 1 & 2 are open)
            {"entry_time": t0 + pd.Timedelta(hours=1), "exit_time": t0 + pd.Timedelta(minutes=90), "pnl_usdt": 2.0},
            # Trade 4: 12:15 - taken under max_positions=2 because Trade 1 exited at 12:00
            {"entry_time": t0 + pd.Timedelta(minutes=135), "exit_time": t0 + pd.Timedelta(hours=4), "pnl_usdt": 4.0},
        ]

        # Test with max_positions=2:
        # At 10:00: Trade 1 entered (open=1)
        # At 10:30: Trade 2 entered (open=2)
        # At 11:00: Trade 3 attempted -> skipped (open=2 >= max_positions=2)
        # At 12:00: Trade 1 exits -> open drops to 1
        # At 12:15: Trade 4 entered (open=2)
        # Total taken: 3 (Trades 1, 2, 4), skipped: 1 (Trade 3)
        res2 = simulate_portfolio(trades, initial_capital=1000.0, trade_size_usdt=100.0, max_positions=2)
        self.assertEqual(res2["trades_taken"], 3)
        self.assertEqual(res2["trades_skipped"], 1)
        self.assertIn("max_drawdown_pct", res2)
        self.assertIsInstance(res2["max_drawdown_pct"], float)

        # Test with max_positions=4: all trades taken
        res4 = simulate_portfolio(trades, initial_capital=1000.0, trade_size_usdt=100.0, max_positions=4)
        self.assertEqual(res4["trades_taken"], 4)
        self.assertEqual(res4["trades_skipped"], 0)
        self.assertAlmostEqual(res4["net_profit_usdt"], 8.0, places=2)
        self.assertAlmostEqual(res4["final_capital"], 1008.0, places=2)

        # Verify bar-by-bar mark-to-market captures intra-trade unrealized drawdown
        # even when a trade ultimately recovers to flat (pnl_usdt = 0.0).
        idx = pd.date_range("2026-08-01 10:00:00", periods=4, freq="1h", tz="UTC")
        df_mtm = pd.DataFrame({
            "open":  [100.0, 100.0, 100.0, 100.0],
            "high":  [100.0, 100.0, 100.0, 100.0],
            "low":   [100.0,  80.0,  95.0, 100.0],  # -20% intratrade drop on bar 1
            "close": [100.0,  85.0,  98.0, 100.0],
            "volume": [10.0, 10.0, 10.0, 10.0],
            "quote_vol": [1000.0, 1000.0, 1000.0, 1000.0],
        }, index=idx)
        mtm_trade = [{
            "symbol": "TESTUSDT",
            "entry_time": idx[0],
            "exit_time": idx[3],
            "entry_price": 100.0,
            "pnl_usdt": 0.0,
            "mae_pct": -20.0,
        }]
        res_mtm = simulate_portfolio(mtm_trade, initial_capital=1000.0, trade_size_usdt=100.0,
                                     max_positions=1, symbol_dfs={"TESTUSDT": df_mtm})
        self.assertAlmostEqual(res_mtm["max_drawdown_pct"], 2.0, places=2)  # 20% of $100 = $20 / $1000 = 2.0%

    def test_simulate_portfolio_captures_exit_candle_adverse_excursion(self):
        """Worst intra-trade excursion occurring on the trade's final exit candle must still count in MTM drawdown."""
        from validate import simulate_portfolio
        idx = pd.date_range("2026-08-02 10:00:00", periods=4, freq="1h", tz="UTC")
        df_exit_dip = pd.DataFrame({
            "open":  [100.0, 100.0, 100.0, 100.0],
            "high":  [100.5, 100.5, 100.5, 104.5],
            "low":   [100.0, 100.0, 100.0,  82.0],  # -18% dip ONLY on exit bar idx[3]
            "close": [100.0, 100.0, 100.0, 104.0],
            "volume": [10.0, 10.0, 10.0, 10.0],
            "quote_vol": [1000.0, 1000.0, 1000.0, 1000.0],
        }, index=idx)

        trade_with_mae = [{
            "symbol": "TESTUSDT",
            "entry_time": idx[0],
            "exit_time": idx[3],
            "entry_price": 100.0,
            "pnl_usdt": 4.0,
            "mae_pct": -18.0,
        }]
        res = simulate_portfolio(trade_with_mae, initial_capital=1000.0, trade_size_usdt=100.0,
                                 max_positions=1, symbol_dfs={"TESTUSDT": df_exit_dip})
        self.assertAlmostEqual(res["max_drawdown_pct"], 1.8, places=2)  # 18% of $100 = $18 / $1000 = 1.8%

        # Even if mae_pct is absent, exit candle bar_low alone must still register the drawdown
        trade_no_mae = [{
            "symbol": "TESTUSDT",
            "entry_time": idx[0],
            "exit_time": idx[3],
            "entry_price": 100.0,
            "pnl_usdt": 4.0,
        }]
        res_bar_only = simulate_portfolio(trade_no_mae, initial_capital=1000.0, trade_size_usdt=100.0,
                                          max_positions=1, symbol_dfs={"TESTUSDT": df_exit_dip})
        self.assertAlmostEqual(res_bar_only["max_drawdown_pct"], 1.8, places=2)

    def test_sanitize_json_removes_infinity_and_nan(self):
        """Ensure validate._save writes strict RFC 8259 JSON without bare Infinity/NaN."""
        from validate import _sanitize_json
        raw = {"pf": float("inf"), "neg": float("-inf"), "bad": float("nan"), "ok": [1.5, float("inf")]}
        clean = _sanitize_json(raw)
        dumped = json.dumps(clean, allow_nan=False)
        parsed = json.loads(dumped)
        self.assertIsNone(parsed["pf"])
        self.assertIsNone(parsed["neg"])
        self.assertIsNone(parsed["bad"])
        self.assertEqual(parsed["ok"], [1.5, None])

    def test_experiment_signals_and_gate0(self):
        """Verify experiment.py signals and Gate 0 audit pass without error."""
        import experiment
        self.assertTrue(experiment.run_gate_0())

        # Test signals on synthetic data
        n = 100
        idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
        base = np.linspace(100, 150, n)
        df_trend = pd.DataFrame({
            "open": base, "high": base + 1.0, "low": base - 0.5, "close": base + 0.5,
            "volume": 1000.0, "quote_vol": 100000.0
        }, index=idx)

        sig_b = experiment.evaluate_signals(df_trend, "breakout")
        sig_t = experiment.evaluate_signals(df_trend, "trend")
        sig_m = experiment.evaluate_signals(df_trend, "mean_reversion")

        self.assertEqual(len(sig_b), n)
        self.assertEqual(len(sig_t), n)
        self.assertEqual(len(sig_m), n)


if __name__ == "__main__":
    unittest.main()
