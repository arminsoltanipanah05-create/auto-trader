"""Decide whether the daily ETF rebalance should run right now.

Exit 0 = trade now. Exit 1 = skip, with the reason printed.

This is the Linux twin of the gate inside run-paper.ps1. It asks Alpaca for the
market clock instead of trusting the server's timezone, so a VPS in UTC (or
anywhere else) behaves identically to the desktop.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.request
from zoneinfo import ZoneInfo

HOST = "https://paper-api.alpaca.markets"


def _clock() -> dict:
    key = os.environ.get("APCA_API_KEY_ID")
    secret = os.environ.get("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise SystemExit("APCA keys missing from the environment")
    req = urllib.request.Request(
        f"{HOST}/v2/clock",
        headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--marker", required=True, help="file holding the last traded date")
    ap.add_argument("--window", type=float, default=45.0,
                    help="trade only within this many minutes of the close")
    ap.add_argument("--min-before-close", type=float, default=3.0)
    ap.add_argument("--now", default=None, help="override the clock, for testing")
    args = ap.parse_args()

    today = (dt.datetime.fromisoformat(args.now) if args.now
             else dt.datetime.now(ZoneInfo("America/New_York"))).date().isoformat()

    if os.path.exists(args.marker):
        with open(args.marker, encoding="utf-8") as fh:
            last = fh.read().strip()
        if last == today:
            print(f"already traded {today}")
            return 1

    c = _clock()
    if not c.get("is_open"):
        print(f"market closed (next open {c.get('next_open')})")
        return 1

    close = dt.datetime.fromisoformat(c["next_close"])
    now = dt.datetime.fromisoformat(args.now) if args.now else dt.datetime.now(dt.timezone.utc)
    minutes = (close - now).total_seconds() / 60

    if minutes > args.window:
        print(f"open, {minutes:.0f} min to close - too early")
        return 1
    if minutes < args.min_before_close:
        print(f"open, only {minutes:.1f} min to close - too late")
        return 1

    print(f"open, {minutes:.0f} min to close - trading")
    return 0


if __name__ == "__main__":
    sys.exit(main())
