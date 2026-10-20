# Walk-forward results — DMN vs baseline vs SPY

Run: `python walkforward.py --epochs 25 --seeds 2 --hidden 48`
Panel: 12 ETFs, 2009–2026, 52,980 samples, 7 features, seq len 63.
Splits: train `< Y-1`, validation `Y-1`, test `Y`. No shuffling.

| # | Train | Val | Test | Ann ret | Sharpe | Max DD | Trades | Turnover | Costs | Profit factor |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 2009-2013 | 2014 | 2015 | -1.4% | -0.62 | -3.3% | 104 | 12.3x | 0.49% | 0.89 |
| 2 | 2009-2014 | 2015 | 2016 | -0.0% | -0.03 | -0.6% | 622 | 16.4x | 0.66% | 0.99 |
| 3 | 2009-2015 | 2016 | 2017 | +1.3% | +2.01 | -0.4% | 414 | 17.8x | 0.71% | 1.45 |
| 4 | 2009-2016 | 2017 | 2018 | -0.6% | -0.21 | -2.3% | 48 | 14.7x | 0.59% | 0.96 |
| 5 | 2009-2017 | 2018 | 2019 | +2.4% | +1.64 | -1.1% | 243 | 16.1x | 0.64% | 1.37 |
| 6 | 2009-2018 | 2019 | 2020 | +1.5% | +0.41 | -3.7% | 63 | 10.7x | 0.43% | 1.11 |
| 7 | 2009-2019 | 2020 | 2021 | +0.9% | +1.01 | -0.7% | 474 | 18.5x | 0.74% | 1.19 |
| 8 | 2009-2020 | 2021 | 2022 | -4.8% | -1.63 | -5.9% | 36 | 12.1x | 0.49% | 0.75 |
| 9 | 2009-2021 | 2022 | 2023 | +0.3% | +0.35 | -0.8% | 651 | 15.6x | 0.62% | 1.06 |
| 10 | 2009-2022 | 2023 | 2024 | +1.6% | +1.36 | -0.5% | 257 | 13.4x | 0.54% | 1.26 |
| 11 | 2009-2023 | 2024 | 2025 | +3.9% | +1.89 | -1.9% | 34 | 7.6x | 0.30% | 1.45 |
| 12 | 2009-2024 | 2025 | 2026 | +0.5% | +0.41 | -0.9% | 173 | 8.7x | 0.35% | 1.08 |

Costs are percentages of equity for the year at 4 bps round-trip on turnover.

## Aggregates

| | Mean Sharpe | Median | Std | Worst | Positive | 95% CI |
|---|---|---|---|---|---|---|
| DMN | +0.55 | +0.41 | 1.09 | -1.63 | 8/12 | [-0.055, +1.121] |
| Baseline | +0.34 | +0.43 | 0.62 | -0.59 | 7/12 | [+0.017, +0.685] |
| SPY | +1.23 | — | — | — | 10/12 | — |

Pooled continuously-compounded OOS: **Sharpe 0.23**, +5.34% total, max DD -6.12%, 2,967 days.

## Why the returns look small

Mean gross exposure **0.184x**, mean absolute position **0.015**, 88.2% long, mean realised vol
**1.72%**. The network emits near-zero `tanh` values, so the book barely trades and the
vol target cannot lift it (factor capped at 4x, gross capped at 1x).

## The implementation flaw this exposed

The Sharpe loss is **scale-invariant**: halving every position leaves `mean/std` unchanged.
Nothing in the objective anchors position size, and the turnover penalty actively rewards
shrinking (`lambda * mean|Δposition|`). The degenerate optimum is "trade almost nothing",
and the model found it.

The paper avoids this by applying the volatility scaler *inside* the training target, so the
model sees returns at portfolio scale. Training on raw next-day returns, as done here, leaves
the scale unconstrained.

## Verdict

No evidence of a stable momentum edge, and no evidence the model beats either the rule-based
baseline or buy-and-hold. Position correlation with `sign(60d momentum)` is **-0.003**: it is
not learning momentum, and what it is learning does not persist across folds.

---

# v2 — vol scaler inside the loss, plus shuffled-label control

Run: `python walkforward.py --scaled-loss --anchor 10 --tag _v2`
Control: `--shuffle-labels --tag _control`

| # | Test | Ann ret | Sharpe | Max DD | Trades | Turnover | Costs | PF | Gross |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 2015 | +3.9% | +0.59 | -5.0% | 12 | 6.6x | 0.26% | 1.11 | 0.85x |
| 2 | 2016 | -0.9% | -0.40 | -1.9% | 79 | 4.8x | 0.19% | 0.92 | 0.19x |
| 3 | 2017 | +0.8% | +0.76 | -0.6% | 284 | 1.9x | 0.08% | 1.17 | 0.10x |
| 4 | 2018 | -0.2% | -0.26 | -1.0% | 123 | 1.2x | 0.05% | 0.94 | 0.06x |
| 5 | 2019 | +0.4% | +1.69 | -0.1% | 12 | 0.2x | 0.01% | 2.15 | 0.01x |
| 6 | 2020 | +0.1% | +0.58 | -0.1% | 24 | 0.2x | 0.01% | 1.16 | 0.01x |
| 7 | 2021 | +0.1% | +0.66 | -0.0% | 337 | 0.3x | 0.01% | 1.29 | 0.01x |
| 8 | 2022 | +0.0% | +0.09 | -0.7% | 12 | 0.6x | 0.02% | 1.02 | 0.02x |
| 9 | 2023 | +0.1% | +0.56 | -0.2% | 12 | 0.1x | 0.00% | 1.26 | 0.00x |
| 10 | 2024 | +0.0% | +1.40 | -0.0% | 12 | 0.02x | 0.00% | 1.26 | 0.00x |
| 11 | 2025 | +0.0% | +1.60 | -0.0% | 14 | 0.02x | 0.00% | 1.39 | 0.00x |
| 12 | 2026 | +0.0% | +0.78 | -0.0% | 32 | 0.02x | 0.00% | 1.14 | 0.00x |

| | Mean Sharpe | Std | Positive | 95% CI | Pooled |
|---|---|---|---|---|---|
| v2 DMN | +0.67 | 0.66 | 10/12 | [+0.309, +1.031] | +0.18 |
| Baseline | +0.34 | 0.62 | 7/12 | [+0.017, +0.685] | — |
| SPY | +1.23 | 1.20 | 10/12 | [+0.575, +1.867] | — |
| Control (shuffled) | -1.02 | 1.30 | 3/12 | [-1.716, -0.317] | -0.80 |

**Control is decisive:** shuffled labels lose 52.9% with pooled Sharpe -0.80. The harness
detects signal, and v2 beats the control 9/12. The model is not pure noise.

**But exposure still collapses.** Mean gross 0.11x, mean vol 1.00%. Folds 9-12 run at 0.00x,
so their Sharpe of +0.6 to +1.6 is computed on a near-flat book and is not meaningful. That is
why pooled (+0.18) sits far below the mean of folds (+0.67).

**Rescaling does not rescue it.** Applying multipliers to the v2 positions:

| Scale | Gross | Vol | CAGR | Sharpe |
|---|---|---|---|---|
| 1x | 0.07 | 1.7% | +0.26% | 0.16 |
| 4x | 0.13 | 2.8% | +0.28% | 0.10 |
| 9x | 0.19 | 3.6% | +0.62% | 0.17 |
| 12x | 0.21 | 3.9% | +0.83% | 0.21 |

Sharpe stays near 0.2 because scaling is scale-invariant, and the book cannot grow because the
model's own outputs are ~0 in most years.

**Remaining implementation defect:** the anchor targets `mean|p| = 1` on vol-scaled positions,
but inference then applies 1/N weights, so the target is inconsistent with the deployed book.
Fixing it would raise returns proportionally, not Sharpe.

**Final:** weak, unstable skill that does not beat the baseline reliably and is far below
buy-and-hold. Not deployable.
