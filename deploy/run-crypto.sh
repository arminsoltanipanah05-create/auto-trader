#!/usr/bin/env bash
# 24/7 crypto sleeve. Invoked by autotrader-crypto.timer every four hours.
set -euo pipefail

ROOT="${AUTOTRADER_ROOT:-/opt/auto-trader}"
PY="$ROOT/python/.venv/bin/python"

cd "$ROOT/python"

if [ -f output/KILL_SWITCH ]; then
    echo "kill switch present - not trading"
    exit 0
fi

exec "$PY" crypto_bot.py --send
