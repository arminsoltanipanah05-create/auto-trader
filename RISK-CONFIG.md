# Risk config — set these BEFORE any real money

Fill this in as you configure each platform. If a platform can't enforce a
row, that's a reason to reject the platform, not to trust yourself.

| Guardrail | Target setting | Enforced where | Set? |
|---|---|---|---|
| Max single position | 10% of account | TradersPost / broker | [ ] |
| Max total exposure | 100% (no leverage) | Broker | [ ] |
| Risk per trade (to ATR stop) | 1.0% of equity | Pine sizing / TradersPost | [ ] |
| Max concurrent positions | 5 | TradersPost | [ ] |
| Daily loss limit | -3% → halt new entries | TradersPost | [ ] |
| Monthly loss limit | -10% → flatten + halt | Manual rule | [ ] |
| Max orders per day | 10 (prevents alert storms) | TradersPost | [ ] |
| Duplicate-alert protection | On | TradersPost | [ ] |
| Kill switch | One click, flattens all | Broker | [ ] |
| Kill switch tested | Yes, in paper mode | You | [ ] |

## The two rules that matter most

1. **Risk per trade, not position size.** Size from the distance to your stop.
   A volatile stock gets a smaller position for the same dollar risk. The Pine
   template already does this — do not override it with a fixed percentage.

2. **The kill switch is the strategy.** Every automated system eventually
   produces a bug: a stale alert, a doubled entry, a halt that never fires.
   The only thing that guarantees survival is being able to flatten everything
   in seconds. Test it on a paper account and time yourself.

## Sizing maths (why 1%)

On a $25,000 account, 1% risk is $250 per trade. At a 2×ATR stop on a $50
stock with ATR of $1.50, the stop is $3.00 away, so you buy 83 shares (~$4,150,
about 17% of the account). Losing the stop costs $250.

Ten losses in a row — entirely normal — costs 9.6% of the account. At 5% risk
per trade the same streak costs 40%. That's the difference between a bad month
and a blown account.
