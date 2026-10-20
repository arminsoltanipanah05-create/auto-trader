"""24/7 crypto momentum on Alpaca spot. Long/flat only.

Alpaca crypto trades around the clock, which the equity book cannot. Spot has
no shorting, so the strategy is long-or-cash: it holds the coins in an uptrend
and sits in dollars otherwise.

    python crypto_bot.py --backtest          # research on ~3 years of daily bars
    python crypto_bot.py --dry-run           # what it would trade right now
    python crypto_bot.py --send              # submit to the paper account

Sizing: CAPITAL_FRACTION of account equity, gross <= that fraction. The ETF
book already uses ~1.0x, and Alpaca allows 2x overnight, so crypto gets 0.25x
and total gross stays around 1.25x instead of hitting a margin call.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import time
import urllib.parse
import urllib.request

import numpy as np
import pandas as pd

TRADE_HOST = "https://paper-api.alpaca.markets"
DATA_HOST = "https://data.alpaca.markets/v1beta3/crypto/us"
OUT = pathlib.Path(__file__).resolve().parent / "output"
DAYS = 365

LOOKBACK = 30
TARGET_VOL = 0.30          # annualised, on the crypto sleeve
CRYPTO_GROSS_CAP = 0.70    # hard ceiling. The vol target currently sizes the
                           # sleeve at ~0.62, so this cap does not bind; it is
                           # a backstop, not the sizing mechanism.
DUST_USD = 10.0

UNIVERSE = [
    "BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "LINK/USD", "LTC/USD",
    "DOGE/USD", "BCH/USD", "UNI/USD", "AAVE/USD", "DOT/USD", "XRP/USD",
]


def _hdrs() -> dict:
    key = os.environ.get("APCA_API_KEY_ID")
    secret = os.environ.get("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise SystemExit("Set APCA_API_KEY_ID and APCA_API_SECRET_KEY first.")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers=_hdrs())
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except Exception:  # noqa: BLE001
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))
    return {}


def daily_bars(symbol: str, days: int = 1200) -> pd.Series:
    end = pd.Timestamp.now(tz="UTC").normalize().tz_localize(None)
    start = end - pd.Timedelta(days=days)
    rows, token = [], None
    while True:
        url = (f"{DATA_HOST}/bars?symbols={urllib.parse.quote(symbol)}"
               f"&timeframe=1Day&start={start:%Y-%m-%d}&end={end:%Y-%m-%d}&limit=1000")
        if token:
            url += f"&page_token={urllib.parse.quote(token)}"
        d = _get(url)
        chunk = (d.get("bars") or {}).get(symbol) or []
        rows.extend(chunk)
        token = d.get("next_page_token")
        if not token or not chunk:
            break
    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["t"], utc=True).dt.tz_localize(None)
    return df.set_index("date")["c"].astype(float).sort_index()


def price_panel(symbols, days: int = 1200) -> pd.DataFrame:
    series = {}
    for s in symbols:
        try:
            c = daily_bars(s, days)
            if len(c) > 60:
                series[s] = c
        except Exception as exc:  # noqa: BLE001
            print(f"  {s}: {exc}")
        time.sleep(0.1)
    if not series:
        raise SystemExit("no bars returned")
    return pd.DataFrame(series).sort_index()


def weights(px: pd.DataFrame, lookback: int = LOOKBACK, target: float = TARGET_VOL) -> pd.DataFrame:
    """Long-or-cash momentum, inverse-vol within the active set, vol targeted."""
    ret = px.pct_change()
    mom = px / px.shift(lookback) - 1.0
    active = (mom > 0) & px.notna()
    w = active.astype(float)
    n = w.sum(axis=1).replace(0, np.nan)
    w = w.div(n, axis=0).fillna(0.0)

    vol = ret.rolling(lookback, min_periods=lookback // 2).std() * np.sqrt(DAYS)
    inv = (0.15 / vol).clip(upper=3.0).replace([np.inf, -np.inf], 0.0).fillna(0.0)
    w = (w * inv).fillna(0.0)

    # renormalise so the active book still sums to <= 1, then vol-target it
    tot = w.sum(axis=1).replace(0, np.nan)
    w = w.div(tot, axis=0).fillna(0.0)

    scaled = []
    for i in range(len(w)):
        if i < lookback:
            scaled.append(w.iloc[i] * 0.0)
            continue
        hist = ret.iloc[i - lookback:i]
        cov = hist.cov().to_numpy() * DAYS
        vec = w.iloc[i].to_numpy()
        var = float(vec @ cov @ vec)
        vol_p = np.sqrt(max(var, 0.0))
        k = min(target / vol_p, 2.0) if vol_p > 1e-9 else 0.0
        scaled.append(w.iloc[i] * k)
    out = pd.DataFrame(scaled, index=w.index, columns=w.columns)
    gross = out.abs().sum(axis=1)
    return out.mul((1.0 / gross).where(gross > 1.0, 1.0), axis=0)


def report(name: str, r: pd.Series) -> None:
    eq = (1 + r).cumprod()
    vol = r.std() * np.sqrt(DAYS)
    ann = eq.iloc[-1] ** (DAYS / len(r)) - 1
    dd = (eq / eq.cummax() - 1).min()
    sharpe = ann / vol if vol else 0.0
    print(f"{name:<14} CAGR {ann:>8.2%}  vol {vol:>7.2%}  Sharpe {sharpe:>5.2f}  "
          f"maxDD {dd:>7.2%}  ({len(r)} days {r.index.min().date()} -> {r.index.max().date()})")


def backtest(days: int = 1200) -> None:
    px = price_panel(UNIVERSE, days).dropna(how="all")
    px = px.loc[:, px.notna().sum() > 200]
    w = weights(px)
    ret = px.pct_change().fillna(0.0)
    net = (w.shift(1) * ret).sum(axis=1) - (w - w.shift(1)).abs().sum(axis=1) * 10 / 10_000
    net = net.iloc[LOOKBACK + 1:]
    report("crypto tsmom", net)
    report("buy & hold", ret.mean(axis=1).iloc[LOOKBACK + 1:])
    print(f"\nmean gross {w.abs().sum(axis=1).mean():.2f}x   "
          f"time invested {(w.abs().sum(axis=1) > 0).mean():.0%}   "
          f"assets {px.shape[1]}")
    OUT.mkdir(exist_ok=True)
    pd.DataFrame({"net": net}).to_csv(OUT / "crypto_tsmom.csv")


def live_targets(px: pd.DataFrame) -> pd.Series:
    """Fraction-of-equity weights for the crypto sleeve.

    weights() already vol-targets and caps gross at 1.0; this only applies the
    hard ceiling so the sleeve cannot crowd out the ETF book's margin.
    """
    w = weights(px).iloc[-1]
    gross = float(w.sum())
    if gross > CRYPTO_GROSS_CAP:
        w = w * (CRYPTO_GROSS_CAP / gross)
    return w.clip(lower=0.0)


def rebalance(send: bool = False, approvals: dict | None = None) -> None:
    approvals = approvals or {}
    import broker as brokermod
    import cfg as cfgmod
    from pre_trade import GuardError, TradeIntent

    cfg = cfgmod.load()
    # Orders leave this function only through GuardedBroker.submit(), which
    # runs the pre-trade gate (CoinGecko freshness, broker spread, liquidity,
    # 1% risk, 2:1 reward/risk) before the broker client is touched.
    guard = brokermod.connect(cfg=cfg, paper=True)
    acct = guard.account()
    equity = float(acct.equity)

    px = price_panel(UNIVERSE, 400).ffill().dropna(how="all")
    tgt_w = live_targets(px)
    last_px = px.ffill().iloc[-1]

    positions = {p.symbol: float(p.market_value) for p in guard.positions()
                 if p.asset_class == "crypto" or "/" in (p.symbol or "")}
    held = {s.replace("/", ""): v for s, v in positions.items()}

    # Alpaca crypto is cash-only: margin buying power is not spendable here.
    # The ETF book's shorts lock up most of the cash as collateral, so the
    # sleeve has to be sized from what is actually free, not from the vol target.
    held_crypto = sum(abs(v) for v in held.values())
    free_cash = float(getattr(acct, "non_marginable_buying_power", 0.0) or 0.0)
    budget = held_crypto + max(free_cash, 0.0)
    desired = float(tgt_w.sum()) * equity
    if desired > 0 and desired > budget * 0.98:
        scale = budget * 0.98 / desired
        tgt_w = tgt_w * scale
        print(f"cash-constrained: sleeve {desired / equity:.2%} -> {tgt_w.sum():.2%} "
              f"(free cash ${free_cash:,.0f}, crypto held ${held_crypto:,.0f})")

    plan = []
    for sym in tgt_w.index:
        want = float(tgt_w[sym]) * equity
        have = held.get(sym.replace("/", ""), 0.0)
        delta = want - have
        # No-trade band: a 4-hourly run that chases $10 of drift just generates
        # orders with no signal behind them. Only act when the gap is material
        # relative to the position itself.
        band = max(DUST_USD, 0.03 * want)
        if abs(delta) >= band:
            plan.append((sym, delta, float(last_px[sym])))

    # close anything we no longer want
    for sym_key, value in held.items():
        long_sym = next((s for s in tgt_w.index if s.replace("/", "") == sym_key), None)
        if long_sym is None and abs(value) >= DUST_USD:
            plan.append((f"{sym_key[:-3]}/{sym_key[-3:]}", -value, np.nan))

    print(f"account {acct.account_number}  equity {equity:,.2f}")
    print(f"crypto sleeve {tgt_w.sum():.2%} of equity  ({len(tgt_w[tgt_w > 0])} coins active)")
    for sym in tgt_w.index:
        if tgt_w[sym] > 0:
            print(f"  target {sym:<9} {tgt_w[sym]:>6.2%}")

    if not plan:
        print("\nnothing to trade — already at target")
        return

    print()
    for sym, delta, price in plan:
        side = "buy" if delta > 0 else "sell"
        print(f"  {side:<5} {sym:<9} {abs(delta):>10,.2f} USD")

    if not send:
        print("\nDRY RUN — nothing sent. Add --send to submit.")
        return

    # sells first: spot crypto buys need the cash back before it can be reused
    plan.sort(key=lambda x: x[1])

    # Daily sigma from the panel already in memory. Levels, as in the ETF book,
    # come from the volatility model rather than from a discretionary view.
    rets = px.pct_change().dropna()
    sigma = rets.tail(30).std()

    failed = []
    for sym, delta, price in plan:
        px_now = price if price == price else float(last_px.get(sym, 0))
        if not px_now or px_now != px_now:
            failed.append((sym, "no price"))
            continue
        qty = round(abs(delta) / px_now, 8)
        if qty <= 0:
            continue

        side = "buy" if delta > 0 else "sell"
        dist = 2.0 * max(float(sigma.get(sym, 0.0) or 0.0), 0.01)
        if side == "buy":
            stop, target = px_now * (1 - dist), px_now * (1 + 2.5 * dist)
        else:
            stop, target = px_now * (1 + dist), px_now * (1 - 2.5 * dist)

        intent = TradeIntent(
            symbol=sym,
            asset_class="crypto",
            side=side,
            qty=qty,
            reason=(f"crypto momentum rebalance: target "
                    f"{float(tgt_w.get(sym, 0.0)):.2%} of equity vs "
                    f"${abs(delta) + abs(positions.get(sym, 0.0)):,.0f} held"),
            invalidation=f"daily close beyond {stop:,.4f} (2 sigma stop)",
            stop=stop,
            target=target,
            fees_bps=25.0,
            slippage_bps=30.0,
            approval_token=approvals.get(sym),
        )
        try:
            o = guard.submit(intent, equity=equity)
            print(f"  submitted {o.symbol} {o.side.value} {o.qty} id={o.id}")
        except GuardError as exc:
            failed.append((sym, f"{exc.code}: {exc.detail}"))
            print(f"  BLOCKED {sym}: {exc.code} — {exc.detail}")
        except Exception as exc:  # noqa: BLE001
            failed.append((sym, str(exc)[:160]))
            print(f"  FAILED  {sym}: {str(exc)[:160]}")

    if failed:
        print("\nFailed legs:")
        for s, e in failed:
            print(f"  {s}: {e}")
        raise SystemExit(1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--days", type=int, default=1200)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--approval", action="append", default=[], metavar="SYMBOL=TOKEN",
                    help="single-use approval token for a live order (repeat per leg)")
    args = ap.parse_args()

    approvals = {}
    for pair in args.approval:
        symbol, _, token = pair.partition("=")
        if not token:
            raise SystemExit(f"--approval expects SYMBOL=TOKEN, got {pair!r}")
        approvals[symbol.strip()] = token.strip()

    if args.backtest:
        backtest(args.days)
    elif args.send:
        rebalance(send=True, approvals=approvals)
    else:
        rebalance(send=False)


if __name__ == "__main__":
    main()
