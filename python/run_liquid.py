"""TSMOM on Liquid perps, with real funding costs.

Difference from the ETF backtest, all of which matters:
  * 365-day annualisation, not 252 — crypto trades every day
  * funding is charged from hourly history, longs pay when it is positive
  * taker fees at 4.5 bps (perp venue), not 2 bps
  * gross capped at 1.0x; the venue offers 20-40x, which is not an invitation

    python run_liquid.py
    python run_liquid.py --coins BTC,ETH,SOL --days 900 --lookback 30
"""
from __future__ import annotations

import argparse
import json
import pathlib
import time
import urllib.request

import numpy as np
import pandas as pd

import liquid_data

OUT = pathlib.Path(__file__).resolve().parent / "output"
DAYS = 365
FEE_BPS = 4.5


def funding_series(coin: str, days: int) -> pd.Series:
    """Daily sum of the hourly funding rate.

    The endpoint caps a single response at roughly 500 hourly records, so the
    window has to be walked in chunks. Asking for 900 days in one call silently
    returns only the first ~20 days, which looks like zero funding cost.
    """
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86_400_000
    chunk = 18 * 86_400_000
    frames = []

    t = start_ms
    while t < end_ms:
        body = json.dumps({"type": "fundingHistory", "coin": coin,
                           "startTime": t, "endTime": min(t + chunk, end_ms)}).encode()
        for attempt in range(4):
            try:
                req = urllib.request.Request(liquid_data.BASE, data=body,
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = json.loads(resp.read().decode())
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 3:
                    raise
                wait = 2.0 * (attempt + 1)
                print(f"  funding {coin}: {exc} — retrying in {wait:.0f}s")
                time.sleep(wait)
        else:
            raw = []
        if raw:
            frames.append(pd.DataFrame(raw))
        t += chunk
        time.sleep(0.35)

    if not frames:
        return pd.Series(dtype=float)

    df = pd.concat(frames, ignore_index=True).drop_duplicates(subset="time")
    df["date"] = pd.to_datetime(df["time"], unit="ms", utc=True).dt.tz_localize(None)
    df["fundingRate"] = pd.to_numeric(df["fundingRate"], errors="coerce")
    return df.set_index("date")["fundingRate"].resample("1D").sum()


def vol_scale(px: pd.DataFrame, window: int, target: float, cap: float) -> pd.DataFrame:
    vol = px.pct_change().rolling(window, min_periods=window // 2).std() * np.sqrt(DAYS)
    return (target / vol).clip(upper=cap).fillna(0.0)


def run(px, funding, lookback, target_vol=0.15, cap=4.0, gross_cap=1.0):
    ret = px.pct_change().fillna(0.0)
    mom = px / px.shift(lookback) - 1.0
    sig = np.sign(mom).fillna(0.0)

    n = px.notna().sum(axis=1).replace(0, np.nan)
    w = px.notna().astype(float).div(n, axis=0)
    pos = w * sig * vol_scale(px, lookback, target_vol, cap)

    # portfolio-level vol targeting, causal
    fac = []
    for i in range(len(pos)):
        if i < lookback:
            fac.append(0.0)
            continue
        win = ret.iloc[i - lookback:i].to_numpy()
        cov = np.cov(win, rowvar=False)
        v = float(pos.iloc[i].to_numpy() @ np.atleast_2d(cov) @ pos.iloc[i].to_numpy())
        vol = np.sqrt(max(v, 0.0) * DAYS)
        fac.append(min(target_vol / vol, 4.0) if vol > 1e-8 else 0.0)
    pos = pos.mul(pd.Series(fac, index=pos.index), axis=0)

    gross = pos.abs().sum(axis=1)
    pos = pos.mul((gross_cap / gross).where(gross > gross_cap, 1.0), axis=0)

    held = pos.shift(1).fillna(0.0)
    gross_pnl = (held * ret).sum(axis=1)

    f = funding.reindex(px.index).fillna(0.0)          # daily funding per unit
    fund_pnl = -(held * pd.DataFrame(
        {c: f[c] if c in f else 0.0 for c in px.columns}, index=px.index)).sum(axis=1)

    traded = (pos - pos.shift(1)).fillna(0.0).abs().sum(axis=1)
    fees = traded * FEE_BPS / 10_000

    net = gross_pnl + fund_pnl - fees
    return pos, net, gross_pnl, fund_pnl, fees, traded


def report(name, net, fees, fund, traded):
    eq = (1 + net).cumprod()
    vol = net.std() * np.sqrt(DAYS)
    ann = eq.iloc[-1] ** (DAYS / len(net)) - 1
    yrs = len(net) / DAYS
    print(f"{name:<10} CAGR {ann:>8.2%}  vol {vol:>6.2%}  Sharpe {ann/vol if vol else 0:>5.2f}  "
          f"maxDD {(eq/eq.cummax()-1).min():>7.2%}  turnover/yr {traded.sum()/yrs:>5.1f}x  "
          f"fees {fees.sum()*100:>5.1f}%  funding {fund.sum()*100:>6.1f}%")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", default="BTC,ETH,SOL,HYPE")
    ap.add_argument("--days", type=int, default=900)
    ap.add_argument("--lookback", type=int, default=30)
    args = ap.parse_args()

    coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]
    print(f"fetching {len(coins)} coins, {args.days} days, lookback {args.lookback}")
    px = liquid_data.close_panel(coins, "1d", args.days).dropna()

    funding = {}
    for c in px.columns:
        try:
            funding[c] = funding_series(c, args.days)
        except Exception as exc:  # noqa: BLE001
            print(f"  funding {c}: {exc}")
            funding[c] = pd.Series(dtype=float)
    funding_df = pd.DataFrame(funding)

    print(f"panel {px.shape[1]} coins x {len(px)} bars  "
          f"{px.index.min().date()} -> {px.index.max().date()}\n")

    pos, net, gross_pnl, fund_pnl, fees, traded = run(px, funding_df, args.lookback)
    report("TSMOM+fund", net, fees, fund_pnl, traded)

    # what the same signal looks like if funding were free
    net_nofund = net - fund_pnl
    report("no funding", net_nofund, fees, pd.Series(0.0, index=net.index), traded)

    # buy and hold, equal weight, for context
    bh = (px.pct_change().fillna(0.0).mean(axis=1))
    report("buy & hold", bh, pd.Series(0.0, index=bh.index), pd.Series(0.0, index=bh.index),
           pd.Series(0.0, index=bh.index))

    OUT.mkdir(exist_ok=True)
    pd.DataFrame({"net": net, "gross": gross_pnl, "funding": fund_pnl,
                  "fees": fees}).to_csv(OUT / "liquid_tsmom.csv")
    print(f"\nmean gross {pos.abs().sum(axis=1).mean():.2f}x  "
          f"mean |pos| {pos.abs().mean().mean():.3f}")
    print(f"wrote {OUT / 'liquid_tsmom.csv'}")


if __name__ == "__main__":
    main()
