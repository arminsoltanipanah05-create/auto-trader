#!/usr/bin/env bash
# setup.sh - one-shot installer for the paper trading bots on Ubuntu 24.04.
#
#   sudo bash /opt/auto-trader/deploy/setup.sh
#
# First run creates /etc/auto-trader/env and stops so you can add your keys.
# Second run installs dependencies and enables both timers.
set -euo pipefail

ROOT="${AUTOTRADER_ROOT:-/opt/auto-trader}"
STATE="${AUTOTRADER_STATE:-/var/lib/auto-trader}"
ENVFILE="/etc/auto-trader/env"
SVCUSER="trader"

say() { printf '\n\033[36m== %s\033[0m\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this with sudo." >&2
    exit 1
fi

if [ ! -d "$ROOT/python" ]; then
    echo "Expected the repo at $ROOT (missing $ROOT/python)." >&2
    exit 1
fi

say "packages"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    python3 python3-venv python3-pip tzdata ca-certificates >/dev/null

say "service user"
if ! id -u "$SVCUSER" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "$STATE" --shell /usr/sbin/nologin "$SVCUSER"
    echo "created user $SVCUSER"
else
    echo "user $SVCUSER exists"
fi
mkdir -p "$STATE"

say "credentials"
if [ ! -f "$ENVFILE" ]; then
    mkdir -p /etc/auto-trader
    cat > "$ENVFILE" <<'EOF'
# Alpaca PAPER keys. Generate at https://app.alpaca.markets/paper/dashboard/overview
# Paper key IDs start with PK. No quotes, no spaces around '='.
APCA_API_KEY_ID=PK_REPLACE_ME
APCA_API_SECRET_KEY=REPLACE_ME
EOF
    chmod 600 "$ENVFILE"
    chown root:root "$ENVFILE"
    cat <<EOF

Created $ENVFILE (mode 600, root only).

Put your PAPER keys in it, then run this script again:

    nano $ENVFILE
    sudo bash $ROOT/deploy/setup.sh

EOF
    exit 1
fi

if grep -q "REPLACE_ME" "$ENVFILE"; then
    echo "$ENVFILE still contains placeholder keys. Edit it and re-run." >&2
    exit 1
fi
chmod 600 "$ENVFILE"

say "line endings"
# The bundle is built on Windows; a stray CR makes bash scream "bad interpreter".
find "$ROOT" \( -name '*.sh' -o -name '*.py' \) -print0 | xargs -0 -r sed -i 's/\r$//'

say "python environment"
if [ ! -x "$ROOT/python/.venv/bin/python" ]; then
    python3 -m venv "$ROOT/python/.venv"
fi
"$ROOT/python/.venv/bin/pip" install --quiet --upgrade pip
"$ROOT/python/.venv/bin/pip" install --quiet -r "$ROOT/deploy/requirements-server.txt"
"$ROOT/python/.venv/bin/python" -c "import alpaca, pandas, numpy; print('deps ok')"

say "permissions"
mkdir -p "$ROOT/python/output" "$ROOT/logs"
chown -R "$SVCUSER:$SVCUSER" "$ROOT" "$STATE"
chmod +x "$ROOT/deploy/run-paper.sh" "$ROOT/deploy/run-crypto.sh"

say "timers"
# Fail loudly now if the calendar expressions are wrong, rather than silently
# installing a timer that never fires.
systemd-analyze calendar "Mon..Fri *-*-* 20..22:00/15:00" >/dev/null
systemd-analyze calendar "*-*-* 00,04,08,12,16,20:05:00" >/dev/null

install -m 644 "$ROOT"/deploy/systemd/*.service /etc/systemd/system/
install -m 644 "$ROOT"/deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now autotrader-paper.timer autotrader-crypto.timer

say "done"
systemctl list-timers 'autotrader-*' --no-pager || true
cat <<EOF

Next steps:
  systemctl start autotrader-crypto     # run the crypto sleeve once, now
  journalctl -u autotrader-crypto -n 40 --no-pager

  systemctl list-timers 'autotrader-*'
  systemctl stop autotrader-paper.timer autotrader-crypto.timer   # pause everything
  touch $ROOT/python/output/KILL_SWITCH                           # halt trading only

Local Windows tasks must be disabled while this server runs, or both machines
trade the same account.
EOF
