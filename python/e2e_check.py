"""End-to-end paper validation for BTC.  Submits nothing.

A sentinel wraps the real paper client: reads pass through, but ``submit_order``
raises and increments a counter.  If any code path reaches the broker, this
script fails loudly.  A zero counter is the proof of interception.

    python e2e_check.py
"""

from __future__ import annotations

import os
import sys

import broker
import cfg as cfgmod
import pre_trade
from pre_trade import GuardError, TradeIntent


class Sentinel:
    """Delegates reads to the real client, refuses to submit."""

    def __init__(self, inner):
        self._inner = inner
        self.submit_calls = 0

    def get_account(self):
        return self._inner.get_account()

    def get_all_positions(self):
        return self._inner.get_all_positions()

    def submit_order(self, request):
        self.submit_calls += 1
        raise RuntimeError("SENTINEL: submit_order must never be reached in a dry check")


def report(clearance: pre_trade.Clearance, equity: float) -> None:
    intent = clearance.intent
    market = clearance.market
    print("=" * 72)
    print("PRE-TRADE REPORT  (paper — nothing submitted)")
    print("=" * 72)
    print(f"Action          : {intent.side.upper()} (validation only, not sent)")
    print(f"Asset           : {intent.symbol}  ({market.coin_id if market else 'n/a'})")
    print(f"Size            : {intent.qty:g} units = ${clearance.notional:,.2f} "
          f"({clearance.notional / equity:.2%} of equity)")
    print()
    print("Market data")
    print(f"  CoinGecko     : {market.price:,.2f} @ {market.last_updated.isoformat()}"
          if market else "  CoinGecko     : n/a")
    if market:
        print(f"                  24h {market.chg_24h:+.2f}%  "
              f"7d {market.chg_7d:+.2f}%  30d {market.chg_30d:+.2f}%  "
              f"vol ${market.volume_24h / 1e9:,.1f}B")
        print(f"                  mcap ${market.market_cap / 1e9:,.1f}B  "
              f"from ATH {market.ath_change_pct:+.2f}%")
    print(f"  Broker quote  : {clearance.mid:,.2f} mid @ {clearance.quote_ts.isoformat()}")
    print(f"  Spread        : {clearance.spread_bps:.1f} bps")
    print()
    print("Trade plan")
    print(f"  Entry         : {clearance.entry:,.2f} (ask — the executable side)")
    print(f"  Stop          : {intent.stop:,.2f}")
    print(f"  Target        : {intent.target:,.2f}")
    print(f"  Reward/risk   : {clearance.reward_risk:.2f}:1")
    print(f"  Risk          : ${clearance.risk_amount:,.2f} "
          f"({clearance.risk_pct:.3f}% of equity, cap 1%)")
    print(f"  Fees+slip     : {clearance.cost_bps:.1f} bps")
    print(f"  Reason        : {intent.reason}")
    print(f"  Invalidation  : {intent.invalidation}")
    print()
    print("Data quality")
    print("  CoinGecko     : reachable, REST API, age checked against 180s limit")
    print("  Broker        : reachable, age checked against 60s limit")
    print("  Crypto.com    : not connected (single market source + broker cross-check)")
    print("=" * 72)


def main() -> int:
    from alpaca.trading.client import TradingClient

    cfg = cfgmod.load()
    real = TradingClient(
        os.environ["APCA_API_KEY_ID"], os.environ["APCA_API_SECRET_KEY"], paper=True
    )
    sentinel = Sentinel(real)
    guard = broker.GuardedBroker(sentinel, paper=True, cfg=cfg)

    account = sentinel.get_account()
    equity = float(account.equity)

    reference = pre_trade.CoinGeckoProvider(
        cfg["pretrade"]["coingecko_api"]
    ).snapshot("bitcoin").price

    intent = TradeIntent(
        symbol="BTC/USD",
        asset_class="crypto",
        side="buy",
        qty=0.05,
        reason="e2e validation: momentum sleeve top-up (paper, not sent)",
        invalidation=f"daily close below {reference * 0.96:,.0f}",
        stop=round(reference * 0.96, 2),
        target=round(reference * 1.10, 2),
        fees_bps=25.0,
        slippage_bps=30.0,
    )

    try:
        clearance = guard.check(intent, equity=equity)
    except GuardError as exc:
        print(f"BLOCKED at validation: {exc.code} — {exc.detail}")
        print(f"submit_order calls: {sentinel.submit_calls}")
        return 1

    report(clearance, equity)
    print()
    print("Now attempting a deliberately oversized order to prove interception…")

    risky = TradeIntent(**{**intent.__dict__, "qty": 1.0})
    try:
        guard.submit(risky, equity=equity)
    except GuardError as exc:
        print(f"  BLOCKED: {exc.code} — {exc.detail}")
    else:
        print("  ERROR: oversized order was accepted")
        return 1

    print(f"  submit_order calls during this run: {sentinel.submit_calls}")
    if sentinel.submit_calls != 0:
        print("  FAIL: broker was reached")
        return 1
    print("  PASS: no order reached the broker")
    return 0


if __name__ == "__main__":
    sys.exit(main())
