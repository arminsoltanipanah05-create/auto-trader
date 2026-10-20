"""Compare walk-forward runs: model vs baseline vs SPY, plus control.

    python assess.py --tag _v2
    python assess.py --tag _v2 --control _control

Prints the fold table, bootstrap intervals, and the overfit diagnostics.
"""
from __future__ import annotations

import argparse
import pathlib

import numpy as np
import pandas as pd

OUT = pathlib.Path(__file__).resolve().parent / "output"
RNG = np.random.default_rng(0)


def bootstrap_ci(x, n=20_000, alpha=0.05):
    x = pd.Series(x).dropna().to_numpy()
    if len(x) < 3:
        return (np.nan, np.nan)
    m = RNG.choice(x, size=(n, len(x)), replace=True).mean(axis=1)
    return float(np.quantile(m, alpha / 2)), float(np.quantile(m, 1 - alpha / 2))


def pooled(r: pd.Series) -> float:
    r = pd.to_numeric(r, errors="coerce").dropna()
    return float(r.mean() / r.std() * np.sqrt(252)) if r.std() else np.nan


def load(tag: str) -> pd.DataFrame:
    return pd.read_csv(OUT / f"walkforward_folds{tag}.csv", index_col=0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="")
    ap.add_argument("--control", default=None)
    args = ap.parse_args()

    folds = load(args.tag)
    base = pd.read_csv(OUT / "walkforward_baseline.csv", index_col=0)
    spy = pd.read_csv(OUT / "walkforward_spy.csv", index_col=0)

    cols = {"DMN": folds["sharpe"], "baseline": base["sharpe"], "SPY": spy["sharpe"]}
    if args.control:
        c = load(args.control)
        cols["control"] = c["sharpe"]

    tbl = pd.DataFrame(cols)
    print(tbl.round(3).to_string())

    print("\n" + "=" * 72)
    d = folds["sharpe"]
    for name, s in tbl.items():
        lo, hi = bootstrap_ci(s)
        print(f"{name:<9} mean {s.mean():+.3f}  median {s.median():+.3f}  std {s.std():.3f}  "
              f"worst {s.min():+.3f}  positive {int((s > 0).sum())}/{len(s)}  "
              f"CI [{lo:+.3f}, {hi:+.3f}]")

    r = pd.read_csv(OUT / f"walkforward_returns{args.tag}.csv", index_col=0).iloc[:, -1]
    r = pd.to_numeric(r, errors="coerce").dropna()
    print(f"\npooled OOS Sharpe {pooled(r):+.3f} | total {(1 + r).prod() - 1:+.2%} | "
          f"max DD {((1 + r).cumprod() / (1 + r).cumprod().cummax() - 1).min():+.2%}")
    print(f"mean gross exposure {folds['gross_exposure'].mean():.2f}x | "
          f"mean vol {folds['vol'].mean():.2%}")

    print("\n" + "=" * 72)
    print("DIAGNOSTICS")
    print(f"val Sharpe mean {folds['val_sharpe'].mean():+.3f}  vs test {d.mean():+.3f}")
    print(f"corr with 60d momentum: mean {folds['corr_mom'].mean():+.3f} "
          f"[{folds['corr_mom'].min():+.3f}, {folds['corr_mom'].max():+.3f}]")
    print(f"beats baseline {(d > tbl['baseline']).sum()}/{len(d)}  "
          f"beats SPY {(d > tbl['SPY']).sum()}/{len(d)}")
    if args.control:
        print(f"beats control {(d > tbl['control']).sum()}/{len(d)}  "
              f"(control mean {tbl['control'].mean():+.3f})")

    print("\n" + "=" * 72)
    checks = {
        "mean OOS Sharpe > 0": d.mean() > 0,
        "beats baseline in >50% folds": (d > tbl["baseline"]).mean() > 0.5,
        "beats SPY in >50% folds": (d > tbl["SPY"]).mean() > 0.5,
        "beats control in >60% folds": (not args.control) or (d > tbl["control"]).mean() > 0.6,
        "all folds positive": (d > 0).all(),
    }
    for k, v in checks.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")


if __name__ == "__main__":
    main()
