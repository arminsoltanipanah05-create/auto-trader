# News Intelligence Layer

The strategies decide **what** to trade. This layer decides whether the news
gives them permission, and it can only ever take permission away.

```
strategy signal ─► news screen ─► existing pre-trade gate ─► GuardedBroker.submit
                   (veto / pause / halve / confirm / boost)
```

## The one rule

News is an **additional veto**, never a source of size, leverage or a new
strategy. Concretely, in code:

| Effect | Meaning | Order quantity |
|---|---|---|
| `BOOST` | verified news agrees with the signal | unchanged |
| `CONFIRM` | nothing material opposes the signal | unchanged |
| `REDUCE` | verified news opposes, but not strongly enough to veto | halved |
| `VETO` | contradiction, fully priced in, or strong verified opposition | zero |
| `PAUSE` | unverified material news, extreme move, or imminent event | zero |
| `IGNORE` | Tier 3 below the materiality floor | unchanged |

`BOOST` is deliberately indistinguishable from `CONFIRM` at the order level: the
brief says never to exceed the existing maximum position size, and the simplest
way to guarantee that is to never change quantity on a positive signal.

**A risk-reducing order is never blocked.** Trimming or closing a position is
how risk is removed; a headline must not be able to trap the book. The check is
`is_entry(side, current_qty, qty)`, which mirrors the gate's own definition of
added exposure — so an exit, a partial trim, and a flip are each classified
correctly.

## Sources and tiers

| Tier | Examples | Can authorize alone? |
|---|---|---|
| 1 | SEC EDGAR filings, Federal Reserve press releases, company releases on Business Wire / GlobeNewswire / PR Newswire | yes |
| 2 | Reuters, Bloomberg, FT, WSJ, AP, CNBC, MarketWatch, Nikkei, Barron's, Benzinga | two independent outlets |
| 3 | aggregators (fiscal.ai, MarketScreener), analyst notes, blogs, social | **never** — alert only |

Verification follows directly:

- `VERIFIED` — one Tier 1 source, or two distinct Tier 2 outlets;
- `PARTIALLY_VERIFIED` — exactly one Tier 2 outlet;
- `UNVERIFIED` — Tier 3 only;
- `CONTRADICTED` — two sources whose direction reads are opposite.

Adapters live in `python/news_sources.py`:

- **Alpaca News** (`NewsClient`) — uses the API keys the bots already hold. Two
  calls per run: one tagged query for the equity universe, one untagged
  discovery feed (which is how coin stories surface — Alpaca returns nothing for
  `SPY` or `BTC/USD` tags).
- **SEC EDGAR** (`data.sec.gov/submissions`) — material forms only: 8-K, 10-Q,
  10-K, S-1/S-3, 424B, SC 13D, 25-NSE, 6-K. Requires a contact address in
  `sec_user_agent` or the `NEWS_SEC_USER_AGENT` environment variable; without
  one the adapter refuses rather than inventing a contact and getting a 403.
  On the runners, set the repository secret `NEWS_SEC_USER_AGENT` so the
  address never lands in a public commit.
- **RSS** — Fed (Tier 1), CNBC and MarketWatch (Tier 2). Tier is per feed.

Dead feeds are reported and the run continues. `required = false` (default)
means a blind news layer does not halt lawful trading; `required = true` turns
blindness into `NO_TRADE` for new risk only.

## What a story is allowed to talk about

An instrument is affected when the publisher tagged it, or the headline names it
as a standalone token (ticker, or coin name via `CRYPTO_ALIASES`: BTC→bitcoin,
ETH→ethereum, …). Substring matching is banned — "software" is not a war.

Macro releases and central-bank decisions name no ticker. They may speak about a
declared `macro_proxies` index (default `SPY`, `QQQ`) **only if** the headline
reports the release itself (`FOMC`, `rate cut`, `CPI`, `payrolls`, `tariffs`, …)
and the event is material. "Mortgage demand falls as interest rates rise" is a
central-bank headline by keyword and a false positive in substance.

## Scoring

All scores are pure functions of (story set, price context, stored history) in
`python/news_intel.py`:

- **impact** — event-type base weight, raised by polarity words ("plunge",
  "beats") and strong modifiers ("sharply", "unexpectedly");
- **confidence** — verification tier, corroborating source count, freshness
  penalty, multiplied by the source's learned weight (never above 1.0);
- **novelty** — age band, minus 20 per prior sighting of the same fingerprint;
- **urgency**, **expected_duration** — per event type;
- **priced_in_percent** — `|move since publication| / expected move × 100`,
  where the move comes from Alpaca minute bars and the expected move from the
  impact band. No price available ⇒ 0, never an invented one.

## Decision order

`decide()` applies, in order:

1. not an entry → `CONFIRM` (never block an exit);
2. `CONTRADICTED` → `VETO`;
3. `priced_in ≥ 80` → `VETO` (fully priced in);
4. move since publication `≥ 3%` → `PAUSE` (do not chase);
5. declared event inside the blackout window → `PAUSE`;
6. `UNVERIFIED` and material → `PAUSE` (alert only, cannot authorize);
7. direction opposes the order → `VETO` if confidence ≥ 60 and impact ≥ 60,
   else `REDUCE`;
8. direction supports the order → `BOOST` (or `CONFIRM` if no longer novel);
9. otherwise `CONFIRM`.

## Required output

Every screening decision appends one block to `state/news/decisions.jsonl` and
one line to `logs/news.log`, before the order is submitted:

```json
{"news_influence": {
  "event_summary": "", "event_type": "", "affected_symbols": [],
  "verification": "VERIFIED", "direction": "BEARISH", "confidence": 0,
  "impact": 0, "novelty": 0, "priced_in_percent": 0,
  "expected_duration": "intraday", "primary_source_url": "",
  "confirming_source_urls": [], "published_at": "", "retrieved_at": "",
  "supports_existing_signal": false, "invalidation_condition": "",
  "decision_effect": "VETO", "reason": ""
}}
```

`news_audit` follows the block with the urgency, the relation
(`SUPPORTS`/`OPPOSES`/`NEUTRAL`), the source list, and the two observed prices.
NO_TRADE decisions are logged too — the absence of a record is a defect.

## Commands

```powershell
python news_intel.py check --symbols SPY,QQQ,BTC/USD --hours 24   # what the feeds say
python news_intel.py scan  --symbols NVDA,SPY                     # decisions + JSON
python news_intel.py history                                      # effect counts
python news_review.py                                             # score + reweight
```

## Review loop

`python/news_review.py` re-reads the decision log, waits until the 120-hour
horizon has actually elapsed (no peeking), then pulls hourly bars forward from
the decision timestamp and computes return, MFE, MAE, direction correctness and
the priced-in estimate. It groups results by source, event type, confidence
bucket and holding period, writes `state/news/review.json`, and lowers the
weight of a source whose out-of-sample hit rate is poor (bounded to
`[0.25, 1.0]`, minimum 8 observations, slow decay). Those weights feed back into
`confidence` on the next run — and can only ever reduce it.

This runs automatically: both workflows end with a step that scores any decision
whose horizon closed since the previous run (`--max-new 40` caps the price
calls) and commits the state back to `main`. Each decision is scored exactly
once and appended to `state/news/outcomes.jsonl`, so the hourly crypto job costs
a handful of bar requests, not a rescan of history. The report is only rewritten
when its content changes, so the cron does not commit a timestamp every hour.

## Deliberate limits

- **The screen runs where orders are decided, not on dry runs.** `--send` paths
  screen; a dry run prints the strategy plan untouched. Screening a plan that
  will not be sent would fill the decision log with trades nobody attempted and
  corrupt the review sample.
- **No paid feeds.** No Bloomberg or Reuters terminal, so coverage is what the
  free Tier 1/2 sources publish. Absence of news is not evidence of calm.
- **No standalone event trades by default.** `allow_standalone = false`: the
  repo is systematic, and an event-driven trade needs a human at the keyboard.
  The capability exists and is tested; turning it on is an explicit decision.
- **Headline-level classification.** Keyword polarity cannot read a whole
  article. It is auditable and reproducible, which is why it is used; a story
  whose meaning inverts inside the body will be scored wrongly.
- **State survives the runners through git.** `state/news/` is committed by the
  workflows, so dedupe, novelty and the learned weights carry across ephemeral
  machines. The logs rotate at 2 MB / 2,000 lines, are marked `merge=union` in
  `.gitattributes` so two runs in the same minute append instead of conflicting,
  and the workflows rebase before pushing. `cik_map.json` and `review.json` are
  derived, so they stay local.
