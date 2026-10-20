"""Price ingestion with a local-first cache.

Order of resolution for each ticker:
  1. data/raw/TICKER.csv      (you provide it)
  2. data/cache/TICKER.csv    (written by this module)
  3. yfinance                 (if installed and data_source allows)

Accepted CSV shapes: a `Date`/`Datetime` column (or index) plus one of
`Adj Close`, `adj_close`, `Close`, `close`.
"""
from __future__ import annotations

import pathlib
import sys

import pandas as pd

HERE = pathlib.Path(__file__).resolve().parent
RAW = HERE / "data" / "raw"
CACHE = HERE / "data" / "cache"

_DATE_NAMES = {"date", "datetime", "time", "timestamp"}
_CLOSE_NAMES = {"adj close", "adj_close", "adjusted_close", "close"}


def _parse_csv(path: pathlib.Path) -> pd.Series:
    df = pd.read_csv(path)
    date_col = next((c for c in df.columns if c.strip().lower() in _DATE_NAMES), None)
    if date_col is None:
        if isinstance(df.index, pd.DatetimeIndex):
            df = df.reset_index().rename(columns={df.index.name or "index": "date"})
            date_col = next(c for c in df.columns if c.strip().lower() in _DATE_NAMES)
        else:
            raise ValueError(f"{path}: no date column found in {list(df.columns)}")

    close_col = next((c for c in df.columns if c.strip().lower() in _CLOSE_NAMES), None)
    if close_col is None:
        raise ValueError(f"{path}: no close column found in {list(df.columns)}")

    idx = pd.to_datetime(df[date_col], utc=True, errors="coerce").dt.tz_convert(None).dt.normalize()
    s = pd.Series(pd.to_numeric(df[close_col], errors="coerce").values, index=idx, name=path.stem)
    s = s[~s.index.isna()].sort_index()
    return s[~s.index.duplicated(keep="last")].dropna()


def _from_yfinance(ticker: str, start: str) -> pd.Series | None:
    try:
        import yfinance as yf  # noqa: PLC0415
    except ImportError:
        return None
    df = yf.download(ticker, start=start, auto_adjust=True, progress=False, threads=False)
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    col = "Close" if "Close" in df.columns else df.columns[0]
    s = pd.to_numeric(df[col], errors="coerce").dropna()
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
    s.name = ticker
    return s


def load_prices(universe, start=None, source="auto", refresh=False, min_obs=250) -> pd.DataFrame:
    """Return a wide DataFrame of close prices, columns = tickers."""
    RAW.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)

    series, missing = {}, []

    for ticker in universe:
        raw_path, cache_path = RAW / f"{ticker}.csv", CACHE / f"{ticker}.csv"

        if raw_path.exists():
            series[ticker] = _parse_csv(raw_path)
            continue

        if cache_path.exists() and not refresh:
            series[ticker] = _parse_csv(cache_path)
            continue

        fetched = None
        if source in ("auto", "yfinance"):
            fetched = _from_yfinance(ticker, start or "2000-01-01")
        if fetched is None or fetched.empty:
            missing.append(ticker)
            continue

        fetched.to_frame("close").to_csv(cache_path, index_label="date")
        series[ticker] = fetched

    if missing:
        print(
            "MISSING DATA for: " + ", ".join(missing) + "\n"
            "  -> install yfinance (pip install yfinance), or drop "
            "data/raw/<TICKER>.csv files with date,close columns.",
            file=sys.stderr,
        )
    if not series:
        raise RuntimeError("No price data loaded. See the message above.")

    prices = pd.DataFrame(series).sort_index()
    if start:
        prices = prices.loc[prices.index >= pd.Timestamp(start)]
    prices = prices.dropna(how="all")

    keep = [c for c in prices.columns if prices[c].notna().sum() >= min_obs]
    dropped = [c for c in prices.columns if c not in keep]
    if dropped:
        print(f"dropped (too little history): {', '.join(dropped)}", file=sys.stderr)

    return prices[keep]


if __name__ == "__main__":
    import yaml

    cfg = yaml.safe_load((HERE / "config.yaml").read_text())
    px = load_prices(cfg["universe"], cfg["start"], cfg["data_source"])
    print(px.tail())
    print(f"\n{px.shape[1]} tickers, {len(px)} rows, "
          f"{px.index.min().date()} -> {px.index.max().date()}")
