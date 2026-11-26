# Public portfolio

A public, static site that shows the paper account's own results: equity
curve, profit and loss, closed trades, the research board, the rules the
system trades by, and the English recap videos. The app is in
[`apps/portfolio/`](../apps/portfolio/README.md). It is a separate site from the
private ops dashboard ([`docs/cloud-dashboard.md`](cloud-dashboard.md)), which
stays behind Cloudflare Access. The two sites share no code and no bucket.

- **Home box:** `web/public_export.py` builds one file, `portfolio.json`, from
  the account's own figures and uploads it to the R2 bucket `alpaca-public`.
- **GitHub Actions:** `.github/workflows/portfolio.yml` builds the site in a
  job with no secrets, then a second job downloads that build and the file
  (read-only token), checks the file against the JSON schema again, writes a
  re-serialisation of the checked value into the build and deploys.
- **Cloudflare:** a Worker named `alpaca-portfolio` serves static assets only
  (no script, no bindings) on its `workers.dev` hostname.

## What the file carries, and what it never does

The owner decided this list. It covers the published file and the site's own
pages and charts, not the recap videos the site embeds (see "The recap
videos" below). Code enforces it in three places:

1. The exporter builds each section field by field from a whitelist.
2. [`apps/portfolio/schema/portfolio.schema.json`](../apps/portfolio/schema/portfolio.schema.json)
   closes every object with `additionalProperties: false`. The file must pass
   it on the home box before upload (`web/schema_gate.py`) and again in CI
   before deploy (`apps/portfolio/scripts/check-portfolio.ts`). Both read the
   file strictly: an object with the same key twice is refused, because a JSON
   parser keeps the last copy and would validate that while the bytes still
   carried the first. CI then publishes a re-serialisation of the value that
   passed (`stage-data`), never the bytes it downloaded.
3. The page checks the file a third time when it loads, and shows nothing from
   a file that fails.

| Section | Published fields |
| --- | --- |
| `meta` | `schema` (always 1), `as_of`, `data_delay` (a fixed note), `generated_by`, `unavailable` (section names only) |
| `account` | `paper`, `start_date`, `start_equity`, `equity`, `day_pl_pct`, `total_return_pct`, `max_drawdown_pct`, `best_day{date, return_pct}`, `worst_day{date, return_pct}`, `daily[{date, equity}]` (one point per completed session: nothing from a session still in progress, no intraday series) |
| `positions` | `equities[{symbol, side, qty, avg_entry_price, stop_pct}]`, `options[{underlying, type, side, strike, expiry, qty, premium_received}]` |
| `trades` | `stats{round_trips, wins, losses, win_rate, avg_win, avg_loss, avg_win_pct, avg_loss_pct, profit_factor, realized_pnl}`, `round_trips[{symbol, side, qty, entry_price, exit_price, entry_at, exit_at, pnl, pnl_pct, strategy}]` (newest first, at most 500) |
| `signals` | `date`, `rows[{symbol, bias, priority, suggested_size, contingencies[{condition, level, valid_until}]}]` |
| `strategy` | `symbols[{symbol, enabled, on_watchlist, interval, stop_pct, deploy, strategies[{type, and that type's parameters}]}]` (`type` is one of `ema`, `bollinger_rsi`, `macd_sma`), `risk{daily_loss_limit, daily_loss_hard_cap, max_position_shares_cap, allow_extended_hours, sleeve_reserve_pct}` |
| `recaps` | `[{date, youtube_id, duration_s}]`: English editions whose recorded YouTube privacy is exactly `public`, newest first, at most 12. No title: a language model writes it, so the site labels each video by its date |

Conventions: every `*_pct`, `win_rate`, `stop_pct`, `deploy`,
`suggested_size` and `daily_loss_limit` is a fraction (0.0123 = 1.23%).
Timestamps are UTC and dates are US/Eastern session dates.

How some figures are worked out:

- **Equity and its statistics** (`equity`, `day_pl_pct`, `total_return_pct`,
  `max_drawdown_pct`, best and worst day, `daily`) come from Alpaca's daily
  portfolio history, using only the days that are over in New York: a point
  counts once its US/Eastern date has ended, even if the broker already
  serves the day in progress. `equity` is the last of those points and
  `start_equity` the first, so `total_return_pct` (`equity / start_equity -
  1`) and `max_drawdown_pct` use closing equity of completed sessions only;
  `day_pl_pct` is the last point's change from the session before. The
  account's current equity and the broker's day P&L are never read. On the
  account's first day there is no completed session yet, so the section is
  unavailable.
- **Round trips** are cash basis: what the sells brought in less what the buys
  cost. For a crypto trip that counts the coins Alpaca keeps as its fee on the
  buy (the sell is for about 0.25% fewer coins than were bought). A dollar fee
  charged on a crypto sale is not in the fill price and is not deducted.
- **`deploy` and `suggested_size`** are shares of the share book, which is
  equity less `sleeve_reserve_pct` (the options sleeve's reserve,
  `decisions/csp.json` `reserve_frac` while the sleeve is enabled). A buy is
  sized at `deploy x (equity - reserve)`, so 97% with a 20% reserve is about
  78% of equity. The engine also keeps back the live put collateral when that
  is larger; that figure is not published.
- **`on_watchlist`** says whether research's watchlist lists the symbol. The
  risk gate refuses every buy for a symbol that is not listed, crypto
  included (crypto is exempt only from the bias). It is null when research
  declares no watchlist, so the gate refuses none.
- **Best and worst day** compare each session's equity with the session just
  before it on the exchange calendar (Alpaca's market calendar, read with
  the history). A session the broker leaves out of its history, or lists
  without equity, is skipped: its neighbours' two-session move is never
  filed under one date. Without the calendar, the sessions the broker stamped
  are used.
- **A position's `stop_pct`** is published only where the site can vouch
  for a stop (`armed_stop` in `web/public_export.py`, from
  `sigengine/engine.py`): a long, in a symbol enabled in
  `sigengine/config.json` (looked up by its canonical name, so Alpaca's
  `BTCUSD` finds `BTC/USD`), that the engine's entry sidecar says the engine
  itself opened, and that is not shares the put sleeve was assigned (those
  have no stop by the operator's choice). An unreadable sidecar publishes no
  stop at all.

  How the engine really sells on a stop: each strategy keeps its own
  simulated trade, entered at the close of the bar its buy signal fired on,
  and emits a close when a later bar closes at or below that entry less the
  symbol's stop. The engine runs enabled symbols only, and fires that close
  as a full sale of whatever the account holds in the symbol (`fire_event`
  checks only that something is held). So the stop follows the simulated
  trade, not the broker's position, and neither published answer is proof:
  a null does not mean no stop exists (a strategy that is long in its own
  simulation can also stop out a position the engine did not open), and a
  value does not mean one is armed (when a simulation enters and stops out
  on the same bar, the engine handles the stop before the real buy, and the
  position can be left with none). The page says both.

  The stop is a check on closed bars, not an order at the broker: a gap or a
  fast bar can take the exit well past it, so a 2% stop does not cap a
  trip's loss at 2%. The page says so and quotes the worst closed trip from
  the published trades.
- **Strategy types** are exactly `backtest/multi_strategy.py`
  `STRATEGY_RUNNERS` (`ema`, `bollinger_rsi`, `macd_sma`): the exporter's
  whitelist (`STRATEGY_PARAMS`), the schema's per-type definitions and the
  round trip's `strategy` enum all match it, and a test holds the four
  together. A spec of any other type in the engine config (the retired
  `donchian`, say) is left out of the file.
- **The risk block** is what `relay/risk_filter.py` `check()` applies, read the
  way it reads it: the daily loss limit clamped by the hard cap, and
  `allow_extended_hours` as `bool(value)`, so a missing key is `false`.
  `decisions.json`'s `max_open_positions` is not published: nothing enforces
  it. One position at a time is `relay/order_manager.py`'s rotation, fixed in
  code, and it covers the positions the signals open. Shares the put sleeve
  was assigned are outside it (`protected_symbols`): the rotation never sells
  them, so they can be held alongside the engine's position until the sleeve
  sells them at its next weekly run. The page says "One from the signals,
  fixed in code, plus any shares the options sleeve was assigned".

**Never in the file, nor in the site's own pages and charts:**

- Third-party market data: candles, bars, quotes, current or last prices,
  market values, option quotes. The data vendors' terms restrict
  redistribution, so no price in the file, or on the site's own pages and
  charts, comes from a market-data feed: each is one of the account's own
  fills or a level the system set itself (a stop, a standing order's level, a
  put's strike).
- Model-written text: research notes, contingency reasons and the recap
  titles. The board shows only bias, priority, size and the standing order's
  level, and a recap is listed by its date.
- Engine logs, account numbers, account or order ids, file paths, host names
  and email addresses.
- The presenter's look and the desk digest as data, the zh recap, and any
  upload that is not public.

**The recap videos.** The site embeds the English recap videos from YouTube,
and the list above does not cover what is in them. They are AI-generated. A
language model writes each script and YouTube title from research's summary
of the market and the account's own figures. On weekdays an AI-generated
presenter reads the script; the Sunday editions are cartoons. Most of the
videos also show a screenshot of the live dashboard (the desk slide), so they
show its market candles, current prices from the market-data feeds and the
account's figures. The Recaps page and "How it's built" say this in the same
terms, and the file lists each video by its date, id and length only.

**No figure that gives back a live price.** An open position's unrealized
P&L (dollars or percent) and its distance to the stop are both worked out from
the broker's current price, and either one with the entry price and quantity
returns that price to the cent. Neither is published; the page shows the
account's own stop level (entry less the stop percentage) instead.

The account's equity is a price in disguise too. With one share position open
and no cash flows, `avg_entry_price + (equity - start_equity - realized_pnl) /
qty` is that symbol's mark to the cent, and every term in it is published; a
constant offset (option P&L, say) can be calibrated from any file taken while
the book was flat. The broker's day P&L against the previous close gives the
move since that close the same way. So neither the current equity nor the
broker's day P&L is published: every equity figure stops at the last
completed session (see "Equity and its statistics" above).

What remains: from `equity`, a held symbol's price **at the end of the last
completed session** can still be worked out to the cent. That is an
end-of-day mark, never a live one, and it changes once a day, not with each
export. It is a property of publishing the account's value at all, and the
site's "What this site leaves out" says so.

**Daily equity only** (owner decision). With a position open, equity sampled
every 5 minutes, divided by the position's quantity, traces that symbol's price
through the session. The exporter never requests an intraday series, the
schema has no field for one, and the curve has one point per completed
session.

**Recap privacy is as recorded at upload.** A video made private on YouTube
later stays listed (it will not play) until its edition's metadata at home says
otherwise and the next export and deploy run.

## Data flow

```
 HOME BOX (keys stay here)
 ─────────────────────────
 web/public_export.py         cron, every 15 min (suggested)
   read-only builders + Alpaca portfolio history
   whitelist → web/schema_gate.py (exit 1 on any error: nothing uploaded)
   skips unchanged content, except an hourly heartbeat
        │
        │ S3 PUT (home R2 token: write)
        ▼
 ┌───────────────────────────────┐
 │ R2 bucket alpaca-public       │   one object: portfolio.json
 │ public access OFF             │   (never the private alpaca-dashboard bucket)
 └───────────────┬───────────────┘
                 │ S3 GET (CI R2 token: READ-ONLY, this bucket only)
                 ▼
 GITHUB ACTIONS  .github/workflows/portfolio.yml
   job `build` (no secrets, no environment)
   1 install (frozen lockfile), lint, typecheck, test, build: no data;
     scripts/check-licenses.ts checks the build's licences file
   2 upload dist/ as an artifact (only when a deploy follows; kept 1 day)
   job `deploy` (main only; installs nothing from the app's lockfile)
   3 download that build; download portfolio.json to $RUNNER_TEMP
   4 scripts/check-portfolio.ts: the JSON schema gate, FAILS CLOSED,
     refuses a duplicate key
   5 scripts/stage-data.ts: the page's own check, then WRITE the checked
     value to dist/data/ (the downloaded bytes are deleted, never copied)
   6 scheduled runs only: skip when the live site already serves this
     exact build and file
   7 npm installs wrangler alone (locked version, deps 7+ days old);
     that wrangler runs `deploy` directly, at WRANGLER_LOG=warn
                 │
                 ▼
 ┌───────────────────────────────┐
 │ Worker alpaca-portfolio       │   static assets only, workers.dev
 │ /  /trades  /strategy  ...    │   SPA fallback, strict CSP (public/_headers)
 │ /data/portfolio.json          │   the only data the page fetches
 └───────────────────────────────┘
                 ▲
             browsers (YouTube loads only after a click on play)
```

Nothing in this pipeline calls the home box. The CI's R2 token can only read
one object from one bucket. The deployed site holds a copy of the file, so the
page never reads R2. (The private dashboard is a separate path: the home box
reads requests that dashboard writes to its own bucket. See
[`docs/cloud-dashboard.md`](cloud-dashboard.md).)

## Freshness and schedule

| What | When (UTC) | Runs |
| --- | --- | --- |
| Exporter at home | every 15 min (your cron) | uploads only when content changed, plus an hourly heartbeat |
| Deploy, weekdays | `7,37 13-21 * * 1-5`: every 30 min, 13:07 to 21:37 | 18 |
| Deploy, every day | `7 3,9 * * *`: 03:07 and 09:07 (picks up the nightly recap) | 2 |
| Deploy, weekends | `7 15,21 * * 0,6` | 2 |
| Pushes to `main` that touch the app, the exporter or the workflow | on push | always deploy |

That comes to 20 runs on a weekday and 4 on a weekend day (108 a week).
`apps/portfolio/scripts/workflow.test.ts` fails if any day goes over 22. The
13:07 to 21:37 window covers the US session from before the open to after the
close in both EST and EDT.

A scheduled run deploys only when something changed. It compares the new
`index.html` (which names every hashed bundle) and the new `portfolio.json`,
byte for byte, with what the live site serves. If both match, it skips the
deploy and leaves a "Deploy skipped" notice. That needs `siteUrl` in
`apps/portfolio/site.config.json`; until it is set, every run deploys. A
change that does not touch `index.html` (say `_headers` alone) arrives by a
push, and pushes always deploy.

Expected delay: up to 15 min for the export plus up to 30 min for the next
scheduled deploy during US market hours on weekdays (about 45 minutes), and
up to about 6 hours at other times, plus GitHub's own scheduling delay.
Equity, returns and the curve lag further by design: they stop at the last
completed session (see "No figure that gives back a live price"). The
site says so in the same words (`CADENCE` in `apps/portfolio/src/lib/copy.ts`,
and `DATA_DELAY_NOTE` in the file's `meta.data_delay`); keep both in step
with the crons above and the home cron.

Every page says "These figures were published on ... and have not been updated
since" once `meta.as_of` is more than 26 hours old
(`apps/portfolio/src/lib/freshness.ts`). A healthy pipeline keeps it under
about 7.5 hours: the exporter re-sends an unchanged file every hour, and
deploys run weekends and market holidays too, so a closed market never trips
the notice. 26 hours leaves room for GitHub to delay or drop three scheduled
runs in a row.

## Platform limits, and how this stays inside them

### Cloudflare Workers

Checked 2026-11-26 against Cloudflare's
[Workers limits](https://developers.cloudflare.com/workers/platform/limits/),
[API rate limits](https://developers.cloudflare.com/fundamentals/api/reference/limits/)
and [static assets billing](https://developers.cloudflare.com/workers/static-assets/billing-and-limitations/):

- **No documented limit on deploys or versions per day.** The limits page
  lists none for script updates per day or per hour.
- **Versions:** "You can only deploy, create a gradual deployment with, or
  roll back to the 100 most recent versions of a Worker." Versions are
  retained for 30 days, and the 100 most recent plus the deployed one are
  always kept.
- **Cloudflare API:** 1,200 requests per five minutes per user, across the
  dashboard, keys and tokens. A deploy makes a handful of calls.
- **Static assets:** "Requests to static assets are free and unlimited." Up
  to 20,000 files per version on the free plan (this site has about a dozen) and
  25 MiB per file.

Design: at most 20 scheduled runs a weekday, and a run deploys only when
something changed. Even at 20 deploys every day, the 100 versions you can roll
back to reach back about 5 days. Roll back code by reverting the commit and
pushing, which redeploys with current figures. `wrangler rollback` is the
emergency tool, but it would bring back old figures too.

### GitHub Actions

Checked against GitHub's documentation for the
[schedule event](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
and [disabling and enabling workflows](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows):

- **"In a public repository, scheduled workflows are automatically disabled
  when no repository activity has occurred in 60 days."** This would freeze
  the site's figures silently.
- "The `schedule` event can be delayed during periods of high loads ... High
  load times include the start of every hour." That is why the crons sit at
  :07 and :37, not :00 and :30. Scheduled runs can also be dropped under heavy
  load.
- Scheduled workflows run only on the latest commit of the default branch, and
  are disabled in forks by default.
- Actions minutes on standard runners are free for public repositories.

**Keep-alive:** [`.github/workflows/keepalive.yml`](../.github/workflows/keepalive.yml)
runs every Monday. It reads the committer date of the newest commit on `main`.
Only when that is 45 days old or more does it add one empty commit to `main`
through the GitHub API ("Keep the scheduled workflows enabled ..."). A
repository in use never gets one, and the worst case (day 45 plus a week) is
still inside the 60 days.

- Its job is the only one with `contents: write`. It does no checkout and runs
  no third-party code.
- A commit made with the workflow token starts no other workflow.
- Calling "enable workflow" on the API is not used. Authors of keep-alive
  tools report that toggling a workflow's state does not reset GitHub's clock
  ([gh-action-keepalive](https://github.com/efrecon/gh-action-keepalive)); a
  commit does.
- **If `main` is protected** (a ruleset that requires pull requests or blocks
  bot pushes), the commit is refused and the job fails. GitHub then emails the
  person who last edited the workflow's cron line, so you have about two weeks
  to push any commit to `main`. Alternatively, add GitHub Actions to the
  ruleset's bypass list.
- **If the workflows were disabled anyway:** Actions tab → Portfolio → Enable
  workflow, and the same for Keep schedules alive. Or run
  `gh workflow enable portfolio.yml` and `gh workflow enable keepalive.yml`.

## Owner setup (once)

Do these in order. Nothing in the repository holds a real account id, token
or URL; you enter those in the places below.

1. **Bucket.** In the Cloudflare dashboard go to R2 → Create bucket, and name
   it exactly `alpaca-public` (automatic location, Standard storage). Leave
   **Settings → Public access** off: no `r2.dev` subdomain and no custom
   domain. The site never reads the bucket; only the CI token does.

2. **Home write token.** The exporter uses the publisher's credentials
   (`R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` in the home
   `.env`), so that token must be able to write to `alpaca-public` as well.
   In R2 → Manage R2 API Tokens, edit the publisher's token. Keep **Object
   Read & Write**, set **Apply to specific buckets only** →
   `alpaca-dashboard` **and** `alpaca-public`, and save. If you would rather
   not edit it, create a new token scoped to both buckets and replace the
   three values in `.env`. The bucket name can be changed with
   `R2_PUBLIC_BUCKET` (default `alpaca-public`); the exporter refuses to write
   to the private dashboard's bucket.

3. **The exporter in the production install.** Before the exporter runs, that
   install needs the exporter, its schema and every module it imports, all
   from the same commit of this repository: `web/public_export.py`,
   `web/schema_gate.py`, `apps/portfolio/schema/portfolio.schema.json`,
   `web/server.py` (its `build_executions` pages through every closed order;
   an older copy reads only the first 500 and freezes the trade stats),
   `web/options_view.py`, `web/publisher.py` and `web/r2.py`. Copy them
   together: an older exporter still writes `intraday` and
   `max_open_positions`, which the current schema, and so CI's gate, refuse,
   and the deploys then fail closed. Without the schema it exits 2 and
   publishes nothing. `web/server.py` is also the :8787 dashboard, which
   picks up the new copy on its next restart. It also needs boto3
   (`.venv/bin/python -m pip install 'boto3>=1.34'`). Try it:

   ```sh
   PYTHONPATH=. .venv/bin/python -m web.public_export --dry-run   # build + gate, upload nothing
   PYTHONPATH=. .venv/bin/python -m web.public_export --once      # one real upload
   ```

4. **CI read-only token.** In R2 → Manage R2 API Tokens → Create API token,
   choose permission **Object Read only** and **Apply to specific buckets
   only** → `alpaca-public`. Copy the Access Key ID and Secret Access Key;
   they are shown once.

5. **A Cloudflare account and API token for the site's deploys.** This token
   is used by public CI, which installs and runs npm packages on the same
   runner (see Residual risk below). Its permission, **Workers Scripts:
   Edit**, cannot be limited to one Worker: it can replace or delete every
   Worker in its account. In the private dashboard's account that includes
   `alpaca-dashboard`, whose binding reads the private bucket.
   - **Recommended: host the site in a Cloudflare account of its own** (a
     free account; it needs only Workers on `workers.dev`). Create the token
     in that account. The `alpaca-public` bucket can stay where it is: CI
     reads it with the step 4 token, and `R2_PUBLIC_ACCOUNT_ID` (step 6) names
     its account separately from the Worker's.
   - **Never reuse the dashboard's deploy token.** Create a separate one, so
     each can be revoked on its own.
   - **Create it trimmed.** My Profile → API Tokens → Create Token, start
     from the **Edit Cloudflare Workers** template, then before creating it:
     - **Account Resources:** Include → the site's account only (never "All
       accounts").
     - **Permissions:** keep **Account → Workers Scripts → Edit** and
       **Account → Account Settings → Read**. Delete every other row,
       including the template's read-only **User → User Details → Read** and
       **User → Memberships → Read**: a deploy does not need them, and on a
       failed login wrangler uses them to look up your email and your
       memberships (see "Your email and account names stay out of the
       public log" below). Delete in particular **Zone → Workers Routes**
       (the site is on `workers.dev` and uses no zone; with it, a leaked
       token could put a Worker in front of any site on the account),
       **Workers R2 Storage** (with it, a leaked token could read or write
       every bucket in the account), **Workers KV Storage**, **Workers
       Tail**, and any Pages, Builds or Observability rows.
     - With no zone permission left there are no **Zone Resources** to pick.
       Optionally set a **TTL** and renew it.
     - If a deploy then fails with an authentication error, add back only
       the permission the error names, never the User rows.
   - **Give the account a neutral name.** A new Cloudflare account is named
     "<your email>'s Account". In the dashboard, go to the account's
     **Manage Account → Configurations** (account name) and rename it to
     something with no name or email in it, such as `alpaca-portfolio`. Do
     the same for the private dashboard's account if the site shares it.
   - **Your email and account names stay out of the public log.** The
     deploy step runs wrangler at `WRANGLER_LOG=warn`. On an authentication
     error wrangler otherwise runs `whoami`, which prints the email of the
     token's user (when the token has **User Details → Read**) and a table of
     the name and id of every account the token can read, User rows or not,
     which is why the account's name matters too. GitHub masks the secrets'
     values, not these. At `warn` only warnings and the error itself print.
     Never raise the level in the workflow to debug a failure: run
     `wrangler whoami` locally instead.

6. **GitHub environment.** In the repository go to Settings → Environments →
   New environment and name it `portfolio`.
   - **Deployment branches and tags:** choose **Selected branches and tags**
     and add `main`.
   - Do not add required reviewers: every scheduled run would wait for an
     approval.
   - Add five **environment secrets**:
     - `CLOUDFLARE_API_TOKEN` (step 5)
     - `CLOUDFLARE_ACCOUNT_ID`: the 32-character id of the account the site's
       Worker is deployed to (step 5)
     - `R2_PUBLIC_ACCOUNT_ID`: the id of the account that holds the
       `alpaca-public` bucket. With one account for both, the same value.
     - `R2_PUBLIC_READ_KEY_ID` (step 4)
     - `R2_PUBLIC_READ_SECRET` (step 4)

7. **Turn deploys on.** In Settings → Secrets and variables → Actions →
   Variables, add a repository variable `PORTFOLIO_DEPLOY` with the value
   `true`. Until it exists, the deploy job is skipped and scheduled runs do
   nothing. If the variable is set but a secret is missing, the deploy steps
   are skipped, the job still passes, and the run shows a "Deploy skipped"
   notice naming what is missing.

8. **First deploy.** Go to Actions → Portfolio → Run workflow, on `main`.
   - The gate step should print `valid: the file passes the schema gate`.
   - The deploy step should print `Deployed to https://alpaca-portfolio.<subdomain>.workers.dev`
     (read from wrangler's output file, not its log), and the URL also
     appears on the environment.
   - Instead of CI, you can deploy by hand from a checkout with
     `wrangler login` (see [`apps/portfolio/README.md`](../apps/portfolio/README.md)).

9. **Tell the site its address.** Put that origin (no trailing slash) in
   `apps/portfolio/site.config.json` → `siteUrl` and push to `main`. This
   turns on the canonical link and social-card tags, and lets scheduled runs
   skip deploys when nothing changed.

10. **Home cron (a suggestion; nothing is installed).** Run it in the install
    that holds the live `.env` and data:

    ```cron
    */15 * * * * cd <that install> && PYTHONPATH=. .venv/bin/python -m web.public_export --once >> agent/public_export.log 2>&1
    ```

    Exit codes: 0 means uploaded, unchanged, or another run holds the lock. 1
    means the gate refused the file or the upload failed. 2 means the R2
    settings, boto3 or the schema file are missing. The log carries section
    names, counts and sizes, never values.

11. **Verify from outside:**

    ```sh
    U=https://alpaca-portfolio.<subdomain>.workers.dev
    curl -sI "$U/" | grep -i content-security-policy
    curl -s -o /dev/null -w '%{http_code} %{content_type}\n' "$U/data/portfolio.json"   # 200 application/json
    ```

## Take it down quickly

Do these in order. If you take the site down without stopping the deploys
first, the next scheduled run (at most 30 minutes later) publishes it again.

1. **Stop redeploys.** Set the repository variable `PORTFOLIO_DEPLOY` to
   `false`, or disable the workflow (Actions → Portfolio → ⋯ → Disable
   workflow). Cancel a run that is in progress.
2. **Take the site offline.** Either option works:
   - **Fastest, and reversible:** go to Workers & Pages → `alpaca-portfolio`
     → Settings → Domains & Routes, and disable the `workers.dev` route. The
     URL stops answering at once; the Worker and its versions stay.
   - **Permanent:** delete the Worker (Settings → Delete), or run
     `pnpm exec wrangler delete` in `apps/portfolio` and confirm.
3. **Withdraw the data at the source.** Delete `portfolio.json` from the
   `alpaca-public` bucket and comment out the home cron line. A deploy that
   finds no object fails at the download step and publishes nothing.
4. **If a credential may have leaked,** revoke the R2 tokens (Manage R2 API
   Tokens) and the Cloudflare API token, then replace the environment
   secrets.

To bring it back, reverse the order: restore the cron and the object,
re-enable `workers.dev`, set `PORTFOLIO_DEPLOY` to `true`, and run the
workflow.

## What runs where, and what it can see

| Job | Runs on | Secrets | Permissions |
| --- | --- | --- | --- |
| `build` (portfolio.yml) | pull requests, pushes, and scheduled runs while deploys are on | none, and no environment | `contents: read` |
| `deploy` (portfolio.yml) | `main` only: push, schedule, manual | the five in environment `portfolio`, each passed only to the step that needs it | `contents: read` |
| `keepalive` (keepalive.yml) | Mondays, and manual on `main` | the workflow token | `contents: write` |

- Pull requests, including those from forks, run `build` only. There is no
  `pull_request_target`.
- Every action is pinned to a full commit SHA, and Dependabot proposes updates
  weekly with a 7-day cooldown: GitHub Actions plus npm for `apps/portfolio`.
  pnpm enforces the same 7 days on the app's lockfile (`minimumReleaseAge` in
  `apps/portfolio/pnpm-workspace.yaml`): `pnpm install`, `--frozen-lockfile`
  included, fails on any locked version younger than that.
- Checkouts do not persist credentials. Secrets reach steps only through
  `env:` or action inputs, never inside a script, and `workflow.test.ts`
  checks this.
- Logs of a public repository are public. No step prints the file or a value
  from it. The gate prints pass or fail and, for a refusal, JSON pointers and
  rules (`/account/daily/2/equity: expected number`). An undeclared key is
  reported at its parent object, never by name. `stage-data --quiet` prints no
  counts. Data-touching steps run with `set +x`. wrangler runs at
  `WRANGLER_LOG=warn`, so a failed login never prints the token user's email
  or the account names (step 5); `workflow.test.ts` checks this for this
  workflow and for the private dashboard's `dashboard.yml`, whose logs are
  just as public.
- No third-party action receives the Cloudflare token: the deploy runs the
  wrangler it installed itself.
- **The app's build dependencies never share a runner with a secret.** The
  `build` job installs the lockfile, builds and uploads `dist/` as an
  artifact; it has no secret and no environment. The `deploy` job installs
  nothing from the app's lockfile: it runs this repository's own gate scripts
  with Node (no packages), and installs wrangler alone, at the locked version
  (`WRANGLER_VERSION`, which `workflow.test.ts` checks against
  `pnpm-lock.yaml`), into a directory with no `package.json`.
- **Residual risk:**
  - **wrangler runs with the Cloudflare token**, as any deploy must. Its own
    dependencies are resolved by npm without the app's lockfile, so the
    install uses `--before` to take only versions at least 7 days old (the
    cooldown Dependabot keeps), which keeps out a malicious version caught
    within a week. A compromised wrangler, or a dependency of it older than
    that, could still use the token.
  - **A compromised build dependency** can change what the site's code does
    in visitors' browsers (it builds that code), but it cannot reach the
    token, the R2 key or any data: the build job has none of them, and the
    deploy job only copies its files.
  - What the token can do depends on its account:
    - **Site in its own account (step 5):** replace or delete this public
      site, and any other Worker in that account (keep none there). It cannot
      touch the private dashboard or its bucket.
    - **Site in the dashboard's account:** also redeploy `alpaca-dashboard`
      with its own code, and through that Worker's binding read the private
      bucket and write the requests the home box applies. This is why step 5
      recommends a separate account.
  - The R2 token can only read `alpaca-public`, wherever the site lives.

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| The deploy job is grey (skipped) | `PORTFOLIO_DEPLOY` is not `true`, or the run is not on `main`. |
| "Deploy skipped: Not set in the portfolio environment: ..." | Add the named secrets to the `portfolio` environment (step 6). |
| Download fails with `AccessDenied` / 403 | The CI token is not scoped to `alpaca-public`, or `R2_PUBLIC_ACCOUNT_ID` does not name the account that holds the bucket. |
| Deploy fails with an authentication error | The token is not for the account in `CLOUDFLARE_ACCOUNT_ID`, or it lacks the permission the error names (step 5). The log shows only the error, by design; to see which user and accounts the token reaches, run `wrangler whoami` locally with the token, not in CI. |
| Download fails with `NoSuchKey` / 404 | The exporter has not uploaded yet. Run `python -m web.public_export --once` at home. |
| The gate refuses the file | The exporter's own gate should refuse first, so the home install's schema is probably out of step with `main`. Copy the schema and exporter from `main` (setup step 3). The pointers in the log say which field. |
| "refused: an object in the file has the same key twice" | The object in the bucket was not written by the exporter (it cannot produce one). Treat it as tampering: check who holds the home write token, delete the object, and let the exporter write it again. |
| "Install wrangler alone" fails with `ETARGET` / "No matching version" | The locked wrangler (or one of its exact dependencies) is less than 7 days old. pnpm's `minimumReleaseAge` should have refused that lockfile: check it is still set in `apps/portfolio/pnpm-workspace.yaml`, then lock the newest wrangler at least 7 days old and set `WRANGLER_VERSION` to it. |
| "Download the built site" fails | The `build` job uploaded no artifact: it only does so when `PORTFOLIO_DEPLOY` is `true` on `main`. Re-run the whole workflow, not the deploy job alone after a day (the artifact is kept 1 day). |
| "Deploy skipped: The live site already serves this build and this file." | Normal: the home box sent nothing new since the last deploy. |
| The site says "not updated since ..." | Check in this order: the home cron and `agent/public_export.log`, a banner on the Actions tab saying scheduled workflows are disabled (see Keep-alive), and whether `PORTFOLIO_DEPLOY` is still `true`. |
| The keep-alive job failed | `main` refused the bot's commit. Push any commit to `main`, or let GitHub Actions bypass the ruleset. |

## Local checks

```sh
cd apps/portfolio
pnpm build
node scripts/check-licenses.ts dist                      # the build's licences file
node scripts/check-portfolio.ts fixtures/portfolio.json   # the CI gate
pnpm stage-data --from fixtures/portfolio.json           # writes the checked value to dist/data/
pnpm dev:assets                                          # wrangler dev over dist/, _headers applied
```

`stage-data --dist DIR` stages into another build (the deploy job stages into
the build it downloaded).

`wrangler dev` listens on port 8787 by default. On the home box the local
dashboard already uses that port, so add `--port 8799` there.
