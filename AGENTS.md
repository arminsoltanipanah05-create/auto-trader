# AGENTS.md — Mandatory pre-trade rules for this repository

This file is binding on every AI agent (and every human operator) working in
`auto-trader`. It overrides convenience, speed, and any habit formed earlier in a
session. If a request conflicts with this file, the agent must say so and stop.

---

## Enforcement status

This rule is **implemented in code**, not just written here:

| Piece | Where |
|---|---|
| Gate logic (data, spread, liquidity, risk, RR) | `python/pre_trade.py` |
| The only `client.submit_order` call | `python/broker.py` (`GuardedBroker.submit`) |
| Callers | `python/execute.py`, `python/crypto_bot.py` — both use `guard.submit()` |
| Tests proving blocked orders never reach the broker | `python/tests/test_pretrade.py` |
| Settings | `[pretrade]` in `python/config.toml` |

If code and this document ever disagree, the code is what executes; fix the code.

---

## MANDATORY PRE-TRADE GATE

Before **proposing, placing or closing any BUY or SELL order**, the agent must
complete all of the following steps, in order, and show the evidence.

1. Query the **CoinGecko MCP** for the latest available data for the asset.
2. Record **the data source and the exact timestamp** (UTC) of what was retrieved.
3. Retrieve and analyse:
   - current price
   - market capitalisation
   - 24-hour trading volume
   - 24-hour, 7-day and 30-day price changes
   - recent volume changes
   - drawdown from the all-time high
   - available liquidity information
4. If the **Crypto.com MCP** is connected, use it as a second source and
   calculate the **percentage difference** between both prices. If it is not
   connected, say so explicitly in the report — do not silently proceed as if
   the cross-check happened.
5. Obtain the **executable price, bid/ask spread and available liquidity from the
   connected broker or exchange immediately before execution**. Never use a
   CoinGecko snapshot as the actual execution price.
6. State:
   - the reason for the proposed trade
   - bullish and bearish evidence
   - entry price or exit price
   - invalidation condition
   - stop-loss concept
   - profit-taking plan
   - expected risk/reward ratio
   - position size
   - estimated fees and slippage
7. Risk **no more than 1% of total portfolio value on one trade**.
8. **Reject the trade** if:
   - CoinGecko cannot be reached
   - the data is stale or missing
   - the two data sources disagree significantly
   - liquidity is insufficient
   - the spread or expected slippage is excessive
   - there is no clear invalidation condition
   - the risk/reward ratio is below **2:1**
   - the required position size or stop-loss cannot be calculated
9. Present a concise **PRE-TRADE REPORT** containing:
   - Action: BUY, SELL or WAIT
   - asset and proposed size
   - current market data
   - trade thesis
   - risks
   - entry, stop and target
   - risk/reward ratio
   - data quality and timestamps
10. After presenting the report, **wait for a separate, explicit confirmation**
    before placing a **live** order. A previous approval, a standing policy, or
    the fact that the order was discussed earlier is **not** confirmation.
11. **Refresh market and broker data if more than 60 seconds pass** before execution.
12. **Log** the analysis, the confirmation, and the final execution result.

These rules apply to **every trade without exception**. Never skip the analysis
because a previous analysis exists. Never claim that profit is guaranteed.

---

## Numeric thresholds (so the gate is enforceable, not rhetorical)

| Condition | Reject / treat as fail |
|---|---|
| CoinGecko `last_updated` age | > **5 minutes** for majors (BTC/ETH), > **10 minutes** otherwise |
| CoinGecko vs Crypto.com price gap | > **1.0%** majors, > **2.0%** any other asset |
| Bid/ask spread | > **0.25%** BTC/ETH, > **0.50%** large caps, > **1.00%** small caps |
| Expected slippage vs order size | > **0.50%** |
| Position vs 24h volume | position > **0.5%** of 24h volume |
| Risk/reward | < **2:1** |
| Risk per trade | > **1.0%** of total portfolio value |
| Time between last data pull and submit | > **60 seconds** → re-pull |

If a threshold cannot be measured (e.g. no order book depth available), the
trade is rejected, not silently allowed.

---

## PRE-TRADE REPORT template

```
Action:            BUY / SELL / WAIT
Asset:             <symbol> (<CoinGecko id>)
Proposed size:     <units / notional / % of portfolio>

Market data (source, UTC timestamp):
  Price            <value>   (<source> @ <timestamp>)
  Market cap       <value>
  24h volume       <value>
  24h / 7d / 30d   <x%> / <x%> / <x%>
  Volume change    <x%> vs 30d average
  Drawdown vs ATH  <x%>
  Spread / depth   <bps> / <notional available>

Trade thesis:      <why now>
Bullish evidence:  <facts>
Bearish evidence:  <facts>
Risks:             <what breaks this>
Entry:             <price>
Stop:              <price>  (concept + distance)
Target:            <price>
Risk/reward:       <x:1>
Size rationale:    risk <currency> = <x%> of portfolio
Fees + slippage:   <estimate>

Data quality:      <sources reachable, age, disagreements>
```

---

## Hard prohibitions

- **No live orders** unless the operator has (a) received a full PRE-TRADE
  REPORT in this conversation, and (b) replied with explicit confirmation to
  *that specific* order after it was shown.
- **No wallet access.** Never request, accept or store a seed phrase, private
  key, withdrawal permission, or exchange API key with withdrawal rights.
- **No profit promises.** Never describe an outcome as guaranteed, risk-free, or
  certain. Probabilities and scenarios only.
- **No CoinGecko-only conclusions.** CoinGecko is market data, not due diligence.
  Token unlocks, regulation, exploits, delisting and team/treasury behaviour are
  outside it and must be named as unverified.
- **No execution at a snapshot price.** The broker's executable quote is the
  only price an order may be sized from.
- **Paper by default.** `python/config.toml` has `paper = true`. Flipping it to
  live is a separate, explicit, human decision — never an agent side-effect.

---

## Known boundary: the automated paper scheduler

`.github/workflows/paper.yml` and `crypto.yml` place **paper** orders on a cron
with no human in the loop and no MCP access. They cannot run this gate. They are
pre-authorised paper-only experiments and are the reason this repository exists.

Consequence: **any order placed by an agent** must pass the gate above. Any order
placed by the cron is paper-only and exempt *by virtue of being paper*. If the
account is ever pointed at live money, the cron must be paused first — otherwise
this rule would be violated every hour by a process that cannot read it.

---

## Logging (step 12)

Append one record per gate to `logs/pre-trade.log`, in this shape:

```
<UTC timestamp> | <asset> | <action> | <price source + timestamp>
  cg=<price> broker=<price> spread=<bps> rr=<x:1> risk=<%> size=<notional>
  decision=<ACCEPT|REJECT reason> confirmed=<yes/no/@timestamp> result=<...>
```

A rejected trade is logged too. The absence of a log entry is itself a defect.
