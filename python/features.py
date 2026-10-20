"""Feature construction for the Deep Momentum Network.

The paper feeds the network volatility-normalised returns over several
horizons plus a MACD-style trend feature, and lets the LSTM learn both the
trend estimate and the position size.

All functions are strictly causal: the value at row t uses data up to and
including t, never beyond. `tests/test_smoke.py` asserts this.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def daily_returns(prices: pd.DataFrame) -> pd.DataFrame:
    return prices.pct_change()


def realised_vol(prices: pd.DataFrame, window: int = 60, floor: float = 1e-4) -> pd.DataFrame:
    """Annualised rolling volatility of daily returns."""
    vol = daily_returns(prices).rolling(window, min_periods=window // 2).std() * np.sqrt(TRADING_DAYS)
    return vol.clip(lower=floor)


def vol_normalised_returns(prices: pd.DataFrame, horizons, window: int = 60) -> dict[str, pd.DataFrame]:
    """Return `(P_t / P_{t-h} - 1) / (sigma_t * sqrt(h))` per horizon.

    Dividing by sqrt(h) puts every horizon on a comparable scale: a 252-day
    move and a 5-day move both come out roughly unit-variance.
    """
    vol_daily = realised_vol(prices, window) / np.sqrt(TRADING_DAYS)
    out = {}
    for h in horizons:
        raw = prices / prices.shift(h) - 1.0
        out[f"ret_{h}"] = raw / (vol_daily * np.sqrt(h))
    return out


def macd_feature(prices: pd.DataFrame, fast: int = 8, slow: int = 24, window: int = 60) -> pd.DataFrame:
    """MACD in return units, then volatility-normalised.

    MACD is a price difference, so divide by price first to express it as a
    percentage, then by daily vol so it matches the other features.

    Column names stay as the tickers — the caller stacks this into the same
    (date, ticker) index as the return features.
    """
    ema_fast = prices.ewm(span=fast, adjust=False).mean()
    ema_slow = prices.ewm(span=slow, adjust=False).mean()
    macd_pct = (ema_fast - ema_slow) / prices
    vol_daily = realised_vol(prices, window) / np.sqrt(TRADING_DAYS)
    return macd_pct / vol_daily


def _stack(frame: pd.DataFrame) -> pd.Series:
    """`stack` that works on pandas 2.x and 3.x (the kwarg was removed in 3.0)."""
    try:
        return frame.stack(future_stack=True)
    except TypeError:
        return frame.stack(dropna=True)


def build_feature_panel(prices: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Long-format feature panel indexed by (date, ticker).

    Columns: the horizon returns plus macd. Values are winsorised at +/-5 sigma
    because a single bad print can otherwise dominate the LSTM's gradient.
    """
    fcfg = cfg["features"]
    window = cfg["signal"]["vol_window"]

    rets = vol_normalised_returns(prices, fcfg["horizons"], window)
    macd = macd_feature(prices, fcfg["macd_fast"], fcfg["macd_slow"], window)

    frames = [_stack(frame).rename(name) for name, frame in rets.items()]
    frames.append(_stack(macd).rename("macd"))

    panel = pd.concat(frames, axis=1)
    panel.index.names = ["date", "ticker"]
    panel = panel.replace([np.inf, -np.inf], np.nan)
    return panel.clip(-5.0, 5.0)


def to_sequences(panel: pd.DataFrame, feature_cols, seq_len: int):
    """Convert a (date, ticker) panel into LSTM sequences.

    Returns
    -------
    X : (n_samples, seq_len, n_features)
    meta : DataFrame with the `date` and `ticker` for each sample, in order.

    Samples are built per ticker so no sequence ever spans two assets, and a
    sequence only exists when all `seq_len` rows are present.
    """
    X_parts, meta_parts = [], []
    for ticker, grp in panel.groupby(level="ticker", sort=True):
        grp = grp.droplevel("ticker").sort_index()
        values = grp[list(feature_cols)].to_numpy(dtype=np.float64)
        dates = grp.index
        valid = ~np.isnan(values).any(axis=1)

        for end in range(seq_len - 1, len(values)):
            start = end - seq_len + 1
            if not valid[start : end + 1].all():
                continue
            X_parts.append(values[start : end + 1])
            meta_parts.append((dates[end], ticker))

    if not X_parts:
        return np.empty((0, seq_len, len(feature_cols))), pd.DataFrame(columns=["date", "ticker"])

    X = np.stack(X_parts)
    meta = pd.DataFrame(meta_parts, columns=["date", "ticker"])
    return X, meta
