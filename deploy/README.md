# Deploy notes

The system runs entirely on this machine — there is **no public endpoint
anymore**. TradingView and the HTTP webhook/tunnel were removed: the signal
engine (`sigengine/engine.py`) generates every trade signal and routes it
through `relay.order_manager.handle()` **in-process**, so nothing needs to be
exposed to the internet. Alpaca is the only outbound connection (order
submission + paper data).

## Run it

```bash
./deploy/run_local.sh
# starts the signal engine in a detached tmux session "alpaca" (window: engine)
tmux attach -t alpaca        # view logs (Ctrl-b d to detach)
tmux kill-session -t alpaca  # stop
```

### After a reboot or a crash

Cron runs `deploy/ensure_up.sh` at boot and every 5 minutes. It starts IB Gateway,
the engine (tmux `alpaca`) and the dashboard (tmux `dashboard`) with the launchers
above, but only the ones that are not running; on a healthy box it does nothing.
Its log is `agent/ensure_up.log`. `./deploy/ensure_up.sh --check` prints what is up.

Because of it, a stop by hand lasts only until the next pass. To keep something
down on purpose, hold it first:
```bash
mkdir -p ~/.config/alpaca/hold
touch ~/.config/alpaca/hold/engine     # or gateway, dashboard, all
rm ~/.config/alpaca/hold/engine        # release; back within 5 min
```

Logs and generated data land on the big disk under `$ALPACA_DATA_DIR`
(default `/mnt/sandisk/alpaca`): `logs/sigengine.log`, `journal/trades.jsonl`,
`bars/bars.sqlite`, `backtest/*.csv`, `sigengine/state.json`. See the main
README's "Data storage" section.

For a safe first look that never places orders:
```bash
PYTHONPATH=. .venv/bin/python -m sigengine.engine --once --dry-run
```

## IBKR (IB Gateway)

IBKR is the engine's primary bar source. `deploy/run_ibgateway.sh` starts IB
Gateway (paper, data only) headlessly via IBC, and the engine falls back to
yfinance whenever the Gateway is down. It requires the Market Data API
Acknowledgement signed in Client Portal. See "IBKR data (IB Gateway)" in the
main README.

**2FA**: no secret is ever stored on disk to auto-answer it. `deploy/ibc.ini`
uses IBC's `AutoRestartTime` (daily warm restart, reuses the session, no
re-auth) + `ColdRestartTime` (Sunday only, full re-login — IBKR invalidates
all security tokens weekly regardless of tooling, so this is the one
unavoidable manual step: tap "approve" on your phone once a week, at a time
you choose). See the comments in `deploy/ibc.ini` for the exact mechanism and
how to set `ColdRestartTime` correctly for your timezone.

## Scheduling the Claude agents

The research/review agents are independent of the engine and run from cron.
Every line goes through `cron/at_et.sh`, so it fires at New York wall-clock
time whatever the machine's own time zone (the schedule is the main README's
"Research cadence"):
```
0 * * * *  cd <repo> && ./cron/at_et.sh 10:00 1-5 ./agent/run_research_intraday.sh >> agent/research_intraday.log 2>&1
```
Install the backtest + IBKR extras in the venv too (`pip install -e ".[backtest,ibkr,dev]"`);
the agents themselves run through the `claude -p` CLI and need no Python extra.

## Monitoring

- A down engine = silently missed signals (the engine does not retry a missed
  poll, but the next poll re-diffs from the last closed bar, so a brief outage
  only loses signals that crossed during the gap). Uptime is the main concern.
- Watch `$ALPACA_DATA_DIR/journal/trades.jsonl` for `order_error` spikes.
- `python -m relay.check_account` confirms Alpaca connectivity.

## Security checklist

- `.env` is never committed (in `.gitignore`). Real keys live there only.
- No inbound ports need to be open — the machine has no public endpoint.
- Keep `PAPER_TRADING=true` until paper is proven; only flip to `false` to go live.