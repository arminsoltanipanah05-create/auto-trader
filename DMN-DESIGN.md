# Deep Momentum Network — implementation notes

Source: Lim, Zohren & Roberts, *Enhancing Time Series Momentum Strategies Using
Deep Neural Networks*, J. Financial Data Science (2019). arXiv:1904.04912.

## What the paper actually does

Baseline TSMOM has two hand-made parts: a **trend estimator** (the sign of past
returns) and a **position sizing rule** (scale to a volatility target). The
paper's contribution is to learn both at once.

- **Inputs:** volatility-normalised returns over several horizons (roughly 1 to
  252 days) plus a MACD-style trend feature.
- **Model:** LSTM produces a hidden state; a dense layer maps it through `tanh`
  to a position in `[-1, 1]`.
- **Sizing:** that position is multiplied by `target_vol / realised_vol`, with a
  15% annualised target. So the network decides *direction and conviction*, the
  vol scaler decides *dollars*.
- **Loss:** the Sharpe ratio of the resulting returns, differentiated directly —
  not a classification loss on up/down. This is the core trick. Optimising
  accuracy would be wrong; you want the P&L path to be smooth.
- **Turnover regularisation:** a penalty on `|position_t - position_{t-1}|`, so
  the model prices in transaction costs during training instead of after.
- **Universe:** 88 continuous futures, multiple asset classes, ~1990 onward.
- **Reported result:** Sharpe-optimised LSTM beat traditional methods by more
  than 2× before costs, and kept outperforming with costs at 2–3 bps.

## Why this cannot run on the current stack

| Requirement | TradingView + TradersPost | Verdict |
|---|---|---|
| LSTM inference | No NN support in Pine | Blocked |
| Daily retrain | No | Blocked |
| 88 futures | Equities/ETFs only on Alpaca | Blocked |
| Long/short + leverage | Possible via broker | OK |
| Sharpe-loss training | Needs PyTorch | Blocked |

The Python service replaces TradingView entirely for signal generation.

## Architecture

```
cron (daily, after close)
  │
  ├─ 1. ingest      Alpaca bars (or yfinance for history)
  ├─ 2. features    vol-normalised returns 1..252d, MACD
  ├─ 3. infer       load checkpoint → position per symbol
  ├─ 4. portfolio   vol-target scale, cap leverage, net exposure
  ├─ 5. risk        kill switch, daily loss limit, max position
  └─ 6. execute     alpaca-py → PAPER endpoint
        │
        └─ 7. log    signals, fills, slippage vs signal price
```

Everything up to step 6 is offline and testable. Step 6 is the only part that
touches money, and it should stay behind a `PAPER=true` flag until the
walk-forward results are in.

## The data problem, stated plainly

The paper's edge comes partly from a wide, long futures panel. On Alpaca you
have US ETFs from roughly 2005 with clean history, so:

- **~20 years, not 35.** Fewer regimes, so a weaker out-of-sample claim.
- **~10–20 instruments, not 88.** Less diversification, noisier ensemble.
- **ETF proxies for futures.** TLT is not a bond future; USO is not crude.

An honest replication is "TSMOM with a learned sizer on an ETF panel." That can
still work — but the paper's headline numbers do not transfer, and anyone who
claims they do is guessing.

## Evaluation protocol (non-negotiable)

1. **Walk-forward, expanding window.** Train on everything up to year *t*,
   predict year *t+1*, roll. Never shuffle time series.
2. **Costs from day one** — 2 bps plus slippage, applied to turnover.
3. **Turnover regularisation** in training, tuned so realised turnover matches
   what you can actually execute.
4. **Baseline comparison.** If the LSTM does not beat plain sign-of-momentum on
   the same panel with the same costs, it has learned nothing.
5. **Ensemble** across seeds, as the paper does. Single-seed NNs are noise.
6. **Report the worst fold, not the average.** The average hides the failure.

## File layout

```
python/
  requirements.txt
  config.yaml          # universe, vol target, costs, paper flag
  data.py              # ingest + cache bars
  features.py          # normalised returns, MACD, scaling
  model.py             # LSTM + tanh head + Sharpe loss
  train.py             # walk-forward loop, checkpoints
  backtest.py          # costs, turnover, metrics, fold report
  portfolio.py         # vol targeting, leverage caps, netting
  execute.py           # alpaca-py, paper endpoint, idempotent orders
  risk.py              # kill switch, limits
```

## Realistic timeline

| Week | Output |
|---|---|
| 1 | Data pipeline + features, verified against the paper's description |
| 2 | Baseline TSMOM backtest on the ETF panel — this is the bar to beat |
| 3–4 | LSTM + Sharpe loss, single fold, sanity-checked |
| 5–6 | Walk-forward, ensembles, cost sensitivity |
| 7+ | Paper execution, then months of monitoring |

If the baseline beats the network on your data, stop and run the baseline. That
happens more often than the papers suggest.

## Honest caveats

- Backtests are historical evidence, not predictions.
- The paper is 2019; momentum crowding has increased since.
- An LSTM trained on 20 years of ETFs can memorise regimes.
- Live results will be worse than backtest. The question is by how much.
