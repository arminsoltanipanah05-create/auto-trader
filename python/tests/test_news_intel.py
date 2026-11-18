"""Tests for the News Intelligence Layer.

Run from the repository root:

    python -m unittest discover -s python/tests -v

The theme is the same as the pre-trade tests: prove the *negative*. News may
veto, pause, halve or confirm — it may never enlarge a position, never bypass
the guard, and never block an exit. Tier 3 may not authorize anything.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import tempfile
import unittest

PYTHON_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PYTHON_DIR))

import news_intel  # noqa: E402
import news_review  # noqa: E402
import news_sources  # noqa: E402
from news_intel import (  # noqa: E402
    Influence, Store, Story, apply_effect, classify, decide, evaluate_event,
    is_entry, no_trade_reasons, source_tier, verification_of,
)

NOW = dt.datetime(2026, 11, 18, 15, 0, tzinfo=dt.timezone.utc)


def story(source="reuters", headline="Acme beats estimates", *, tier=2,
          minutes_ago=10, summary="", symbols=("ACME",), url="https://example.test/a") -> Story:
    return Story(
        source=source, headline=headline, url=url,
        published_at=NOW - dt.timedelta(minutes=minutes_ago),
        retrieved_at=NOW, summary=summary, symbols=tuple(symbols), tier=tier,
        external_id="x",
    )


def influence(**overrides) -> Influence:
    base = dict(
        event_summary="Acme beats estimates",
        event_type="earnings_surprise",
        affected_symbols=["ACME"],
        verification="VERIFIED",
        direction="BULLISH",
        confidence=75.0,
        impact=80.0,
        novelty=90.0,
        priced_in_percent=10.0,
        expected_duration="intraday",
        invalidation_condition="a filing contradicting the report",
        supports_existing_signal=True,
        published_at=NOW.isoformat(),
        retrieved_at=NOW.isoformat(),
    )
    base.update(overrides)
    inf = Influence(**base)
    # evaluate_event derives the relation; mirror that here so a NEUTRAL story
    # is never mistaken for an opposing one.
    inf.relation = ("NEUTRAL" if inf.direction == "NEUTRAL"
                    else "SUPPORTS" if inf.supports_existing_signal else "OPPOSES")
    return inf


class TmpStoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self._log = news_intel.LOG_PATH
        news_intel.LOG_PATH = self.tmp / "news.log"
        self.store = Store(self.tmp)

    def tearDown(self):
        news_intel.LOG_PATH = self._log


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #
class TestClassification(unittest.TestCase):
    def test_earnings_beat_is_bullish(self):
        event, direction, impact = classify("Acme beats estimates, raises outlook")
        self.assertIn(event, {"earnings_surprise", "guidance_change"})
        self.assertIn(direction, {"BULLISH", "STRONGLY_BULLISH"})
        self.assertGreaterEqual(impact, 60)

    def test_guidance_cut_is_bearish(self):
        event, direction, _ = classify("Acme cuts guidance for the year")
        self.assertEqual(event, "guidance_change")
        self.assertIn(direction, {"BEARISH", "STRONGLY_BEARISH"})

    def test_rumour_is_flagged(self):
        event, _, _ = classify("Reportedly Acme is in takeover talks, sources say")
        self.assertEqual(event, "rumor")

    def test_delisting_form_impact(self):
        event, _, impact = classify("Acme files 25-NSE: delisting notice")
        self.assertGreaterEqual(impact, 45)

    def test_neutral_headline_stays_neutral(self):
        _, direction, _ = classify("Acme appoints new office manager for Ohio")
        self.assertEqual(direction, "NEUTRAL")


class TestSourceTiers(unittest.TestCase):
    def test_tiers(self):
        self.assertEqual(source_tier("SEC EDGAR"), 1)
        self.assertEqual(source_tier("federal reserve"), 1)
        self.assertEqual(source_tier("Reuters"), 2)
        self.assertEqual(source_tier("WSJ"), 2)
        self.assertEqual(source_tier("fiscal.ai"), 3)      # aggregator
        self.assertEqual(source_tier("some blog"), 3)


class TestMatching(unittest.TestCase):
    def test_coins_are_matched_by_name(self):
        self.assertTrue(news_intel._symbol_in_text("BTC/USD", "Bitcoin rallies past $90,000"))
        self.assertTrue(news_intel._symbol_in_text("ETH/USD", "Ethereum's next upgrade ships"))
        self.assertTrue(news_intel._symbol_in_text("BCH/USD", "Bitcoin Cash volume doubles"))

    def test_ticker_must_be_a_standalone_token(self):
        self.assertFalse(news_intel._symbol_in_text("BTC/USD", "The BTCUSD pair trades flat"))
        self.assertTrue(news_intel._symbol_in_text("NVDA", "NVDA files an 8-K"))
        self.assertFalse(news_intel._symbol_in_text("NVDA", "NVDAX is not a ticker"))

    def test_substring_is_not_a_keyword(self):
        # "software" must not become a war, "execute" must not become a cut.
        event, _, _ = news_intel.classify("Acme Software announces new partners")
        self.assertNotEqual(event, "geopolitical_event")
        event, _, _ = news_intel.classify("Acme executes its share repurchase plan")
        self.assertNotEqual(event, "dividend_change")

    def test_macro_release_speaks_about_a_declared_proxy(self):
        fed = story("cnbc", "Fed holds rates steady after FOMC meeting",
                    symbols=(), url="https://cnbc.test/1")
        found = news_intel.relevant_stories("SPY", [fed], {"macro_proxies": ["SPY"]})
        self.assertEqual(len(found), 1)

    def test_macro_small_talk_does_not_speak_about_a_proxy(self):
        noise = story("cnbc", "Mortgage demand turns lower as interest rates rise",
                      symbols=(), url="https://cnbc.test/2")
        found = news_intel.relevant_stories("SPY", [noise], {"macro_proxies": ["SPY"]})
        self.assertEqual(found, [])

    def test_macro_release_ignores_a_symbol_that_is_not_a_proxy(self):
        fed = story("cnbc", "Fed holds rates steady after FOMC meeting",
                    symbols=(), url="https://cnbc.test/3")
        self.assertEqual(
            news_intel.relevant_stories("AAPL", [fed], {"macro_proxies": ["SPY"]}), [])

    def test_tagged_story_is_always_relevant(self):
        tagged = story("reuters", "Acme wins a contract", symbols=("ACME",))
        self.assertEqual(len(news_intel.relevant_stories("ACME", [tagged])), 1)


class TestVerification(unittest.TestCase):
    def test_primary_source_verifies_alone(self):
        self.assertEqual(
            verification_of([story("SEC EDGAR", "Acme files 8-K: material event", tier=1)]),
            "VERIFIED")

    def test_two_independent_tier2_verify(self):
        self.assertEqual(
            verification_of([story("reuters"), story("bloomberg", "Acme beats estimates")]),
            "VERIFIED")

    def test_one_tier2_is_partial(self):
        self.assertEqual(verification_of([story("reuters")]), "PARTIALLY_VERIFIED")

    def test_same_outlet_twice_is_one_source(self):
        self.assertEqual(
            verification_of([story("reuters"), story("reuters", "Acme beats estimates")]),
            "PARTIALLY_VERIFIED")

    def test_tier3_only_is_unverified(self):
        self.assertEqual(
            verification_of([story("fiscal.ai", "Acme beats estimates", tier=3)]),
            "UNVERIFIED")

    def test_opposing_sources_are_contradicted(self):
        self.assertEqual(
            verification_of([
                story("reuters", "Acme beats estimates, raises outlook"),
                story("bloomberg", "Acme cuts guidance, shares plunge"),
            ]),
            "CONTRADICTED")

    def test_no_stories_is_unverified(self):
        self.assertEqual(verification_of([]), "UNVERIFIED")


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #
class TestScoring(unittest.TestCase):
    def test_scores_are_bounded(self):
        inf = evaluate_event([story()], symbol="ACME", side="buy", now=NOW)
        for field in ("confidence", "impact", "novelty", "priced_in_percent", "urgency"):
            value = getattr(inf, field)
            self.assertGreaterEqual(value, 0.0, field)
            self.assertLessEqual(value, 100.0, field)

    def test_supports_flag_follows_side(self):
        self.assertTrue(evaluate_event([story()], symbol="ACME", side="buy", now=NOW)
                        .supports_existing_signal)
        self.assertFalse(evaluate_event([story()], symbol="ACME", side="sell", now=NOW)
                         .supports_existing_signal)

    def test_priced_in_uses_the_move_since_publication(self):
        inf = evaluate_event([story()], symbol="ACME", side="buy", now=NOW,
                             price_at_publication=100.0, price_now=103.0)
        self.assertAlmostEqual(inf.move_since_publication_pct, 3.0, places=3)
        self.assertGreater(inf.priced_in_percent, 0)

    def test_missing_prices_never_invent_one(self):
        inf = evaluate_event([story()], symbol="ACME", side="buy", now=NOW)
        self.assertIsNone(inf.price_at_publication)
        self.assertIsNone(inf.price_now if hasattr(inf, "price_now") else None)
        self.assertEqual(inf.priced_in_percent, 0.0)

    def test_freshness_penalty(self):
        fresh = evaluate_event([story(minutes_ago=5)], symbol="ACME", side="buy", now=NOW)
        stale = evaluate_event([story(minutes_ago=600)], symbol="ACME", side="buy", now=NOW)
        self.assertGreater(fresh.confidence, stale.confidence)

    def test_novelty_decays_with_age_and_repeats(self):
        new = evaluate_event([story(minutes_ago=5)], symbol="ACME", side="buy", now=NOW)
        old = evaluate_event([story(minutes_ago=1000)], symbol="ACME", side="buy",
                             now=NOW, prior_mentions=2)
        self.assertGreater(new.novelty, old.novelty)

    def test_confidence_scales_with_learned_source_weight(self):
        full = evaluate_event([story()], symbol="ACME", side="buy", now=NOW)
        cut = evaluate_event([story()], symbol="ACME", side="buy", now=NOW, source_weight=0.5)
        self.assertLess(cut.confidence, full.confidence)


# --------------------------------------------------------------------------- #
# the decision matrix from the brief
# --------------------------------------------------------------------------- #
class TestDecisionRules(unittest.TestCase):
    def test_exits_are_never_blocked(self):
        emea = influence(verification="CONTRADICTED", decision_effect="VETO")
        out = decide(emea, side="sell", entry=False)
        self.assertEqual(out.decision_effect, "CONFIRM")
        self.assertIn("risk-reducing", out.reason)

    def test_verified_opposing_news_vetoes_an_entry(self):
        out = decide(influence(direction="BEARISH", supports_existing_signal=False),
                     side="buy", entry=True)
        self.assertEqual(out.decision_effect, "VETO")

    def test_weak_opposing_news_halves_the_entry(self):
        out = decide(influence(direction="BEARISH", supports_existing_signal=False,
                               confidence=30.0, impact=70.0),
                     side="buy", entry=True)
        self.assertEqual(out.decision_effect, "REDUCE")

    def test_supporting_news_boosts_without_adding_size(self):
        out = decide(influence(direction="BULLISH", supports_existing_signal=True),
                     side="buy", entry=True)
        self.assertEqual(out.decision_effect, "BOOST")

    def test_supporting_but_stale_news_only_confirms(self):
        out = decide(influence(direction="BULLISH", supports_existing_signal=True, novelty=10),
                     side="buy", entry=True)
        self.assertEqual(out.decision_effect, "CONFIRM")

    def test_unverified_material_news_pauses_entries(self):
        out = decide(influence(verification="UNVERIFIED", confidence=20.0,
                               direction="BEARISH", supports_existing_signal=False),
                     side="buy", entry=True)
        self.assertEqual(out.decision_effect, "PAUSE")

    def test_contradicted_sources_veto(self):
        out = decide(influence(verification="CONTRADICTED"), side="buy", entry=True)
        self.assertEqual(out.decision_effect, "VETO")

    def test_priced_in_event_vetoes(self):
        out = decide(influence(priced_in_percent=95.0), side="buy", entry=True)
        self.assertEqual(out.decision_effect, "VETO")
        self.assertIn("priced in", out.reason)

    def test_extreme_move_is_not_chased(self):
        out = decide(influence(move_since_publication_pct=6.5), side="buy", entry=True)
        self.assertEqual(out.decision_effect, "PAUSE")
        self.assertIn("chase", out.reason)

    def test_imminent_announcement_pauses(self):
        out = decide(influence(), side="buy", entry=True, imminent_event=True)
        self.assertEqual(out.decision_effect, "PAUSE")

    def test_neutral_directionless_news_confirms(self):
        out = decide(influence(direction="NEUTRAL", supports_existing_signal=False,
                               impact=30.0), side="buy", entry=True)
        self.assertEqual(out.decision_effect, "CONFIRM")

    def test_neutral_material_news_never_reduces_a_trade(self):
        # A big Tier-2 macro story with no directional read is information, not
        # a contradiction: it must not halve or block the strategy's order.
        out = decide(influence(direction="NEUTRAL", supports_existing_signal=False,
                               impact=80.0, confidence=45.0), side="buy", entry=True)
        self.assertEqual(out.decision_effect, "CONFIRM")
        self.assertEqual(out.relation, "NEUTRAL")


class TestNoTradeReasons(unittest.TestCase):
    def test_unverified_material_lists_verification(self):
        reasons = no_trade_reasons(
            influence(verification="UNVERIFIED", impact=80.0), entry=True)
        self.assertIn("source cannot be verified", reasons)

    def test_contradiction_is_listed(self):
        reasons = no_trade_reasons(influence(verification="CONTRADICTED"), entry=True)
        self.assertIn("sources materially contradict one another", reasons)

    def test_stale_news_is_listed(self):
        reasons = no_trade_reasons(influence(novelty=10), entry=True)
        self.assertIn("news is stale or duplicated", reasons)

    def test_exits_produce_no_blocking_reasons(self):
        self.assertEqual(
            no_trade_reasons(influence(verification="CONTRADICTED"), entry=False), [])


# --------------------------------------------------------------------------- #
# exposure semantics
# --------------------------------------------------------------------------- #
class TestEntryDefinition(unittest.TestCase):
    def test_opening_from_flat_is_an_entry(self):
        self.assertTrue(is_entry("buy", 0.0, 10.0))
        self.assertTrue(is_entry("sell", 0.0, 10.0))

    def test_closing_or_trimming_is_not_an_entry(self):
        self.assertFalse(is_entry("sell", 100.0, 100.0))
        self.assertFalse(is_entry("sell", 100.0, 40.0))

    def test_adding_to_a_long_is_an_entry(self):
        self.assertTrue(is_entry("buy", 100.0, 10.0))

    def test_flipping_through_zero_is_an_entry(self):
        self.assertTrue(is_entry("sell", 100.0, 150.0))


class TestApplyEffect(unittest.TestCase):
    def test_boost_never_increases_size(self):
        qty, _ = apply_effect(10.0, influence(decision_effect="BOOST"))
        self.assertEqual(qty, 10.0)

    def test_reduce_halves(self):
        qty, why = apply_effect(10.0, influence(decision_effect="REDUCE"))
        self.assertEqual(qty, 5.0)
        self.assertIn("REDUCE", why)

    def test_veto_and_pause_zero_the_order(self):
        for effect in ("VETO", "PAUSE"):
            qty, _ = apply_effect(10.0, influence(decision_effect=effect))
            self.assertEqual(qty, 0.0)

    def test_no_news_is_a_no_op(self):
        self.assertEqual(apply_effect(7.0, None), (7.0, "no relevant news"))


# --------------------------------------------------------------------------- #
# the JSON contract
# --------------------------------------------------------------------------- #
class TestOutputContract(unittest.TestCase):
    REQUIRED = {
        "event_summary", "event_type", "affected_symbols", "verification",
        "direction", "confidence", "impact", "novelty", "priced_in_percent",
        "expected_duration", "primary_source_url", "confirming_source_urls",
        "published_at", "retrieved_at", "supports_existing_signal",
        "invalidation_condition", "decision_effect", "reason",
    }

    def test_payload_matches_the_required_schema(self):
        payload = influence().to_dict()
        self.assertIn("news_influence", payload)
        block = payload["news_influence"]
        self.assertEqual(set(block), self.REQUIRED)
        self.assertEqual(block["decision_effect"], "CONFIRM")
        json.dumps(payload)          # must be serialisable as-is

    def test_payload_is_ascii_safe(self):
        text = influence(event_summary="Résumé — naïve café").to_json()
        text.encode("ascii")
        self.assertIn("R\\u00e9sum\\u00e9", text)


# --------------------------------------------------------------------------- #
# store, dedupe and the screener
# --------------------------------------------------------------------------- #
class TestStore(TmpStoreCase):
    def test_first_sighting_is_recorded_once(self):
        s = story()
        self.store.record_story(s, "ACME")
        self.assertEqual(self.store.fingerprints(72)[s.fingerprint], 1)

    def test_decision_is_written_with_the_json_block(self):
        self.store.record_decision(symbol="ACME", side="buy", influence=influence(), entry=True)
        row = json.loads(self.store.decisions.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["symbol"], "ACME")
        self.assertIn("news_influence", row)
        self.assertTrue(news_intel.LOG_PATH.exists())


class TestScreener(TmpStoreCase):
    def build(self, stories, **cfg):
        return news_intel.Screener(
            {"enabled": True, "min_impact_to_pause": 40.0, **cfg},
            ["ACME"], stories=stories, store=self.store,
        )

    def test_relevant_story_vetoes_an_entry(self):
        screener = self.build([story("reuters", "Acme cuts guidance, shares plunge")])
        out = screener.for_leg("ACME", side="buy", current_qty=0.0, qty=10.0)
        self.assertIsNotNone(out)
        self.assertIn(out.decision_effect, {"VETO", "REDUCE"})

    def test_same_story_never_blocks_the_exit(self):
        screener = self.build([story("reuters", "Acme cuts guidance, shares plunge")])
        out = screener.for_leg("ACME", side="sell", current_qty=100.0, qty=100.0)
        self.assertEqual(out.decision_effect, "CONFIRM")

    def test_unrelated_story_is_silent(self):
        screener = self.build([story("reuters", "Other Co wins contract", symbols=("OTHER",))])
        self.assertIsNone(screener.for_leg("ACME", side="buy", current_qty=0.0, qty=1.0))

    def test_disabled_layer_is_silent(self):
        screener = news_intel.Screener({"enabled": False}, ["ACME"], stories=[story()],
                                       store=self.store)
        self.assertIsNone(screener.for_leg("ACME", side="buy", current_qty=0.0, qty=1.0))

    def test_blind_layer_without_required_does_not_block(self):
        screener = news_intel.Screener({"enabled": True, "required": False}, ["ACME"],
                                       stories=[], store=self.store)
        screener.blind = True
        self.assertIsNone(screener.for_leg("ACME", side="buy", current_qty=0.0, qty=1.0))

    def test_blind_layer_with_required_blocks_entries_only(self):
        screener = news_intel.Screener({"enabled": True, "required": True}, ["ACME"],
                                       stories=[], store=self.store)
        screener.blind = True
        entry = screener.for_leg("ACME", side="buy", current_qty=0.0, qty=1.0)
        self.assertEqual(entry.decision_effect, "VETO")
        self.assertIsNone(screener.for_leg("ACME", side="sell", current_qty=10.0, qty=10.0))

    def test_declared_event_blackout_pauses_entries(self):
        soon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)
        cfg = {"enabled": True, "min_impact_to_pause": 40.0,
               "upcoming_events": [{"at": soon.isoformat(), "symbols": ["ACME"]}]}
        screener = news_intel.Screener(cfg, ["ACME"], stories=[story()], store=self.store)
        out = screener.for_leg("ACME", side="buy", current_qty=0.0, qty=10.0)
        self.assertEqual(out.decision_effect, "PAUSE")


# --------------------------------------------------------------------------- #
# feeds (offline)
# --------------------------------------------------------------------------- #
class TestRssParsing(unittest.TestCase):
    XML = b"""<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>Fed holds rates steady</title><link>https://fed.test/1</link>
        <pubDate>Wed, 18 Nov 2026 14:00:00 +0000</pubDate>
        <description>&lt;p&gt;Decision announced.&lt;/p&gt;</description></item>
      <item><title>Old news</title><link>https://fed.test/2</link>
        <pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate></item>
    </channel></rss>"""

    def test_parses_and_filters_by_date(self):
        since = NOW - dt.timedelta(hours=48)
        rows = news_sources._parse_rss(self.XML, feed_name="federal reserve",
                                       tier=1, since=since)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].headline, "Fed holds rates steady")
        self.assertEqual(rows[0].tier, 1)
        self.assertNotIn("<p>", rows[0].summary)

    def test_missing_date_is_skipped(self):
        xml = b"""<rss><channel><item><title>No date</title>
                  <link>https://x.test</link></item></channel></rss>"""
        rows = news_sources._parse_rss(xml, feed_name="x", tier=2,
                                       since=NOW - dt.timedelta(days=1))
        self.assertEqual(rows, [])


# --------------------------------------------------------------------------- #
# review
# --------------------------------------------------------------------------- #
class TestReview(unittest.TestCase):
    def outcome(self, verdict, ret, source="reuters"):
        return news_review.Outcome(
            decided_at=NOW, symbol="ACME", verdict=verdict, horizon_hours=120,
            ret=ret, mfe=ret, mae=ret, priced_in=10.0, sources=(source,),
            event_type="earnings_surprise", confidence=80.0, duration="intraday",
        )

    def test_summary_counts_hits(self):
        summary = news_review.summarise([
            self.outcome("CORRECT", 0.02), self.outcome("CORRECT", 0.01),
            self.outcome("WRONG", -0.01),
        ])
        self.assertEqual(summary["observations"], 3)
        self.assertAlmostEqual(summary["by_source"]["reuters"]["hit_rate"], 0.667, places=3)

    def test_weights_drop_for_a_poor_record_and_never_exceed_one(self):
        poor = {"by_source": {"badblog": {"n": 20, "hit_rate": 0.1}}}
        good = {"by_source": {"reuters": {"n": 20, "hit_rate": 0.95}}}
        weights = news_review.refresh_weights(poor, {"badblog": 1.0})
        self.assertLess(weights["badblog"], 0.7)
        weights = news_review.refresh_weights(good, {"reuters": 1.0})
        self.assertLessEqual(weights["reuters"], 1.0)

    def test_small_samples_do_not_move_weights(self):
        weights = news_review.refresh_weights(
            {"by_source": {"x": {"n": 2, "hit_rate": 0.0}}}, {"x": 1.0})
        self.assertEqual(weights["x"], 1.0)

    def test_unclosed_decisions_are_not_scored(self):
        row = {"decided_at": (dt.datetime.now(dt.timezone.utc)
                              - dt.timedelta(minutes=30)).isoformat(),
               "symbol": "ACME",
               "news_influence": {"direction": "BULLISH"}}
        self.assertIsNone(news_review.score_decision(row))


if __name__ == "__main__":
    unittest.main(verbosity=2)
