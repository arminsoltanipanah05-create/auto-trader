"""Config loading. Uses stdlib tomllib — no PyYAML, which has no wheel for 3.15."""
from __future__ import annotations

import pathlib
import tomllib

HERE = pathlib.Path(__file__).resolve().parent


def load(path: str | pathlib.Path | None = None) -> dict:
    p = pathlib.Path(path) if path else HERE / "config.toml"
    with open(p, "rb") as fh:
        return tomllib.load(fh)
