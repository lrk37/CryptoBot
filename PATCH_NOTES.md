# Patch notes (applied to CryptoBot-main_1.zip)

Run the offline tests (no network or API keys needed): `python -m unittest discover -s tests -v`
Nothing here has been run against the real Binance API; the tests use fakes. Verify on testnet first (see bottom).

## Bugs fixed

| # | Where | Problem | Fix |
|---|-------|---------|-----|
| 1 | `risk_manager.py` | The "minimum stop" used `max()`, so it acted as a **1.5% ceiling**. ATR multiplier and max-stop settings had no effect live (ATR 1.0% -> 1.5% stop instead of 2.5%). `MIN_TP_PCT` was never applied. | New shared `compute_stops()`: SL = ATR x mult, clamped to [`MIN_SL_PCT`, max stop]; TP floored at `MIN_TP_PCT`. |
| 2 | `bot.py`, `exchange.py` | Balances were read as *free* only. Coins locked in an OCO looked like zero, so the monitor declared "OCO filled" on the first poll, booked a fake close and stopped managing the position. Same flaw in startup cleanup and portfolio value. | OCO fills are detected from **order state** (open orders + filled SELL history), never from balances. API failures are not treated as fills. Startup check and portfolio value use free+locked / tracked quantity. |
| 3 | `exchange.py`, `bot.py` | OCO quantity and tracked quantity were the gross fill; commission taken in the coin made OCO placement fail. | `net_executed_qty()`; OCO quantity clamped to the free balance. |
| 4 | `bot.py` | Exit sold the account's entire free balance of the coin, not the position. | Sells `min(free, position quantity)`. |
| 5 | `risk_manager.py` | `trade_log.json` P&L was gross, and OCO exits were priced at the poll price. | Added `net_pnl_pct` / `net_pnl_usdt` (after fees); OCO exits use the real fill price. Gross fields kept. |
| 6 | `scanner.py` | The cap (`MAX_SCAN_CANDIDATES`) kept the first N pairs in Binance's order, not the most liquid. | Sorted by 24h quote volume before the cap. |
| 7 | `risk_manager.py` | Live after-8h exit (EMA9 and -1%) did not match the EMA21 rule in your v2 backtest and the review. | Live now matches: after 8h exit if the last *closed* 15m candle is below EMA21; hard exit at 16h. A data error means hold. |

## Backtester (`backtest.py`)
* Entry threshold now comes from `config.py` (it was hard-coded to 60). `--min_score` overrides.
* Stops, take-profit, break-even and trailing use the same functions as the live bot (the old backtest used a fixed 2% stop and ignored the ATR logic).
* Fixed: the entry bar's own high/low was never checked against the stop. On pure-noise data the effect is tiny (~0.01%/trade) but it is a correctness fix.
* Fixed: an `elif` chain meant the 8h/16h timeouts never ran once a trade had peaked above +4%.
* Trailing exits now fill at the open when a bar gaps through the trail level.
* Sortino uses the standard downside deviation; Sharpe/Sortino annualise by trades per year (the old version used std of losing trades only and a cap).
* New: `--baseline N` runs N random-entry backtests with the same exits and costs and tells you how often coin-flip entries do at least as well as the signal.

## Behaviour changes to expect
* Live stops are wider for most coins (clamped 1.5%-3.0% instead of at most 1.5%), and TP is at least 3%.
* Trade log has new `net_*` fields; log lines show gross and net.

## Not fixed (your call)
* The backtest cannot model: order-book bonus/filter, 24h pump penalty, BTC regime filter, top-N ranking, 2-candle confirmation, score>=80 fast-track. It tests the core signal and exits only.
* Score >= 80 skips the BTC filter and confirmation (`bot.py`, `_scan_tick`).
* Break-even and trailing both arm at +4.2% (`ROUND_TRIP_FEE_PCT` + `MIN_TRAILING_PROFIT_PCT`), so `BREAK_EVEN_TRIGGER_PCT = 3.5` is inert. For coins with 15m ATR below ~0.84% the take-profit (5 x ATR) is hit before either can arm.
* Break-even/trailing/timeout exits are client-side (30s polling). The exchange OCO keeps the original stop.
* `create_oco_order` uses the old OCO parameters (python-binance 1.0.19 pinned). Binance has deprecated that endpoint; confirm `[OCO SUCCESS]` appears in your logs.
* All thresholds/multipliers were tuned on one 90-day window. Re-validate on 2-3 years with an out-of-sample split.

## Verify on testnet
1. `grep -E "OCO SUCCESS|OCO FAILED|EXCHANGE FILL" bot.log` -- OCOs should be placed, and "EXCHANGE FILL" should only appear when an OCO leg really filled (with a realistic fill price).
2. `trade_log.json`: no more `OCO_*` trades lasting under a minute; compare `net_pnl_pct` to `pnl_pct`.
3. Backtest: `python backtest.py --symbol AVAXUSDT --days 90 --trade_size 100 --baseline 100`
