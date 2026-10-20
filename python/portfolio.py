"""Volatility targeting and portfolio assembly.

The paper's sizing rule: a raw signal in [-1, 1] is multiplied by
`target_vol / realised_vol`, so a quiet asset gets more notional and a wild
one gets less. This module applies that on top of whatever the model outputs,
and is shared by the baseline and the neural net so both are comparable.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from features import realised_vol

TRADING_DAYS = 252


def vol_scale(prices: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Per-asset multiplier: target / realised vol, capped at max_leverage."""
    s = cfg["signal"]
    vol = realised_vol(prices, s["vol_window"])
    scale = s["vol_target_ann"] / vol
    return scale.clip(upper=s["max_leverage"]).fillna(0.0)


def equal_weights(prices: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """1/N across assets that have data, capped per asset."""
    n = prices.notna().sum(axis=1).replace(0, np.nan)
    w = prices.notna().astype(float).div(n, axis=0)
    return w.clip(upper=cfg["signal"]["max_weight_per_asset"]).fillna(0.0)


def tsmom_signal(prices: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """The baseline: sign of the trailing return. +1 long, -1 short, 0 flat."""
    lookback = cfg["signal"]["lookback_days"]
    mom = prices / prices.shift(lookback) - 1.0
    return np.sign(mom).fillna(0.0)


def build_positions(signals: pd.DataFrame, prices: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Convert [-1, 1] signals into position weights (fraction of equity).

    position = weight * signal * vol_scale

    `signals` holds the model's raw conviction. The baseline passes sign(momentum),
    which is exactly the strategy the paper says its network beats.
    """
    return (equal_weights(prices, cfg) * signals * vol_scale(prices, cfg)).fillna(0.0)


def cap_gross(positions: pd.DataFrame, max_gross: float | None = None) -> pd.DataFrame:
    """Scale the whole book down if gross exposure exceeds the limit."""
    if max_gross is None:
        return positions
    gross = positions.abs().sum(axis=1)
    factor = (max_gross / gross).where(gross > max_gross, 1.0)
    return positions.mul(factor, axis=0)


def target_portfolio_vol(positions: pd.DataFrame, prices: pd.DataFrame, cfg: dict,
                         max_factor: float = 4.0) -> pd.DataFrame:
    """Scale the book so *ex-ante* portfolio vol hits the target.

    Per-asset vol scaling alone under-shoots badly: with N assets at 1/N
    weights, portfolio vol lands near `target * sqrt(1/N + (1-1/N)*rho)`,
    which for 12 ETFs is roughly half the target. The paper targets vol at
    the portfolio level, so we estimate it from trailing covariance and
    correct.

    Causal: row t uses returns strictly before t.
    """
    s = cfg["signal"]
    window = s["vol_window"]
    target = s["vol_target_ann"]

    rets = prices.pct_change().fillna(0.0)
    factors = np.ones(len(positions))

    for i in range(len(positions)):
        if i < window:
            factors[i] = 0.0                       # no estimate yet, stay flat
            continue
        w = positions.iloc[i].to_numpy()
        if not np.any(w):
            continue
        cov = np.cov(rets.iloc[i - window : i].to_numpy(), rowvar=False)
        var = float(w @ np.atleast_2d(cov) @ w)
        vol = np.sqrt(max(var, 0.0) * TRADING_DAYS)
        factors[i] = min(target / vol, max_factor) if vol > 1e-8 else 0.0

    return positions.mul(pd.Series(factors, index=positions.index), axis=0)
