"""Deep Momentum Network: LSTM with a differentiable Sharpe loss.

Requires torch, which does NOT install on Python 3.15. Use the 3.12 venv:

    py -3.12 -m venv .venv-torch
    .venv-torch\\Scripts\\activate
    pip install torch

This module is UNTESTED on this machine (no torch available). Treat it as a
reviewed starting point, not a validated implementation.
"""
from __future__ import annotations

import torch
import torch.nn as nn

EPS = 1e-8


class DMN(nn.Module):
    """Sequence of features -> hidden state -> position in [-1, 1].

    The tanh is the paper's trick for keeping the output bounded. The network
    learns direction and conviction; volatility scaling is applied later by
    `portfolio.build_positions`, so the model never needs to know account size.
    """

    def __init__(self, n_features: int, hidden: int = 64, layers: int = 1, dropout: float = 0.1):
        super().__init__()
        self.lstm = nn.LSTM(
            input_size=n_features,
            hidden_size=hidden,
            num_layers=layers,
            batch_first=True,
            dropout=dropout if layers > 1 else 0.0,
        )
        self.head = nn.Sequential(
            nn.Linear(hidden, hidden // 2),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden // 2, 1),
            nn.Tanh(),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out, _ = self.lstm(x)
        return self.head(out[:, -1, :]).squeeze(-1)


class SharpeLoss(nn.Module):
    """Negative Sharpe ratio, plus an optional turnover penalty.

    The paper's central idea: train on the P&L path's Sharpe ratio instead of
    on direction accuracy. A model that is right 55% of the time with tiny wins
    and huge losses is useless, and a classification loss cannot see that.

    `returns` are already position * asset_return for each sample.
    """

    def __init__(self, turnover_penalty: float = 0.0):
        super().__init__()
        self.turnover_penalty = turnover_penalty

    def forward(self, positions: torch.Tensor, asset_returns: torch.Tensor,
                prev_positions: torch.Tensor | None = None) -> torch.Tensor:
        pnl = positions * asset_returns
        mean = pnl.mean()
        std = pnl.std(unbiased=False)
        sharpe = mean / (std + EPS)
        loss = -sharpe

        if self.turnover_penalty and prev_positions is not None:
            loss = loss + self.turnover_penalty * (positions - prev_positions).abs().mean()
        return loss


def sharpe_ratio(returns) -> float:
    """Annualised Sharpe for reporting. Accepts a tensor or a sequence."""
    if hasattr(returns, "detach"):
        returns = returns.detach()
    if len(returns) < 2:
        return float("nan")
    return float(returns.mean() / (returns.std(unbiased=False) + EPS) * (252 ** 0.5))
