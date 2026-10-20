# Running the paper bots for free with GitHub Actions

No server, no credit card, no monthly bill. GitHub runs the schedule on their
machines, which is why your PC can stay off.

The trade-off: GitHub's scheduler is **best-effort**. Runs can be delayed by
10–30 minutes and occasionally skipped under load. That is why the ETF workflow
fires four times around the close instead of once — the gate picks whichever
run lands inside the 45-minute window, and the committed marker stops it from
trading twice.

## What runs

| Workflow | Schedule (UTC) | Purpose |
|---|---|---|
| `paper-rebalance` | 19:30, 20:00, 20:30, 21:00 Mon–Fri | daily ETF book |
| `crypto-rebalance` | 00:05, 04:05, 08:05, 12:05, 16:05, 20:05 daily | crypto sleeve |

Each run boots a fresh Ubuntu machine, installs dependencies (~40 s), trades,
and disappears. Nothing is stored between runs except one file:
`state/last-trade.txt`, which is committed back to the repo.

## 1. Create an empty repository

On github.com: **New repository** → name it `auto-trader` → **Private** →
do *not* add a README, .gitignore or licence. An empty repo.

## 2. Push this folder

```powershell
cd C:\Users\armin\Documents\auto-trader
git remote add origin https://github.com/YOUR_USERNAME/auto-trader.git
git push -u origin main
```

## 3. Add the keys as repository secrets

Repository → **Settings** → **Secrets and variables** → **Actions** →
**New repository secret**, twice:

| Name | Value |
|---|---|
| `APCA_API_KEY_ID` | your paper key id (`PK...`) |
| `APCA_API_SECRET_KEY` | your paper secret |

Secrets are masked in logs. Never put them in a file in this repo.

## 4. Test it by hand

Actions → `paper-rebalance` → **Run workflow**. Watch the log:

- `Market gate` prints either *"already traded"* (the marker says today is done)
  or *"open, N min to close"*
- `Rebalance` only runs when the gate says yes

To force a real run today, delete the date in `state/last-trade.txt`, commit,
and run the workflow again outside the gate window — it will still refuse,
because the gate also checks the clock. That is the point.

## Pausing

| Want | Do |
|---|---|
| Pause ETF only | Actions → `paper-rebalance` → ⋯ → Disable workflow |
| Pause everything | Disable both workflows |
| Halt trading, keep the schedule | create `python/output/KILL_SWITCH`, commit, push |
| Resume | delete that file, commit, push |

## The bill

Private repos get 2,000 free Actions minutes per month. These workflows use
roughly **450–550** (268 runs × ~2 minutes). Public repos are unlimited, but
then your strategy is public — harmless, and your keys are not in the repo.

## Limits worth knowing

- **GitHub can run late or skip a slot.** The four-slot design absorbs this for
  the ETF book. A skipped crypto slot is corrected on the next run four hours
  later.
- **Scheduled workflows are disabled after 60 days with no repository activity.**
  The marker commit on every trading day keeps the repo active, so this only
  becomes a risk if the bot stops trading for two months.
- **Runners are ephemeral.** No logs survive on the machine; use the Actions
  run history. It is searchable and keeps months of runs.
- **The repo is the state.** If you reset it, the bot forgets it already traded
  today and may re-submit. `execute.py` recomputes from live positions, so a
  duplicate is a no-op rather than a double position.
