"""Strictly chronological walk-forward, v2.

Split for test year Y:
    train : year <  Y-1
    val   : year == Y-1   (early stopping only)
    test  : year == Y     (touched once)

v2 change — scale invariance:
    v1 trained on raw next-day returns, so the Sharpe loss had no opinion
    about position size and the model collapsed to ~1.5% positions. Here the
    volatility scaler sits *inside* the loss:

        p = tanh(z) * (target_vol / realised_vol)          # paper's sizing
        loss = -Sharpe(p * r) + lambda_turn * mean|dp|
                             + lambda_anchor * (mean|p| - 1)^2

    The anchor is what stops the degenerate "trade nothing" optimum; Sharpe
    alone cannot, because it is invariant to scaling p.

    --shuffle-labels permutes training returns only. If out-of-sample Sharpe
    survives that, the pipeline is measuring luck.

    python walkforward.py --scaled-loss --anchor 10 --tag _v2
"""
from __future__ import annotations

import argparse
import copy
import pathlib
import time

import numpy as np
import pandas as pd
import torch
from torch import nn

import backtest
import cfg as cfgmod
import data
import features
import model as modelmod
import portfolio

OUT = pathlib.Path(__file__).resolve().parent / "output"
torch.set_num_threads(min(16, torch.get_num_threads()))


def build_dataset(prices, cfg):
    panel = features.build_feature_panel(prices, cfg)
    cols = list(panel.columns)
    X, meta = features.to_sequences(panel, cols, cfg["model"]["sequence_length"])

    fwd = prices.pct_change().shift(-1)
    y = np.array([fwd.at[d, t] for d, t in zip(meta["date"], meta["ticker"])], dtype=np.float64)

    vs_wide = portfolio.vol_scale(prices, cfg)
    vs = np.array([vs_wide.at[d, t] for d, t in zip(meta["date"], meta["ticker"])], dtype=np.float64)

    keep = ~np.isnan(y) & ~np.isnan(vs)
    return X[keep].astype(np.float32), y[keep], vs[keep].astype(np.float32), meta[keep].reset_index(drop=True), cols


@torch.no_grad()
def _predict(net, X) -> np.ndarray:
    net.eval()
    out = []
    for i in range(0, len(X), 8192):
        out.append(net(torch.from_numpy(X[i : i + 8192])).numpy())
    return np.concatenate(out) if out else np.array([])


def _loss(pos_raw, ret, vs, prev_pos, cfg, anchor):
    p = pos_raw * vs if cfg["_scaled"] else pos_raw
    pnl = p * ret
    sharpe = pnl.mean() / (pnl.std(unbiased=False) + 1e-8)
    loss = -sharpe
    if cfg["model"]["turnover_penalty"]:
        loss = loss + cfg["model"]["turnover_penalty"] * (p - prev_pos).abs().mean()
    if anchor > 0:
        loss = loss + anchor * (p.abs().mean() - 1.0).pow(2)
    return loss


def train_fold(Xtr, ytr, vstr, Xva, yva, vsva, cfg, seed, epochs, anchor, patience=4):
    m = cfg["model"]
    torch.manual_seed(seed)
    net = modelmod.DMN(Xtr.shape[2], cfg["_hidden"], m["lstm_layers"], m["dropout"])
    opt = torch.optim.Adam(net.parameters(), lr=m["learning_rate"])

    xtr, ytr_t, vstr_t = map(torch.from_numpy, (Xtr, ytr, vstr))
    yva_t, vsva_t = torch.from_numpy(yva), torch.from_numpy(vsva)

    best = (-np.inf, None)
    since = 0

    for _ in range(epochs):
        net.train()
        perm = torch.randperm(len(xtr))
        for i in range(0, len(perm), m["batch_size"]):
            idx = perm[i : i + m["batch_size"]]
            if len(idx) < 32:
                continue
            opt.zero_grad()
            raw = net(xtr[idx])
            prev = torch.cat([raw[:1].detach(), raw[:-1]])
            loss = _loss(raw, ytr_t[idx], vstr_t[idx], prev, cfg, anchor)
            loss.backward()
            nn.utils.clip_grad_norm_(net.parameters(), 5.0)
            opt.step()

        # validation on the model's own sizing, same transform as training
        raw_va = torch.from_numpy(_predict(net, Xva))
        p_va = raw_va * vsva_t if cfg["_scaled"] else raw_va
        pnl = p_va * yva_t
        v = float(pnl.mean() / (pnl.std(unbiased=False) + 1e-8))

        if v > best[0] + 1e-6:
            best = (v, copy.deepcopy(net.state_dict()))
            since = 0
        else:
            since += 1
            if since >= patience:
                break

    if best[1] is not None:
        net.load_state_dict(best[1])
    return net, best[0]


def count_trades(pos: pd.DataFrame) -> int:
    prev = pos.shift(1).fillna(0.0)
    entered = ((pos.abs() > 1e-9) & (prev.abs() <= 1e-9)).to_numpy().sum()
    exited = ((pos.abs() <= 1e-9) & (prev.abs() > 1e-9)).to_numpy().sum()
    flipped = ((np.sign(pos) != np.sign(prev)) & (pos.abs() > 1e-9) & (prev.abs() > 1e-9)).to_numpy().sum()
    return int(entered + exited + flipped)


def profit_factor(r: pd.Series) -> float:
    gain = r[r > 0].sum()
    loss = -r[r < 0].sum()
    return float(gain / loss) if loss > 0 else float("nan")


def fold_metrics(net_ret, turnover, positions, cfg, label) -> dict:
    eq = (1 + net_ret).cumprod()
    vol = net_ret.std() * np.sqrt(252)
    ann = eq.iloc[-1] ** (252 / max(len(net_ret), 1)) - 1
    rate = (cfg["costs"]["cost_bps"] + cfg["costs"]["slippage_bps"]) / 10_000
    return {
        "fold": label, "ann_return": ann,
        "sharpe": ann / vol if vol > 0 else np.nan,
        "max_dd": (eq / eq.cummax() - 1).min(),
        "trades": count_trades(positions),
        "turnover": float(turnover.sum()),
        "cost_pct": float(turnover.sum() * rate),
        "profit_factor": profit_factor(net_ret),
        "days": len(net_ret), "vol": vol,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=25)
    ap.add_argument("--seeds", type=int, default=2)
    ap.add_argument("--hidden", type=int, default=48)
    ap.add_argument("--first-test-year", type=int, default=2015)
    ap.add_argument("--scaled-loss", action="store_true")
    ap.add_argument("--anchor", type=float, default=0.0)
    ap.add_argument("--shuffle-labels", action="store_true")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    cfg = cfgmod.load()
    cfg["_hidden"] = args.hidden
    cfg["_scaled"] = args.scaled_loss
    cfg["model"]["batch_size"] = 512

    prices = data.load_prices(cfg["universe"], cfg["start"], cfg["data_source"])
    X, y, vs, meta, cols = build_dataset(prices, cfg)
    years = meta["date"].dt.year
    print(f"dataset {len(X):,} | features {X.shape[2]} | seq {X.shape[1]} | "
          f"scaled_loss={args.scaled_loss} anchor={args.anchor} shuffle={args.shuffle_labels}")

    rng = np.random.default_rng(1234)
    rows, curves, pos_parts = [], {}, []
    test_years = sorted(set(years[years >= args.first_test_year]))

    for ty in test_years:
        t0 = time.time()
        tr = (years < ty - 1).to_numpy()
        va = (years == ty - 1).to_numpy()
        te = (years == ty).to_numpy()
        if tr.sum() < 5000 or va.sum() < 200 or te.sum() < 200:
            print(f"skip {ty}")
            continue

        ytr = y[tr].copy()
        if args.shuffle_labels:
            rng.shuffle(ytr)                      # control: no feature-return link

        preds, val_scores = [], []
        for seed in range(args.seeds):
            net, vsh = train_fold(X[tr], ytr, vs[tr], X[va], y[va], vs[va],
                                  cfg, seed, args.epochs, args.anchor)
            preds.append(_predict(net, X[te]))
            val_scores.append(vsh)

        raw = pd.DataFrame({"date": meta.loc[te, "date"].values,
                            "ticker": meta.loc[te, "ticker"].values,
                            "signal": np.mean(preds, axis=0)})
        raw = raw.pivot_table(index="date", columns="ticker", values="signal", aggfunc="mean")
        raw = raw.reindex(prices.index).fillna(0.0)

        positions = portfolio.build_positions(raw, prices, cfg)
        positions = portfolio.target_portfolio_vol(positions, prices, cfg)
        positions = portfolio.cap_gross(positions, cfg["execution"]["max_gross_exposure"])

        res = backtest.run(prices, positions, cfg)
        mask = prices.index.year == ty

        m = fold_metrics(res["net_returns"][mask], res["turnover"][mask], positions[mask], cfg, str(ty))
        mom = portfolio.tsmom_signal(prices, cfg).reindex_like(raw)
        a, b_ = raw.to_numpy().ravel(), mom.to_numpy().ravel()
        ok = ~(np.isnan(a) | np.isnan(b_))
        m.update({
            "train": f"{years[tr].min()}-{ty-2}", "val": str(ty - 1), "test": str(ty),
            "val_sharpe": float(np.mean(val_scores)),
            "corr_mom": float(np.corrcoef(a[ok], b_[ok])[0, 1]) if ok.sum() > 10 else np.nan,
            "gross_exposure": float(positions[mask].abs().sum(axis=1).mean()),
        })
        rows.append(m)
        curves[ty] = res["net_returns"][mask]
        pos_parts.append(positions[mask])
        print(f"  {ty} {time.time()-t0:5.1f}s | val {m['val_sharpe']:+.2f} "
              f"| test {m['sharpe']:+.2f} | gross {m['gross_exposure']:.2f}x | trades {m['trades']}")

    df = pd.DataFrame(rows).set_index("fold")
    OUT.mkdir(exist_ok=True)
    df.to_csv(OUT / f"walkforward_folds{args.tag}.csv")
    flat = pd.concat(curves)
    flat.index = pd.DatetimeIndex(flat.index.get_level_values(1))
    flat.rename("net_return").to_frame().to_csv(OUT / f"walkforward_returns{args.tag}.csv")
    pd.concat(pos_parts).to_csv(OUT / f"walkforward_positions{args.tag}.csv")

    print("\n" + df[["train", "val", "test", "ann_return", "sharpe", "max_dd",
                     "trades", "turnover", "cost_pct", "profit_factor"]].to_string(
        float_format=lambda x: f"{x:,.3f}"))
    print(f"\nmean Sharpe {df['sharpe'].mean():+.3f} | median {df['sharpe'].median():+.3f} "
          f"| worst {df['sharpe'].min():+.3f} | positive {(df['sharpe'] > 0).sum()}/{len(df)}")
    r = flat
    print(f"pooled Sharpe {r.mean() / r.std() * np.sqrt(252):+.3f} | "
          f"mean gross {df['gross_exposure'].mean():.2f}x | "
          f"total return {(1 + r).prod() - 1:+.2%}")


if __name__ == "__main__":
    main()
