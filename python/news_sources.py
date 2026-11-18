"""Feed adapters for the news layer.

Every adapter returns :class:`news_intel.Story` objects and never raises: a dead
feed is reported in ``Bundle.errors`` and the run continues.  Which source can
*authorize* a trade is decided by tier in ``news_intel``, not here — an adapter
only reports where a story came from and when it says it was published.

Sources, and why these:

* **Alpaca News** — already authenticated with the keys the bots use, so the
  scheduled runners need no extra secret.  Aggregates many publishers, so the
  tier comes from the publisher name (fiscal.ai and similar aggregators are
  Tier 3 by construction).
* **SEC EDGAR** — free, official, and the cheapest way to turn a rumour into a
  verified filing.  Tier 1.
* **RSS** — central-bank and financial-press releases.  Tier is configured per
  feed because the Federal Reserve's own feed is Tier 1 and a newspaper is not.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import pathlib
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

import news_intel
from news_intel import Story

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE = news_intel.STATE_DIR
CIK_CACHE = STATE / "cik_map.json"
CIK_TTL_HOURS = 168
HTTP_TIMEOUT = 20

# Forms that are material to an equity owner.  A 4 (insider) or a 144 (resale
# notice) is noise for a 30-day signal, so they are deliberately excluded.
MATERIAL_FORMS = {
    "8-K": ("material event", 75),
    "8-K/A": ("material event (amended)", 75),
    "10-Q": ("quarterly results", 70),
    "10-K": ("annual results", 70),
    "S-1": ("capital raise registration", 70),
    "S-3": ("shelf registration", 65),
    "424B4": ("offering priced", 70),
    "424B5": ("offering priced", 70),
    "SC 13D": ("activist stake", 65),
    "SC 13D/A": ("activist stake (amended)", 65),
    "25-NSE": ("delisting notice", 85),
    "DEF 14A": ("shareholder meeting", 45),
    "6-K": ("foreign issuer disclosure", 60),
}


@dataclasses.dataclass
class Bundle:
    stories: list[Story] = dataclasses.field(default_factory=list)
    reached: list[str] = dataclasses.field(default_factory=list)
    errors: dict[str, str] = dataclasses.field(default_factory=dict)

    def add(self, name: str, stories: list[Story]) -> None:
        self.reached.append(name)
        self.stories.extend(stories)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _aware(value: dt.datetime | None) -> dt.datetime:
    if value is None:
        return _now()
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def _get(url: str, headers: dict | None = None, timeout: int = HTTP_TIMEOUT) -> bytes:
    request = urllib.request.Request(url, headers=headers or {
        "User-Agent": "auto-trader-news/1.0 (research; contact via repo)",
        "Accept": "*/*",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _base_symbol(symbol: str) -> str:
    return symbol.split("/")[0].split("-")[0].upper()


# --------------------------------------------------------------------------- #
# Alpaca news
# --------------------------------------------------------------------------- #
def fetch_alpaca(symbols: list[str], *, since: dt.datetime, limit: int = 250) -> list[Story]:
    """Two calls: one tagged query for equities, one untagged discovery feed.

    Alpaca's tag filter knows equities but returns nothing for ``SPY`` or
    ``BTC/USD``; coin stories only appear in the general feed, where the symbol
    matcher picks them up by name (see ``news_intel._symbol_in_text``).
    """
    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest

    client = NewsClient(os.environ["APCA_API_KEY_ID"], os.environ["APCA_API_SECRET_KEY"])
    per_call = max(1, min(int(limit), 50))
    equities = [s for s in symbols if "/" not in s]

    queries: list[dict] = []
    if equities:
        queries.append({"symbols": ",".join(equities)})
    queries.append({})                       # discovery: crypto and index stories

    rows = []
    seen: set[str] = set()
    errors: list[Exception] = []
    for extra in queries:
        params: dict = {"start": since, "limit": per_call,
                        "exclude_contentless": True, **extra}
        try:
            result = client.get_news(NewsRequest(**params))
        except Exception as exc:              # noqa: BLE001 - one query may fail
            errors.append(exc)
            continue
        for group in (result.data or {}).values():
            for row in group:
                key = str(getattr(row, "id", "") or getattr(row, "url", "") or "")
                if key and key in seen:
                    continue
                seen.add(key)
                rows.append(row)

    if errors and len(errors) == len(queries):
        raise errors[0]

    stories: list[Story] = []
    for row in rows:
        source = str(getattr(row, "source", "") or "alpaca")
        stories.append(Story(
            source=source,
            headline=str(getattr(row, "headline", "") or ""),
            url=str(getattr(row, "url", "") or ""),
            published_at=_aware(getattr(row, "created_at", None)),
            retrieved_at=_now(),
            summary=str(getattr(row, "summary", "") or ""),
            symbols=tuple(getattr(row, "symbols", ()) or ()),
            tier=news_intel.source_tier(source),
            external_id=str(getattr(row, "id", "") or ""),
        ))
    return [s for s in stories if s.published_at >= since and s.headline]


# --------------------------------------------------------------------------- #
# SEC EDGAR
# --------------------------------------------------------------------------- #
def _sec_agent(cfg: dict) -> str:
    """Contact address for EDGAR: config first, then the environment.

    The environment path exists so CI can supply it as a repository secret —
    an email address in a public config file is a gift to scrapers.  SEC wants
    to be able to reach whoever is polling them, so the value must be a real
    contact, not a placeholder.
    """
    for candidate in (cfg.get("sec_user_agent"),
                      os.environ.get("NEWS_SEC_USER_AGENT"),
                      os.environ.get("SEC_USER_AGENT")):
        value = str(candidate or "").strip()
        if value:
            return value
    return ""


def _load_cik_map(cfg: dict) -> dict[str, dict]:
    """ticker -> {cik, title}.  Cached for a week; the file is ~1 MB."""
    if CIK_CACHE.exists():
        age = (_now() - dt.datetime.fromtimestamp(CIK_CACHE.stat().st_mtime, dt.timezone.utc))
        if age < dt.timedelta(hours=CIK_TTL_HOURS):
            try:
                return json.loads(CIK_CACHE.read_text(encoding="utf-8"))
            except ValueError:
                pass

    agent = _sec_agent(cfg)
    if not agent:
        agent = "auto-trader-news research contact via repository"
    body = _get("https://www.sec.gov/files/company_tickers.json",
                {"User-Agent": agent, "Accept": "application/json"})
    raw = json.loads(body)
    mapping = {
        str(row["ticker"]).upper(): {"cik": int(row["cik_str"]), "title": row["title"]}
        for row in raw.values()
    }
    CIK_CACHE.parent.mkdir(parents=True, exist_ok=True)
    CIK_CACHE.write_text(json.dumps(mapping), encoding="utf-8")
    return mapping


def fetch_sec(symbols: list[str], *, since: dt.datetime, cfg: dict) -> list[Story]:
    agent = _sec_agent(cfg)
    # SEC rejects anonymous clients with 403. Rather than invent a contact
    # address, say plainly that one must be configured.
    if "@" not in agent:
        raise RuntimeError(
            "sec_user_agent is not set in [news] and NEWS_SEC_USER_AGENT is empty; "
            "SEC EDGAR requires a contact address, e.g. \"Your Name you@example.com\""
        )
    mapping = _load_cik_map(cfg)

    stories: list[Story] = []
    for symbol in symbols:
        base = _base_symbol(symbol)
        if "/" in symbol:                    # crypto has no EDGAR identity
            continue
        entry = mapping.get(base)
        if not entry:
            continue
        cik = f"{entry['cik']:010d}"
        body = _get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                    {"User-Agent": agent, "Accept": "application/json"})
        recent = json.loads(body).get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        docs = recent.get("primaryDocument", [])

        for form, date, accession, doc in zip(forms, dates, accessions, docs):
            if form not in MATERIAL_FORMS:
                continue
            filed = dt.datetime.fromisoformat(date).replace(tzinfo=dt.timezone.utc)
            if filed < since:
                continue
            label, _impact = MATERIAL_FORMS[form]
            accession_clean = str(accession).replace("-", "")
            url = (f"https://www.sec.gov/Archives/edgar/data/{entry['cik']}/"
                   f"{accession_clean}/{doc}") if doc else \
                  f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
            stories.append(Story(
                source="SEC EDGAR",
                headline=f"{base} files {form}: {label}",
                url=url,
                published_at=filed,
                retrieved_at=_now(),
                summary=f"{entry['title']} filed form {form} with the SEC on {date}.",
                symbols=(base,),
                tier=1,
                external_id=f"sec-{cik}-{accession}",
            ))
    return stories


# --------------------------------------------------------------------------- #
# RSS
# --------------------------------------------------------------------------- #
_ATOM = "{http://www.w3.org/2005/Atom}"


def _parse_rss(body: bytes, *, feed_name: str, tier: int, since: dt.datetime) -> list[Story]:
    root = ET.fromstring(body)
    stories: list[Story] = []

    def text(node, *names):
        for name in names:
            found = node.find(name)
            if found is not None and (found.text or "").strip():
                return found.text.strip()
        return ""

    items = root.findall(".//item") or root.findall(f".//{_ATOM}entry")
    for item in items:
        title = text(item, "title", f"{_ATOM}title")
        link = text(item, "link", f"{_ATOM}link")
        if not link:
            node = item.find(f"{_ATOM}link")
            link = (node.get("href") if node is not None else "") or ""
        published = text(item, "pubDate", f"{_ATOM}updated", f"{_ATOM}published")
        summary = text(item, "description", f"{_ATOM}summary")
        when = _parse_date(published)
        if not title or when is None or when < since:
            continue
        stories.append(Story(
            source=feed_name,
            headline=title,
            url=link,
            published_at=when,
            retrieved_at=_now(),
            summary=re.sub(r"<[^>]+>", " ", summary)[:400],
            tier=tier,
        ))
    return stories


def _parse_date(value: str) -> dt.datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            parsed = dt.datetime.strptime(value, fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_rss(cfg: dict, *, since: dt.datetime) -> tuple[list[Story], dict[str, str]]:
    stories: list[Story] = []
    errors: dict[str, str] = {}
    for feed in cfg.get("rss_feeds") or []:
        name = feed.get("name", "rss")
        try:
            body = _get(feed["url"], timeout=12)
            stories.extend(_parse_rss(body, feed_name=name, tier=int(feed.get("tier", 3)),
                                     since=since))
        except Exception as exc:  # noqa: BLE001 - one dead feed must not stop the run
            errors[name] = f"{type(exc).__name__}: {exc}"
    return stories, errors


# --------------------------------------------------------------------------- #
# bundle
# --------------------------------------------------------------------------- #
def fetch_all(cfg: dict, symbols: list[str], *, hours: int = 24,
              limit: int = 250) -> Bundle:
    """Pull every configured source.  Partial failure is reported, not raised."""
    since = _now() - dt.timedelta(hours=hours)
    bundle = Bundle()

    try:
        bundle.add("alpaca", fetch_alpaca(symbols, since=since, limit=limit))
    except Exception as exc:  # noqa: BLE001
        bundle.errors["alpaca"] = f"{type(exc).__name__}: {exc}"

    try:
        bundle.add("sec_edgar", fetch_sec(symbols, since=since, cfg=cfg))
    except Exception as exc:  # noqa: BLE001
        bundle.errors["sec_edgar"] = f"{type(exc).__name__}: {exc}"

    rss, rss_errors = fetch_rss(cfg, since=since)
    if rss or not rss_errors:
        bundle.add("rss", rss)
    bundle.errors.update(rss_errors)

    # Newest first, then collapse exact duplicates from the same source.
    bundle.stories.sort(key=lambda s: s.published_at, reverse=True)
    seen: set[tuple[str, str]] = set()
    unique: list[Story] = []
    for story in bundle.stories:
        key = (news_intel._norm(story.source), story.fingerprint)
        if key in seen:
            continue
        seen.add(key)
        unique.append(story)
    bundle.stories = unique
    return bundle


# --------------------------------------------------------------------------- #
# price context: was the move already made?
# --------------------------------------------------------------------------- #
def price_context(symbol: str, *, since: dt.datetime) -> tuple[float | None, float | None]:
    """(price at publication, price now).  ``(None, None)`` when unavailable.

    Never invents a price: the lower the data quality, the higher the reported
    priced-in uncertainty, and the guard's quote check remains the execution
    price regardless.
    """
    try:
        if "/" in symbol:
            from alpaca.data.historical.crypto import CryptoHistoricalDataClient
            from alpaca.data.requests import CryptoBarsRequest, CryptoLatestQuoteRequest
            from alpaca.data.timeframe import TimeFrame

            client = CryptoHistoricalDataClient(os.environ["APCA_API_KEY_ID"],
                                                os.environ["APCA_API_SECRET_KEY"])
            bars = client.get_crypto_bars(CryptoBarsRequest(
                symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
                start=since - dt.timedelta(minutes=30), limit=1000,
            ))
            closes = [float(b.close) for b in bars.data.get(symbol, [])]
            quote = client.get_crypto_latest_quote(
                CryptoLatestQuoteRequest(symbol_or_symbols=symbol))
            row = quote.get(symbol) if isinstance(quote, dict) else None
            now_px = (float(row.bid_price) + float(row.ask_price)) / 2 if row else None
            return (closes[0] if closes else None, now_px)

        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
        from alpaca.data.timeframe import TimeFrame

        client = StockHistoricalDataClient(os.environ["APCA_API_KEY_ID"],
                                          os.environ["APCA_API_SECRET_KEY"])
        bars = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
            start=since - dt.timedelta(minutes=30), limit=1000,
        ))
        closes = [float(b.close) for b in bars.data.get(symbol, [])]
        trade = client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol))
        row = trade.get(symbol) if isinstance(trade, dict) else None
        return (closes[0] if closes else None, float(row.price) if row else None)
    except Exception:  # noqa: BLE001 - absence of a price is data, not a crash
        return (None, None)
