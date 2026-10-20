"""Generate today's target book. The bridge between research and execute.py.

    python predict_live.py --strategy tsmom
    python predict_live.py --strategy dmn

Writes output/live_positions.csv with columns: date, ticker, position
where `position` is a fraction of account equity (0.05 = 5%).

Note on strategy choice: the DMN currently emits near-zero positions
(mean gross 0.11x), so a live DMN bot would trade a few dollars and mostly
sit flat. TSMOM actually takes positions, which is why it is the default here.
"""
from __future__ import annotations

import argparse
import pathlib

import numpy as np
import pandas as pd

import cfg as cfgmod
import data
import portfolio

OUT = pathlib.Path(__file__).resolve().parent / "output"


def tsmom_today(prices: pd.DataFrame, cfg: dict) -> pd.Series:
    sig = portfolio.tsmom_signal(prices, cfg).iloc[-1]
    return sig


def dmn_today(prices: pd.DataFrame, cfg: dict, seeds: int, epochs: int) -> pd.Series:
    import torch  # local import: only needed for the model path

    import features
    import model as modelmod

    panel = features.build_feature_panel(prices, cfg)
    cols = list(panel.columns)
    X, meta = features.to_sequences(panel, cols, cfg["model"]["sequence_length"])

    fwd = prices.pct_change().shift(-1)
    y = np.array([fwd.at[d, t] for d, t in zip(meta["date"], meta["ticker"])], dtype=np.float64)
    keep = ~np.isnan(y)
    X, y = X[keep].astype(np.float32), y[keep]

    xt = torch.from_numpy(X)
    yt = torch.from_numpy(y)
    preds = []

    for seed in range(seeds):
        torch.manual_seed(seed)
        net = modelmod.DMN(X.shape[2], cfg["model"]["lstm_hidden"],
                           cfg["model"]["lstm_layers"], cfg["model"]["dropout"])
        opt = torch.optim.Adam(net.parameters(), lr=cfg["model"]["learning_rate"])
        loss_fn = modelmod.SharpeLoss(cfg["model"]["turnover_penalty"])

        for _ in range(epochs):
            net.train()
            perm = torch.randperm(len(xt))
            for i in range(0, len(perm), cfg["model"]["batch_size"]):
                idx = perm[i : i + cfg["model"]["batch_size"]]
                if len(idx) < 32:
                    continue
                opt.zero_grad()
                pos = net(xt[idx])
                prev = torch.cat([pos[:1].detach(), pos[:-1]])
                loss = loss_fn(pos, yt[idx], prev)
                loss.backward()
                torch.nn.utils.clip_grad_norm_(net.parameters(), 5.0)
                opt.step()

        net.eval()
        with torch.no_grad():
            out = np.concatenate([net(xt[i : i + 8192]).numpy() for i in range(0, len(xt), 8192)])
        last_date = meta["date"].iloc[-1]
        today_mask = (meta["date"] == last_date).to_numpy()
        preds.append(pd.Series(out[today_mask], index=meta.loc[today_mask, "ticker"]))

    return pd.concat(preds, axis=1).mean(axis=1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", choices=["tsmom", "dmn"], default="tsmom")
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    cfg = cfgmod.load()
    prices = data.load_prices(cfg["universe"], cfg["start"], cfg["data_source"], refresh=args.refresh)

    # Drop the last bar if it is today and the close may be incomplete.
    asof = prices.index[-1]
    print(f"as of {asof.date()}  ({prices.shape[1]} assets)")

    signal = tsmom_today(prices, cfg) if args.strategy == "tsmom" else dmn_today(
        prices, cfg, args.seeds, args.epochs)
    signal = signal.reindex(prices.columns).fillna(0.0)

    w = portfolio.equal_weights(prices, cfg).iloc[-1] * signal * portfolio.vol_scale(prices, cfg).iloc[-1]

    # ex-ante portfolio vol from trailing covariance, then scale to target
    rets = prices.pct_change().dropna().iloc[-cfg["signal"]["vol_window"]:]
    cov = np.cov(rets.to_numpy(), rowvar=False)
    vol = float(np.sqrt(max(w.to_numpy() @ np.atleast_2d(cov) @ w.to_numpy(), 0.0) * 252))
    target = cfg["signal"]["vol_target_ann"]
    factor = min(target / vol, 4.0) if vol > 1e-8 else 0.0
    w = w * factor

    gross = float(w.abs().sum())
    if gross > cfg["execution"]["max_gross_exposure"] > 0:
        w = w * (cfg["execution"]["max_gross_exposure"] / gross)
        gross = float(w.abs().sum())

    book = w[w.abs() > 0.001].sort_values()
    print(f"\nsignal={args.strategy}  ex-ante vol {vol:.2%}  scale {factor:.2f}x  gross {gross:.2f}x")
    if book.empty:
        print("BOOK IS FLAT — nothing to trade. The signal produced no positions.")
    else:
        print(f"\n{'ticker':<7}{'weight':>9}{'$ per 25k':>12}")
        for t, v in book.items():
            print(f"{t:<7}{v:>8.2%}{v * 25000:>12,.0f}")

    OUT.mkdir(exist_ok=True)
    rows = pd.DataFrame({"date": asof, "ticker": w.index, "position": w.to_numpy()})
    rows = rows[rows["position"].abs() > 0.001]
    rows.to_csv(OUT / "live_positions.csv", index=False)
    print(f"\nWrote {OUT / 'live_positions.csv'} ({len(rows)} positions)")


if __name__ == "__main__":
    main()
