"""Research only: does a faster crypto signal survive its own trading costs?

The live bot uses daily bars with a 30-day lookback. This tests hourly bars with
short lookbacks, and charges the turnover at several fee levels, because the
whole question is whether more trades add anything after costs.

    python fast_test.py
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request

import numpy as np
import pandas as pd

HOST = "https://data.alpaca.markets/v1beta3/crypto/us"
HOURS_PER_YEAR = 24 * 365
UNIVERSE = [
    "BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "LINK/USD", "LTC/USD",
    "DOGE/USD", "BCH/USD", "UNI/USD", "AAVE/USD", "DOT/USD", "XRP/USD",
]


def _get(url: str) -> dict:
    h = {"APCA-API-KEY-ID": os.environ["APCA_API_KEY_ID"],
         "APCA-API-SECRET-KEY": os.environ["APCA_API_SECRET_KEY"]}
    req = urllib.request.Request(url, headers=h)
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except Exception:
            if i == 2:
                raise
            time.sleep(2 * (i + 1))
    return {}


def bars(symbol: str, timeframe: str, days: int) -> pd.Series:
    end = pd.Timestamp.now(tz="UTC").normalize().tz_localize(None)
    start = end - pd.Timedelta(days=days)
    rows, token = [], None
    while True:
        url = (f"{HOST}/bars?symbols={urllib.parse.quote(symbol)}"
               f"&timeframe={timeframe}&start={start:%Y-%m-%d}&end={end:%Y-%m-%d}&limit=1000")
        if token:
            url += f"&page_token={urllib.parse.quote(token)}"
        d = _get(url)
        chunk = (d.get("bars") or {}).get(symbol) or []
        rows.extend(chunk)
        token = d.get("next_page_token")
        if not token or not chunk:
            break
        time.sleep(0.1)
    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame(rows)
    df["t"] = pd.to_datetime(df["t"], utc=True).dt.tz_localize(None)
    return df.set_index("t")["c"].astype(float).sort_index()


def panel(timeframe: str, days: int) -> pd.DataFrame:
    out = {}
    for s in UNIVERSE:
        c = bars(s, timeframe, days)
        if len(c) > 100:
            out[s] = c
        time.sleep(0.1)
    return pd.DataFrame(out).sort_index()


def weights(px: pd.DataFrame, lookback: int, ann: int, target: float = 0.30) -> pd.DataFrame:
    ret = px.pct_change()
    mom = px / px.shift(lookback) - 1.0
    w = ((mom > 0) & px.notna()).astype(float)
    n = w.sum(axis=1).replace(0, np.nan)
    w = w.div(n, axis=0).fillna(0.0)

    vol = ret.rolling(lookback, min_periods=max(6, lookback // 2)).std() * np.sqrt(ann)
    inv = (0.15 / vol).clip(upper=3.0).replace([np.inf, -np.inf], 0.0).fillna(0.0)
    w = (w * inv).fillna(0.0)
    tot = w.sum(axis=1).replace(0, np.nan)
    w = w.div(tot, axis=0).fillna(0.0)

    rows = []
    for i in range(len(w)):
        if i < lookback:
            rows.append(w.iloc[i] * 0.0)
            continue
        hist = ret.iloc[i - lookback:i]
        cov = hist.cov().to_numpy() * ann
        vec = w.iloc[i].to_numpy()
        v = np.sqrt(max(float(vec @ cov @ vec), 0.0))
        k = min(target / v, 2.0) if v > 1e-9 else 0.0
        rows.append(w.iloc[i] * k)
    out = pd.DataFrame(rows, index=w.index, columns=w.columns)
    gross = out.abs().sum(axis=1)
    return out.mul((1.0 / gross).where(gross > 1.0, 1.0), axis=0)


def evaluate(px, w, ann: int, cost_bps: float, label: str) -> dict:
    ret = px.pct_change().fillna(0.0)
    turnover = (w - w.shift(1)).abs().sum(axis=1)
    net = (w.shift(1) * ret).sum(axis=1) - turnover * cost_bps / 10_000
    net = net.iloc[max(200, 1):]
    eq = (1 + net).cumprod()
    vol = net.std() * np.sqrt(ann)
    cagr = eq.iloc[-1] ** (ann / len(net)) - 1
    dd = (eq / eq.cummax() - 1).min()
    per_year = turnover.sum() / (len(net) / ann)
    print(f"  {label:<22} CAGR {cagr:>7.2%}  vol {vol:>6.2%}  Sharpe {cagr/vol if vol else 0:>5.2f}"
          f"  maxDD {dd:>7.2%}  turnover {per_year:>7.0f}x/yr  cost {cost_bps:>4.0f}bps")
    return {"cagr": cagr, "vol": vol, "sharpe": cagr / vol if vol else 0, "dd": dd}


def main() -> None:
    print("fetching hourly bars (~250 days)...")
    h = panel("1Hour", 250)
    h = h.loc[:, h.notna().sum() > 2000]
    print(f"  {h.shape[1]} symbols x {len(h)} hourly bars "
          f"{h.index.min()} -> {h.index.max()}")

    print("\nHOURLY SIGNAL")
    for lb, name in [(24, "1 day"), (72, "3 days"), (168, "7 days")]:
        w = weights(h, lb, HOURS_PER_YEAR)
        for cost in (10, 25):
            evaluate(h, w, HOURS_PER_YEAR, cost, f"{name} lookback")

    print("\nDAILY SIGNAL (what runs now)")
    d = panel("1Day", 400)
    d = d.loc[:, d.notna().sum() > 200]
    wd = weights(d, 30, 365)
    for cost in (10, 25):
        evaluate(d, wd, 365, cost, "30-day lookback")


if __name__ == "__main__":
    main()
