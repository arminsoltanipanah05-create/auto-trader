"""The only module permitted to submit orders.

``GuardedBroker.submit`` is the single choke point: it runs
``pre_trade.evaluate`` (market data, spread, liquidity, risk, reward/risk) and,
for live orders, burns a signed approval token, before it ever reaches
``client.submit_order``.

The raw ``TradingClient`` is held privately and is never returned to callers.
Reads are exposed as ``account()`` / ``positions()`` so the bots do not need a
reference to the client at all.  ``tests/test_pretrade.py`` asserts statically
that ``submit_order`` appears nowhere outside this file.
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib

import pre_trade
from pre_trade import GuardError, Quote, TradeIntent


class AlpacaQuotes:
    """Executable quotes straight from the broker's data API."""

    def __init__(self, key: str, secret: str) -> None:
        from alpaca.data.historical.crypto import CryptoHistoricalDataClient
        from alpaca.data.historical.stock import StockHistoricalDataClient

        self._crypto = CryptoHistoricalDataClient(key, secret)
        self._stock = StockHistoricalDataClient(key, secret)

    def quote(self, symbol: str, asset_class: str) -> Quote:
        try:
            if asset_class == "crypto":
                from alpaca.data.requests import CryptoLatestQuoteRequest

                payload = self._crypto.get_crypto_latest_quote(
                    CryptoLatestQuoteRequest(symbol_or_symbols=symbol)
                )
            else:
                from alpaca.data.requests import StockLatestQuoteRequest

                payload = self._stock.get_stock_latest_quote(
                    StockLatestQuoteRequest(symbol_or_symbols=symbol)
                )
        except Exception as exc:  # noqa: BLE001 - fail closed
            raise GuardError(
                "quote_unavailable", f"{symbol}: {type(exc).__name__}: {exc}"
            ) from exc

        row = payload.get(symbol) if isinstance(payload, dict) else None
        if row is None:
            raise GuardError("quote_unavailable", f"no quote returned for {symbol}")

        ts = getattr(row, "timestamp", None)
        if ts is None:
            raise GuardError("quote_unavailable", f"{symbol}: quote has no timestamp")
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)

        return Quote(
            symbol=symbol,
            bid=float(row.bid_price or 0.0),
            ask=float(row.ask_price or 0.0),
            bid_size=float(row.bid_size or 0.0),
            ask_size=float(row.ask_size or 0.0),
            ts=ts,
        )


class GuardedBroker:
    def __init__(
        self,
        client,
        *,
        paper: bool,
        cfg: dict | None = None,
        quotes=None,
        markets=None,
        clock=None,
        approvals_dir: pathlib.Path | None = None,
        secret: str | None = None,
    ) -> None:
        self._client = client
        self._paper = bool(paper)
        self._cfg = cfg or {}
        self._settings = pre_trade.settings(self._cfg)

        key = os.environ.get("APCA_API_KEY_ID", "")
        sec = os.environ.get("APCA_API_SECRET_KEY", "")
        self._quotes = quotes or AlpacaQuotes(key, sec)
        self._markets = markets or pre_trade.CoinGeckoProvider(
            self._settings["coingecko_api"]
        )
        self._clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self._approvals = pathlib.Path(approvals_dir or pre_trade.APPROVALS_DIR)
        self._secret = secret if secret is not None else os.environ.get("PRETRADE_APPROVAL_SECRET", "")

        if not self._paper and len(self._secret) < 32:
            raise GuardError(
                "approval_secret_missing",
                "live mode requires PRETRADE_APPROVAL_SECRET (>= 32 chars) in the environment",
            )

    # ---- reads ---------------------------------------------------------- #
    def account(self):
        return self._client.get_account()

    def positions(self):
        return self._client.get_all_positions()

    @property
    def paper(self) -> bool:
        return self._paper

    # ---- the gate ------------------------------------------------------- #
    def _market_for(self, intent: TradeIntent):
        coin_id = intent.coin_id or pre_trade.coin_id_for(intent.symbol)
        if coin_id is None:
            return None
        return self._markets.snapshot(coin_id)

    def check(self, intent: TradeIntent, equity: float | None = None) -> pre_trade.Clearance:
        """Run the guard without submitting.  Used by --dry-run and the CLI."""
        try:
            quote = self._quotes.quote(intent.symbol, intent.asset_class)
            market = self._market_for(intent)
            if equity is None:
                equity = float(self.account().equity)
            return pre_trade.evaluate(
                intent,
                quote=quote,
                market=market,
                equity=float(equity),
                cfg=self._cfg,
                now=self._clock(),
            )
        except GuardError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed
            raise GuardError(
                "guard_unexpected", f"{type(exc).__name__}: {exc}"
            ) from exc

    def submit(self, intent: TradeIntent, equity: float | None = None):
        """Validate, then (and only then) place the order."""
        try:
            clearance = self.check(intent, equity=equity)
        except GuardError as exc:
            pre_trade.log_rejection(intent, exc, paper=self._paper)
            raise

        try:
            if not self._paper:
                pre_trade.consume_approval(
                    intent,
                    secret=self._secret,
                    directory=self._approvals,
                    now=self._clock(),
                )
            pre_trade.log_clearance(intent, clearance, paper=self._paper)
        except GuardError as exc:
            pre_trade.log_rejection(intent, exc, paper=self._paper)
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed
            err = GuardError("guard_unexpected", f"{type(exc).__name__}: {exc}")
            pre_trade.log_rejection(intent, err, paper=self._paper)
            raise err from exc

        request = self._request(intent)
        try:
            return self._client.submit_order(request)
        except Exception as exc:  # noqa: BLE001
            pre_trade.log(
                f"{intent.symbol} | {intent.side.upper()} | SUBMIT_FAILED | "
                f"{type(exc).__name__}: {exc} | paper={self._paper}"
            )
            raise

    def _request(self, intent: TradeIntent):
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        kwargs = {
            "symbol": intent.symbol,
            "qty": intent.qty,
            "side": OrderSide.BUY if intent.side == "buy" else OrderSide.SELL,
            "time_in_force": TimeInForce.DAY if not intent.is_crypto else TimeInForce.GTC,
        }
        if intent.client_order_id:
            kwargs["client_order_id"] = intent.client_order_id
        return MarketOrderRequest(**kwargs)


def connect(cfg: dict | None = None, paper: bool = True, **kwargs) -> GuardedBroker:
    """Build the guarded broker for this process."""
    if "client" in kwargs:
        return GuardedBroker(kwargs.pop("client"), paper=paper, cfg=cfg, **kwargs)

    from alpaca.trading.client import TradingClient

    client = TradingClient(
        os.environ["APCA_API_KEY_ID"], os.environ["APCA_API_SECRET_KEY"], paper=paper
    )
    return GuardedBroker(client, paper=paper, cfg=cfg, **kwargs)
