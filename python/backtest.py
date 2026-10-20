"""Backtest, costs and reporting.

Two rules this module enforces, both of which are easy to get wrong:

1. **No lookahead.** Positions computed with data through day t are applied to
   the return from t to t+1, which `positions.shift(1)` implements.
2. **Costs are charged on turnover.** Moving a weight from 0 to 1 and back
   costs twice the per-side rate; that is what kills most momentum strategies,
   so it is measured rather than assumed.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def run(prices: pd.DataFrame, positions: pd.DataFrame, cfg: dict) -> dict:
    """Backtest target weights. Returns a result dict with curves and stats."""
    c = cfg["costs"]
    rate = (c["cost_bps"] + c["slippage_bps"]) / 10_000.0

    asset_ret = prices.pct_change().fillna(0.0)
    held = positions.shift(1).fillna(0.0)                 # decided yesterday
    gross = (held * asset_ret).sum(axis=1)

    traded = (positions - positions.shift(1)).fillna(0.0).abs().sum(axis=1)
    costs = traded * rate
    net = gross - costs

    equity = (1.0 + net).cumprod()
    return {
        "net_returns": net,
        "gross_returns": gross,
        "equity": equity,
        "turnover": traded,
        "positions": positions,
        "stats": stats(net, equity, traded),
    }


def stats(net: pd.Series, equity: pd.Series, turnover: pd.Series) -> dict:
    if len(net) < 2:
        return {}

    ann_ret = equity.iloc[-1] ** (TRADING_DAYS / len(net)) - 1.0
    ann_vol = net.std() * np.sqrt(TRADING_DAYS)
    downside = net[net < 0].std() * np.sqrt(TRADING_DAYS)

    peak = equity.cummax()
    drawdown = equity / peak - 1.0
    years = max(len(net) / TRADING_DAYS, 1e-9)

    return {
        "cagr": ann_ret,
        "ann_vol": ann_vol,
        "sharpe": ann_ret / ann_vol if ann_vol > 0 else np.nan,
        "sortino": ann_ret / downside if downside and downside > 0 else np.nan,
        "max_dd": drawdown.min(),
        "calmar": ann_ret / abs(drawdown.min()) if drawdown.min() < 0 else np.nan,
        "turnover_yr": turnover.sum() / years,
        "hit_rate": (net > 0).mean(),
        "n_days": len(net),
        "final_equity": equity.iloc[-1],
        "worst_day": net.min(),
    }


def yearly_table(net: pd.Series) -> pd.DataFrame:
    by_year = net.groupby(net.index.year)
    rows = []
    for year, r in by_year:
        eq = (1 + r).cumprod()
        vol = r.std() * np.sqrt(TRADING_DAYS)
        rows.append({
            "year": year,
            "return": eq.iloc[-1] - 1,
            "vol": vol,
            "sharpe": (r.mean() * TRADING_DAYS) / vol if vol > 0 else np.nan,
            "max_dd": (eq / eq.cummax() - 1).min(),
        })
    return pd.DataFrame(rows).set_index("year")


def fold_report(net: pd.Series, fold_years: int = 1) -> pd.DataFrame:
    """Chunked results. The worst chunk matters more than the average."""
    rows = []
    for start in range(net.index[0].year, net.index[-1].year + 1, fold_years):
        chunk = net[(net.index.year >= start) & (net.index.year < start + fold_years)]
        if chunk.empty:
            continue
        eq = (1 + chunk).cumprod()
        vol = chunk.std() * np.sqrt(TRADING_DAYS)
        rows.append({
            "fold": f"{start}-{start + fold_years - 1}",
            "return": eq.iloc[-1] - 1,
            "sharpe": (chunk.mean() * TRADING_DAYS) / vol if vol > 0 else np.nan,
            "max_dd": (eq / eq.cummax() - 1).min(),
            "days": len(chunk),
        })
    return pd.DataFrame(rows).set_index("fold")


def format_stats(name: str, s: dict) -> str:
    if not s:
        return f"{name}: no data"
    return (
        f"{name:<12} CAGR {s['cagr']:>7.2%}  vol {s['ann_vol']:>6.2%}  "
        f"Sharpe {s['sharpe']:>5.2f}  maxDD {s['max_dd']:>7.2%}  "
        f"turnover/yr {s['turnover_yr']:>5.1f}x  hit {s['hit_rate']:>5.1%}"
    )
