#!/usr/bin/env bash
# Daily ETF rebalance. Invoked by autotrader-paper.timer every 15 minutes;
# the gate decides whether this is the right one.
set -euo pipefail

ROOT="${AUTOTRADER_ROOT:-/opt/auto-trader}"
STATE="${AUTOTRADER_STATE:-/var/lib/auto-trader}"
PY="$ROOT/python/.venv/bin/python"
MARKER="$STATE/last-trade.txt"

mkdir -p "$STATE"
cd "$ROOT/python"

if [ -f output/KILL_SWITCH ]; then
    echo "kill switch present - not trading"
    exit 0
fi

if ! "$PY" paper_gate.py --marker "$MARKER"; then
    exit 0
fi

"$PY" predict_live.py --strategy tsmom
"$PY" execute.py --send

# ET date, matching paper_gate's notion of "today"
TZ=America/New_York date +%F > "$MARKER"
echo "marker set for $(cat "$MARKER")"

"$PY" status.py || true
