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
        rm, cfg = self.make_rm()
        p = self.pos(0.1)
        rm.evaluate(p, 103.9)
        self.assertEqual(p.min_be_price, 0.0)
        rm.evaluate(p, 104.3)
        self.assertGreater(p.min_be_price, 0.0)

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
    def get_price(self, s): return 100.0
    def get_open_orders_strict(self, s):
        self.strict_calls += 1
        return self.open_orders
    def get_last_sell_fill(self, s, since_ms): return self.fill
    def cancel_open_orders(self, s): pass
    def get_asset_balance_fresh(self, s): return self.free
    def market_sell(self, s, q):
        self.sold.append(q); return {"fills": [{"price": "100", "qty": str(q)}], "executedQty": str(q)}
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
            # Trade 3: 11:00 to 11:30, +$2 (exits before Trade 1 & 2)
            {"entry_time": t0 + pd.Timedelta(hours=1), "exit_time": t0 + pd.Timedelta(minutes=90), "pnl_usdt": 2.0},
            # Trade 4: 11:15 - should be skipped if max_positions=2, or accepted if max_positions=3
            {"entry_time": t0 + pd.Timedelta(minutes=75), "exit_time": t0 + pd.Timedelta(hours=4), "pnl_usdt": 4.0},
        ]

        # Test with max_positions=2: at 11:15, Trades 1, 2, and 3 are in the mix
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


if __name__ == "__main__":
    unittest.main()
