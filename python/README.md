# python/ — research pipeline

Verified on real ETF data, 2008-01-02 → 2026-10-20 (12 assets, 4,730 days).

## Setup

Two interpreters, because PyTorch has no build for Python 3.15.

```powershell
cd C:\Users\armin\Documents\auto-trader\python

# research (numpy/pandas) — 3.15 is fine
py -3.12 -m venv .venv-torch
.\.venv-torch\Scripts\Activate.ps1
pip install torch alpaca-py yfinance
```

> `python` on this machine is 3.15.0. NumPy 2.5.4 and pandas 3.0.6 install
> fine there. PyYAML and torch do **not** — no wheels. Hence `config.toml`
> (stdlib `tomllib`) instead of YAML, and the 3.12 venv for training.

## Run order

```powershell
python run_baseline.py       # the bar the model must beat
python sensitivity.py        # leverage and cost sensitivity
python tests/test_smoke.py   # 6 checks, no network needed

# 3.12 venv, after installing torch:
python train.py              # walk-forward, writes output/dmn_positions.csv
python execute.py            # dry run by default
```

## Files

| File | Role | Status |
|---|---|---|
| `data.py` | Prices from local CSV, cache, or yfinance | tested |
| `features.py` | Vol-normalised returns, MACD, sequences | tested |
| `portfolio.py` | Vol targeting, weights, gross caps | tested |
| `backtest.py` | Costs, Sharpe, folds, yearly table | tested |
| `cfg.py` | TOML config loader | tested |
| `run_baseline.py` | TSMOM baseline report | tested |
| `sensitivity.py` | Leverage/cost grids | tested |
| `tests/test_smoke.py` | Lookahead, costs, lag, vol scaling | 6/6 pass |
| `model.py` | LSTM + Sharpe loss | **untested** (no torch) |
| `train.py` | Walk-forward training | **untested** |
| `execute.py` | Alpaca orders, paper, idempotent | **untested** |
| `risk.py` | Kill switch, gross and loss limits | tested (logic) |

## Baseline result

| Config | Ann vol | CAGR | Sharpe | Max DD |
|---|---|---|---|---|
| gross ≤ 1.0 (no margin) | 7.7% | 1.9% | 0.24 | -19.7% |
| gross ≤ 2.0 | 12.5% | 3.6% | 0.28 | -23.0% |
| gross ≤ 3.0 | 14.6% | 4.2% | 0.29 | -29.0% |
| **Buy & hold SPY** | **19.7%** | **11.3%** | **0.64** | **-51.9%** |

Leverage raises vol without raising Sharpe — as it must, since it scales both
numerator and denominator. The baseline does **not** beat buy-and-hold SPY on
this panel.

Cost sensitivity at gross ≤ 2.0:

| Cost | CAGR | Sharpe |
|---|---|---|
| 0 bps | 6.0% | 0.48 |
| 2 bps | 4.8% | 0.38 |
| 5 bps | 3.0% | 0.24 |
| 10 bps | 0.1% | 0.00 |

Turnover is 57x/year at 2x gross. The strategy is cost-fragile, which is
exactly why the paper adds a turnover penalty to the loss.

## What this means for the model

The LSTM should be judged against **0.64 (SPY)** and **0.48 (the baseline at
zero cost)**, not against the paper's headline. If a walk-forward fold cannot
clear buy-and-hold after costs, the honest move is to run the simpler thing or
nothing.

Clock: the first fold trains on 2008–2014 and predicts 2015, so the earliest
meaningful out-of-sample Sharpe is 2015 onward — 11 years, roughly 8 folds.
That is a thin sample. Treat a good fold as a hint, and a bad one as decisive.
