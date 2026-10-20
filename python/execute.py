"""Order execution against Alpaca. PAPER endpoint only unless you change it.

Requires alpaca-py (installs fine on 3.12; check before assuming 3.15).
UNTESTED on this machine — no account was connected and no order was sent.

    python execute.py               # dry run (default)
    python execute.py --send        # actually submits, paper account only

Safety design:
  * every order carries a deterministic client_order_id, so a retry after a
    timeout cannot double the position;
  * the kill-switch file blocks everything;
  * target notional is derived from current equity, never hard-coded.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import pathlib
import sys

import pandas as pd

import broker as brokermod
import cfg as cfgmod
import risk
from pre_trade import GuardError, TradeIntent

OUT = pathlib.Path(__file__).resolve().parent / "output"
PAPER_HOST = "https://paper-api.alpaca.markets"
LIVE_HOST = "https://api.alpaca.markets"

# Stop at two daily sigma; target at 2.5x that distance.  The extra half-sigma
# is a buffer, not extra edge: the guard prices a buy at the ask and a sell at
# the bid, which shortens reward and lengthens risk by roughly the spread. With
# a 2.5x multiple the realised reward/risk stays above 2:1 on every venue we
# trade.  The guard re-checks the real number from the broker quote anyway.
STOP_SIGMA = 2.0
TARGET_MULT = 2.5
MIN_SIGMA = 0.005          # 0.5% daily floor, so a quiet tape cannot shrink stops to noise

def _order_id(date: str, ticker: str, side: str, qty: int) -> str:
    raw = f"dmn-{date}-{ticker}-{side}-{qty}".encode()
    return "dmn-" + hashlib.sha1(raw).hexdigest()[:24]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--positions", default=str(OUT / "live_positions.csv"))
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--live", action="store_true",
                    help="override config and use the LIVE endpoint (real money)")
    ap.add_argument("--max-age-days", type=int, default=5)
    args = ap.parse_args()

    cfg = cfgmod.load()
    paper = cfg["execution"].get("paper", True)

    if args.live and paper:
        paper = False
    if not paper and not args.live:
        raise SystemExit(
            "config has paper=false but --live was not passed.\n"
            "Refusing to guess. Re-run with --live if you really mean real money."
        )

    if risk.kill_switch_engaged():
        raise SystemExit("KILL SWITCH IS ON (output/KILL_SWITCH exists). Refusing to trade.")

    path = pathlib.Path(args.positions)
    if not path.exists():
        raise SystemExit(f"{path} not found. Run train.py first.")

    df = pd.read_csv(path)
    latest = pd.to_datetime(df["date"]).max().date()
    todays = df[pd.to_datetime(df["date"]).dt.date == latest]

    age = (dt.date.today() - latest).days
    if age > args.max_age_days:
        raise SystemExit(
            f"positions are dated {latest} ({age} days old, limit {args.max_age_days}). "
            "A stale signal is how bots buy last month's idea."
        )

    if not paper:
        print("=" * 60)
        print("  LIVE TRADING — REAL MONEY — paper=false, --live passed")
        print("=" * 60)

    # Every order goes through broker.GuardedBroker: market data, spread,
    # liquidity, risk and (for live) an approval token are checked before the
    # broker client is touched.  This module never holds a broker client.
    guard = brokermod.connect(cfg=cfg, paper=paper)
    account = guard.account()
    equity = float(account.equity)
    print(f"Account {account.account_number}  equity {equity:,.2f}  status {account.status}")

    # Unattended runs need a circuit breaker, not just an order-size check.
    # If today is already down past the limit, stop the bot entirely and make
    # a human look at it rather than rebalancing into a bad day.
    last_close = float(account.last_equity or equity)
    if last_close > 0 and not risk.check_daily_loss(equity, last_close, cfg):
        risk.engage_kill_switch("daily loss limit")
        raise SystemExit(
            "Kill switch engaged: delete output/KILL_SWITCH to resume trading."
        )

    # Only equities belong to this book. Crypto positions live in the same
    # account but are managed by crypto_bot.py; treating them as ETFs here
    # would close the entire sleeve on the next rebalance.
    held = {
        p.symbol: float(p.market_value)
        for p in guard.positions()
        if str(getattr(p.asset_class, "value", p.asset_class)) == "us_equity"
    }
    targets = {row.ticker: float(row.position) * equity for row in todays.itertuples()}

    orders = []
    for ticker in sorted(set(held) | set(targets)):
        want = targets.get(ticker, 0.0)
        have = held.get(ticker, 0.0)
        delta = want - have
        if abs(delta) < max(equity * 0.005, 25.0):     # ignore dust
            continue
        orders.append((ticker, delta))

    gross = sum(abs(v) for v in targets.values()) / equity if equity else 0
    print(f"Target gross exposure: {gross:.2f}x  ({len(orders)} orders)")

    # A single fat-finger order is the most common way an automated bot ruins
    # an account. Refuse anything above a quarter of equity.
    for ticker, delta in orders:
        if abs(delta) > equity * 0.25:
            raise SystemExit(
                f"REJECT: {ticker} order {abs(delta):,.0f} exceeds 25% of equity. "
                "Check the positions file before sending."
            )

    if not risk.check_order_batch(gross, cfg):
        raise SystemExit("Risk check failed. No orders sent.")

    # Whole shares only. Alpaca rejects a fractional order that would open a
    # short ("fractional orders cannot be sold short"), so a dollar-sized short
    # leg kills the batch. Size every leg in shares instead.
    from alpaca.data.historical import StockHistoricalDataClient  # noqa: PLC0415
    from alpaca.data.requests import StockLatestTradeRequest  # noqa: PLC0415

    symbols = sorted({t for t, _ in orders})
    prices: dict[str, float] = {}
    if symbols:
        data_client = StockHistoricalDataClient(
            os.environ["APCA_API_KEY_ID"], os.environ["APCA_API_SECRET_KEY"]
        )
        trades = data_client.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=symbols)
        )
        prices = {s: float(t.price) for s, t in trades.items()}

    sized = []
    for ticker, delta in orders:
        px = prices.get(ticker)
        if not px:
            raise SystemExit(f"No price for {ticker} — refusing to size blind.")
        # Round rather than floor: flooring leaves a permanent sub-share
        # shortfall that never gets corrected, and the book drifts below target.
        qty = int(round(abs(delta) / px))
        if qty < 1:
            print(f"  skip {ticker}: {abs(delta):,.0f} USD is under one share at {px:,.2f}")
            continue
        sized.append((ticker, "buy" if delta > 0 else "sell", qty, px))

    for ticker, side, qty, px in sized:
        print(f"  {side:<5} {ticker:<6} {qty:>6,} sh @ ~{px:>8,.2f} = {qty * px:>10,.0f} USD")

    if not args.send:
        mode = "LIVE" if not paper else "PAPER"
        print(f"\nDRY RUN ({mode} endpoint) — nothing sent. Add --send to submit.")
        return

    # Realised daily sigma per leg, used to place stop and target.  A rebalance
    # has no discretionary stop, so the levels come from the same volatility
    # model that sized the position.
    sigma: dict[str, float] = {}
    if sized:
        from alpaca.data.requests import StockBarsRequest  # noqa: PLC0415
        from alpaca.data.timeframe import TimeFrame  # noqa: PLC0415

        start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=70)
        try:
            frame = data_client.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=sorted({t for t, _, _, _ in sized}),
                    timeframe=TimeFrame.Day,
                    start=start,
                )
            )
            for sym in {t for t, _, _, _ in sized}:
                closes = pd.Series([float(b.close) for b in frame[sym]]).dropna()
                rets = closes.pct_change().dropna().tail(30)
                if len(rets) >= 5:
                    sigma[sym] = float(rets.std())
        except Exception as exc:  # noqa: BLE001
            print(f"  volatility unavailable ({type(exc).__name__}); using the floor")

    failed = []
    for ticker, side, qty, px in sized:
        dist = STOP_SIGMA * max(sigma.get(ticker, 0.0), MIN_SIGMA)
        if side == "buy":
            stop, target = px * (1 - dist), px * (1 + TARGET_MULT * dist)
        else:
            stop, target = px * (1 + dist), px * (1 - TARGET_MULT * dist)

        intent = TradeIntent(
            symbol=ticker,
            asset_class="us_equity",
            side=side,
            qty=float(qty),
            reason=(f"TSMOM rebalance: target ${targets.get(ticker, 0.0):,.0f} "
                    f"vs held ${held.get(ticker, 0.0):,.0f}"),
            invalidation=f"daily close beyond {stop:,.2f} ({STOP_SIGMA:g} sigma stop)",
            stop=stop,
            target=target,
            fees_bps=0.0,
            slippage_bps=5.0,
            client_order_id=_order_id(str(latest), ticker, side, qty),
        )
        try:
            o = guard.submit(intent, equity=equity)
            print(f"  submitted {o.symbol} {o.side.value} {o.qty} sh id={o.client_order_id}")
        except GuardError as exc:
            failed.append((ticker, f"{exc.code}: {exc.detail}"))
            print(f"  BLOCKED {ticker}: {exc.code} — {exc.detail}")
        except Exception as exc:  # noqa: BLE001
            failed.append((ticker, str(exc)[:200]))
            print(f"  FAILED  {ticker}: {str(exc)[:200]}")

    print(f"\nDone. {len(sized) - len(failed)}/{len(sized)} orders at {dt.datetime.now():%Y-%m-%d %H:%M:%S}")
    if failed:
        print("Book is INCOMPLETE — rerun once the failed legs are understood:")
        for t, err in failed:
            print(f"  {t}: {err}")
        raise SystemExit(1)


if __name__ == "__main__":
    sys.exit(main())
