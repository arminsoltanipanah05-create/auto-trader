# Go-live checklist

Do these in order. Don't skip a stage because it's boring — stage 2 is the
only place bugs show up before they cost money.

## Stage 0 — get it running (no money)

- [ ] Paste `strategy.pine` into TradingView's Pine editor, add to a daily chart.
- [ ] Check the strategy tester: trades, max drawdown, profit factor, trade count.
- [ ] Sanity check by eye: do the arrows appear where the rules say they should?
- [ ] Fix any repainting surprises (`process_orders_on_close=true` is already set).

## Stage 1 — alerts only (~2 weeks)

- [ ] Create an alert on the strategy, condition = "order fills".
- [ ] Send it to email/phone only. No webhook yet.
- [ ] Each time it fires, write down: ticker, price, time, what you'd have done.
- [ ] Look for: duplicate alerts, alerts at 15:59, alerts on the wrong symbol.
- [ ] After 2 weeks, compare alerts to what the backtest would have done.

**Do not proceed if alerts are duplicated or mistimed.** Fix the condition first.

## Stage 2 — paper execution (~1–3 months)

- [ ] Open a TradersPost account, connect a **paper** Alpaca account.
- [ ] Paste `alert-payload.json` into the webhook builder, confirm every field name.
- [ ] Start with `quantityType: percent`, `quantity: 2` — smaller than final.
- [ ] Switch the TradingView alert from email to webhook URL.
- [ ] Confirm orders appear in the paper account within seconds.
- [ ] Run for at least 8 weeks. Track: fills vs signal price, slippage, missed signals.
- [ ] Compare paper equity to backtest equity. A large gap = no edge, or bad config.

## Stage 3 — live, tiny

- [ ] Fund an amount you'd shrug at losing 30% of.
- [ ] Point TradersPost at the live account, keep `quantity: 2` for a month.
- [ ] Set every row in `RISK-CONFIG.md`.
- [ ] Test the kill switch on a small live position. Time yourself.
- [ ] Only then scale position size — one step at a time, never mid-drawdown.

## Ongoing

- [ ] Weekly: compare live fills to signal prices.
- [ ] Monthly: is the live curve tracking the backtest? If not, size down.
- [ ] Quarterly: re-backtest. Edges decay; a strategy that worked in 2021 may be dead.
- [ ] Keep a written log of every manual intervention and why.

## Hard rules

- Never add money to a losing system to "average in."
- Never increase size after a drawdown to "recover."
- Never disable the kill switch to "let it ride."
- If you can't explain in one sentence why it bought, it's not your strategy.
