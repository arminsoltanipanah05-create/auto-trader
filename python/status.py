"""One-screen status for the paper account. Read-only.

    python status.py
"""
from __future__ import annotations

import os
import sys

from alpaca.trading.client import TradingClient


def main() -> int:
    key = os.environ.get("APCA_API_KEY_ID")
    secret = os.environ.get("APCA_API_SECRET_KEY")
    if not key or not secret:
        print("Set APCA_API_KEY_ID and APCA_API_SECRET_KEY first.")
        return 1

    c = TradingClient(key, secret, paper=True)
    a = c.get_account()
    equity = float(a.equity)
    pos = c.get_all_positions()
    gross = sum(abs(float(p.market_value)) for p in pos)
    net = sum(float(p.market_value) for p in pos)
    day_pl = equity - float(a.last_equity or equity)
    day_pct = day_pl / float(a.last_equity) * 100 if float(a.last_equity or 0) else 0.0

    print(f"account   {a.account_number}  {a.status.value if hasattr(a.status, 'value') else a.status}")
    print(f"equity    {equity:>12,.2f}   day {day_pl:>+10,.2f} ({day_pct:>+5.2f}%)")
    print(f"cash      {float(a.cash):>12,.2f}   buying power {float(a.buying_power):>12,.2f}")
    print(f"gross     {gross:>12,.2f}   ({gross / equity:.2f}x)   net {net:>+12,.2f}")
    print(f"positions {len(pos):>12}   shorting {a.shorting_enabled}")

    if pos:
        print()
        print(f"  {'sym':<6}{'qty':>12}{'value':>12}  side")
        for p in sorted(pos, key=lambda x: -abs(float(x.market_value))):
            print(f"  {p.symbol:<6}{p.qty:>12}{float(p.market_value):>12,.0f}  {p.side.value}")

    opens = c.get_orders()
    if opens:
        print(f"\n{len(opens)} open order(s)")
        for o in opens[:10]:
            print(f"  {o.symbol} {o.side.value} {o.qty} {o.status.value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
