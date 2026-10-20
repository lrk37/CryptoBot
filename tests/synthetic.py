"""Synthetic 15m candle generator for offline tests (no network needed).
Zero-drift random walk with fat tails, volatility clustering and volume tied to |return| -- i.e. a market
with NO edge by construction. Useful as a 'what does pure noise look like in this backtester?' benchmark."""
import numpy as np
import pandas as pd


def make_series(seed: int, n: int = 8640) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    sigma0 = rng.lognormal(np.log(0.0040), 0.25)           # ~0.4% per 15m bar
    h = np.zeros(n)
    for t in range(1, n):
        h[t] = 0.98 * h[t - 1] + 0.10 * rng.standard_normal()
    sig = sigma0 * np.exp(h - h.var() / 2)
    z = rng.standard_t(4, n) / np.sqrt(2.0)                # unit-variance fat tails
    r = sig * z
    close = 100 * np.exp(np.cumsum(r))
    open_ = np.concatenate([[100.0], close[:-1]])
    wick_u = np.abs(rng.standard_normal(n)) * sig * 0.5
    wick_d = np.abs(rng.standard_normal(n)) * sig * 0.5
    high = np.maximum(open_, close) * (1 + wick_u)
    low = np.minimum(open_, close) * (1 - wick_d)
    vol = 1000 * rng.lognormal(0, 0.5, n) * (1 + 2 * np.abs(r) / sigma0)
    idx = pd.date_range("2026-07-01", periods=n, freq="15min", tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": vol, "quote_vol": vol * close}, index=idx)
