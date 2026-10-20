"""Walk-forward training for the DMN.

Requires torch (Python 3.12 venv). UNTESTED on this machine — review before
trusting the output.

    python train.py --config config.toml

Protocol: train on everything before year Y, predict year Y, roll forward.
Never random-split a time series. Each step gets its own model, so the
"model" you deploy is only ever the last checkpoint.
"""
from __future__ import annotations

import argparse
import pathlib

import numpy as np
import pandas as pd
import torch
from torch import nn

import cfg as cfgmod
import data
import features
import model as modelmod

OUT = pathlib.Path(__file__).resolve().parent / "output"
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"


def build_dataset(prices: pd.DataFrame, cfg: dict):
    """Sequences of features with the next-day return attached to each."""
    panel = features.build_feature_panel(prices, cfg)
    cols = list(panel.columns)
    X, meta = features.to_sequences(panel, cols, cfg["model"]["sequence_length"])

    fwd = prices.pct_change().shift(-1)                     # return AFTER the decision date
    y = np.array([fwd.at[d, t] for d, t in zip(meta["date"], meta["ticker"])], dtype=np.float64)

    keep = ~np.isnan(y)
    return X[keep], y[keep], meta[keep].reset_index(drop=True), cols


def train_one(X: np.ndarray, y: np.ndarray, n_features: int, cfg: dict, seed: int) -> modelmod.DMN:
    m = cfg["model"]
    torch.manual_seed(seed)
    net = modelmod.DMN(n_features, m["lstm_hidden"], m["lstm_layers"], m["dropout"]).to(DEVICE)
    opt = torch.optim.Adam(net.parameters(), lr=m["learning_rate"])
    loss_fn = modelmod.SharpeLoss(m["turnover_penalty"])

    xb = torch.tensor(X, dtype=torch.float32)
    yb = torch.tensor(y, dtype=torch.float32)
    n = len(xb)

    for epoch in range(m["epochs"]):
        net.train()
        perm = torch.randperm(n)
        for i in range(0, n, m["batch_size"]):
            idx = perm[i : i + m["batch_size"]]
            if len(idx) < 32:
                continue
            opt.zero_grad()
            pos = net(xb[idx].to(DEVICE))
            # Turnover proxy: consecutive samples inside the batch, which are
            # time-ordered only within a ticker. Good enough for regularisation;
            # not an exact accounting of realised turnover.
            prev = torch.cat([pos[:1].detach(), pos[:-1]])
            loss = loss_fn(pos, yb[idx].to(DEVICE), prev)
            loss.backward()
            nn.utils.clip_grad_norm_(net.parameters(), 5.0)   # LSTMs blow up without this
            opt.step()
    return net


@torch.no_grad()
def predict(net: modelmod.DMN, X: np.ndarray) -> np.ndarray:
    net.eval()
    out = []
    for i in range(0, len(X), 4096):
        xb = torch.tensor(X[i : i + 4096], dtype=torch.float32).to(DEVICE)
        out.append(net(xb).cpu().numpy())
    return np.concatenate(out) if out else np.array([])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--start", default=None)
    args = ap.parse_args()

    cfg = cfgmod.load(args.config)
    if args.start:
        cfg["start"] = args.start

    prices = data.load_prices(cfg["universe"], cfg["start"], cfg["data_source"])
    X, y, meta, cols = build_dataset(prices, cfg)
    print(f"{len(X)} samples, {X.shape[2]} features, device={DEVICE}")

    start_year = pd.Timestamp(cfg["model"]["walk_forward_start"]).year
    years = sorted({d.year for d in meta["date"]})
    rows = []

    for year in [y_ for y_ in years if y_ >= start_year]:
        tr = meta["date"].dt.year < year
        te = meta["date"].dt.year == year
        if tr.sum() < 1000 or te.sum() == 0:
            print(f"skip {year}: train={tr.sum()} test={te.sum()}")
            continue

        preds = []
        for seed in range(cfg["model"]["ensemble_seeds"]):
            net = train_one(X[tr], y[tr], X.shape[2], cfg, seed)
            preds.append(predict(net, X[te]))
            print(f"  {year} seed {seed} done")

        avg = np.mean(preds, axis=0)
        rows.append(pd.DataFrame({
            "date": meta.loc[te, "date"].values,
            "ticker": meta.loc[te, "ticker"].values,
            "position": avg,
            "hit": (np.sign(avg) == np.sign(y[te])).mean(),
        }))

    if not rows:
        raise SystemExit("No folds produced. Is walk_forward_start inside the data range?")

    out = pd.concat(rows, ignore_index=True)
    OUT.mkdir(exist_ok=True)
    out.to_csv(OUT / "dmn_positions.csv", index=False)

    folded = out.groupby(out["date"].dt.year)["hit"].mean()
    print("\nDirectional hit rate by fold")
    print(folded.to_string(float_format=lambda v: f"{v:.3f}"))
    print(f"\nWrote {OUT / 'dmn_positions.csv'} — feed it to evaluate.py")
    print("Reminder: hit rate is not P&L. Compare Sharpe after costs, per fold.")


if __name__ == "__main__":
    main()
