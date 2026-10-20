# auto-trader — the no-code stack

> **Updated after the strategy choice.** The DMN paper needs Python and a
> retraining loop, so the primary path is now `python/`. TradingView +
> TradersPost remains a fallback for the rules-based baseline only.
> See `DMN-DESIGN.md` and `python/README.md`.

## The chain (baseline, rules-based)

```
TradingView (Pine strategy + alert)
        │  webhook (JSON)
        ▼
TradersPost (routing, risk limits, paper mode)
        │  broker API
        ▼
Alpaca or Interactive Brokers (orders, fills)
```

## The chain (DMN, paper's actual model)

```
daily cron → feature panel → LSTM (Sharpe loss) → vol targeting → Alpaca paper
```

No TradingView, no TradersPost — a Python process calls Alpaca directly.


Each arrow is an account you own and a key you control. Nothing in this
folder can place an order on its own.

## What's in here

| File | Use |
|---|---|
| `strategy.pine` | Paste into TradingView's Pine editor. Trend filter + pullback entry, ATR-sized risk. |
| `alert-payload.json` | Webhook body for TradersPost. Confirm field names in their builder. |
| `RISK-CONFIG.md` | The guardrails, and why 1% risk per trade. |
| `GO-LIVE-CHECKLIST.md` | Staged path: alerts → paper → live tiny. |

## What you have to do yourself

These cannot be installed or automated from here. They need your identity,
your money, and your keys:

1. **TradingView account** — webhooks need a paid plan (~$15–60/mo).
2. **TradersPost account** — ~$50+/mo, has paper mode.
3. **Brokerage account** — Alpaca (free API, easy) or IBKR (cheaper, clunkier).
4. **Link them** — paste the TradersPost webhook URL into the TradingView alert.
5. **Fund it** — start with money you'd shrug at.

Never paste API keys or webhook URLs into a chat with any assistant, including
me. They belong in the platform's own settings page.

## The order of operations that actually works

1. Backtest the Pine on TradingView. Expect it to look mediocre — that's honest.
2. Run alerts-only for two weeks. Catch the dumb bugs.
3. Paper mode for two months. Compare fills to the backtest.
4. Live at 2% position size for a month.
5. Only then scale.

Roughly 3–4 months from now to real money. Anyone who tells you it's faster is
selling something.

## Reality check

This template is a scaffold, not an edge. A 200-day trend filter with a
pullback entry is one of the most crowded ideas in retail trading, and the
backtest will look good in 2016–2021 and mediocre since. That's not a reason to
avoid it — it's a reason to test it yourself and see the drawdown before you
fund it.

Backtests are historical evidence, not promises.
