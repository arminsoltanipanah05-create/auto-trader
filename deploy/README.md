# Running the paper bots on a VPS

The Windows machine has to be awake. A server doesn't. This folder turns the
same Python code into two systemd timers on a small Ubuntu box.

Nothing here changes the strategy — it changes who holds the schedule.

## What you need

| Item | Recommendation | Cost |
|---|---|---|
| Server | Hetzner CX22, **Ashburn, VA** (US East), Ubuntu 24.04 LTS | ~€4.35/mo |
| Alternative | DigitalOcean Basic Droplet, New York, Ubuntu 24.04 | $6/mo |
| Free option | Oracle Cloud "Always Free" ARM instance | $0, signup is fussy |

Pick a **US East** region — Alpaca's API lives there, so order round-trips are
tens of milliseconds instead of hundreds. 2 GB RAM is plenty; the bots are
pure Python, no torch.

Add your SSH public key during creation. No password login.

## 1. Build the bundle on Windows

From `C:\Users\armin\Documents`:

```powershell
tar -czf autotrader-bundle.tar.gz --exclude=.venv-torch --exclude=data --exclude=logs --exclude=__pycache__ auto-trader
```

Excluding `.venv-torch` keeps it small — that folder is torch and the training
environment, which the server never uses.

## 2. Upload and unpack

```powershell
scp autotrader-bundle.tar.gz root@YOUR_SERVER_IP:/tmp/
ssh root@YOUR_SERVER_IP "tar -xzf /tmp/autotrader-bundle.tar.gz -C /opt"
```

That lands the code at `/opt/auto-trader`.

## 3. First installer run — it stops on purpose

```powershell
ssh root@YOUR_SERVER_IP "bash /opt/auto-trader/deploy/setup.sh"
```

It creates `/etc/auto-trader/env` with placeholder keys and exits. That is
deliberate: it never asks you to paste secrets into a terminal history.

## 4. Add the paper keys

```powershell
ssh root@YOUR_SERVER_IP "nano /etc/auto-trader/env"
```

Replace both placeholders with your **paper** keys, save, then:

```powershell
ssh root@YOUR_SERVER_IP "bash /opt/auto-trader/deploy/setup.sh"
```

Second run installs dependencies, creates the `trader` user, and enables both
timers.

## 5. Verify

```powershell
ssh root@YOUR_SERVER_IP "systemctl list-timers 'autotrader-*' --no-pager"
ssh root@YOUR_SERVER_IP "systemctl start autotrader-crypto; journalctl -u autotrader-crypto -n 40 --no-pager"
```

You want `activates` and `n/a`-free output, and a crypto log ending in either
order lines or `nothing to trade`.

## 6. Disable the Windows tasks

**Do this before the server's first scheduled run**, or both machines trade the
same account from different clocks.

```powershell
Disable-ScheduledTask -TaskName AutoTrader-Paper
Disable-ScheduledTask -TaskName AutoTrader-Crypto
```

## Schedule

| Timer | When (UTC) | What |
|---|---|---|
| `autotrader-paper.timer` | every 15 min, 20:00–22:59, Mon–Fri | gate + ETF rebalance |
| `autotrader-crypto.timer` | 00:05, 04:05, 08:05, 12:05, 16:05, 20:05 daily | crypto sleeve |

The ETF timer fires repeatedly on purpose. `paper_gate.py` asks Alpaca for the
market clock and only proceeds when it is within 45 minutes of the close — so
summer and winter time need no edits, and a run the server missed is picked up
by the next slot.

## Operations

```bash
systemctl list-timers 'autotrader-*'              # next runs
journalctl -u autotrader-paper -n 80 --no-pager   # what the ETF bot did
journalctl -u autotrader-crypto -n 80 --no-pager  # what the crypto bot did
systemctl stop autotrader-paper.timer autotrader-crypto.timer   # pause
systemctl start autotrader-paper.timer autotrader-crypto.timer  # resume
touch /opt/auto-trader/python/output/KILL_SWITCH  # halt trading, keep timers
rm /opt/auto-trader/python/output/KILL_SWITCH     # resume trading
```

`Persistent=true` means a run missed while the server rebooted executes once on
startup, rather than being skipped.

## Updating the code later

```powershell
tar -czf autotrader-bundle.tar.gz --exclude=.venv-torch --exclude=data --exclude=logs --exclude=__pycache__ auto-trader
scp autotrader-bundle.tar.gz root@YOUR_SERVER_IP:/tmp/
ssh root@YOUR_SERVER_IP "tar -xzf /tmp/autotrader-bundle.tar.gz -C /opt && chown -R trader:trader /opt/auto-trader && systemctl restart autotrader-paper.timer"
```

## Things that will bite

- **Keys in `/etc/auto-trader/env` are readable by root only.** Anyone with root
  on that box has your Alpaca paper keys. They are paper keys, but treat them
  like live ones anyway — the same code path trades real money if `paper=false`.
- **Two runners is the main hazard.** If the Windows tasks stay enabled, both
  machines submit orders for the same book. Deterministic `client_order_id`s
  will reject most duplicates, but the desk gets noisy and positions can drift.
- **The server has no idea your PC exists.** Logs are the only feedback channel
  unless you add alerting.
