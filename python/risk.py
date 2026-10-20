"""Hard limits. These run before any order leaves the machine."""
from __future__ import annotations

import pathlib

OUT = pathlib.Path(__file__).resolve().parent / "output"
KILL_FILE = OUT / "KILL_SWITCH"


def kill_switch_engaged() -> bool:
    """Create output/KILL_SWITCH to stop the bot. Delete it to resume."""
    return KILL_FILE.exists()


def engage_kill_switch(reason: str = "manual") -> pathlib.Path:
    OUT.mkdir(exist_ok=True)
    KILL_FILE.write_text(f"engaged {reason}\n")
    return KILL_FILE


def check_order_batch(gross_exposure: float, cfg: dict) -> bool:
    limit = cfg["execution"]["max_gross_exposure"]
    if gross_exposure > limit + 1e-9:
        print(f"REJECT: gross {gross_exposure:.2f}x exceeds limit {limit:.2f}x")
        return False
    return True


def check_daily_loss(equity_now: float, equity_prev_close: float, cfg: dict) -> bool:
    limit = cfg["execution"]["max_daily_loss_pct"]
    if equity_prev_close <= 0:
        return True
    change = equity_now / equity_prev_close - 1
    if change <= -limit:
        print(f"REJECT: day loss {change:.2%} breaches -{limit:.1%} limit")
        return False
    return True
