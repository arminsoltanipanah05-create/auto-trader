"""Sensitivity: how leverage and costs change the baseline.

    python sensitivity.py

Answers two questions that decide whether a strategy is tradable at all:
  1. Can this panel reach the 15% vol target without margin?
  2. At what cost level does the edge disappear?
"""
from __future__ import annotations

import copy

import cfg as cfgmod
import data
import portfolio
import backtest


def main() -> None:
    cfg = cfgmod.load()
    px = data.load_prices(cfg["universe"], cfg["start"], cfg["data_source"])
    base = portfolio.build_positions(portfolio.tsmom_signal(px, cfg), px, cfg)

    print("Leverage vs realised vol (target 15%)")
    print(f"{'max gross':>10} {'ann vol':>9} {'CAGR':>8} {'Sharpe':>8} {'maxDD':>8}")
    scaled = portfolio.target_portfolio_vol(base, px, cfg)
    for gross in (1.0, 1.5, 2.0, 3.0):
        p = portfolio.cap_gross(scaled, gross)
        s = backtest.run(px, p, cfg)["stats"]
        print(f"{gross:>10.1f} {s['ann_vol']:>8.2%} {s['cagr']:>7.2%} {s['sharpe']:>8.2f} {s['max_dd']:>7.2%}")

    print("\nCost sensitivity (gross <= 2.0)")
    p = portfolio.cap_gross(scaled, 2.0)
    for bps in (0, 1, 2, 5, 10):
        c = copy.deepcopy(cfg)
        c["costs"] = {"cost_bps": bps, "slippage_bps": 0}
        s = backtest.run(px, p, c)["stats"]
        print(f"{bps:>3} bps   CAGR {s['cagr']:>7.2%}   Sharpe {s['sharpe']:>5.2f}   turnover/yr {s['turnover_yr']:>5.1f}x")

    print("\nBaseline signal detail")
    pos = portfolio.cap_gross(scaled, 1.0)
    long_share = (pos > 0).sum(axis=1).sum() / max((pos != 0).sum(axis=1).sum(), 1)
    print(f"share of positions that are long: {long_share:.1%}")
    print("(a long-biased book means the strategy is partly just beta)")


if __name__ == "__main__":
    main()
