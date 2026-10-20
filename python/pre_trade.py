"""Centralised pre-trade guard.

Every BUY and SELL in this repository must pass through :func:`evaluate`, which
is called by ``broker.GuardedBroker.submit`` — the only place in the codebase
that is allowed to touch ``client.submit_order``.

Design notes
------------
*Market data source.*  CoinGecko's *official REST API* is used directly
(``https://api.coingecko.com/api/v3``), not the CoinGecko MCP server.  The MCP
is a chat-side integration: a scheduled GitHub Actions job has no MCP client,
no session, and no agent in the loop.  Speaking HTTP to the same public API
gives the same numbers with a timestamp we can verify.  The MCP remains the
right tool for interactive research; this is the machine-checkable twin.

*CoinGecko covers crypto, not US equities.*  ``coingecko_required_classes`` is
therefore ``["crypto"]`` by default.  Equity orders are validated against the
broker's own executable quote and depth, and the report records
``coingecko=not_applicable`` rather than pretending a snapshot exists.  Set the
list to include ``"us_equity"`` to make equity orders fail closed when no
CoinGecko mapping is configured.

*Liquidity, honestly.*  Alpaca's crypto top-of-book size is a single-venue print
(BTC/USD often shows ~$80 of depth), so it is used as a *market-exists* check,
not as the size constraint.  Crypto size is bounded by CoinGecko 24h volume.
Equity depth is real and is enforced against top-of-book.

*Fail closed.*  Every rejection raises :class:`GuardError`.  Any unexpected
exception inside the guard is converted into a ``GuardError`` before the broker
client is reached.

*A note on risk/reward for systematic rebalances.*  The bots derive stop and
target from the same volatility model used for sizing: stop at two daily sigma,
target at twice that distance.  That makes RR structurally 2:1 for rebalance
legs, so the *binding* protections are data freshness, divergence, spread,
liquidity and the 1% risk cap.  The RR check exists for discretionary or
signal-driven orders where levels are chosen by the caller.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import hmac
import json
import math
import os
import pathlib
import secrets
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "logs" / "pre-trade.log"
APPROVALS_DIR = ROOT / "state" / "approvals"

CRYPTO_IDS = {
    "BTC/USD": "bitcoin",
    "LTC/USD": "litecoin",
    "AAVE/USD": "aave",
    "DOT/USD": "polkadot",
    "BCH/USD": "bitcoin-cash",
    "ETH/USD": "ethereum",
    "SOL/USD": "solana",
    "LINK/USD": "chainlink",
}

DEFAULTS = {
    "enabled": True,
    "max_data_age_sec": 60,
    # Measured on the public feed (2026-10-20): last_updated advances every
    # 60-140s, occasionally 240s. A 60s rule on *this* source blocked nearly
    # every crypto order, so the freshness rule that governs execution is
    # max_data_age_sec (the broker quote). This one only catches a frozen or
    # abandoned feed.
    "max_market_age_sec": 180,
    "max_price_divergence_pct": 1.0,
    "max_spread_bps_equity": 25.0,
    "max_spread_bps_crypto": 100.0,
    "max_position_pct_of_volume": 0.5,
    "max_depth_fraction": 0.25,
    "max_risk_pct": 1.0,
    "min_reward_risk": 2.0,
    "max_cost_bps": 100.0,
    "coingecko_required_classes": ["crypto"],
    "coingecko_api": "https://api.coingecko.com/api/v3",
}


class GuardError(Exception):
    """A blocked order.  ``code`` is stable and safe to branch on."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclasses.dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    ts: dt.datetime

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_bps(self) -> float:
        mid = self.mid
        return 0.0 if mid <= 0 else (self.ask - self.bid) / mid * 1e4


@dataclasses.dataclass(frozen=True)
class Market:
    coin_id: str
    price: float
    market_cap: float
    volume_24h: float
    chg_24h: float
    chg_7d: float
    chg_30d: float
    ath_change_pct: float
    last_updated: dt.datetime


@dataclasses.dataclass
class TradeIntent:
    """Everything an order must declare before it is allowed out the door."""

    symbol: str
    asset_class: str          # 'crypto' | 'us_equity'
    side: str                 # 'buy' | 'sell'
    qty: float
    reason: str
    invalidation: str
    stop: float
    target: float
    fees_bps: float
    slippage_bps: float
    coin_id: str | None = None
    client_order_id: str | None = None
    approval_token: str | None = None
    note: str = ""

    @property
    def is_crypto(self) -> bool:
        return self.asset_class == "crypto"

    def bind_qty(self) -> str:
        return f"{float(self.qty):.8f}"


@dataclasses.dataclass(frozen=True)
class Clearance:
    intent: TradeIntent
    entry: float
    mid: float
    notional: float
    risk_amount: float
    risk_pct: float
    reward_risk: float
    spread_bps: float
    cost_bps: float
    liquidity_usd: float
    quote_ts: dt.datetime
    market: Market | None

    @property
    def symbol(self) -> str:
        return self.intent.symbol


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
def settings(cfg: dict | None) -> dict:
    merged = dict(DEFAULTS)
    if cfg:
        section = cfg.get("pretrade", cfg)
        for key, value in section.items():
            if key in merged:
                merged[key] = value
    return merged


def coin_id_for(symbol: str, override: dict | None = None) -> str | None:
    if override and symbol in override:
        return override[symbol]
    return CRYPTO_IDS.get(symbol)


# --------------------------------------------------------------------------- #
# market data (CoinGecko official REST API)
# --------------------------------------------------------------------------- #
class CoinGeckoProvider:
    """Minimal reader for the public markets endpoint.  No key required.

    Results are cached for ``ttl`` seconds *inside one process* so a 5-leg
    rebalance does not burn the free-tier rate limit.  Cached entries are still
    age-checked against ``last_updated`` on every evaluation, so caching can
    never smuggle stale data past the guard.
    """

    def __init__(self, base: str, ttl: float = 50.0, timeout: float = 12.0,
                 clock=None, opener=None) -> None:
        self.base = base.rstrip("/")
        self.ttl = ttl
        self.timeout = timeout
        self._clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self._cache: dict[str, tuple[Market, dt.datetime]] = {}
        self._opener = opener or urllib.request.urlopen

    def snapshot(self, coin_id: str) -> Market:
        now = self._clock()
        hit = self._cache.get(coin_id)
        if hit and (now - hit[1]).total_seconds() < self.ttl:
            return hit[0]

        query = urllib.parse.urlencode({
            "vs_currency": "usd",
            "ids": coin_id,
            "price_change_percentage": "24h,7d,30d",
        })
        url = f"{self.base}/coins/markets?{query}"
        request = urllib.request.Request(
            url, headers={"User-Agent": "auto-trader/1.0", "Accept": "application/json"}
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise GuardError("coingecko_http_error", f"{exc.code} for {coin_id}") from exc
        except Exception as exc:  # noqa: BLE001 - fail closed
            raise GuardError("coingecko_unreachable", f"{type(exc).__name__}: {exc}") from exc

        if not isinstance(payload, list) or not payload:
            raise GuardError("coingecko_empty", f"no row returned for {coin_id}")
        row = payload[0]
        try:
            market = Market(
                coin_id=coin_id,
                price=float(row["current_price"]),
                market_cap=float(row.get("market_cap") or 0.0),
                volume_24h=float(row.get("total_volume") or 0.0),
                chg_24h=float(row.get("price_change_percentage_24h_in_currency") or 0.0),
                chg_7d=float(row.get("price_change_percentage_7d_in_currency") or 0.0),
                chg_30d=float(row.get("price_change_percentage_30d_in_currency") or 0.0),
                ath_change_pct=float(row.get("ath_change_percentage") or 0.0),
                last_updated=_parse_iso(row["last_updated"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GuardError("coingecko_malformed", f"{coin_id}: {exc}") from exc

        self._cache[coin_id] = (market, now)
        return market


def _parse_iso(value: str) -> dt.datetime:
    text = str(value).replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


# --------------------------------------------------------------------------- #
# validation (pure)
# --------------------------------------------------------------------------- #
def evaluate(
    intent: TradeIntent,
    *,
    quote: Quote,
    market: Market | None,
    equity: float,
    cfg: dict | None = None,
    now: dt.datetime | None = None,
) -> Clearance:
    """Validate one order.  Raises GuardError on the first broken rule."""
    cfg = settings(cfg)
    now = now or dt.datetime.now(dt.timezone.utc)

    if not cfg["enabled"]:
        # Disabling the guard blocks trading rather than bypassing it.
        raise GuardError("guard_disabled", "pretrade.enabled is false")

    _check_intent(intent)
    if equity <= 0 or not math.isfinite(equity):
        raise GuardError("bad_equity", f"equity={equity!r}")

    entry = _check_quote(intent, quote, cfg, now)

    # A stop on the wrong side of the entry is not a stop, it is a typo that
    # would make the reward/risk calculation meaningless. Check direction first.
    if intent.side == "buy" and not (intent.stop < entry < intent.target):
        raise GuardError(
            "bad_levels", f"buy needs stop < entry < target, got {intent.stop} / {entry} / {intent.target}"
        )
    if intent.side == "sell" and not (intent.target < entry < intent.stop):
        raise GuardError(
            "bad_levels", f"sell needs target < entry < stop, got {intent.target} / {entry} / {intent.stop}"
        )

    liquidity = _check_liquidity(intent, quote, market, cfg, entry)
    _check_divergence(intent, quote, market, cfg, now)

    risk_per_unit = abs(entry - intent.stop)
    reward_per_unit = abs(intent.target - entry)
    if risk_per_unit <= 0 or reward_per_unit <= 0:
        raise GuardError("bad_levels", "stop/target distance is zero")

    notional = intent.qty * entry
    risk_amount = intent.qty * risk_per_unit
    risk_pct = risk_amount / equity * 100.0
    reward_risk = reward_per_unit / risk_per_unit
    cost_bps = float(intent.fees_bps) + float(intent.slippage_bps)

    if risk_pct > float(cfg["max_risk_pct"]) + 1e-9:
        raise GuardError(
            "risk_too_high",
            f"{risk_pct:.3f}% of equity exceeds {cfg['max_risk_pct']}% (risk ${risk_amount:,.2f})",
        )
    if reward_risk < float(cfg["min_reward_risk"]) - 1e-9:
        raise GuardError(
            "rr_too_low", f"{reward_risk:.2f}:1 below {cfg['min_reward_risk']}:1"
        )
    if cost_bps > float(cfg["max_cost_bps"]) + 1e-9:
        raise GuardError("cost_too_high", f"{cost_bps:.1f} bps > {cfg['max_cost_bps']} bps")

    return Clearance(
        intent=intent,
        entry=entry,
        mid=quote.mid,
        notional=notional,
        risk_amount=risk_amount,
        risk_pct=risk_pct,
        reward_risk=reward_risk,
        spread_bps=quote.spread_bps,
        cost_bps=cost_bps,
        liquidity_usd=liquidity,
        quote_ts=quote.ts,
        market=market,
    )


def _check_intent(intent: TradeIntent) -> None:
    if intent.side not in ("buy", "sell"):
        raise GuardError("bad_side", intent.side)
    if intent.asset_class not in ("crypto", "us_equity"):
        raise GuardError("bad_asset_class", intent.asset_class)
    if not isinstance(intent.qty, (int, float)) or not math.isfinite(float(intent.qty)) or intent.qty <= 0:
        raise GuardError("bad_qty", repr(intent.qty))
    if not str(intent.reason or "").strip():
        raise GuardError("missing_reason", "every order must state why it exists")
    if not str(intent.invalidation or "").strip():
        raise GuardError("missing_invalidation", "no invalidation condition given")
    for name in ("stop", "target"):
        value = getattr(intent, name)
        if value is None or not math.isfinite(float(value)) or float(value) <= 0:
            raise GuardError(f"missing_{name}", f"{name}={value!r}")
    for name in ("fees_bps", "slippage_bps"):
        value = getattr(intent, name)
        if value is None or not math.isfinite(float(value)) or float(value) < 0:
            raise GuardError(f"missing_{name}", f"{name}={value!r}")


def _check_quote(intent: TradeIntent, quote: Quote, cfg: dict, now: dt.datetime) -> float:
    if quote is None:
        raise GuardError("quote_missing", "no broker quote")
    if not (math.isfinite(quote.bid) and math.isfinite(quote.ask)) or quote.bid <= 0 or quote.ask <= 0:
        raise GuardError("quote_missing", f"bid={quote.bid!r} ask={quote.ask!r}")
    if quote.ask < quote.bid:
        raise GuardError("quote_inconsistent", f"ask {quote.ask} < bid {quote.bid}")
    if quote.ask_size <= 0 or quote.bid_size <= 0:
        raise GuardError("quote_one_sided", "no size on one side of the book")

    age = (now - _aware(quote.ts)).total_seconds()
    if age > float(cfg["max_data_age_sec"]):
        raise GuardError("stale_quote", f"broker quote is {age:.1f}s old (limit {cfg['max_data_age_sec']}s)")
    if age < -5:
        raise GuardError("quote_from_future", f"quote timestamp {-age:.1f}s ahead of clock")

    limit_key = "max_spread_bps_crypto" if intent.is_crypto else "max_spread_bps_equity"
    limit = float(cfg[limit_key])
    if quote.spread_bps > limit:
        raise GuardError(
            "wide_spread", f"{quote.spread_bps:.1f} bps > {limit:.1f} bps for {intent.symbol}"
        )

    # executable price: pay the ask, receive the bid — never the mid
    return quote.ask if intent.side == "buy" else quote.bid


def _check_liquidity(intent, quote, market, cfg, entry: float) -> float:
    notional = intent.qty * entry
    if intent.is_crypto:
        if market is None:
            raise GuardError("market_missing", f"no CoinGecko snapshot for {intent.symbol}")
        cap = market.volume_24h * float(cfg["max_position_pct_of_volume"]) / 100.0
        if cap <= 0:
            raise GuardError("liquidity_unknown", f"24h volume {market.volume_24h!r}")
        if notional > cap:
            raise GuardError(
                "insufficient_liquidity",
                f"order ${notional:,.0f} > {cfg['max_position_pct_of_volume']}% of 24h volume (${cap:,.0f})",
            )
        # broker depth is a single-venue print for crypto; require only that it exists
        return min(cap, float(quote.ask_size if intent.side == "buy" else quote.bid_size) * entry)

    depth = (quote.ask_size * quote.ask) if intent.side == "buy" else (quote.bid_size * quote.bid)
    if depth <= 0:
        raise GuardError("liquidity_unknown", "no top-of-book depth")
    cap = depth * float(cfg["max_depth_fraction"])
    if notional > cap:
        raise GuardError(
            "insufficient_liquidity",
            f"order ${notional:,.0f} > {cfg['max_depth_fraction']:.0%} of book depth (${depth:,.0f})",
        )
    return depth


def _check_divergence(intent, quote, market, cfg, now: dt.datetime) -> None:
    required = {str(c) for c in cfg["coingecko_required_classes"]}
    if market is None:
        if intent.asset_class in required:
            raise GuardError("market_missing", f"CoinGecko data required for {intent.symbol}")
        return

    age = (now - _aware(market.last_updated)).total_seconds()
    if age > float(cfg["max_market_age_sec"]):
        raise GuardError(
            "stale_market",
            f"CoinGecko {market.coin_id} is {age:.1f}s old (limit {cfg['max_market_age_sec']}s)",
        )
    if market.price <= 0:
        raise GuardError("market_malformed", f"{market.coin_id} price={market.price!r}")

    mid = quote.mid
    divergence = abs(market.price - mid) / mid * 100.0
    if divergence > float(cfg["max_price_divergence_pct"]):
        raise GuardError(
            "price_divergence",
            f"CoinGecko {market.price:,.2f} vs broker {mid:,.2f} = {divergence:.2f}% "
            f"(limit {cfg['max_price_divergence_pct']}%)",
        )


def _aware(ts: dt.datetime) -> dt.datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=dt.timezone.utc)
    return ts.astimezone(dt.timezone.utc)


# --------------------------------------------------------------------------- #
# live approval tokens
# --------------------------------------------------------------------------- #
def _sign(secret: str, token: str, symbol: str, side: str, qty: str, expires_at: str) -> str:
    message = f"{token}|{symbol}|{side}|{qty}|{expires_at}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def issue_approval(
    symbol: str,
    side: str,
    qty: float,
    *,
    secret: str | None = None,
    ttl_sec: int = 900,
    directory: pathlib.Path | None = None,
    now: dt.datetime | None = None,
) -> tuple[str, pathlib.Path]:
    """Mint a single-use, order-bound approval token (human-run CLI only).

    The HMAC secret lives in ``PRETRADE_APPROVAL_SECRET`` and is deliberately
    absent from the GitHub Actions secrets.  An unattended cron job therefore
    cannot mint or validate a token, no matter what files it can reach.
    """
    secret = secret or os.environ.get("PRETRADE_APPROVAL_SECRET", "")
    if len(secret) < 32:
        raise GuardError("approval_secret_missing", "PRETRADE_APPROVAL_SECRET must be >= 32 chars")
    if side not in ("buy", "sell"):
        raise GuardError("bad_side", side)

    now = now or dt.datetime.now(dt.timezone.utc)
    directory = pathlib.Path(directory or APPROVALS_DIR)
    directory.mkdir(parents=True, exist_ok=True)

    token = secrets.token_hex(16)
    expires_at = (now + dt.timedelta(seconds=int(ttl_sec))).isoformat()
    qty_text = f"{float(qty):.8f}"
    record = {
        "token": token,
        "symbol": symbol,
        "side": side,
        "qty": qty_text,
        "issued_at": now.isoformat(),
        "expires_at": expires_at,
        "signature": _sign(secret, token, symbol, side, qty_text, expires_at),
    }
    path = directory / f"{token}.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return token, path


def consume_approval(
    intent: TradeIntent,
    *,
    secret: str | None = None,
    directory: pathlib.Path | None = None,
    now: dt.datetime | None = None,
) -> None:
    """Validate and burn a token.  Raises GuardError on any mismatch."""
    secret = secret or os.environ.get("PRETRADE_APPROVAL_SECRET", "")
    if len(secret) < 32:
        raise GuardError("approval_secret_missing", "no approval secret in this environment")

    token = str(intent.approval_token or "").strip()
    if not token:
        raise GuardError("approval_missing", "live order requires an explicit approval token")

    directory = pathlib.Path(directory or APPROVALS_DIR)
    path = directory / f"{token}.json"
    if not path.exists():
        raise GuardError("approval_unknown", f"no approval record for token {token[:8]}...")

    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        expected = _sign(
            secret, record["token"], record["symbol"], record["side"],
            record["qty"], record["expires_at"],
        )
        if not hmac.compare_digest(expected, str(record.get("signature", ""))):
            raise GuardError("approval_forged", "signature mismatch")
        now = now or dt.datetime.now(dt.timezone.utc)
        if _parse_iso(record["expires_at"]) <= now:
            raise GuardError("approval_expired", record["expires_at"])
        if record["symbol"] != intent.symbol or record["side"] != intent.side:
            raise GuardError(
                "approval_mismatch",
                f"token is for {record['symbol']} {record['side']}, order is {intent.symbol} {intent.side}",
            )
        if record["qty"] != intent.bind_qty():
            raise GuardError("approval_mismatch", f"token qty {record['qty']} != order qty {intent.bind_qty()}")
    except GuardError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed
        raise GuardError("approval_malformed", f"{type(exc).__name__}: {exc}") from exc
    finally:
        pass

    used = directory / f"{token}.used"
    if used.exists():
        raise GuardError("approval_replayed", "token already spent")
    os.replace(path, used)


# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #
def log(record: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} | {record}\n")


def log_clearance(intent: TradeIntent, clearance: Clearance, *, paper: bool) -> None:
    market = clearance.market
    cg = f"{market.price:.4f}@{market.last_updated.isoformat()}" if market else "n/a"
    log(
        f"{intent.symbol} | {intent.side.upper()} | {intent.asset_class} | "
        f"qty={intent.qty:g} entry={clearance.entry:,.4f} mid={clearance.mid:,.4f} "
        f"notional=${clearance.notional:,.2f} spread={clearance.spread_bps:.1f}bps "
        f"risk=${clearance.risk_amount:,.2f} ({clearance.risk_pct:.3f}%) "
        f"rr={clearance.reward_risk:.2f}:1 cost={clearance.cost_bps:.1f}bps "
        f"cg={cg} quote={clearance.quote_ts.isoformat()} paper={paper} "
        f"reason={intent.reason!r} ALLOW"
    )


def log_rejection(intent: TradeIntent | None, error: GuardError, *, paper: bool) -> None:
    symbol = intent.symbol if intent else "?"
    log(f"{symbol} | BLOCKED | {error.code} | {error.detail} | paper={paper}")


# --------------------------------------------------------------------------- #
# CLI: check an order by hand, or mint an approval token
# --------------------------------------------------------------------------- #
def _cli() -> None:  # pragma: no cover - exercised manually
    import argparse
    import cfg as cfgmod

    ap = argparse.ArgumentParser(description="Pre-trade guard utilities")
    sub = ap.add_subparsers(dest="cmd", required=True)

    chk = sub.add_parser("check", help="run the guard against live data")
    chk.add_argument("--symbol", required=True)
    chk.add_argument("--side", required=True, choices=["buy", "sell"])
    chk.add_argument("--qty", type=float, required=True)
    chk.add_argument("--stop", type=float, required=True)
    chk.add_argument("--target", type=float, required=True)
    chk.add_argument("--reason", default="manual check")
    chk.add_argument("--invalidation", default="manual check")
    chk.add_argument("--fees-bps", type=float, default=0.0)
    chk.add_argument("--slippage-bps", type=float, default=5.0)

    app = sub.add_parser("approve", help="mint a live-order approval token")
    app.add_argument("--symbol", required=True)
    app.add_argument("--side", required=True, choices=["buy", "sell"])
    app.add_argument("--qty", type=float, required=True)
    app.add_argument("--ttl", type=int, default=900)

    args = ap.parse_args()
    cfg = cfgmod.load()

    if args.cmd == "approve":
        token, path = issue_approval(args.symbol, args.side, args.qty, ttl_sec=args.ttl)
        print(f"approval token: {token}")
        print(f"written to   : {path}")
        print("single use, bound to symbol/side/qty, expires in "
              f"{args.ttl}s. Pass it as --approval {token}")
        return

    import broker
    import pre_trade as _guard  # canonical module instance, even when run as a script

    intent = TradeIntent(
        symbol=args.symbol,
        asset_class="crypto" if "/" in args.symbol else "us_equity",
        side=args.side,
        qty=args.qty,
        reason=args.reason,
        invalidation=args.invalidation,
        stop=args.stop,
        target=args.target,
        fees_bps=args.fees_bps,
        slippage_bps=args.slippage_bps,
        coin_id=coin_id_for(args.symbol),
    )
    guard = broker.connect(cfg=cfg, paper=True)
    try:
        clearance = guard.check(intent)
    except _guard.GuardError as exc:
        print(f"BLOCKED  {intent.symbol} {intent.side} {intent.qty:g}: {exc.code} — {exc.detail}")
        raise SystemExit(1) from None
    print(f"ALLOW  {clearance.symbol} {intent.side} {intent.qty:g}")
    print(f"  entry      {clearance.entry:,.4f}  (mid {clearance.mid:,.4f})")
    print(f"  notional   ${clearance.notional:,.2f}")
    print(f"  risk       ${clearance.risk_amount:,.2f} ({clearance.risk_pct:.3f}% of equity)")
    print(f"  reward/risk {clearance.reward_risk:.2f}:1")
    print(f"  spread     {clearance.spread_bps:.1f} bps")


if __name__ == "__main__":  # pragma: no cover
    _cli()
