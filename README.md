# Alpaca Trading System

**An autonomous paper-trading system with a Claude research layer and a nightly
AI video recap.**

A self-hosted signal engine trades a watchlist of US large caps, a leveraged
Nasdaq ETF and bitcoin on an [Alpaca](https://alpaca.markets) **paper**
account, around the clock and with nobody at the keyboard. Every trading day
Claude, run headless through the Claude Code CLI, researches the watchlist and
decides which names the engine may buy and in what order of preference. The
engine decides *when*, from its own strategies on hourly bars, and a risk gate
written in plain code has the last word on every order. A small options sleeve
sells one cash-secured put a week. Every weekday evening a photoreal AI
presenter delivers an English video recap of what the book did.

> **Disclaimer.** This is a personal engineering project. It trades a **paper
> (simulated) account only**, and nothing in this repository or on its sites
> is investment advice, an offer, or a recommendation to buy or sell any
> security or asset. Simulated results are not indicative of future results.
> The project is **not affiliated with, endorsed by or sponsored by Alpaca,
> Interactive Brokers, Anthropic or Cloudflare**; their names appear only to
> describe the services the code talks to.

## How it fits together

```
 research crons (New York time)   claude -p · Alpaca MCP (read-only data tools) · web search
        │                         writes decisions/decisions.json, then a validator clamps it
        ▼
 sigengine/engine.py   1-hour bars: IBKR primary, yfinance fallback, SQLite bar cache
        │              per symbol: MACD + SMA-200 and Bollinger + RSI, the same code the backtests run
        ▼ in-process
 relay/ (a library)    risk gate: watchlist · bias · hours · size · daily-loss halt
        │              rotation by research priority · synthetic stops · trade journal
        ▼ alpaca-py
 Alpaca paper account ◄── relay/csp_manager.py: one cash-secured put a week, from its own reserve

 read-only views:  web/server.py         legacy local dashboard, 127.0.0.1:8787
                   web/publisher.py      ─► R2 ─► apps/dashboard  private, behind Cloudflare Access
                   web/public_export.py  ─► R2 ─► apps/portfolio  public static site
 briefing/         the weekday video recap and a Sunday cartoon edition
```

- **Signal engine** (`sigengine/`). Polls every 5 minutes, acts on closed
  1-hour bars only, runs each symbol's strategies and turns every new event
  into an order request for the relay. [Details](#signal-engine).
- **Relay** (`relay/`). Not a service: the engine calls it in-process. It
  holds the risk gate, the signal-driven rotation between candidates, the
  extended-hours order routing and the append-only journal
  (`trades.jsonl`). There is no HTTP endpoint and no webhook.
- **Research layer** (`agent/`). `claude -p` on a cron anchored to New York
  time, with web search and Alpaca's MCP server. The server is registered
  without its trading toolset, and each research cron's `--allowed-tools`
  list admits only read-only market-data and news tools from it (seven at
  most). It writes `decisions/decisions.json`: the
  watchlist, a bias and priority per symbol, and price-level contingencies.
  Code then clamps it to hard ceilings. [Details](#claude-research-layer-uses-the-claude-code-harness--no-api-key).
- **Options sleeve** (`relay/csp_manager.py`). One SOXL cash-secured put a
  week, sized from its own reserve (20% of equity); the share book sizes off
  what is left.
- **Video recap** (`briefing/`). After the close, Claude summarises the market
  day, and a language model turns that summary and the account's own numbers
  into a slide deck, a script and the YouTube title. One slide is a screenshot
  of the live dashboard, with its market candles and current prices.
  Qwen3-TTS voices the script, and LTX video (through ComfyUI) animates the
  presenter slide by slide, upscaled with NVIDIA RTX Video Super Resolution.
  An AI stylist dresses the presenter for the day's New York weather. A new
  look needs a face-identity check (OpenCV YuNet + SFace) and a human sign-off
  before it reaches the video; without them the presenter's locked look is
  used. The Sunday edition
  tells the week as a cartoon story with a fixed cast of ticker characters,
  rendered by MiniMax H3. See [`briefing/README.md`](briefing/README.md).
- **Dashboards.** The private cloud dashboard (`apps/dashboard`, fed by
  `web/publisher.py`) is described in
  [`docs/cloud-dashboard.md`](docs/cloud-dashboard.md). The public portfolio
  (`apps/portfolio`, fed by `web/public_export.py`) is described in
  [`docs/public-portfolio.md`](docs/public-portfolio.md). Both are read-only
  and push-only: the cloud never calls the trading machine, and broker keys
  never leave it.

## Tech stack

- **Trading path (Python 3.11+):** alpaca-py, ib_async (IB Gateway driven by
  IBC), yfinance, pandas, NumPy, pydantic, SQLite. The local dashboard is the
  standard library's `http.server`; boto3 uploads to R2. Tested with pytest,
  linted with ruff.
- **Decision layer:** the Claude Code CLI in headless mode (`claude -p`), the
  Alpaca MCP server (no trading toolset; the crons allow only its read-only
  data tools), cron with a New York-time wrapper (`cron/at_et.sh`).
- **Web:** TypeScript, React 19, Vite, Tailwind CSS and TradingView
  Lightweight Charts, on Cloudflare Workers (Static Assets), R2 and Access;
  wrangler, pnpm, Vitest, oxlint.
- **Video:** ComfyUI, LTX video, MiniMax H3, Qwen3-TTS, Gemma models served
  by Ollama (scripting and visual QC), OpenCV YuNet + SFace (identity checks),
  NVIDIA RTX Video Super Resolution, ffmpeg, and HTML/GSAP slides rendered
  headless.
- **Delivery:** GitHub Actions (pytest, ruff and a gitleaks scan of the whole
  history; lint, typecheck, test, build and deploy for each app) and
  Dependabot.

## This repository vs. the working install

This repo is the **publishable copy** of the system, started with fresh history on
2026-11-26. The working install on the trading desktop keeps its full private
history and a set of files that are deliberately never pushed (see the
"LOCAL-ONLY BY DESIGN" block in `.gitignore`):

- secrets: `.env`, its backups and every other `.env.*` variant except
  `.env.example`, `briefing/.youtube/` (OAuth)
- presenter assets (the locked plates, the voice clips, the persona file and
  design notes): `briefing/assets/host2/`, `briefing/host2.json`,
  `briefing/HOST2*.md`
- personal research and notes: `decisions/*.md`, `agent/lessons.md`
- per-machine state: `.claude/settings.local.json`, cron backups

Account data never enters the repository either: the journal, bar cache, logs
and state live under `$ALPACA_DATA_DIR`, and the live `decisions.json` is
written by the research crons on the trading machine. Model weights for the
video stack are installed separately.

The presenter is whichever persona file `briefing/config.json` `host.host_json`
names (`host2.json`); there is no fallback presenter, so without that local
file the recap stops rather than rendering anyone else.

A fresh clone therefore runs the trading path and the full test suite (CI does
exactly that, with dummy keys; the presenter tests skip without `host2.json`),
but not the presenter recap, which needs those local assets plus the GPU stack.
CI: `.github/workflows/ci.yml` (pytest + ruff on the trading path + a gitleaks
scan of the whole history).

## Copyright

Copyright © 2026 zhen-qian101. All rights reserved.

No licence is granted. You are welcome to read the source on GitHub, and to
view and fork it there as GitHub's Terms of Service provide, but it may not
otherwise be copied, modified, distributed or used without written
permission. Third-party files committed here (a charting library, fonts, two
face models, and the Cloudflare and Microsoft runtime type definitions in
`apps/dashboard/worker-configuration.d.ts`) keep their own licences; see
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

---

The rest of this file is the operator's manual for the working install.

## Manual setup (do once)

1. **Alpaca paper account:** sign up at [app.alpaca.markets](https://app.alpaca.markets), open the **Paper Trading** tab, generate an API key + secret. Paper accounts start with $100k.
2. **Python env:**
   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -e ".[backtest,ibkr,dev]"
   ```
3. **Configure secrets:** `cp .env.example .env` and fill in `ALPACA_KEY` and `ALPACA_SECRET`. (`WEBHOOK_SECRET` is deprecated and unused — the relay has no HTTP endpoint anymore. `ALPACA_DATA_DIR` defaults to `/mnt/sandisk/alpaca`.)
4. **Data disk:** make sure the disk that holds `ALPACA_DATA_DIR` is mounted. The engine creates `ALPACA_DATA_DIR` and its subdirs on first run; nothing to do by hand.
5. **Verify Alpaca connectivity:**
   ```bash
   python -m relay.check_account
   ```

## Running the signal engine

Detached, via tmux (survives shell exits; re-runnable):
```bash
./deploy/run_local.sh
# prints the tmux session name + data-dir paths
tmux attach -t alpaca        # view logs (Ctrl-b d to detach)
tmux kill-session -t alpaca  # stop (cron restarts it within 5 min, see below)
```

Cron runs `deploy/ensure_up.sh` at boot and every 5 minutes and restarts the engine,
the dashboard and IB Gateway if any of them is down (see deploy/README.md). To keep
one down on purpose, hold it before stopping it:
`mkdir -p ~/.config/alpaca/hold && touch ~/.config/alpaca/hold/engine` (or `dashboard`,
`gateway`, `all`); `rm` the file to release.

Safe first look (compute signals, never place orders):
```bash
PYTHONPATH=. .venv/bin/python -m sigengine.engine --once --dry-run   # one poll, no orders
PYTHONPATH=. .venv/bin/python -m sigengine.engine --dry-run           # daemon, never orders
```

## Web dashboard

A read-only terminal for the running system — account equity/day P&L with the
daily-halt meter, open positions with their synthetic stop distance, the
decisions.json rotation board, per-symbol candles with the *real* strategy
overlays and engine entry/exit markers, and the trade journal + per-symbol
engine liveness.

```bash
./deploy/run_dashboard.sh          # http://localhost:8787  (tmux session "dashboard")
tmux kill-session -t dashboard     # stop (back within 5 min unless ~/.config/alpaca/hold/dashboard exists)
```

This legacy local dashboard binds to loopback (`127.0.0.1:8787`) for use on the
trading machine itself; the [cloud dashboard](#cloud-dashboard-publisher)
replaces any public exposure of it.

It is a **viewer over the trading path, by construction**: `web/server.py` never
imports `relay.order_manager` and no route reaches the broker, so the page
cannot place, size or cancel an order. `tests/test_web_server.py` locks that,
plus the token gate and static-root confinement. Stdlib `http.server` only — no
FastAPI/uvicorn is reintroduced into the trading path.

It is not literally method-read-only any more. One write route exists —
`POST /api/look/approve`, which signs off the presenter's outfit for a date (see
[Look review](#look-review) below) — and its entire reach is one boolean in one
cosmetic JSON file. `PUT`/`DELETE`/`PATCH` remain absent and `POST` 404s on
every other path, both pinned by tests.

**Broker request budget.** The dashboard reads Alpaca with the same key the
engine trades with, and Alpaca allows that key about 200 requests a minute in
total. The broker-backed panels are cached for 5 seconds (60 seconds after an
error). The executions panel keeps the closed orders it has already read: a refresh asks
only for orders submitted since the newest one it holds (one request,
normally), starting 15 minutes earlier to catch late fills, and the whole
history is read again at most every 10 minutes
(`tests/test_executions_cache.py`).

### Look review

`/look.html` shows the day's outfit and stage keyframe, and is where they get
signed off. `briefing/host_briefing.day_keyframe` renders a stylist look **only**
when its record carries `"approved": true`; an unapproved look falls back to the
host's locked keyframe exactly as a look that failed its identity gates does, so
review can never cost a video — the worst case is the presenter in yesterday's
shirt. The stylist runs at 09:35 ET and the render at 20:00 ET, which is the
window this page exists to fill.

## Signal engine

`sigengine/engine.py` is a self-hosted daemon that generates the trade signals
and routes them straight into the relay **in-process** — no TradingView, no
webhook, no manual alert setup. On every poll it runs each symbol's
`strategies` through `backtest.multi_strategy.run_symbol_strategies`, the
**same** strategy code the backtests run, over a sliding window of live bars,
**diffs** the resulting order events against the last bar it has already
accounted for, and translates each fresh event into an `OrderRequest` handed to
`relay.order_manager.handle()` — so every signal still goes through the full
risk gate (allowlist / bias / market-hours / sizing / daily-loss), the
signal-driven rotation, and the `trades.jsonl` journal. Sharing the strategy
code between research/backtest and live trading guarantees the signal logic
cannot drift.

- **Bars:** 1-hour bars for every symbol, polled every 5 minutes
  (`poll_seconds: 300`). IBKR (`ib_async`, through IB Gateway) is the primary
  source and yfinance the automatic fallback (the `data` block in
  `sigengine/config.json`); crypto has its own `crypto_source` knob. The last
  row is the in-progress candle, so the engine acts on **closed** bars only.
  Every fetched batch is upserted into the SQLite bar cache
  (`relay.bars_db`), so warm-up is instant on restart.
- **Warm-up:** on the first poll per symbol, the engine records the last closed
  bar and fires nothing — pre-existing signals that formed before it started do
  not fire. State persists to `ALPACA_DATA_DIR/sigengine/state.json` so a restart
  does not re-fire history.
- **Event → order:** `buy` sizes from `deploy × (equity − the options sleeve's
  reserve) / price` (whole shares for equities, fractional for crypto); `sell`
  and `close` are the SAME act — both liquidate the whole real position, the
  broker sizing it. This book is all-in/all-out: the signals hold one
  position at a time, entered and exited in one piece. Shares the put sleeve
  was assigned sit outside that rule (`protected_symbols`): the rotation never
  sells them, and the sleeve sells them at its next weekly run.
  `reason=="eod"` events are ignored — that is a backtest-window artifact, not
  a live signal.
- **Config:** per-symbol `strategies`/`interval`/`deploy`/`stop`/`enabled` in
  `sigengine/config.json` (the flat `fast`/`slow`/`window` keys only draw the
  reference EMA overlay on the dashboard chart). `enabled=false` skips a
  symbol entirely; the relay's `bias=flat` is a **separate** layer that blocks
  buys on non-candidates (expected `risk_block` journal lines, not errors).

To run the engine in the foreground instead of under `deploy/run_local.sh`
(hold and stop the supervised one first — see "Running the signal engine"):
```bash
PYTHONPATH=. .venv/bin/python -m sigengine.engine --log-level INFO   # logs to stdout
PYTHONPATH=. .venv/bin/python -m sigengine.engine --once --dry-run   # one poll, no orders
```
Under `deploy/run_local.sh` the same output is tee'd to
`$ALPACA_DATA_DIR/logs/sigengine.log` (default `/mnt/sandisk/alpaca/logs/sigengine.log`).

Tests: `.venv/bin/python -m pytest -q` (through `python -m`, so the repository
root is on `sys.path`).

## Data storage (on /mnt/sandisk)

Generated data lives on the big disk, not the repo, so the bar cache / journal /
logs / backtest CSVs don't grow the project dir on the NVMe. Controlled by
`ALPACA_DATA_DIR` (default `/mnt/sandisk/alpaca`):

```
$ALPACA_DATA_DIR/
  bars/bars.sqlite        # shared SQLite bar cache (relay.bars_db), UTC-normalized
  journal/trades.jsonl    # every order decision (order_ok / risk_block / rotation_close / order_error)
  logs/sigengine.log      # engine log
  backtest/*.csv          # backtest CSV output
  sigengine/state.json    # last processed bar per symbol (warm-up bookkeeping)
```

Code + venv + `decisions/decisions.json` stay in the repo on the NVMe. The
SQLite cache is shared by the engine (writer) and the backtester (`source="cache"`
reader) for fast, rate-limit-free sweeps.

## Backtesting

The engine runs `backtest.multi_strategy.run_symbol_strategies`, which
dispatches to the strategy modules in `backtest/` (`macd_sma.py`,
`bollinger_rsi.py`, and the legacy `ema_scaled_rolling.py`), so backtest ==
live. The command-line sweep tool is the legacy EMA strategy's:

```bash
# Scaled-exit EMA-cross, rolling N-bar close extremes. Sweeps fast/slow x window.
.venv/bin/python -m backtest.ema_scaled_rolling --symbol AAPL --sweep
.venv/bin/python -m backtest.ema_scaled_rolling --symbol BTC/USD --interval 1h --sweep-windows 50,100,150,200,250,300

# Read bars from the SQLite cache (fast, no network) instead of yfinance:
.venv/bin/python -m backtest.ema_scaled_rolling --symbol AAPL --source cache
```

`load_ohlcv` sources: `yfinance` (default, free), `alpaca` (paid SIP plan),
`cache` (SQLite via `relay.bars_db`), `ibkr` (IBKR via `sigengine.ibkr`).

## Strategy & single-position policy

Every enabled symbol runs two independent strategies on 1-hour bars (the
`strategies` list in `sigengine/config.json`, each symbol with its own
grid-searched parameters):

- **MACD + SMA-200** (`backtest/macd_sma.py`): a trend follower, adapted from
  ChartArt's public "MACD + SMA 200 Strategy" script. It buys when the MACD
  histogram crosses up through zero with MACD positive, the fast average above
  the slow one, and the script's 200-bar SMA filter passing.
- **Bollinger + RSI** (`backtest/bollinger_rsi.py`): a rebound trade, adapted
  from ChartArt's public "RSI + Bollinger Bands, Double Strategy" script. It
  buys when RSI crosses above 50 on the same bar that price crosses back above
  the lower band.

Whichever strategy's entry fires first takes the symbol's slot, and an exit
from either strategy's own rules closes the position (OR-exit). Both run
long-only: the originals' short signals mean "exit to flat" here.

- **Entry size:** a buy deploys `deploy` (97%) of the *share book*, which is
  equity less the options sleeve's reserve: `reserve_frac` in
  `decisions/csp.json` (20%) while the sleeve is enabled, or the collateral
  actually pledged if that is larger. With the sleeve on, a buy is therefore
  about 78% of equity; with it off, 97%.
- **Stop:** a synthetic stop the engine checks at bar close (`stop` per
  symbol: 2%, or 3% for the leveraged ETFs). It is measured from each
  strategy's own simulated entry, not the broker's fill, and its close sells
  whatever the account holds in the symbol; `docs/public-portfolio.md` spells
  out what that means for a position. There is no resting stop order at the
  broker, and no take-profit: positions leave on a strategy exit, the stop or
  a rotation.
- **Legacy EMA cross:** the original scaled-exit EMA-cross strategy
  (`backtest/ema_scaled_rolling.py`) remains for backtests and comparison, but
  no configured symbol trades it. Were it re-enabled, its half exit would
  execute as a FULL exit, since the book is all-in/all-out.

`decisions/decisions.json` sets `max_open_positions: 1` and `max_position` per
symbol high enough (10000 for equities; ~10 for crypto in base units) that a
full-balance entry isn't blocked. Claude's research sets `bias=long` on
**multiple** candidates (each with a `priority`, higher = more preferred) and
`flat` on the rest — the system watches the whole list but still holds **one**
stock from the signals at a time (plus any shares the put sleeve was assigned,
which the sleeve itself sells).

**Crypto (BTC/USD):** Alpaca crypto pairs are tradable, fractional, not
shortable, 100% margin (cash-only), and trade **24/7**. The relay's risk gate
exempts crypto symbols (`BTC/USD`, `BTCUSD`, `BTC`) from the equity market-hours
check, so a BTC buy signal is allowed any hour. `max_position` for crypto is in
base units (BTC), not shares.

**Rotation is signal-driven, not research-driven.** The relay
(`relay/order_manager.py`) rotates only when:
- a **higher-priority** candidate's buy signal fires while holding a
  lower-priority name → sell the old, buy the new (`rotation_close` journal
  event); **or**
- the held stock's own exit (a strategy exit or its stop) fires → exit
  naturally, then the next candidate's buy signal enters.

Lower/equal-priority buy signals while holding are blocked (no churn). When
flat, the first candidate's buy signal to fire enters. Re-running research and
re-ranking priorities does **not** itself trade — so `agent/rotate.py` is a
**status-only audit** (it prints the held position vs. the ranked candidates and
places no orders). Edge case: if the held symbol is pruned from the watchlist
entirely, it is NOT force-closed — it stays held until its own chart fires an
exit signal (flatten manually if you want out sooner).

## Data sources (where does live data come from?)

| Process | Live data source | Notes |
|---|---|---|
| **Signal path** (sigengine → relay) | **IBKR** (`ib_async`) primary, **yfinance** automatic fallback | Equities try IBKR first; if the Gateway is down or a symbol isn't subscribed, the engine falls back to yfinance for that poll so trading never stops. Crypto uses yfinance/Alpaca (IBKR crypto is Paxos-limited). Every fetched batch is cached in the SQLite bar cache. |
| **Backtest** (`backtest/`) | **yfinance** / **cache** / **alpaca** / **ibkr** | `source="cache"` reads the SQLite bar cache (fast, no network); the engine fills it. Cache-sourced results match yfinance-direct exactly. |
| **Claude research** (`agent/run_research.sh`) | **Alpaca MCP** + WebSearch | Pulls structured snapshots/news/movers via the Alpaca MCP tools, plus web search for macro/analyst context. |
| **Order execution** (relay → Alpaca) | n/a | Just submits orders; Alpaca is the broker. |

Alpaca's market-data API (snapshots, bars, quotes, trades, **news from Benzinga**, market movers, corporate actions, options, crypto, indices) is available to your paper keys and is exposed to Claude via the **Alpaca MCP** (below). Alpaca has no dedicated sentiment endpoint — Claude reads the news articles from `get_news` and scores sentiment itself.

## Alpaca MCP (data tools for the Claude harness)

The official [alpacahq/alpaca-mcp-server](https://github.com/alpacahq/alpaca-mcp-server) is registered with Claude Code at **user scope** with every toolset except `trading`, so no session can place an order through it; all orders go through the relay and its risk gate. The registration is not read-only as a whole: the `account` and `watchlists` toolsets include tools that change account settings and create, edit or delete watchlists. The unattended research crons therefore never get the whole server. Each `claude -p` call names the tools it may use in `--allowed-tools`, and the Alpaca tools on those lists are read-only market-data and news tools: seven in `agent/run_research.sh` (`get_stock_snapshot`, `get_stock_bars`, `get_crypto_snapshot`, `get_crypto_bars`, `get_news`, `get_market_movers`, `get_most_active_stocks`), at most seven in the others. To add it on a new machine:

```bash
claude mcp add alpaca --scope user \
  -e ALPACA_API_KEY="$(grep '^ALPACA_KEY=' .env | cut -d= -f2-)" \
  -e ALPACA_SECRET_KEY="$(grep '^ALPACA_SECRET=' .env | cut -d= -f2-)" \
  -e ALPACA_PAPER_TRADE=true \
  -e ALPACA_TOOLSETS=stock-data,crypto-data,options-data,news,account,watchlists,assets,corporate-actions,index-data \
  -- /path/to/.venv/bin/alpaca-mcp-server
```

Verify with `claude mcp list` (look for `alpaca ... ✔ Connected`) and `/mcp` in the CLI.
An interactive Claude Code session sees every registered tool, including the account and watchlist tools that write; the research crons can call only the read-only tools their `--allowed-tools` lists name. The research prompt (`agent/research_prompt.md`) instructs Claude to use them for price and news facts.

## Claude research layer (uses the Claude Code harness — no API key)

The decision engine runs the **Claude Code CLI headless** (`claude -p`) with the
WebSearch, WebFetch, Read and Write tools plus the read-only Alpaca MCP tools
named [above](#alpaca-mcp-data-tools-for-the-claude-harness), so it uses your
existing Claude Code auth — **no
`ANTHROPIC_API_KEY` needed**. Claude web-researches the watchlist and writes
`decisions/decisions.json`; a validator then clamps any over-limit values to the
hard ceilings, and the relay's risk gate loads it as the trade allowlist/bias.

```bash
# Daily research (run ~15 min before market open) — full: mines new candidates,
# prunes broken theses, picks ONE best long:
./agent/run_research.sh                                 # default 9-symbol list
./agent/run_research.sh GOOG,NVDA,MSFT,AMZN,TSLA,AAPL,META,SOXL,TQQQ

# Intraday refresh — re-picks the single best long from the existing
# watchlist; does NOT mine/prune the list:
./agent/run_research_intraday.sh

# Weekly journal review (reads trades.jsonl -> agent/lessons.md):
./agent/run_journal_review.sh 7
```

### Research cadence

| When (ET) | What | Script |
|---|---|---|
| **09:15**, Mon–Fri (pre-market) | Full rebuild: watchlist mining/pruning + multi-candidate longs with priorities | `run_research.sh` |
| **10:00 / 12:30 / 15:00**, Mon–Fri | Light refresh: re-rank the long candidates + priorities from the existing list | `run_research_intraday.sh` |
| **18:00**, Mon–Fri (before the overnight book) | Same refresh, told it is writing permissions for a ~13h unattended window | `run_research_intraday.sh` |
| **Sat 11:00** | Review trades.jsonl, write lessons.md, sanity-check the watchlist | `run_journal_review.sh` |

The research layer decides *which* symbols and their *priority*; the signal
engine decides *when*. Rotation is **signal-driven** — the relay rotates out of
the current holding only when a higher-priority candidate's buy signal fires (or
the held name's exit fires), so re-ranking priorities in research does not itself
trade. Each run finishes with `agent/rotate.py`, a **status-only audit** that
prints the held position against the ranked candidates and places no orders.

**Why the 18:00 run exists.** Bias refreshes on a schedule; the engine fires on
any 1h bar, including pre-market, after-hours and the overnight book. A buy that
arrives against a stale `flat` is refused by the gate — and it is edge-triggered,
so it is never retried. 26 buys have been refused that way and 12 landed outside
the research window entirely; on 2026-07-29 one of them was MSFT, which closed
+15.51% the next session. Two mechanisms close that gap, both fed by
`agent/live_state.py`:

- `blocked_since_last_run` — every refusal since the last run, which the prompts
  require the agent to judge (right call, or set long / write a contingency).
- `near_trigger_symbols` — symbols the engine's own gates say could fire on the
  next bar; leaving one `flat` requires a contingency or a written reason.

A contingency is the only thing that can act while nobody is awake: it
pre-authorizes a specific price level with an expiry, which the risk gate
evaluates mechanically at signal time with no LLM call.

### Does the research run automatically?

**Yes — installed and running.** Every job is anchored to NEW YORK wall-clock
time through `cron/at_et.sh`, not to local time: the trading machine is not on
US Eastern time, and its offset to ET changes by up to two hours across the year
depending on which side of each zone's DST switch you are on, so a fixed local
time drifted in ET. Cron fires the wrapper hourly at the target minute and the
wrapper decides whether this is the moment (see its header for the drift this
replaced).

```bash
crontab -l                     # verify — every line goes through cron/at_et.sh
```

```
0 * * * * cd <repo> && ./cron/at_et.sh 10:00 1-5 ./agent/run_research_intraday.sh >> agent/research_intraday.log 2>&1
50 * * * * cd <repo> && ./cron/at_et.sh 18:00 1-5 ./agent/run_research_intraday.sh >> agent/research_intraday.log 2>&1
```

The weekday spec is evaluated in ET too, which removes the other trap: on a
machine ahead of New York, a US session's afternoon falls on the NEXT local
calendar day, so a plain `1-5` in local time silently dropped two of every
Friday's three intraday runs.

Heads-up: `claude -p` must work non-interactively under cron (it uses your stored
Claude Code auth, no TTY). Run each script once by hand first and check the
`agent/*.log` files to confirm headless auth works before relying on cron.

The relay (`relay/risk_filter.py`) re-clamps everything in code, so Claude can
only loosen within hard ceilings — it can never raise limits past
`max_position=10000` or `daily_loss_limit=0.05`. (`suggested_size` is not a lever at all: the book is all-in/all-out, so it is derived from `bias` as the engine's deploy fraction and any `size_cap` on a contingency is stripped.)

## IBKR data (IB Gateway)

`ib_async` is the primary bar source for the signal engine, with yfinance as
the automatic fallback (with the Gateway down, every symbol falls back to
yfinance and keeps trading). To set IBKR up on a new machine (one-time):
install IB Gateway + IBC, run `./deploy/run_ibgateway.sh` (needs
`TWS_USERID`/`TWS_PASSWORD` in `.env`), sign the Market Data API
Acknowledgement in Client Portal and confirm the exchange subscriptions, then
verify with `PYTHONPATH=. .venv/bin/python -m sigengine.check_ibkr` (exit 0 =
IBKR usable as primary). The connection is data-only (`ReadOnlyApi` in
`deploy/ibc.ini`); orders always go to Alpaca.

**Paper only.** `PAPER_TRADING` defaults to `true`, and this project runs on an
Alpaca paper account.

## Cloud dashboard publisher

`web/publisher.py` feeds the cloud dashboard (`apps/dashboard`, a Cloudflare
Worker; architecture, R2 contract and the step-by-step Cloudflare setup are in
[`docs/cloud-dashboard.md`](docs/cloud-dashboard.md)). The flow is **push only**. The publisher runs on this box, calls the
same read-only builders `web/server.py` serves, and uploads their output to an
R2 bucket. The cloud never calls this box, and broker keys never leave it. The
local dashboard on :8787 keeps running unchanged, because the recap renderer
screenshots it.

Each cycle runs these steps in order, and a failure in one does not stop the
others:

1. Apply new look approvals from `approvals/<date>.json`. Each distinct object
   is applied once, through `set_look_approval`. Malformed or stale requests are
   never applied.
2. Upload `snapshot/latest.json`. If one source fails (for example, the broker
   is unreachable), only its own section becomes `{"error": ...}`. Besides the
   `web/server.py` builders it carries the put sleeve (`options`, built by the
   read-only `web/options_view.py`) and the outcome of the last approval
   request (`publisher.approval`). Each engine symbol is marked `enabled` from
   `sigengine/config.json`, so a symbol the engine no longer polls shows as
   "disabled — not polled" instead of as hours of lag.
3. Upload the session date's look images to `look/<date>/{plate,keyframe}.png`.
4. Upload `chart/<SYMBOL>.json` for each enabled symbol, at most every 5
   minutes. Chart times are true UTC, not the local-time-shifted times the old
   page uses.
5. Sync media. The cloud dashboard plays each recap from YouTube, so for an
   edition whose English upload is public or unlisted this uploads only its
   English cover and `meta.json`, and lists it in `media/history.json` with
   its `youtube_id`. The English video goes up only as a fallback, for an
   edition still without an embeddable id 3 hours after its render, and is
   deleted again once an id appears. The Chinese (zh) cut stays on this box:
   it is archived and plays on :8787, but no zh file or field is ever
   uploaded, and per-scene `clip_*.mp4` files never are either. URLs carry a
   content hash (`/media/...?v=<hash>`), so a re-render with the same file
   names is still a new URL and no browser replays the old file from its
   cache. Each sync then deletes whatever else is under `media/<date>/` for
   the dates it keeps (zh files, replaced videos, editions gone from the
   archive), and hourly it deletes recaps older than the retention window.
   `docs/cloud-dashboard.md` (Recap videos) has the exact rules.

An object whose content has not changed is not uploaded again. There are two
exceptions. The snapshot is re-sent on every run, because its `as_of` time is
how the cloud page knows this box is alive (the page warns after 3 minutes).
`media/history.json` is re-sent at least daily, so the bucket's `media/`
lifecycle rule never expires it.
Upload state lives in `$ALPACA_DATA_DIR/publisher/state.json`. Overlapping runs
exit at once. Media has its own lock, so a long video upload never delays the
next snapshot.

Setup:

```bash
# the 'publish' extra, only for real R2 uploads: boto3, botocore, s3transfer and
# jmespath at the exact versions in requirements/constraints.txt (which also
# holds urllib3 and python-dateutil at the versions this venv already has)
.venv/bin/python -m pip install -c requirements/constraints.txt boto3
# .env: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY (an R2 API token
# scoped to this one bucket, Object Read & Write), optional R2_BUCKET
# (default alpaca-dashboard) and PUBLISHER_HOST_LABEL (default "home").
PYTHONPATH=. .venv/bin/python -m web.publisher --dry-run      # see what would be sent
PYTHONPATH=. .venv/bin/python -m web.publisher --once         # one real cycle
```

Suggested cron line. It is **not installed**, so add it yourself when you are
ready:

```cron
* * * * * cd <repo> && PYTHONPATH=. .venv/bin/python -m web.publisher --once >> agent/publisher.log 2>&1
```

Other modes:

- `--loop 60` keeps one process running instead of cron. Media then runs on a
  background thread, and the closed-order history behind the executions
  section is kept between cycles, so each cycle asks Alpaca only for new
  orders (a cron run is a new process and reads the history once per run).
- `--no-media` skips the archive.
- `--retention-days N` keeps N days of recaps in the bucket (default 40: about
  0.15 GB with the recaps on YouTube, and under 4 GB even if every one fell
  back to its video; see `docs/cloud-dashboard.md`, step 2, which also says
  how to set the bucket's lifecycle rule to match).
- `--force` re-sends everything.

Exit status is 0 on success, or when another run already holds the lock. It
is 1 if a step failed and 2 if the configuration is missing.

**Local development without R2.** `--out-dir PATH` writes the exact bucket
layout into a folder. It needs no boto3, no credentials and no network beyond
what the builders already use. Approvals are read from `PATH/approvals/`, which
is how the Worker's approve flow can be tried end to end. Media is off by
default in this mode, because it would copy the covers, and any fallback
video, inside the retention window; add `--media` (and a small
`--retention-days`) to include it.

```bash
PYTHONPATH=. .venv/bin/python -m web.publisher --out-dir /tmp/r2-layout
```

The publisher is read-only over the trading path, just like `web/server.py`.
A run never imports `relay.order_manager`, `relay.csp_manager` or
`relay.options_client`. It does hold a broker client, the same
`relay.alpaca_client.AlpacaClient` the local dashboard builds for the account,
executions and options sections, and that client could place orders. The
publisher only calls its read methods: `get_account`, `get_all_positions`,
`get_latest_trade`, and `get_orders` on the trading client. Outside its own
state and lock files, the only file it can change is the look record's
approval flag. `tests/test_publisher.py` checks this at import time, and by
running a whole cycle in a fresh interpreter against a fake broker that fails
on any other call. `tests/test_publisher_chart_utc.py` and
`tests/test_options_view.py` check it too. The last also runs
`web/options_view.py` against `relay.csp_manager` on the same fakes, so the
read-only mirror cannot drift.

## Public portfolio

`apps/portfolio` is a separate, public, static site showing the paper
account's own results: equity curve, closed trades at its own fill prices, the
research board (bias, priority, size and standing orders only), the rules and
the English recap videos. Its data file and its own pages and charts carry no
third-party market data and no model-written text; each recap is listed by its
date. The recap videos it embeds are a different matter: a language model
writes their scripts and YouTube titles, on weekdays an AI-generated presenter
reads them (the Sunday editions are cartoons), and most of them show the live dashboard, including market candles and current
prices. The site says so on its Recaps page.
`web/public_export.py` builds its one data file from a whitelist, gates it
with `apps/portfolio/schema/portfolio.schema.json` and uploads it to the R2
bucket `alpaca-public`. A GitHub Actions job gates the file again and deploys
the site to Cloudflare. What is published, the setup, the schedule and how to
take it down are in [`docs/public-portfolio.md`](docs/public-portfolio.md).

```bash
PYTHONPATH=. .venv/bin/python -m web.public_export --dry-run    # build + gate, upload nothing
PYTHONPATH=. .venv/bin/python -m web.public_export --once       # one upload (for cron)
```
