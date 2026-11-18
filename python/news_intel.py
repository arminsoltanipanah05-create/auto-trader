"""News Intelligence Layer.

Purpose
-------
Read current public news, decide whether it supports, contradicts, delays or
cancels a signal the *existing* strategies already produced, and record the
decision.  It never invents a signal and never sizes a position: the strategies
keep their signals, the broker keeps its limits, and this module may only veto,
pause, reduce, confirm or boost a trade that was going to happen anyway.

Hard constraints (see AGENTS.md)
--------------------------------
* Every order still leaves through ``broker.GuardedBroker.submit``.  Nothing
  here touches the broker, the risk limits or the pre-trade gate.
* News may never *increase* exposure.  ``BOOST`` raises confidence in the log
  and changes nothing else; ``REDUCE`` can only shrink an entry.
* A risk-reducing order (a sell that trims or closes a long) is never blocked by
  news.  Being unable to leave a position because of a headline is the failure
  mode this layer is meant to prevent, not cause.
* Tier 3 (social, aggregators, blogs, analysts) may raise an alert but can never
  authorize a trade.  On its own it can only pause an entry.
* Missing facts stay missing: no score, URL, price or quotation is invented.  An
  absent field is recorded as absent, and that lowers the score.

Determinism
-----------
Every score is a pure function of (story, corroboration, price context, stored
history).  Same inputs, same output — which is what makes the log auditable.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import math
import pathlib
import re
from typing import Iterable, Sequence

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state" / "news"
EVENTS_PATH = STATE_DIR / "events.jsonl"
DECISIONS_PATH = STATE_DIR / "decisions.jsonl"
WEIGHTS_PATH = STATE_DIR / "source_weights.json"
LOG_PATH = ROOT / "logs" / "news.log"

# --------------------------------------------------------------------------- #
# sources and tiers
# --------------------------------------------------------------------------- #
# Tier 1: the organisation itself, or the state.  Can authorize on its own.
TIER1_SOURCES = {
    "sec", "sec edgar", "edgar", "federal reserve", "federal reserve board",
    "fed", "ecb", "european central bank", "bank of england", "boj",
    "bank of japan", "us treasury", "treasury", "bls", "bea", "eia", "cftc",
    "fda", "ema", "cms", "company ir", "investor relations",
    "business wire", "businesswire", "globe newswire", "globenewswire",
    "pr newswire", "prnewswire", "accesswire",
}
# Tier 2: established financial press.  Two independent outlets can authorize.
TIER2_SOURCES = {
    "reuters", "bloomberg", "financial times", "ft", "wall street journal",
    "wsj", "dow jones", "associated press", "ap", "cnbc", "marketwatch",
    "nikkei", "nikkei asia", "barron's", "barrons", "benzinga", "the economist",
    "handelsblatt", "les echos", "il sole 24 ore", "south china morning post",
    "scmp", "the globe and mail", "australian financial review", "afx",
}
# Everything else — social, aggregators, blogs, analyst notes — is Tier 3.

# Scheduled events with no reliable machine feed can be declared by the
# operator in config; until then the bot only reacts to published news.
DEFAULT_CONFIG = {
    "enabled": True,
    # required=False: an unreachable news feed must not stop a strategy that is
    # otherwise lawful. required=True turns a blind news layer into NO_TRADE.
    "required": False,
    "lookback_hours": 24,
    "max_items": 250,
    "min_impact_to_pause": 55.0,
    "min_confidence_to_veto": 60.0,
    "min_impact_to_veto": 60.0,
    "priced_in_veto": 80.0,
    "chase_move_pct": 3.0,
    "fresh_minutes": 90,
    "dedupe_hours": 72,
    "allow_standalone": False,
    "pre_event_blackout_minutes": 60,
    "upcoming_events": [],
    # Macro and central-bank news has no ticker, but it hits broad indices.
    # These are the instruments such a story is allowed to speak about.
    "macro_proxies": ["SPY", "QQQ"],
    "sec_user_agent": "",
    "rss_feeds": [
        {"name": "federal reserve", "url": "https://www.federalreserve.gov/feeds/press_all.xml", "tier": 1},
        {"name": "cnbc", "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html", "tier": 2},
        {"name": "marketwatch", "url": "http://feeds.marketwatch.com/marketwatch/topstories/", "tier": 2},
    ],
}

# --------------------------------------------------------------------------- #
# event taxonomy
# --------------------------------------------------------------------------- #
# Event types that have no ticker but reach a broad index.  A story of this
# kind may speak about a configured macro proxy (SPY, QQQ) and nothing else.
MACRO_EVENTS = frozenset({"macro_release", "central_bank_decision", "geopolitical_event"})

# A coin is usually named, not tickered, in the press.  Without these aliases a
# Bitcoin story is invisible to the crypto book.
CRYPTO_ALIASES = {
    "BTC": ("bitcoin",),
    "ETH": ("ethereum", "ether"),
    "SOL": ("solana",),
    "XRP": ("ripple",),
    "DOGE": ("dogecoin",),
    "LTC": ("litecoin",),
    "BCH": ("bitcoin cash",),
    "DOT": ("polkadot",),
    "LINK": ("chainlink",),
    "AAVE": ("aave",),
    "UNI": ("uniswap",),
    "AVAX": ("avalanche",),
}

EVENT_TYPES = (
    "earnings_surprise", "guidance_change", "merger_acquisition",
    "regulatory_decision", "government_investigation", "product_approval",
    "clinical_trial", "management_change", "capital_raise", "share_buyback",
    "dividend_change", "credit_rating", "major_contract", "lawsuit_decision",
    "supply_disruption", "cybersecurity_incident", "macro_release",
    "central_bank_decision", "geopolitical_event", "industry_development",
    "rumor",
)

# (event, keywords, base impact, typical duration, base urgency)
_EVENT_RULES: tuple[tuple[str, tuple[str, ...], float, str, float], ...] = (
    ("clinical_trial", ("phase 3", "phase iii", "phase 2", "trial result", "endpoint met", "efficacy", "placebo"), 80, "several days", 80),
    ("earnings_surprise", ("earnings", "quarterly results", "q1 ", "q2 ", "q3 ", "q4 ", "beat estimates", "missed estimates", "eps", "revenue beat", "revenue miss"), 70, "intraday", 75),
    ("guidance_change", ("guidance", "outlook", "forecast cut", "forecast raised", "raises forecast", "cuts forecast", "profit warning", "pre-announce"), 75, "several days", 75),
    ("merger_acquisition", ("merger", "acquisition", "acquire", "takeover", "buyout", "tender offer", "stake sale", "deal to buy"), 85, "several days", 85),
    ("regulatory_decision", ("regulator", "regulatory approval", "antitrust", "approval granted", "licence", "license granted", "ruling", "settlement with"), 70, "several days", 70),
    ("government_investigation", ("investigation", "probe", "subpoena", "raid", "enforcement action", "sec charges", "doj", "fraud probe"), 80, "several days", 80),
    ("product_approval", ("fda approval", "approved by", "clearance", "approval for", "product launch"), 65, "intraday", 70),
    ("management_change", ("ceo", "cfo", "resigns", "steps down", "appointed", "board departure", "interim chief"), 55, "intraday", 60),
    ("capital_raise", ("capital raise", "share sale", "secondary offering", "placement", "rights issue", "convertible", "dilution", "raise $"), 65, "intraday", 70),
    ("share_buyback", ("buyback", "share repurchase", "repurchase programme", "repurchase program"), 55, "several days", 55),
    ("dividend_change", ("dividend", "payout", "distribution cut", "special dividend"), 50, "intraday", 55),
    ("credit_rating", ("downgrade", "upgrade", "credit rating", "rating action", "outlook negative", "outlook positive", "junk"), 65, "several days", 65),
    ("major_contract", ("contract win", "wins contract", "awarded", "order worth", "supply agreement", "partnership"), 55, "several days", 55),
    ("lawsuit_decision", ("lawsuit", "court", "judge", "verdict", "class action", "injunction", "appeal"), 60, "several days", 60),
    ("supply_disruption", ("outage", "disruption", "shortage", "strike", "plant closure", "halt production", "force majeure"), 65, "several days", 75),
    ("cybersecurity_incident", ("cyberattack", "cyber attack", "data breach", "ransomware", "hacked", "security incident"), 70, "several days", 80),
    ("central_bank_decision", ("fomc", "rate decision", "rate cut", "rate hike", "interest rate", "ecb", "central bank", "monetary policy", "quantitative"), 80, "intraday", 80),
    ("macro_release", ("cpi", "inflation", "nonfarm", "payrolls", "gdp", "unemployment", "pmi", "retail sales", "jobless claims", "ppi"), 70, "intraday", 75),
    ("geopolitical_event", ("war", "sanctions", "invasion", "ceasefire", "tariff", "embargo", "election", "coup", "conflict"), 75, "several days", 80),
    ("industry_development", ("industry", "sector", "market-wide", "across the sector", "peers"), 45, "several days", 45),
)

# Words that move the magnitude and the polarity. Positive => bullish.
_BULL_WORDS = {
    "beats": 18, "beat": 14, "raised": 16, "raises": 16, "upgrade": 16,
    "record": 14, "surge": 18, "soar": 20, "soars": 20, "jump": 12, "jumps": 12,
    "rally": 14, "rallies": 14, "climbs": 10, "gains": 8, "rises": 10, "rise": 10,
    "approval": 16, "approved": 16, "wins": 12, "won": 10, "expands": 10,
    "strong": 10, "outperform": 14, "buyback": 12, "dividend increase": 12,
    "breakthrough": 16, "profit": 8, "growth": 8, "partnership": 8,
    "settlement": 6, "cuts costs": 8,
}
_BEAR_WORDS = {
    "misses": 18, "miss": 14, "cut": 16, "cuts": 16, "lowers": 16, "lowered": 16,
    "downgrade": 16, "plunge": 20, "plummets": 20, "plummet": 20, "slump": 14,
    "slumps": 14, "falls": 10, "sinks": 14, "sink": 12, "tumbles": 16,
    "slides": 10, "drops": 10, "drop": 10, "warning": 16, "probe": 14,
    "investigation": 16, "fraud": 22, "lawsuit": 12, "recall": 14, "halt": 14,
    "halted": 14, "bankruptcy": 24, "default": 18, "resigns": 10, "layoffs": 12,
    "weak": 10, "loss": 10, "losses": 10, "dilution": 12, "tariff": 12,
    "sanctions": 14, "outage": 12, "breach": 16,
}
_STRONG_MODIFIERS = ("sharply", "significantly", "materially", "dramatically",
                     "massive", "unexpectedly", "surprise", "historic")

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'\-\.]*", re.IGNORECASE)

# A macro story only speaks about an index if it reports the release or the
# decision itself.  "Mortgage demand falls as interest rates rise" is a central
# banking headline by keyword and a false positive in substance.
_MACRO_HINT = re.compile(
    r"\b(fomc|rate decision|raises rates|cuts rates|holds rates|rate cut|rate hike"
    r"|cpi|consumer price|ppi|producer price|nonfarm|payrolls|jobless claims|gdp|pmi"
    r"|retail sales|unemployment rate|tariffs?|sanctions?|invasion|ceasefire|opec"
    r"|emergency meeting|debt ceiling|government shutdown)\b",
    re.IGNORECASE,
)


def _has(keyword: str, text: str) -> bool:
    """Word-aware keyword test.

    Plain substring matching is how "software" becomes a geopolitical event and
    "execute" becomes an earnings cut.  A single word must stand alone; a phrase
    is matched as a phrase.
    """
    if " " in keyword:
        return keyword in text
    pattern = rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])"
    return re.search(pattern, text) is not None


def _norm(text: str) -> str:
    return " ".join(str(text or "").lower().replace("\u2019", "'").split())


def _tokens(text: str) -> set[str]:
    return {m.group(0).strip(".-'").lower() for m in _TOKEN_RE.finditer(str(text or ""))}


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return float(max(low, min(high, value)))


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class Story:
    """One item as retrieved.  ``published_at`` is what the source claims."""

    source: str
    headline: str
    url: str
    published_at: dt.datetime
    retrieved_at: dt.datetime
    summary: str = ""
    symbols: tuple[str, ...] = ()
    tier: int = 3
    external_id: str = ""

    @property
    def fingerprint(self) -> str:
        """Identity for dedupe: normalised headline, independent of source."""
        key = re.sub(r"[^a-z0-9 ]", "", _norm(self.headline))
        return hashlib.sha1(key.encode()).hexdigest()[:16]

    @property
    def age_minutes(self) -> float:
        return max(0.0, (self.retrieved_at - self.published_at).total_seconds() / 60.0)


@dataclasses.dataclass
class Influence:
    """The ``news_influence`` block required before a news-influenced trade."""

    event_summary: str = ""
    event_type: str = ""
    affected_symbols: list[str] = dataclasses.field(default_factory=list)
    verification: str = "UNVERIFIED"
    direction: str = "NEUTRAL"
    confidence: float = 0.0
    impact: float = 0.0
    novelty: float = 0.0
    priced_in_percent: float = 0.0
    expected_duration: str = ""
    primary_source_url: str = ""
    confirming_source_urls: list[str] = dataclasses.field(default_factory=list)
    published_at: str = ""
    retrieved_at: str = ""
    supports_existing_signal: bool = False
    invalidation_condition: str = ""
    decision_effect: str = "CONFIRM"
    reason: str = ""

    # extra, not part of the required block, but needed for the audit trail
    urgency: float = 0.0
    # SUPPORTS / OPPOSES / NEUTRAL — the boolean above cannot tell "no view"
    # from "against", and that distinction decides whether a trade is touched.
    relation: str = "NEUTRAL"
    sources: list[str] = dataclasses.field(default_factory=list)
    price_at_publication: float | None = None
    price_at_decision: float | None = None
    move_since_publication_pct: float | None = None

    def to_dict(self) -> dict:
        return {
            "news_influence": {
                "event_summary": self.event_summary,
                "event_type": self.event_type,
                "affected_symbols": list(self.affected_symbols),
                "verification": self.verification,
                "direction": self.direction,
                "confidence": round(self.confidence, 1),
                "impact": round(self.impact, 1),
                "novelty": round(self.novelty, 1),
                "priced_in_percent": round(self.priced_in_percent, 1),
                "expected_duration": self.expected_duration,
                "primary_source_url": self.primary_source_url,
                "confirming_source_urls": list(self.confirming_source_urls),
                "published_at": self.published_at,
                "retrieved_at": self.retrieved_at,
                "supports_existing_signal": bool(self.supports_existing_signal),
                "invalidation_condition": self.invalidation_condition,
                "decision_effect": self.decision_effect,
                "reason": self.reason,
            },
            "news_audit": {
                "urgency": round(self.urgency, 1),
                "relation": self.relation,
                "sources": list(self.sources),
                "price_at_publication": self.price_at_publication,
                "price_at_decision": self.price_at_decision,
                "move_since_publication_pct": self.move_since_publication_pct,
            },
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=True, separators=(",", ":"))


# --------------------------------------------------------------------------- #
# classification and scoring (pure)
# --------------------------------------------------------------------------- #
def source_tier(source: str) -> int:
    name = _norm(source)
    if name in TIER1_SOURCES:
        return 1
    if name in TIER2_SOURCES:
        return 2
    return 3


def classify(headline: str, summary: str = "") -> tuple[str, str, float]:
    """Return (event_type, direction, magnitude 0-100).

    Deterministic keyword scoring.  It is deliberately simple and auditable: a
    model that cannot be reproduced cannot be reviewed.
    """
    text = _norm(f"{headline} {summary}")
    best = ("industry_development", 45.0, "")
    for event, keywords, base_impact, _duration, _urgency in _EVENT_RULES:
        hits = [k for k in keywords if _has(k, text)]
        if len(hits) > len([k for k in best[2].split("|") if k]):
            best = (event, float(base_impact), "|".join(hits))

    bull = sum(w for k, w in _BULL_WORDS.items() if _has(k, text))
    bear = sum(w for k, w in _BEAR_WORDS.items() if _has(k, text))
    strong = 1.25 if any(mod in text for mod in _STRONG_MODIFIERS) else 1.0

    net = (bull - bear) * strong
    if net >= 20:
        direction = "STRONGLY_BULLISH"
    elif net > 4:
        direction = "BULLISH"
    elif net <= -20:
        direction = "STRONGLY_BEARISH"
    elif net < -4:
        direction = "BEARISH"
    else:
        direction = "NEUTRAL"

    magnitude = _clamp(best[1] + abs(net) * 0.5, 0, 100)
    if "rumor" in text or "reportedly" in text or "sources say" in text or "unconfirmed" in text:
        best = ("rumor", best[1], best[2] + "|rumor")
    return best[0], direction, magnitude


def is_material(event_type: str, direction: str, impact: float) -> bool:
    if direction == "NEUTRAL":
        return False
    return impact >= 40.0


_OPPOSITE = {
    "STRONGLY_BULLISH": "STRONGLY_BEARISH",
    "BULLISH": "BEARISH",
    "BEARISH": "BULLISH",
    "STRONGLY_BEARISH": "STRONGLY_BULLISH",
}


def direction_supports(direction: str, side: str) -> bool | None:
    """Does the news point the same way as the order? None when directionless."""
    if direction == "NEUTRAL":
        return None
    bullish = direction in ("BULLISH", "STRONGLY_BULLISH")
    return bullish if side == "buy" else not bullish


def verification_of(stories: Sequence[Story]) -> str:
    """Tier rules from the brief, applied to every story about one event."""
    tiers = [s.tier for s in stories]
    if not tiers:
        return "UNVERIFIED"

    directions = {classify(s.headline, s.summary)[1] for s in stories}
    real = {d for d in directions if d != "NEUTRAL"}
    if len(real) >= 2 and any(_OPPOSITE.get(d) in real for d in real):
        return "CONTRADICTED"

    if 1 in tiers:
        return "VERIFIED"
    tier2 = {_norm(s.source) for s in stories if s.tier == 2}
    if len(tier2) >= 2:
        return "VERIFIED"          # two independent reputable outlets
    if len(tier2) == 1:
        return "PARTIALLY_VERIFIED"
    return "UNVERIFIED"


def _confidence(verification: str, stories: Sequence[Story], weight: float = 1.0) -> float:
    base = {
        "VERIFIED": 70.0,
        "PARTIALLY_VERIFIED": 45.0,
        "UNVERIFIED": 20.0,
        "CONTRADICTED": 0.0,
    }[verification]
    tier1 = sum(1 for s in stories if s.tier == 1)
    tier2 = len({_norm(s.source) for s in stories if s.tier == 2})
    bonus = min(15.0, 5.0 * tier1) + min(10.0, 4.0 * max(0, tier2 - 1))
    freshness_penalty = 0.0
    if stories:
        freshest = min(s.age_minutes for s in stories)
        if freshest > 240:
            freshness_penalty = 10.0
    return _clamp((base + bonus - freshness_penalty) * weight)


def _novelty(age_minutes: float, prior_mentions: int) -> float:
    """Fresh information scores high; a story already seen and re-reported does not."""
    if age_minutes <= 30:
        base = 100.0
    elif age_minutes <= 180:
        base = 80.0
    elif age_minutes <= 720:
        base = 60.0
    elif age_minutes <= 1440:
        base = 40.0
    else:
        base = 20.0
    return _clamp(base - 20.0 * max(0, prior_mentions))


def _priced_in(move_pct: float | None, expected_move_pct: float) -> float:
    """How much of the expected move the market has already delivered."""
    if move_pct is None or expected_move_pct <= 0:
        return 0.0
    return _clamp(abs(move_pct) / expected_move_pct * 100.0)


_EXPECTED_MOVE_BY_IMPACT = {80: 5.0, 70: 3.5, 60: 2.5, 50: 1.5, 40: 1.0}


def expected_move_pct(impact: float) -> float:
    for floor in sorted(_EXPECTED_MOVE_BY_IMPACT, reverse=True):
        if impact >= floor:
            return _EXPECTED_MOVE_BY_IMPACT[floor]
    return 0.7


_DURATION = {rule[0]: rule[3] for rule in _EVENT_RULES}
_URGENCY = {rule[0]: rule[4] for rule in _EVENT_RULES}


def evaluate_event(
    stories: Sequence[Story],
    *,
    symbol: str,
    side: str,
    price_at_publication: float | None = None,
    price_now: float | None = None,
    prior_mentions: int = 0,
    source_weight: float = 1.0,
    now: dt.datetime | None = None,
) -> Influence:
    """Score one event (all stories about it) against one proposed order."""
    now = now or dt.datetime.now(dt.timezone.utc)
    stories = [s for s in stories if s.headline]
    if not stories:
        raise ValueError("evaluate_event needs at least one story")

    stories = sorted(stories, key=lambda s: (s.tier, s.published_at))
    lead = stories[0]
    event_type, direction, impact = classify(lead.headline, lead.summary)
    verification = verification_of(stories)
    confidence = _confidence(verification, stories, source_weight)
    novelty = _novelty(lead.age_minutes, prior_mentions)
    urgency = _clamp(_URGENCY.get(event_type, 45.0) + (15.0 if lead.age_minutes < 60 else 0.0))
    duration = _DURATION.get(event_type, "intraday")

    move = None
    if price_at_publication and price_now and price_at_publication > 0:
        move = (price_now / price_at_publication - 1.0) * 100.0

    supports = direction_supports(direction, side)
    primary = next((s.url for s in stories if s.tier == 1), "")
    confirming = [s.url for s in stories if s.tier in (1, 2) and s.url][:5]

    influence = Influence(
        event_summary=(lead.summary or lead.headline)[:400],
        event_type=event_type,
        affected_symbols=[symbol],
        verification=verification,
        direction=direction,
        confidence=confidence,
        impact=impact,
        novelty=novelty,
        priced_in_percent=_priced_in(move, expected_move_pct(impact)),
        expected_duration=duration,
        primary_source_url=primary,
        confirming_source_urls=confirming,
        published_at=lead.published_at.isoformat(),
        retrieved_at=max(s.retrieved_at for s in stories).isoformat(),
        supports_existing_signal=bool(supports),
        relation=("NEUTRAL" if supports is None else ("SUPPORTS" if supports else "OPPOSES")),
        invalidation_condition=_invalidation(event_type, direction, symbol),
        urgency=urgency,
        sources=sorted({s.source for s in stories}),
        price_at_publication=price_at_publication,
        price_at_decision=price_now,
        move_since_publication_pct=None if move is None else round(move, 3),
    )
    return influence


def _invalidation(event_type: str, direction: str, symbol: str) -> str:
    """What would prove the news read wrong.  Never a price we did not observe."""
    if event_type in ("macro_release", "central_bank_decision"):
        return (f"a subsequent official release contradicting the {direction.lower()} reading "
                f"for {symbol}, or the move fully retracing before entry")
    if event_type == "rumor":
        return f"no primary-source confirmation for {symbol} within the lookback window"
    return (f"an official statement or filing contradicting the {direction.lower()} "
            f"reading of the event for {symbol}")


# --------------------------------------------------------------------------- #
# decision
# --------------------------------------------------------------------------- #
def is_entry(side: str, current_qty: float, qty: float) -> bool:
    """True when the order increases exposure (mirrors the gate's definition)."""
    current = float(current_qty or 0.0)
    signed = float(qty) if side == "buy" else -float(qty)
    new = current + signed
    if new == 0.0:
        return False
    if current == 0.0:
        return True
    if (new > 0) == (current > 0):
        return abs(new) > abs(current)
    return True


def decide(
    influence: Influence,
    *,
    side: str,
    entry: bool,
    cfg: dict | None = None,
    imminent_event: bool = False,
) -> Influence:
    """Apply the integration rules.  Returns the same object, annotated."""
    cfg = {**DEFAULT_CONFIG, **(cfg or {})}

    # 0. Never block an exit.  News may stop new risk, not the removal of risk.
    if not entry:
        influence.decision_effect = "CONFIRM"
        influence.reason = ("risk-reducing order: news may not block a trim or exit, "
                            f"so the {influence.direction.lower()} read is recorded only")
        return influence

    if influence.verification == "CONTRADICTED":
        influence.decision_effect = "VETO"
        influence.reason = "sources materially contradict one another"
        return influence

    if influence.priced_in_percent >= float(cfg["priced_in_veto"]):
        influence.decision_effect = "VETO"
        influence.reason = (f"already priced in: {influence.priced_in_percent:.0f}% of the "
                            f"expected move has happened since publication")
        return influence

    move = influence.move_since_publication_pct or 0.0
    if abs(move) >= float(cfg["chase_move_pct"]):
        influence.decision_effect = "PAUSE"
        influence.reason = (f"price already moved {move:+.2f}% since publication; "
                            "do not chase, wait for stabilization or a pullback")
        return influence

    if imminent_event:
        influence.decision_effect = "PAUSE"
        influence.reason = "a major scheduled announcement is imminent; no new risk before it"
        return influence

    material = influence.impact >= float(cfg["min_impact_to_pause"])

    if influence.verification == "UNVERIFIED" and material:
        influence.decision_effect = "PAUSE"
        influence.reason = ("material but unverified (Tier 3 only): alerts, cannot authorize; "
                            "entries paused until a primary or two Tier-2 sources confirm")
        return influence

    supports = influence.relation
    strong = (influence.confidence >= float(cfg["min_confidence_to_veto"])
              and influence.impact >= float(cfg["min_impact_to_veto"]))

    if supports == "OPPOSES" and material:
        influence.decision_effect = "VETO" if strong else "REDUCE"
        influence.reason = (f"verified news ({influence.verification}, confidence "
                            f"{influence.confidence:.0f}) points {influence.direction.lower()} "
                            f"against a {side} entry; "
                            + ("trade rejected" if strong else "size halved"))
        return influence

    if supports == "SUPPORTS" and material:
        if influence.novelty <= 25:
            influence.decision_effect = "CONFIRM"
            influence.reason = ("news agrees but is no longer novel (already reported and "
                                "priced); confidence unchanged")
            return influence
        influence.decision_effect = "BOOST"
        influence.reason = (f"news agrees with the existing signal ({influence.direction.lower()}, "
                            f"confidence {influence.confidence:.0f}); confidence raised, "
                            "size and limits unchanged")
        return influence

    influence.decision_effect = "CONFIRM"
    influence.reason = "no material news opposes the existing signal"
    return influence


def no_trade_reasons(influence: Influence, *, entry: bool, cfg: dict | None = None) -> list[str]:
    """The explicit NO_TRADE checklist from the brief, as machine text."""
    cfg = {**DEFAULT_CONFIG, **(cfg or {})}
    reasons: list[str] = []
    if not entry:
        return reasons
    if influence.verification == "UNVERIFIED" and influence.impact >= cfg["min_impact_to_pause"]:
        reasons.append("source cannot be verified")
    if influence.verification == "CONTRADICTED":
        reasons.append("sources materially contradict one another")
    if influence.novelty <= 25:
        reasons.append("news is stale or duplicated")
    if not influence.affected_symbols:
        reasons.append("affected ticker is uncertain")
    if influence.priced_in_percent >= cfg["priced_in_veto"]:
        reasons.append("event is already fully priced in")
    if not influence.published_at:
        reasons.append("reliable publication timestamp unavailable")
    if influence.event_type == "rumor":
        reasons.append("rationale depends on an unverified report")
    return reasons


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #
def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _append_jsonl(path: pathlib.Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True, default=str) + "\n")


def _rotate(path: pathlib.Path, max_bytes: int = 2_000_000, keep_lines: int = 2000) -> None:
    """Keep the append-only log bounded so a cron machine never fills its disk."""
    try:
        if path.exists() and path.stat().st_size > max_bytes:
            lines = path.read_text(encoding="utf-8").splitlines()[-keep_lines:]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def log_line(text: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} | {text}\n")


class Store:
    """Append-only event and decision history, plus per-source weights."""

    def __init__(self, directory: pathlib.Path | None = None) -> None:
        self.dir = pathlib.Path(directory or STATE_DIR)
        self.events = self.dir / "events.jsonl"
        self.decisions = self.dir / "decisions.jsonl"
        self.weights_path = self.dir / "source_weights.json"

    # -- dedupe / novelty ------------------------------------------------- #
    def fingerprints(self, hours: int = 72) -> dict[str, int]:
        """fingerprint -> times seen, inside the dedupe window."""
        cutoff = _utcnow() - dt.timedelta(hours=hours)
        counts: dict[str, int] = {}
        if not self.events.exists():
            return counts
        for line in self.events.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                seen = dt.datetime.fromisoformat(row["retrieved_at"])
            except (ValueError, KeyError, TypeError):
                continue
            if seen >= cutoff:
                fp = row.get("fingerprint", "")
                counts[fp] = counts.get(fp, 0) + 1
        return counts

    def record_story(self, story: Story, symbol: str) -> None:
        _append_jsonl(self.events, {
            "fingerprint": story.fingerprint,
            "symbol": symbol,
            "source": story.source,
            "tier": story.tier,
            "headline": story.headline,
            "url": story.url,
            "published_at": story.published_at.isoformat(),
            "retrieved_at": story.retrieved_at.isoformat(),
        })

    def record_decision(self, *, symbol: str, side: str, influence: Influence,
                        entry: bool, signal_ref: str = "") -> None:
        payload = {
            "decided_at": _utcnow().isoformat(),
            "symbol": symbol,
            "side": side,
            "entry": entry,
            "signal_ref": signal_ref,
            **influence.to_dict(),
        }
        _append_jsonl(self.decisions, payload)
        log_line(f"{symbol} | {side.upper()} | {influence.decision_effect} | "
                 f"{influence.event_type} | {influence.verification} | "
                 f"conf={influence.confidence:.0f} imp={influence.impact:.0f} "
                 f"priced_in={influence.priced_in_percent:.0f} | {influence.reason}")

    # -- learned source weights ------------------------------------------- #
    def load_weights(self) -> dict[str, float]:
        if not self.weights_path.exists():
            return {}
        try:
            return json.loads(self.weights_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}

    def save_weights(self, weights: dict[str, float]) -> None:
        self.weights_path.parent.mkdir(parents=True, exist_ok=True)
        self.weights_path.write_text(json.dumps(weights, indent=2, sort_keys=True),
                                     encoding="utf-8")


# --------------------------------------------------------------------------- #
# screening entry point used by the bots
# --------------------------------------------------------------------------- #
def relevant_stories(symbol: str, stories: Iterable[Story],
                     cfg: dict | None = None) -> list[Story]:
    """Stories that may speak about ``symbol``, in one place.

    Two ways in: the publisher tagged the instrument, or the headline names it
    (ticker or coin name).  A macro release names nothing, so it may only speak
    about a declared proxy index, and only when it reports the release itself.
    """
    cfg = {**DEFAULT_CONFIG, **(cfg or {})}
    base = _base(symbol)
    proxies = {str(p).upper() for p in (cfg.get("macro_proxies") or [])}
    is_proxy = base in proxies
    floor = float(cfg.get("min_impact_to_pause", 55.0))

    found: list[Story] = []
    for story in stories:
        tagged = {_base(s) for s in story.symbols}
        if base in tagged or _symbol_in_text(symbol, story.headline):
            found.append(story)
            continue
        if not is_proxy:
            continue
        if not _MACRO_HINT.search(str(story.headline or "")):
            continue
        event_type, _direction, impact = classify(story.headline, story.summary)
        if event_type in MACRO_EVENTS and impact >= floor:
            found.append(story)
    return found


def screen_symbol(
    symbol: str,
    *,
    side: str,
    current_qty: float,
    qty: float,
    cfg: dict | None = None,
    stories: Sequence[Story] | None = None,
    price_at_publication: float | None = None,
    price_now: float | None = None,
    store: Store | None = None,
    imminent_event: bool = False,
    signal_ref: str = "",
) -> Influence | None:
    """Evaluate the news for one leg.  ``None`` when no relevant story exists."""
    cfg = {**DEFAULT_CONFIG, **(cfg or {})}
    if not cfg.get("enabled", True):
        return None

    store = store or Store()
    relevant = relevant_stories(symbol, stories or [], cfg)
    if not relevant:
        return None

    entry = is_entry(side, current_qty, qty)
    fingerprints = store.fingerprints(int(cfg["dedupe_hours"]))
    prior = fingerprints.get(relevant[0].fingerprint, 0)

    weights = store.load_weights()
    weight = 1.0
    for source in {_norm(s.source) for s in relevant}:
        weight = min(weight, float(weights.get(source, 1.0)))
    weight = max(0.25, min(1.0, weight))

    influence = evaluate_event(
        relevant, symbol=symbol, side=side,
        price_at_publication=price_at_publication,
        price_now=price_now,
        prior_mentions=prior,
        source_weight=weight,
    )
    decide(influence, side=side, entry=entry, cfg=cfg, imminent_event=imminent_event)

    # An unverified story that cannot change a decision is noise, not an event:
    # keep it in the event log, out of the decision log.
    if (influence.verification == "UNVERIFIED"
            and influence.decision_effect == "CONFIRM"
            and influence.impact < float(cfg["min_impact_to_pause"])):
        influence.decision_effect = "IGNORE"
        influence.reason = "Tier 3, below the materiality floor: advisory only"

    if prior == 0:                      # record the first sighting, not every rescan
        for story in relevant:
            store.record_story(story, symbol)
    store.record_decision(symbol=symbol, side=side, influence=influence,
                          entry=entry, signal_ref=signal_ref)
    return influence


def _symbol_in_text(symbol: str, text: str) -> bool:
    """Ticker or coin-name mention, as a standalone token.

    ``\\b`` is not enough for a ticker: ``BTCUSD`` must not read as ``BTC``, and
    ``eth`` must not match inside another word. Lookarounds do that correctly.
    """
    base = _base(symbol)
    if len(base) < 2:
        return False
    names = (base, *CRYPTO_ALIASES.get(base, ()))
    pattern = (r"(?<![A-Za-z0-9])(" + "|".join(re.escape(n) for n in names)
               + r")(?![A-Za-z0-9])")
    return re.search(pattern, str(text or ""), flags=re.IGNORECASE) is not None


def apply_effect(qty: float, influence: Influence | None) -> tuple[float, str]:
    """Translate a decision into an order quantity.  Never increases exposure."""
    if influence is None:
        return qty, "no relevant news"
    effect = influence.decision_effect
    if effect in ("VETO", "PAUSE"):
        return 0.0, f"news {effect}: {influence.reason}"
    if effect == "REDUCE":
        return qty * 0.5, f"news REDUCE: {influence.reason}"
    if effect == "IGNORE":
        return qty, "news below materiality"
    return qty, f"news {effect}"


def _imminent(cfg: dict, symbol: str, now: dt.datetime) -> bool:
    """A scheduled announcement declared by the operator is close."""
    window = float(cfg.get("pre_event_blackout_minutes", 60))
    for entry in cfg.get("upcoming_events") or []:
        try:
            at = dt.datetime.fromisoformat(str(entry["at"]).replace("Z", "+00:00"))
            if at.tzinfo is None:
                at = at.replace(tzinfo=dt.timezone.utc)
            symbols = [s.upper() for s in (entry.get("symbols") or [])]
        except (KeyError, TypeError, ValueError):
            continue
        if symbols and symbol.upper() not in symbols and (_base(symbol) not in symbols):
            continue
        if 0 <= (at - now).total_seconds() <= window * 60:
            return True
    return False


def _base(symbol: str) -> str:
    return str(symbol).split("/")[0].upper()


class Screener:
    """Fetch the news once per run, then judge each leg in memory.

    One network pass per run keeps the free feeds inside their rate limits and
    keeps every leg of a rebalance judged against the same snapshot of the news.
    """

    def __init__(self, cfg: dict | None = None, symbols: Iterable[str] = (), *,
                 hours: int | None = None, store: Store | None = None,
                 stories: Sequence[Story] | None = None) -> None:
        self.cfg = {**DEFAULT_CONFIG, **(cfg or {})}
        self.store = store or Store()
        self.symbols = [str(s) for s in symbols]
        self.hours = int(hours or self.cfg["lookback_hours"])
        self.bundle = None
        self.blind = False
        self.error = ""
        self._prices: dict[str, tuple[float | None, float | None]] = {}

        if not self.cfg.get("enabled", True):
            return
        if stories is not None:
            self.bundle = type("Bundle", (), {"stories": list(stories), "reached": ["injected"],
                                              "errors": {}})()
            return
        try:
            import news_sources  # noqa: PLC0415
            self.bundle = news_sources.fetch_all(
                self.cfg, self.symbols, hours=self.hours,
                limit=int(self.cfg["max_items"]),
            )
            if not self.bundle.reached:
                self.blind = True
                self.error = "no news source reachable"
        except Exception as exc:  # noqa: BLE001 - a blind layer is data, not a crash
            self.blind = True
            self.error = f"{type(exc).__name__}: {exc}"

    # -- helpers ---------------------------------------------------------- #
    def _relevant(self, symbol: str) -> list[Story]:
        if self.bundle is None:
            return []
        return relevant_stories(symbol, self.bundle.stories, self.cfg)

    def _price(self, symbol: str, since: dt.datetime) -> tuple[float | None, float | None]:
        if symbol not in self._prices:
            try:
                import news_sources  # noqa: PLC0415
                self._prices[symbol] = news_sources.price_context(symbol, since=since)
            except Exception:  # noqa: BLE001
                self._prices[symbol] = (None, None)
        return self._prices[symbol]

    # -- public ----------------------------------------------------------- #
    def for_leg(self, symbol: str, *, side: str, current_qty: float, qty: float,
                signal_ref: str = "") -> Influence | None:
        """News judgement for one order, or None when the layer is silent."""
        if not self.cfg.get("enabled", True):
            return None

        now = dt.datetime.now(dt.timezone.utc)
        entry = is_entry(side, current_qty, qty)

        if self.blind or self.bundle is None:
            log_line(f"{symbol} | {side.upper()} | NEWS_UNAVAILABLE | {self.error} | "
                     f"required={self.cfg['required']} entry={entry}")
            if self.cfg.get("required") and entry:
                blind = Influence(
                    event_summary="news layer unavailable", event_type="",
                    affected_symbols=[symbol], verification="UNVERIFIED",
                    direction="NEUTRAL", published_at="", retrieved_at=now.isoformat(),
                    decision_effect="VETO",
                    reason=f"news required but unavailable ({self.error}); no new risk taken",
                )
                self.store.record_decision(symbol=symbol, side=side, influence=blind,
                                           entry=entry, signal_ref=signal_ref)
                return blind
            return None

        relevant = self._relevant(symbol)
        if not relevant:
            return None

        lead = max(relevant, key=lambda s: (s.published_at, -s.tier))
        at_publication, price_now = self._price(symbol, lead.published_at)

        influence = screen_symbol(
            symbol, side=side, current_qty=current_qty, qty=qty,
            cfg=self.cfg, stories=relevant, store=self.store,
            price_at_publication=at_publication, price_now=price_now,
            imminent_event=_imminent(self.cfg, symbol, now),
            signal_ref=signal_ref,
        )
        return influence


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _load_config() -> dict:
    try:
        import tomllib
        raw = tomllib.loads((ROOT / "python" / "config.toml").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        try:
            raw = __import__("cfg").load()
        except Exception:  # noqa: BLE001
            raw = {}
    news_cfg = dict(raw.get("news") or {})
    return {**DEFAULT_CONFIG, **news_cfg}


def _cli() -> None:  # pragma: no cover - manual
    import argparse

    ap = argparse.ArgumentParser(description="News intelligence layer")
    ap.add_argument("command", choices=["scan", "history", "check"])
    ap.add_argument("--symbols", default="SPY,QQQ,BTC/USD,ETH/USD")
    ap.add_argument("--side", default="buy", choices=["buy", "sell"])
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    cfg = _load_config()
    store = Store()

    if args.command == "history":
        rows = []
        if store.decisions.exists():
            for line in store.decisions.read_text(encoding="utf-8").splitlines():
                try:
                    rows.append(json.loads(line)["news_influence"])
                except (ValueError, KeyError):
                    continue
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["decision_effect"]] = counts.get(row["decision_effect"], 0) + 1
        print(f"{len(rows)} decisions recorded")
        for effect, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {effect:<24} {n}")
        return

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    import news_sources  # local import: keeps the layer importable offline

    bundle = news_sources.fetch_all(cfg, symbols, hours=args.hours, limit=args.limit)
    if bundle.errors:
        for name, err in bundle.errors.items():
            print(f"  source {name}: {err}")
    print(f"{len(bundle.stories)} stories from {len(bundle.reached)} source(s): "
          f"{', '.join(bundle.reached) or 'none'}")

    if args.command == "check":
        for story in bundle.stories[: args.limit]:
            event_type, direction, impact = classify(story.headline, story.summary)
            if story.tier <= 2 or impact >= 55:
                print(f"  [T{story.tier}] {story.source:<18} {direction:<17} {event_type:<22} "
                      f"{story.headline[:70]}")
        return

    for symbol in symbols:
        influence = screen_symbol(
            symbol, side=args.side, current_qty=0.0, qty=1.0,
            cfg={**cfg, "min_impact_to_pause": 0.0}, stories=bundle.stories, store=store,
        )
        if influence is None:
            print(f"\n{symbol}: no relevant news")
            continue
        if args.json:
            print(influence.to_json())
        else:
            print(f"\n{symbol}: {influence.decision_effect}  "
                  f"({influence.verification}, {influence.direction}, "
                  f"conf {influence.confidence:.0f}, impact {influence.impact:.0f})")
            print(f"  {influence.event_summary[:150]}")
            print(f"  why: {influence.reason}")


if __name__ == "__main__":  # pragma: no cover
    _cli()
