"""Tests for the pre-trade guard.

Run from the repository root:

    python -m unittest discover -s python/tests -v

The point of these tests is negative: prove that when market data is missing,
stale, contradictory or the approval is absent, ``client.submit_order`` is
*never* reached.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

PYTHON_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PYTHON_DIR))

import broker  # noqa: E402
import pre_trade  # noqa: E402
from pre_trade import GuardError, Market, Quote, TradeIntent  # noqa: E402

NOW = dt.datetime(2026, 10, 20, 21, 0, 0, tzinfo=dt.timezone.utc)
SECRET = "s3cr3t-K9fQ2mZx7Lp4Vn8Rt1Wb6Yh0Jd5Gc2Aq7Nm"
EQUITY = 100_000.0


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
def make_quote(symbol="BTC/USD", bid=100.00, ask=100.05, bsize=5_000.0,
               asize=5_000.0, age=1.0) -> Quote:
    return Quote(symbol, bid, ask, bsize, asize, NOW - dt.timedelta(seconds=age))


def make_market(coin_id="bitcoin", price=100.00, volume=1e9, age=1.0) -> Market:
    return Market(
        coin_id=coin_id, price=price, market_cap=1e12, volume_24h=volume,
        chg_24h=1.0, chg_7d=-1.0, chg_30d=2.0, ath_change_pct=-30.0,
        last_updated=NOW - dt.timedelta(seconds=age),
    )


class FakeQuotes:
    def __init__(self, quote=None, error=None):
        self._quote = quote
        self._error = error

    def quote(self, symbol, asset_class):
        if self._error:
            raise self._error
        return self._quote


class FakeMarkets:
    def __init__(self, market=None, error=None):
        self._market = market
        self._error = error

    def snapshot(self, coin_id):
        if self._error:
            raise self._error
        return self._market


class FakeClient:
    """Records every submit_order call.  Nothing leaves the process."""

    def __init__(self, last_equity=None, positions=None):
        self.submitted = []
        self.last_equity = last_equity
        self._positions = positions or []

    def get_account(self):
        return SimpleNamespace(
            equity=EQUITY,
            last_equity=EQUITY if self.last_equity is None else self.last_equity,
            account_number="TEST",
            status="ACTIVE",
        )

    def get_all_positions(self):
        return self._positions

    def submit_order(self, request):
        self.submitted.append(request)
        return SimpleNamespace(
            symbol=request.symbol, side=SimpleNamespace(value=request.side),
            qty=request.qty, id="fake-1", client_order_id=None,
        )


def crypto_position(symbol="BTC/USD", value=0.0):
    return SimpleNamespace(symbol=symbol, asset_class="crypto", market_value=str(value))


def intent(**overrides) -> TradeIntent:
    base = dict(
        symbol="BTC/USD",
        asset_class="crypto",
        side="buy",
        qty=10.0,
        reason="unit test rebalance",
        invalidation="daily close below 97",
        stop=97.0,
        target=108.0,
        fees_bps=25.0,
        slippage_bps=30.0,
    )
    base.update(overrides)
    return TradeIntent(**base)


class GuardTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self._orig_log = pre_trade.LOG_PATH
        pre_trade.LOG_PATH = self.tmp / "pre-trade.log"
        self.client = FakeClient()

    def tearDown(self):
        pre_trade.LOG_PATH = self._orig_log

    def build(self, *, quote=None, market=None, quotes=None, markets=None,
              paper=True, cfg=None, secret=SECRET, client=None):
        return broker.GuardedBroker(
            client or self.client,
            paper=paper,
            cfg=cfg or {"pretrade": {}, "execution": {"max_daily_loss_pct": 0.03}},
            quotes=quotes or FakeQuotes(quote or make_quote()),
            markets=markets or FakeMarkets(market or make_market()),
            clock=lambda: NOW,
            approvals_dir=self.tmp,
            secret=secret,
        )

    def assert_blocked(self, guard, order, code):
        with self.assertRaises(GuardError) as ctx:
            guard.submit(order, equity=EQUITY)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(self.client.submitted, [], "client.submit_order was called")
        return ctx.exception


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #
class TestHappyPath(GuardTestCase):
    def test_valid_paper_order_reaches_broker(self):
        guard = self.build()
        guard.submit(intent(), equity=EQUITY)
        self.assertEqual(len(self.client.submitted), 1)
        self.assertEqual(self.client.submitted[0].symbol, "BTC/USD")

    def test_valid_equity_order_reaches_broker(self):
        guard = self.build(
            quote=make_quote(symbol="SPY", bid=500.00, ask=500.02),
            market=None,
            markets=FakeMarkets(error=AssertionError("equity must not need CoinGecko")),
        )
        guard.submit(
            intent(symbol="SPY", asset_class="us_equity", qty=5, stop=480.0, target=560.0),
            equity=EQUITY,
        )
        self.assertEqual(len(self.client.submitted), 1)


# --------------------------------------------------------------------------- #
# data quality
# --------------------------------------------------------------------------- #
class TestDataQuality(GuardTestCase):
    def test_coingecko_unreachable_blocks(self):
        guard = self.build(markets=FakeMarkets(error=GuardError("coingecko_unreachable", "down")))
        self.assert_blocked(guard, intent(), "coingecko_unreachable")

    def test_missing_coingecko_snapshot_blocks_crypto(self):
        guard = self.build(market=None, markets=FakeMarkets(market=None))
        self.assert_blocked(guard, intent(), "market_missing")

    def test_stale_coingecko_blocks(self):
        guard = self.build(market=make_market(age=600))
        self.assert_blocked(guard, intent(), "stale_market")

    def test_stale_broker_quote_blocks(self):
        guard = self.build(quote=make_quote(age=180))
        self.assert_blocked(guard, intent(), "stale_quote")

    def test_conflicting_prices_block(self):
        guard = self.build(market=make_market(price=103.0))
        self.assert_blocked(guard, intent(), "price_divergence")

    def test_one_sided_book_blocks(self):
        guard = self.build(quote=make_quote(asize=0.0))
        self.assert_blocked(guard, intent(), "quote_one_sided")

    def test_wide_spread_blocks(self):
        guard = self.build(
            quote=make_quote(symbol="SPY", bid=500.0, ask=505.0),
            markets=FakeMarkets(market=None),
        )
        self.assert_blocked(
            guard,
            intent(symbol="SPY", asset_class="us_equity", qty=5, stop=480.0, target=560.0),
            "wide_spread",
        )

    def test_provider_exception_fails_closed(self):
        guard = self.build(quotes=FakeQuotes(error=RuntimeError("socket closed")))
        self.assert_blocked(guard, intent(), "guard_unexpected")

    def test_malformed_market_blocks(self):
        guard = self.build(market=make_market(price=float("nan")))
        self.assert_blocked(guard, intent(), "market_malformed")

    def test_malformed_quote_blocks(self):
        guard = self.build(quote=make_quote(asize=float("nan")))
        self.assert_blocked(guard, intent(), "quote_malformed")


# --------------------------------------------------------------------------- #
# liquidity
# --------------------------------------------------------------------------- #
class TestLiquidity(GuardTestCase):
    def test_crypto_order_above_volume_cap_blocks(self):
        guard = self.build(market=make_market(volume=1_000.0))
        self.assert_blocked(guard, intent(), "insufficient_liquidity")

    def test_equity_order_above_book_depth_blocks(self):
        guard = self.build(
            quote=make_quote(symbol="SPY", bid=500.0, ask=500.02, asize=10.0),
            markets=FakeMarkets(market=None),
        )
        self.assert_blocked(
            guard,
            intent(symbol="SPY", asset_class="us_equity", qty=10, stop=480.0, target=560.0),
            "insufficient_liquidity",
        )


# --------------------------------------------------------------------------- #
# intent completeness, risk and reward
# --------------------------------------------------------------------------- #
class TestIntentAndRisk(GuardTestCase):
    def test_missing_reason_blocks(self):
        self.assert_blocked(self.build(), intent(reason="   "), "missing_reason")

    def test_missing_invalidation_blocks(self):
        self.assert_blocked(self.build(), intent(invalidation=""), "missing_invalidation")

    def test_missing_stop_blocks(self):
        self.assert_blocked(self.build(), intent(stop=0.0), "missing_stop")

    def test_missing_target_blocks(self):
        self.assert_blocked(self.build(), intent(target=0.0), "missing_target")

    def test_wrong_side_levels_block(self):
        self.assert_blocked(self.build(), intent(stop=110.0, target=90.0), "bad_levels")

    def test_reward_risk_below_two_blocks(self):
        self.assert_blocked(self.build(), intent(target=104.0), "rr_too_low")

    def test_risk_above_one_percent_blocks(self):
        self.assert_blocked(self.build(), intent(qty=1_000.0), "risk_too_high")

    def test_slippage_too_high_blocks(self):
        self.assert_blocked(self.build(), intent(slippage_bps=200.0), "slippage_too_high")

    def test_guard_disabled_blocks_rather_than_bypasses(self):
        guard = self.build(cfg={"pretrade": {"enabled": False},
                                "execution": {"max_daily_loss_pct": 0.03}})
        self.assert_blocked(guard, intent(), "guard_disabled")


class TestPortfolioLimits(GuardTestCase):
    def test_daily_loss_breaker_blocks(self):
        client = FakeClient(last_equity=110_000.0)
        guard = self.build(client=client)
        with self.assertRaises(GuardError) as ctx:
            guard.submit(intent(), equity=EQUITY)
        self.assertEqual(ctx.exception.code, "daily_loss_limit")
        self.assertEqual(client.submitted, [])

    def test_crypto_gross_cap_blocks(self):
        client = FakeClient(positions=[crypto_position(value=69_500.0)])
        guard = self.build(client=client)
        with self.assertRaises(GuardError) as ctx:
            guard.submit(intent(), equity=EQUITY)
        self.assertEqual(ctx.exception.code, "gross_exposure")
        self.assertEqual(client.submitted, [])


# --------------------------------------------------------------------------- #
# live approval
# --------------------------------------------------------------------------- #
class TestLiveApproval(GuardTestCase):
    def test_live_without_secret_refuses_to_construct(self):
        with self.assertRaises(GuardError) as ctx:
            self.build(paper=False, secret="")
        self.assertEqual(ctx.exception.code, "approval_secret_missing")

    def test_live_without_token_blocks(self):
        guard = self.build(paper=False)
        self.assert_blocked(guard, intent(), "approval_missing")

    def test_live_with_unknown_token_blocks(self):
        guard = self.build(paper=False)
        self.assert_blocked(guard, intent(approval_token="deadbeef" * 4), "approval_unknown")

    def test_live_with_forged_token_blocks(self):
        token = "a" * 32
        (self.tmp / f"{token}.json").write_text(json.dumps({
            "token": token, "symbol": "BTC/USD", "side": "buy", "qty": "10.00000000",
            "issued_at": NOW.isoformat(),
            "expires_at": (NOW + dt.timedelta(minutes=5)).isoformat(),
            "signature": "0" * 64,
        }), encoding="utf-8")
        guard = self.build(paper=False)
        self.assert_blocked(guard, intent(approval_token=token), "approval_forged")

    def test_live_token_bound_to_different_order_blocks(self):
        token, _ = pre_trade.issue_approval(
            "BTC/USD", "buy", 5.0, secret=SECRET, directory=self.tmp, now=NOW
        )
        guard = self.build(paper=False)
        self.assert_blocked(guard, intent(approval_token=token), "approval_mismatch")

    def test_token_for_another_asset_blocks(self):
        token, _ = pre_trade.issue_approval(
            "ETH/USD", "buy", 10.0, secret=SECRET, directory=self.tmp, now=NOW
        )
        guard = self.build(paper=False)
        self.assert_blocked(guard, intent(approval_token=token), "approval_mismatch")

    def test_expired_token_blocks(self):
        token, _ = pre_trade.issue_approval(
            "BTC/USD", "buy", 10.0, secret=SECRET, directory=self.tmp,
            now=NOW - dt.timedelta(seconds=600), ttl_sec=60,
        )
        guard = self.build(paper=False)
        self.assert_blocked(guard, intent(approval_token=token), "approval_expired")

    def test_weak_secret_refuses_to_construct(self):
        with self.assertRaises(GuardError) as ctx:
            self.build(paper=False, secret="a" * 40)
        self.assertEqual(ctx.exception.code, "approval_secret_weak")

    def test_guard_exposes_no_raw_client(self):
        guard = self.build()
        self.assertFalse(hasattr(guard, "client"), "GuardedBroker leaks the trading client")


class TestKillSwitch(GuardTestCase):
    def test_kill_switch_blocks_both_books(self):
        import risk

        original = risk.KILL_FILE
        risk.KILL_FILE = self.tmp / "KILL_SWITCH"
        risk.KILL_FILE.write_text("test", encoding="utf-8")
        try:
            self.assert_blocked(self.build(), intent(), "kill_switch")
        finally:
            risk.KILL_FILE = original

    def test_live_valid_token_submits_once_and_cannot_be_replayed(self):
        token, _ = pre_trade.issue_approval(
            "BTC/USD", "buy", 10.0, secret=SECRET, directory=self.tmp, now=NOW
        )
        guard = self.build(paper=False)
        guard.submit(intent(approval_token=token), equity=EQUITY)
        self.assertEqual(len(self.client.submitted), 1)

        # second attempt with the same token must be refused
        with self.assertRaises(GuardError) as ctx:
            guard.submit(intent(approval_token=token), equity=EQUITY)
        self.assertIn(ctx.exception.code, {"approval_unknown", "approval_replayed"})
        self.assertEqual(len(self.client.submitted), 1, "replayed token placed a second order")

    def test_mint_requires_long_secret(self):
        with self.assertRaises(GuardError):
            pre_trade.issue_approval("BTC/USD", "buy", 1.0, secret="short", directory=self.tmp)


# --------------------------------------------------------------------------- #
# structural guarantee
# --------------------------------------------------------------------------- #
def _submit_order_calls(path: pathlib.Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "submit_order"
        ):
            lines.append(node.lineno)
    return lines


class TestNoBypass(unittest.TestCase):
    def test_only_broker_module_calls_submit_order(self):
        offenders = {}
        for path in sorted(PYTHON_DIR.glob("*.py")):
            hits = _submit_order_calls(path)
            if hits:
                offenders[path.name] = hits
        self.assertEqual(
            set(offenders), {"broker.py"},
            f"submit_order reached outside the guard: {offenders}",
        )

    def test_both_bots_use_the_guarded_broker(self):
        for name in ("execute.py", "crypto_bot.py"):
            source = (PYTHON_DIR / name).read_text(encoding="utf-8")
            self.assertIn("guard.submit(", source, f"{name} does not use the guard")
            self.assertNotIn("client.submit_order", source, f"{name} still holds a raw client")


if __name__ == "__main__":
    unittest.main(verbosity=2)
