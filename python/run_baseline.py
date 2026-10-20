"""Run the TSMOM baseline. This is the bar the LSTM has to beat.

    python run_baseline.py
    python run_baseline.py --start 2008-01-01
    python run_baseline.py --refresh        # re-download prices

Output: console report + output/baseline_equity.csv
"""
from __future__ import annotations

import argparse
import pathlib

import pandas as pd

import backtest
import cfg as cfgmod
import data
import portfolio

OUT = pathlib.Path(__file__).resolve().parent / "output"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--start", default=None)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    cfg = cfgmod.load(args.config)
    if args.start:
        cfg["start"] = args.start

    prices = data.load_prices(cfg["universe"], cfg["start"], cfg["data_source"], refresh=args.refresh)
    if prices.empty:
        raise SystemExit("No data. See the message above.")

    print(f"\nPanel: {prices.shape[1]} assets, {len(prices)} days, "
          f"{prices.index.min().date()} -> {prices.index.max().date()}")

    signal = portfolio.tsmom_signal(prices, cfg)
    positions = portfolio.build_positions(signal, prices, cfg)
    positions = portfolio.target_portfolio_vol(positions, prices, cfg)
    positions = portfolio.cap_gross(positions, cfg["execution"]["max_gross_exposure"])

    result = backtest.run(prices, positions, cfg)

    # Buy-and-hold on the first asset, for context.
    bh_ret = prices.iloc[:, 0].pct_change().fillna(0.0)
    bh = {
        "cagr": (1 + bh_ret).cumprod().iloc[-1] ** (252 / len(bh_ret)) - 1,
        "ann_vol": bh_ret.std() * 252 ** 0.5,
        "sharpe": (bh_ret.mean() * 252) / (bh_ret.std() * 252 ** 0.5) if bh_ret.std() else float("nan"),
        "max_dd": ((1 + bh_ret).cumprod() / (1 + bh_ret).cumprod().cummax() - 1).min(),
        "turnover_yr": float("nan"),
        "hit_rate": (bh_ret > 0).mean(),
    }

    print()
    print(backtest.format_stats("TSMOM", result["stats"]))
    print(backtest.format_stats(f"Buy&Hold {prices.columns[0]}", bh))

    print("\nPer year")
    print(backtest.yearly_table(result["net_returns"]).to_string(
        float_format=lambda x: f"{x:8.4f}"))

    print("\nFolds (worst fold is the number that matters)")
    print(backtest.fold_report(result["net_returns"]).to_string(
        float_format=lambda x: f"{x:8.4f}"))

    OUT.mkdir(exist_ok=True)
    result["equity"].to_frame("equity").to_csv(OUT / "baseline_equity.csv")
    result["net_returns"].to_frame("net_return").to_csv(OUT / "baseline_returns.csv")
    positions.to_csv(OUT / "baseline_positions.csv")
    print(f"\nWrote {OUT / 'baseline_equity.csv'}")


if __name__ == "__main__":
    main()
