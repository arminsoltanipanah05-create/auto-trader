"""Score the news layer against what the market actually did.

    python news_review.py            # build the report and refresh weights
    python news_review.py --dry-run  # report only, weights untouched

Only *closed* observations count: a decision is scored once the horizon that
follows it has elapsed.  Weight updates therefore use information that existed
when the update ran, never a revised timestamp or a future bar.  A source that
looks good only because it was scored early is exactly the mistake this guard
exists to prevent.
"""
from __future__ import annotations

import argparse
import collections
import dataclasses
import datetime as dt
import json
import os
import statistics

import news_intel

HORIZONS_HOURS = (1, 24, 120)      # intraday, several days
DIRECTION_SIGN = {
    "STRONGLY_BULLISH": 1, "BULLISH": 1, "NEUTRAL": 0,
    "BEARISH": -1, "STRONGLY_BEARISH": -1,
}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def load_decisions(path=None) -> list[dict]:
    path = path or news_intel.DECISIONS_PATH
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _bars(symbol: str, start: dt.datetime, end: dt.datetime) -> list[tuple[dt.datetime, float]]:
    """Hourly closes for the window, or [] when data is unavailable."""
    try:
        if "/" in symbol:
            from alpaca.data.historical.crypto import CryptoHistoricalDataClient
            from alpaca.data.requests import CryptoBarsRequest
            from alpaca.data.timeframe import TimeFrame
            client = CryptoHistoricalDataClient(os.environ["APCA_API_KEY_ID"],
                                                os.environ["APCA_API_SECRET_KEY"])
            frame = client.get_crypto_bars(CryptoBarsRequest(
                symbol_or_symbols=symbol, timeframe=TimeFrame.Hour, start=start, end=end))
        else:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame
            client = StockHistoricalDataClient(os.environ["APCA_API_KEY_ID"],
                                               os.environ["APCA_API_SECRET_KEY"])
            frame = client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=symbol, timeframe=TimeFrame.Hour, start=start, end=end))
        rows = frame.data.get(symbol, [])
        return [(b.timestamp, float(b.close)) for b in rows]
    except Exception:  # noqa: BLE001
        return []


@dataclasses.dataclass
class Outcome:
    decided_at: dt.datetime
    symbol: str
    verdict: str               # CORRECT / WRONG / NEUTRAL
    horizon_hours: int
    ret: float
    mfe: float
    mae: float
    priced_in: float
    sources: tuple[str, ...]
    event_type: str
    confidence: float
    duration: str


def score_decision(row: dict, *, now: dt.datetime | None = None) -> Outcome | None:
    now = now or _now()
    influence = row.get("news_influence") or {}
    direction = influence.get("direction", "NEUTRAL")
    sign = DIRECTION_SIGN.get(direction, 0)
    if sign == 0:
        return None

    decided = dt.datetime.fromisoformat(row["decided_at"])
    if decided.tzinfo is None:
        decided = decided.replace(tzinfo=dt.timezone.utc)
    horizon = HORIZONS_HOURS[-1]
    if (now - decided).total_seconds() < horizon * 3600:
        return None                       # not closed yet: no peeking

    bars = _bars(row["symbol"], decided, decided + dt.timedelta(hours=horizon))
    if len(bars) < 2:
        return None
    start_px = bars[0][1]
    returns = [(px / start_px - 1.0) for _, px in bars]
    final = returns[-1]
    mfe = max(returns)
    mae = min(returns)

    one_hour = returns[0]
    use = one_hour if abs(one_hour) > 1e-9 else final
    verdict = "CORRECT" if sign * use > 0 else ("NEUTRAL" if use == 0 else "WRONG")

    return Outcome(
        decided_at=decided, symbol=row["symbol"], verdict=verdict,
        horizon_hours=horizon, ret=final, mfe=mfe, mae=mae,
        priced_in=float(influence.get("priced_in_percent") or 0.0),
        sources=tuple(influence.get("sources") or (row.get("source") or "unknown",)),
        event_type=influence.get("event_type", ""),
        confidence=float(influence.get("confidence") or 0.0),
        duration=influence.get("expected_duration", ""),
    )


def _bucket(value: float) -> str:
    for low in (80, 60, 40, 20):
        if value >= low:
            return f"{low}-{low + 20}"
    return "0-20"


def summarise(outcomes: list[Outcome]) -> dict:
    def table(key) -> dict:
        groups: dict[str, list[Outcome]] = collections.defaultdict(list)
        for item in outcomes:
            groups[key(item)].append(item)
        out = {}
        for name, items in groups.items():
            correct = sum(1 for i in items if i.verdict == "CORRECT")
            wrong = sum(1 for i in items if i.verdict == "WRONG")
            out[str(name)] = {
                "n": len(items),
                "hit_rate": round(correct / len(items), 3) if items else 0.0,
                "correct": correct,
                "wrong": wrong,
                "avg_return": round(statistics.fmean(i.ret for i in items), 5) if items else 0.0,
                "avg_mfe": round(statistics.fmean(i.mfe for i in items), 5) if items else 0.0,
                "avg_mae": round(statistics.fmean(i.mae for i in items), 5) if items else 0.0,
                "avg_priced_in": round(statistics.fmean(i.priced_in for i in items), 1) if items else 0.0,
            }
        return out

    return {
        "generated_at": _now().isoformat(),
        "observations": len(outcomes),
        "by_source": table(lambda i: i.sources[0] if i.sources else "unknown"),
        "by_event_type": table(lambda i: i.event_type or "unknown"),
        "by_confidence": table(lambda i: _bucket(i.confidence)),
        "by_duration": table(lambda i: i.duration or "unknown"),
    }


def refresh_weights(summary: dict, current: dict[str, float], *,
                    min_observations: int = 8) -> dict[str, float]:
    """Lower the weight of sources with a poor record.  Bounded to [0.25, 1.0].

    Never above 1.0: the layer may distrust a source, never trust it more than
    the published tier rules allow.
    """
    weights = dict(current)
    for source, stats in summary["by_source"].items():
        if stats["n"] < min_observations:
            continue
        hit = stats["hit_rate"]
        # 50% hit rate is a coin flip: keep the source neutral at 0.75.
        proposed = max(0.25, min(1.0, 0.5 + hit * 0.5))
        previous = float(weights.get(source, 1.0))
        weights[source] = round(previous * 0.3 + proposed * 0.7, 3)   # decay fast, recover slow
    return weights


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    store = news_intel.Store()
    decisions = load_decisions()
    now = _now()
    outcomes = [o for o in (score_decision(row, now=now) for row in decisions) if o]
    summary = summarise(outcomes)
    summary["decisions_recorded"] = len(decisions)
    summary["not_yet_scored"] = len(decisions) - len(outcomes)

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"decisions recorded : {summary['decisions_recorded']}")
        print(f"scored (closed)    : {summary['observations']}")
        print(f"awaiting horizon   : {summary['not_yet_scored']}\n")
        for section in ("by_source", "by_event_type", "by_confidence", "by_duration"):
            rows = summary[section]
            if not rows:
                continue
            print(section.replace("by_", "by ").upper())
            print(f"  {'name':<20}{'n':>4}{'hit':>8}{'avg ret':>10}{'MFE':>9}{'MAE':>9}{'priced':>8}")
            for name, s in sorted(rows.items(), key=lambda kv: -kv[1]["n"]):
                print(f"  {name[:20]:<20}{s['n']:>4}{s['hit_rate']*100:>7.0f}%"
                      f"{s['avg_return']*100:>9.2f}%{s['avg_mfe']*100:>8.2f}%"
                      f"{s['avg_mae']*100:>8.2f}%{s['avg_priced_in']:>8.0f}")
            print()

    out_path = news_intel.STATE_DIR / "review.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if args.dry_run:
        print("dry run: source weights unchanged")
        return
    weights = refresh_weights(summary, store.load_weights())
    store.save_weights(weights)
    if weights:
        print("source weights:", json.dumps(weights, sort_keys=True))


if __name__ == "__main__":
    main()
