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

import cfg as cfgmod
import risk

OUT = pathlib.Path(__file__).resolve().parent / "output"
PAPER_HOST = "https://paper-api.alpaca.markets"
LIVE_HOST = "https://api.alpaca.markets"


def _client(paper: bool):
    from alpaca.trading.client import TradingClient  # noqa: PLC0415

    key = os.environ.get("APCA_API_KEY_ID")
    secret = os.environ.get("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise SystemExit("Set APCA_API_KEY_ID and APCA_API_SECRET_KEY first.")
    return TradingClient(key, secret, paper=paper)

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

    client = _client(paper)
    account = client.get_account()
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

    held = {p.symbol: float(p.market_value) for p in client.get_all_positions()}
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

    from alpaca.trading.enums import OrderSide, TimeInForce  # noqa: PLC0415
    from alpaca.trading.requests import MarketOrderRequest  # noqa: PLC0415

    failed = []
    for ticker, side, qty, px in sized:
        req = MarketOrderRequest(
            symbol=ticker,
            qty=qty,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            client_order_id=_order_id(str(latest), ticker, side, qty),
        )
        try:
            o = client.submit_order(req)
            print(f"  submitted {o.symbol} {o.side.value} {o.qty} sh id={o.client_order_id}")
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
