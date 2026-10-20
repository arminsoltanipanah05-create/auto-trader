"""Liquid / Hyperliquid market data — keyless, no OAuth.

The Co-Invest MCP is for chat. A scheduled bot cannot use it, so this module
talks to the public info endpoint that backs Liquid's perps.

    python liquid_data.py                      # universe snapshot
    python liquid_data.py --coins BTC,ETH,SOL  # fetch candles, write CSVs

Endpoints used (all POST /info):
    {"type": "metaAndAssetCtxs"}                 -> universe + funding/OI/mark
    {"type": "candleSnapshot", "req": {...}}     -> OHLCV

Notes that matter for modelling:
  * `funding` is the HOURLY rate. Annualise with * 24 * 365.
  * `openInterest` is in coins, not USD. Multiply by markPx.
  * Funding is a position cost, not a return. A backtest that ignores it
    overstates every long perp trade.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import time
import urllib.request

import numpy as np
import pandas as pd

BASE = "https://api.hyperliquid.xyz/info"
OUT = pathlib.Path(__file__).resolve().parent / "data" / "liquid"
HOURS_PER_YEAR = 24 * 365


def _post(payload: dict, retries: int = 3) -> object:
    body = json.dumps(payload).encode()
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                BASE, data=body, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"info endpoint failed after {retries} tries: {last}")


def universe() -> pd.DataFrame:
    """Every listed perp with its current funding, open interest and price."""
    universe_raw, ctxs = _post({"type": "metaAndAssetCtxs"})
    rows = []
    for meta, ctx in zip(universe_raw["universe"], ctxs):
        if meta.get("isDelisted"):
            continue
        mark = float(ctx.get("markPx") or 0)
        oi_coins = float(ctx.get("openInterest") or 0)
        funding_h = float(ctx.get("funding") or 0)
        rows.append({
            "coin": meta["name"],
            "max_leverage": meta.get("maxLeverage"),
            "mark_px": mark,
            "oracle_px": float(ctx.get("oraclePx") or 0),
            "premium": float(ctx.get("premium") or 0),
            "funding_hourly": funding_h,
            "funding_ann": funding_h * HOURS_PER_YEAR,
            "open_interest_coins": oi_coins,
            "open_interest_usd": oi_coins * mark,
            "day_volume_usd": float(ctx.get("dayNtlVlm") or 0),
        })
    df = pd.DataFrame(rows)
    return df.sort_values("day_volume_usd", ascending=False).reset_index(drop=True)


def candles(coin: str, interval: str = "1d", days: int = 730) -> pd.DataFrame:
    """OHLCV for one coin. `interval` in 1m,5m,15m,30m,1h,4h,1d."""
    end = int(time.time() * 1000)
    start = end - days * 86_400_000
    raw = _post({
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": interval, "startTime": start, "endTime": end},
    })
    if not raw:
        return pd.DataFrame()
    df = pd.DataFrame(raw)
    df["date"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_localize(None)
    for col in ("o", "h", "l", "c", "v"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["trades"] = pd.to_numeric(df.get("n"), errors="coerce")
    return df.set_index("date")[["o", "h", "l", "c", "v", "trades"]].sort_index()


def close_panel(coins, interval: str = "1d", days: int = 730) -> pd.DataFrame:
    """Wide close-price panel, ready for the same backtest code as the ETFs."""
    series = {}
    for coin in coins:
        try:
            df = candles(coin, interval, days)
            if not df.empty:
                series[coin] = df["c"]
        except Exception as exc:  # noqa: BLE001
            print(f"  {coin}: {exc}")
        time.sleep(0.15)                       # be polite
    if not series:
        raise RuntimeError("no candles returned")
    return pd.DataFrame(series).sort_index()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", default=None, help="comma list, e.g. BTC,ETH,SOL")
    ap.add_argument("--interval", default="1d")
    ap.add_argument("--days", type=int, default=730)
    args = ap.parse_args()

    if not args.coins:
        df = universe()
        print(f"{len(df)} live perps\n")
        show = df.head(15)[["coin", "mark_px", "funding_ann", "open_interest_usd",
                            "day_volume_usd", "max_leverage"]]
        show["open_interest_usd"] = (show["open_interest_usd"] / 1e6).round(1)
        show["day_volume_usd"] = (show["day_volume_usd"] / 1e6).round(1)
        show["funding_ann"] = (show["funding_ann"] * 100).round(1)
        print(show.to_string(index=False,
                             header=["coin", "mark", "fund_ann%", "OI $m", "vol $m", "lev"]))
        print("\nfunding_ann is the current hourly funding annualised — it flips sign often.")
        return

    coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]
    OUT.mkdir(parents=True, exist_ok=True)
    px = close_panel(coins, args.interval, args.days)
    px.to_csv(OUT / f"prices_{args.interval}.csv")
    print(f"\n{px.shape[1]} coins x {len(px)} bars  {px.index.min().date()} -> {px.index.max().date()}")
    print(f"wrote {OUT / f'prices_{args.interval}.csv'}")

    u = universe().set_index("coin")
    print("\nfunding (annualised) and open interest")
    for c in px.columns:
        if c in u.index:
            print(f"  {c:<8} {u.loc[c, 'funding_ann'] * 100:>7.1f}%   "
                  f"OI ${u.loc[c, 'open_interest_usd'] / 1e6:>8.0f}m")


if __name__ == "__main__":
    main()
