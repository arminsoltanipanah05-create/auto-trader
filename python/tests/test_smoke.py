"""Smoke tests on synthetic data. No network, no torch.

    python tests/test_smoke.py

Checks the four things that silently break momentum backtests:
  1. no lookahead in features
  2. vol targeting actually scales exposure inversely to vol
  3. costs equal turnover * rate
  4. positions are applied one day late
"""
from __future__ import annotations

import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import backtest  # noqa: E402
import cfg as cfgmod  # noqa: E402
import features  # noqa: E402
import portfolio  # noqa: E402

CFG = {
    "signal": {"lookback_days": 20, "vol_window": 20, "vol_target_ann": 0.15,
               "max_leverage": 1.0, "max_weight_per_asset": 0.5},
    "costs": {"cost_bps": 2.0, "slippage_bps": 2.0},
    "features": {"horizons": [1, 5, 20], "macd_fast": 8, "macd_slow": 24},
    "execution": {"max_gross_exposure": 1.0},
}


def synth(n=600, drift=0.0006, vol=0.01, seed=7, tickers=("AAA", "BBB")) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2015-01-01", periods=n)
    data = {}
    for i, t in enumerate(tickers):
        r = rng.normal(drift * (1 + i), vol * (1 + 0.5 * i), n)
        data[t] = 100 * np.exp(np.cumsum(r))
    return pd.DataFrame(data, index=idx)


def test_no_lookahead() -> None:
    px = synth()
    full = features.build_feature_panel(px, CFG)
    cut = 400
    partial = features.build_feature_panel(px.iloc[:cut], CFG)

    a = full.loc[full.index.get_level_values("date") == px.index[cut - 1]].droplevel("date")
    b = partial.loc[partial.index.get_level_values("date") == px.index[cut - 1]].droplevel("date")
    pd.testing.assert_frame_equal(a.sort_index(), b.sort_index(), check_names=False)
    print("PASS  no lookahead in features")


def test_vol_scaling() -> None:
    idx = pd.bdate_range("2020-01-01", periods=300)
    rng = np.random.default_rng(11)
    calm = pd.DataFrame({"X": 100 * np.exp(np.cumsum(rng.normal(0, 0.004, 300)))}, index=idx)
    wild = pd.DataFrame({"X": 100 * np.exp(np.cumsum(rng.normal(0, 0.020, 300)))}, index=idx)

    s_calm = portfolio.vol_scale(calm, CFG)["X"].iloc[-1]
    s_wild = portfolio.vol_scale(wild, CFG)["X"].iloc[-1]
    assert s_wild < s_calm, (s_calm, s_wild)
    print(f"PASS  vol target: calm {s_calm:.2f} > wild {s_wild:.2f}")


def test_cost_identity() -> None:
    px = synth()
    pos = portfolio.build_positions(portfolio.tsmom_signal(px, CFG), px, CFG)
    res = backtest.run(px, pos, CFG)

    rate = (CFG["costs"]["cost_bps"] + CFG["costs"]["slippage_bps"]) / 10_000
    expected = res["turnover"] * rate
    diff = (res["gross_returns"] - res["net_returns"] - expected).abs().max()
    assert diff < 1e-12, diff
    print(f"PASS  cost identity (max abs diff {diff:.2e})")


def test_one_day_lag() -> None:
    px = synth()
    pos = portfolio.build_positions(portfolio.tsmom_signal(px, CFG), px, CFG)
    res = backtest.run(px, pos, CFG)

    t = 300
    manual = (pos.iloc[t - 1] * px.pct_change().iloc[t]).sum()
    assert abs(manual - res["gross_returns"].iloc[t]) < 1e-12
    print("PASS  positions apply with a one-day lag")


def test_trend_is_profitable() -> None:
    """A clean uptrend must make the long-only vol-scaled baseline money."""
    idx = pd.bdate_range("2015-01-01", periods=800)
    px = pd.DataFrame({"AAA": 100 * np.exp(np.cumsum(np.full(800, 0.0012)))}, index=idx)
    pos = portfolio.build_positions(portfolio.tsmom_signal(px, CFG), px, CFG)
    res = backtest.run(px, pos, CFG)
    assert res["stats"]["cagr"] > 0, res["stats"]
    print(f"PASS  uptrend profitable: CAGR {res['stats']['cagr']:.2%}")


def test_sequences() -> None:
    px = synth()
    panel = features.build_feature_panel(px, CFG)
    cols = list(panel.columns)
    X, meta = features.to_sequences(panel, cols, seq_len=10)
    assert X.shape[1] == 10 and X.shape[2] == len(cols)
    assert len(X) == len(meta)
    assert len(X) > 0, "sequence builder produced nothing"
    assert not np.isnan(X).any()
    # each sequence must belong to exactly one ticker
    assert set(meta["ticker"]).issubset(set(px.columns))
    # and the row count should be close to (n_days - warmup - seq_len) per ticker
    expected = 2 * (len(px) - 20 - 10 + 1)
    assert abs(len(X) - expected) <= 4, (len(X), expected)
    print(f"PASS  sequences: {X.shape}")


if __name__ == "__main__":
    for fn in [test_no_lookahead, test_vol_scaling, test_cost_identity,
               test_one_day_lag, test_trend_is_profitable, test_sequences]:
        fn()
    print("\nALL SMOKE TESTS PASSED")
