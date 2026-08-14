# Tempest

Tempest is a US microcap momentum research and Alpaca PAPER-trading system. It
contains two fixed-rule strategies that share one broker account, one lifecycle
journal, and one set of account-level risk controls. The repository has no
live-money mode.

## Strategies

### Tempest first pullback

Candidate selection requires:

- relative volume of at least 5x and no more than 50x;
- cumulative volume of at least 1,000,000 shares;
- a gap of at least 2%;
- price between $2 and $20; and
- float below 20,000,000 shares.

The entry pattern is a multi-candle advance followed by a shallow pullback that
holds VWAP and the 9 EMA, then a completed one-minute close through the prior
candle's high. The stop is the pullback low and the target is 2R.

### Gale ORB5

Gale uses the same timestamped candidate universe and requires:

- a complete 09:30-09:34 ET opening range;
- a completed one-minute close above the range high and VWAP;
- breakout volume of at least 1.5x the preceding five-bar median;
- an opening-range width no greater than 5%; and
- no more than 0.5% entry chase.

Entries are permitted from 09:35 through 11:00 ET. The stop is the range low,
the target is 2R, and the default horizon is 15 bars.

## Execution and risk controls

Both strategies execute only through Alpaca PAPER:

- `TEMPEST_PAPER=1` is mandatory;
- the Alpaca client is always constructed with `paper=True`;
- entries are DAY limit brackets with broker-side stop-loss and take-profit
  legs;
- Alpaca's clock is authoritative for market-open, session, completed-bar, and
  near-close decisions;
- missing, malformed, or unavailable clock/account/order state prevents new
  entries;
- only completed one-minute candles can produce an actionable signal;
- open positions and unresolved parent orders consume the shared position cap;
- partially filled parents remain pending until filled, canceled, expired, or
  rejected, while filled quantity remains recognized as exposure;
- Alpaca account P&L, including unrealized P&L, controls the daily loss gate;
- default limits are three exposure slots, $1,000 notional per position, $50
  stop risk per position, $200 account daily loss, and a one-hour symbol
  cooldown; and
- `localdata/HALT_TRADING.flag` disables both strategies.

The shared lifecycle journal is `localdata/trade_journal.csv`. Writes are
atomic, and present-but-unreadable journal or cooldown state stops the pass.
Every row includes a `strategy_id`: `tempest_first_pullback` or `gale_orb5`.

## Install

Python 3.12 or later is recommended.

```bash
git clone --branch main --single-branch https://github.com/6ixtyn9-sudo/Tempest.git
cd Tempest
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp -n .env.example .env
${EDITOR:-vi} .env
set -a
source .env
set +a
PYTHONPATH=src python -m pytest -q
```

Use Alpaca PAPER key material only in `.env`:

```dotenv
ALPACA_API_KEY=your_paper_key_id
ALPACA_SECRET_KEY=your_paper_secret
TEMPEST_PAPER=1
```

`.env` is ignored by Git. Do not place credentials in tracked files, commands,
or workflow definitions.

## Local operation

Activate the environment and load configuration in each new shell:

```bash
cd Tempest
source .venv/bin/activate
set -a
source .env
set +a
```

Run one pass:

```bash
PYTHONPATH=src python scripts/paper_trade.py --dry-run
PYTHONPATH=src python scripts/gale_paper_trade.py --dry-run
PYTHONPATH=src python scripts/paper_trade.py
PYTHONPATH=src python scripts/gale_paper_trade.py
```

Other operational commands:

```bash
# Screen and collect bars
PYTHONPATH=src python scripts/screen_market.py --fetch-bars

# Generate strategy reports
PYTHONPATH=src python scripts/run_backtest.py
PYTHONPATH=src python scripts/run_gale_backtest.py

# Attribute journal P&L by strategy and symbol
PYTHONPATH=src python scripts/attribute_pnl.py

# Halt both strategies
mkdir -p localdata
touch localdata/HALT_TRADING.flag

# Resume after confirming account and order state
rm -f localdata/HALT_TRADING.flag
```

## GitHub Actions deployment

The repository contains two manually dispatched workflows:

- `paper_poll.yml` runs Tempest and then Gale during 09:30-16:00 ET;
- `daily_capture.yml` collects screen/bar data and generates reports in its
  configured open/close windows.

Both workflows use the `tempest-paper` concurrency group, restore persistent
`localdata`, commit lifecycle records to `main`, and report a failure when
broker or lifecycle state is unavailable.

Install and authenticate GitHub CLI, then configure PAPER secrets without
putting their values in shell history:

```bash
gh auth login
gh secret set ALPACA_API_KEY --repo 6ixtyn9-sudo/Tempest
gh secret set ALPACA_SECRET_KEY --repo 6ixtyn9-sudo/Tempest
gh secret list --repo 6ixtyn9-sudo/Tempest
```

Deploy the current `main` and verify the repository state:

```bash
git checkout main
git pull --ff-only origin main
git status --short --branch
git push origin main
gh repo view 6ixtyn9-sudo/Tempest --json defaultBranchRef
```

Dispatch and inspect each workflow:

```bash
gh workflow run paper_poll.yml --repo 6ixtyn9-sudo/Tempest --ref main
sleep 3
PAPER_RUN_ID="$(gh run list --repo 6ixtyn9-sudo/Tempest --workflow paper_poll.yml \
  --branch main --limit 1 --json databaseId --jq '.[0].databaseId')"
gh run watch "${PAPER_RUN_ID}" --repo 6ixtyn9-sudo/Tempest --exit-status

gh workflow run daily_capture.yml --repo 6ixtyn9-sudo/Tempest --ref main
sleep 3
CAPTURE_RUN_ID="$(gh run list --repo 6ixtyn9-sudo/Tempest --workflow daily_capture.yml \
  --branch main --limit 1 --json databaseId --jq '.[0].databaseId')"
gh run watch "${CAPTURE_RUN_ID}" --repo 6ixtyn9-sudo/Tempest --exit-status
```

GitHub Actions scheduling is intentionally not defined in this repository.
An external scheduler may dispatch `paper_poll.yml` every five minutes during
US regular trading hours. Each workflow independently rejects weekends and
out-of-window execution.

A direct workflow-dispatch request has this form:

```bash
export GITHUB_TOKEN="$(gh auth token)"
curl --fail-with-body --silent --show-error \
  --request POST \
  --header "Accept: application/vnd.github+json" \
  --header "Authorization: Bearer ${GITHUB_TOKEN}" \
  --header "X-GitHub-Api-Version: 2022-11-28" \
  https://api.github.com/repos/6ixtyn9-sudo/Tempest/actions/workflows/paper_poll.yml/dispatches \
  --data '{"ref":"main"}'
unset GITHUB_TOKEN
```

## Runtime records

- `localdata/paper_status.csv` — Tempest pass outcomes;
- `localdata/gale_status.csv` — Gale pass outcomes;
- `localdata/trade_journal.csv` — shared order/fill lifecycle;
- `localdata/screen_log.csv` and `gale_screen_log.csv` — timestamped candidate
  observations; and
- dated Tempest and Gale JSON reports — point-in-time strategy results.

`state_failed` means the pass stopped because required broker or local state was
unavailable. `no_candidates`, `watching`, and an out-of-window skip are normal
non-entry outcomes.

## Repository layout

```text
src/tempest/
  broker.py          Alpaca PAPER account, clock, order and position adapter
  risk.py            shared limits, lifecycle journal, cooldown and halt flag
  trader.py          shared execution and reconciliation engine
  gale_trader.py     ORB5 specialization on the shared engine
  strategy.py        Tempest first-pullback rules
  gale.py            Gale ORB5 rules
  backtest.py        Tempest point-in-time simulation
  gale_backtest.py   Gale point-in-time simulation
  features.py        session, VWAP, EMA, gap and relative-volume features
  sources/           Alpaca, TradingView and historical-data adapters
scripts/              command-line entry points
.github/workflows/    PAPER poll and daily capture workflows
tests/                behavior-focused automated checks
```
