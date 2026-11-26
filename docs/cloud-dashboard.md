# Cloud dashboard

A read-only view of the trading system, hosted on Cloudflare so it can be
opened from anywhere without exposing the home box to the internet. The local
dashboard (`web/server.py`) stays on loopback; this replaces any public
exposure of it.

- **App:** `apps/dashboard/`, a Vite + React 19 + TypeScript SPA and one
  Cloudflare Worker (Static Assets on `*.workers.dev`). Pages: Terminal
  (account, positions and the options sleeve, rotation board, engine, signal
  chart, executions, journal), Briefing (recap videos) and Look (the
  presenter's outfit sign-off).
- **Publisher:** `web/publisher.py` on the home box pushes the data to an R2
  bucket every minute.
- **Access:** Cloudflare Access protects the whole Worker. The Worker also
  verifies the Access JWT itself and refuses everything when Access is not
  configured.

## Architecture

```
 HOME BOX (keys stay here)                         CLOUDFLARE
 ─────────────────────────                         ──────────────────────────────────────────
                                                    browser
 web/server.py  (127.0.0.1:8787, unchanged)            │  HTTPS, Access login (email one-time PIN)
   ▲  read-only builders                               ▼
   │  build_portfolio / board / journal /        ┌──────────────────────────┐
   │  executions / chart / look                  │ Cloudflare Access (edge) │ every path, one allowed email
   │                                             └────────────┬─────────────┘
 web/publisher.py  (cron, every minute)                       │ Cf-Access-Jwt-Assertion
   │  + web/options_view.py (read-only)                       ▼
   │                                             ┌──────────────────────────┐
   │  S3 API, PUSH ONLY                          │ Worker  alpaca-dashboard │
   └──── PUT snapshot, charts, looks, ──────────►│  /          SPA (assets) │
         media, history ─────────────┐           │  /api/*     JWT → R2     │
   ◄──── GET approvals/<date>.json ──┤           │  /media/*   JWT → R2,    │
         (applied locally with       │           │             Range        │
          set_look_approval)         ▼           └────────────┬─────────────┘
                              ┌────────────────────┐          │ binding DATA
                              │ R2 alpaca-dashboard│◄─────────┘ (reads; writes only
                              └────────────────────┘              approvals/<date>.json)
```

- **Push only.** The cloud never calls the home box, and there is no tunnel
  and no inbound port. Broker keys never leave home; the bucket holds only what
  the builders already show on :8787.
- **One write from the cloud:** a look sign-off is stored as
  `approvals/<date>.json`. The publisher reads it on its next run, validates it
  strictly, and applies it with `set_look_approval`, but only before that
  date's 20:00 ET render starts: a request that reaches the home box later is
  refused, because the render has already used the locked keyframe. Everything
  else the cloud does is read.
- **No Durable Objects, no KV.** One Worker, one R2 bucket.

### What stays local

- `web/server.py` on 127.0.0.1:8787 keeps running unchanged. The recap
  renderer screenshots it for the DESK slide, so it cannot be switched off.
- The trading engine, crons, broker keys, IBKR and the recap renderer.
- The publisher's state (`$ALPACA_DATA_DIR/publisher/state.json`): content
  hashes, which approvals were applied, and their outcomes.

## The R2 contract

Types for every object are in
[`apps/dashboard/src/contract.ts`](../apps/dashboard/src/contract.ts). Units:
epoch **seconds** in true UTC, ratios as fractions (0.0123 = 1.23%).

| Key | Written by | Content |
| --- | --- | --- |
| `snapshot/latest.json` | publisher, every run (every minute), changed or not: `as_of` is the heartbeat | `{"schema":1, "as_of", "publisher":{"version","host","approval"}, "portfolio", "board", "journal", "executions", "symbols", "look", "options"}`. Each section is its builder's output, or `{"error": "..."}` if that builder failed. `options` is the put sleeve (null when none is configured). `publisher.approval` is what the home box did with the newest approval request for the look's date. |
| `chart/<KEY>.json` | publisher, at most every 5 min | `build_chart(symbol, 220)` in TRUE UTC epochs, wrapped as `{"schema":1, "as_of", "time_basis":"utc", "symbols":{...}}` (or `error`). `KEY` is the symbol with `/` replaced by `-` (`BTC/USD` is `chart/BTC-USD.json`). |
| `look/<date>/plate.png`, `look/<date>/keyframe.png` | publisher, before the snapshot that points at them | The session date's wardrobe plate and stage keyframe. The snapshot's `look.plate_url` / `keyframe_url` end in `&v=<first 12 hex of the image's SHA-256>`, so a redraw for the same date is a new URL and the page fetches it. |
| `media/history.json` | publisher | The recap index, URLs rewritten from `/archive/...` to `/media/...?v=<first 12 hex of the file's SHA-256>`, listing only editions whose files were uploaded. The version makes a re-rendered file (same name, new bytes) a new URL. |
| `media/<date>/<slot>/<file>` | publisher | Final EN and zh recap MP4s, cover PNGs and `meta.json`. Per-scene `clip_*.mp4` files are never uploaded. Kept for 40 days (`--retention-days`; the storage budget is in step 2). |
| `approvals/<date>.json` | Worker | `{"date", "approved", "requested_at", "requested_by"}`. Applied at home by the publisher. |

### Worker API

| Route | Does |
| --- | --- |
| `GET /api/snapshot` | `snapshot/latest.json`, plus `approval_request` from `approvals/<look.date>.json` (null when none). 404 before the first publish. |
| `GET /api/chart?symbol=SYM` | `chart/<KEY>.json`. 404 when not published. |
| `GET /api/look/asset?date=YYYY-MM-DD&kind=plate\|keyframe` | The look image. Any `v` parameter is ignored (it only makes the URL change when the image does). |
| `POST /api/look/approve` `{date, approved}` | Writes `approvals/<date>.json`, answers 202 `{request}`. Requires `Content-Type: application/json` and `X-Requested-With: alpaca-dashboard`, same-origin, and a date in [US/Eastern today - 1, today + 3]. `requested_by` is the Access email (or service-token name). |
| `GET /media/<path>` | R2 media with HTTP Range (206 / 416) and ETag (304). Paths are allowlisted (`history.json` or `<date>/<slot>/<file>.(mp4\|png\|jpg\|jpeg\|webp\|json)`). A video or cover asked for with its `?v=` version may be cached for an hour; without one, and for JSON, the browser revalidates every time. |

**Auth.** Every `/api` and `/media` request must carry a valid
`Cf-Access-Jwt-Assertion` (RS256, issuer = the team domain, audience =
`ACCESS_AUD`, keys from `<team domain>/cdn-cgi/access/certs`). If
`ACCESS_TEAM_DOMAIN` or `ACCESS_AUD` is unset, every such request gets 503
("authentication is not configured"). `DEV_AUTH_BYPASS=1` exists for local
development only. It is read from `apps/dashboard/.dev.vars` and works only
under `pnpm dev`, for requests whose TCP peer is loopback. The dev server
stamps each request with the peer address and a per-process key it shares
with the Worker; the Host header is not trusted. It refuses anything relayed
by a proxy or tunnel, never reaches a build, and is ignored as soon as the
Access settings are present.

### Freshness

- The publisher runs every minute and skips objects whose content has not
  changed, except the snapshot, which it uploads on every run so its `as_of`
  is never more than about a minute old. Charts are rebuilt at most every 5
  minutes.
- The SPA polls the snapshot every 30 s and the open chart every 60 s, and
  pauses while the tab is hidden.
- The `executions` section comes from the broker's closed orders, read with
  the same Alpaca key the engine trades with (about 200 requests a minute for
  the whole account). A cron run is a new process, so it reads the closed-order
  history once: one request per 500 orders, at most 20. Under `--loop` the
  history is kept between cycles (`web.server._ClosedOrders`): each cycle asks
  only for orders submitted since the newest one held, starting 15 minutes
  earlier to catch late fills, and the whole history is read again at most
  every 10 minutes, which is also the longest a fill of an order that rested
  for more than 15 minutes can take to appear.
- Every panel shows the snapshot's age. After 3 minutes without a new
  snapshot (about two missed runs), a banner reads "Home box not reporting
  since ...". A tab that comes back from the background asks again first,
  showing "checking…" (for at most 10 s) instead of raising the banner over a
  snapshot it simply had not refreshed while hidden.

## Setup guide

Do these in order. Nothing in the repository holds a real account id, token,
hostname or email; you enter those in the places below.

1. **Cloudflare account.** Sign up at dash.cloudflare.com, or use an existing
   account. The free Workers plan is enough. On first visiting
   **Workers & Pages**, pick your `*.workers.dev` subdomain. The dashboard
   will be at `https://alpaca-dashboard.<subdomain>.workers.dev`.

2. **R2.**
   1. **R2 Object Storage → Enable.** A payment card is required even though
      usage should stay in or near the free tier (10 GB-month of storage,
      1 M Class A and 10 M Class B operations a month). The publisher makes
      roughly 150–200 k writes and lists a month. Storage stays inside the
      free tier with the default 40 days of retention. Measured on the recap
      archive over the 30 days to 2026-11-26: 22 editions. Each uploads
      155 MB on average (EN + zh video, two covers, `meta.json`; per-scene
      clips stay home) and at most 174 MB, which is 114 MB per calendar day.
      Each date's look images add about 2.7 MB, and the snapshot, charts and
      history together about 1 MB. Forty days (41 dated folders) is therefore
      about 4.8 GB at the measured rate. Even with a 174 MB recap every single
      day, plus one uploaded before the hourly prune, it is about 7.4 GB, under
      an 8 GB budget.
   2. **Create bucket** named exactly `alpaca-dashboard` (automatic location,
      Standard storage class).
   3. **Bucket → Settings → Object lifecycle rules → Add rule:** prefix
      `media/`, delete objects **42 days** after upload. (A rule created
      earlier as `expire-media` with 60 days should be changed to 42.) The
      publisher deletes each date's recap 41 days after that date, so the rule
      only acts if the home box stops pruning. It then caps `media/` at about
      42 days of uploads (at most about 7.6 GB). Keep the rule at least
      `--retention-days` + 2: a shorter one deletes recaps that `history.json`
      still lists. The publisher re-sends `media/history.json` at least once a
      day, so the rule never expires the index itself. Optionally, add the
      same rule for the prefix `look/`, which holds about 2.7 MB per date.
   4. **Billing alert:** Manage Account → Billing → Notifications (or
      Notifications → Add → Usage Based Billing). Set a low threshold, for
      example USD 1, for R2.
   5. **Leave public access OFF:** Bucket → Settings → Public access →
      R2.dev subdomain stays **Disabled**, and do not connect a custom domain.
      The only way into the data must be the Worker.

3. **R2 API token for the publisher.** R2 → **Manage R2 API Tokens → Create
   API token**. Permissions: **Object Read & Write**. Specify bucket: **Apply
   to specific buckets only → `alpaca-dashboard`**. Copy the values shown once
   into the home box's `.env` (the install that holds the live data; see
   `.env.example`):

   ```sh
   R2_ACCOUNT_ID=<32-character account id>     # the R2 overview page shows it
   R2_ACCESS_KEY_ID=<Access Key ID>
   R2_SECRET_ACCESS_KEY=<Secret Access Key>
   R2_BUCKET=alpaca-dashboard
   # PUBLISHER_HOST_LABEL=home                  # optional; never a hostname
   ```

   Then try it once, before any cron (from that install):

   ```sh
   # the "publish" extra, at the exact versions in requirements/constraints.txt
   .venv/bin/python -m pip install -c requirements/constraints.txt boto3
   PYTHONPATH=. .venv/bin/python -m web.publisher --dry-run --no-media
   PYTHONPATH=. .venv/bin/python -m web.publisher --once
   ```

   The bucket should now hold `snapshot/latest.json`, `chart/*.json` and
   `look/<date>/*.png`, and after the media step `media/...`. The first media
   run uploads up to 40 days of recaps (about 5 GB) and can take a while. It runs under its
   own lock, so the next minute's snapshot is not held up.

4. **Zero Trust.** Open **Zero Trust** in the dashboard and choose a team
   name. Your team domain becomes `https://<team>.cloudflareaccess.com`. Pick
   the **Free** plan (up to 50 users). It still asks for payment details.
   Under **Settings → Authentication**, the default One-time PIN login is all
   this needs.

5. **Deploy once,** so the Worker exists. From a checkout of this repository
   on your own machine:

   ```sh
   cd apps/dashboard
   pnpm install --frozen-lockfile
   pnpm exec wrangler login        # opens a browser; your Cloudflare account
   pnpm build
   pnpm exec wrangler deploy
   ```

   At this point `/api/snapshot` answers **503 "authentication is not
   configured"**. That is the fail-closed default working, not a bug. The SPA
   shell itself loads, but it holds no data.

6. **Protect the whole Worker with Access.** Workers & Pages →
   `alpaca-dashboard` → **Settings → Domains & Routes** → on the
   `workers.dev` row, enable **Cloudflare Access**. This creates an Access
   application for the hostname. Then open it in **Zero Trust → Access →
   Applications** and check:
   - **Scope:** the `alpaca-dashboard.<subdomain>.workers.dev` hostname with
     **no path**, so every path is protected: `/`, `/api/*`, `/media/*` and
     the static assets. Preview URLs are off in `wrangler.jsonc`, so there is
     no second hostname to cover.
   - **Policy:** Action **Allow**, Include → **Emails** → your exact address,
     for example `owner@example.com`.

   > **Warning:** do NOT use "Emails ending in" `gmail.com` (or any shared
   > domain). That rule admits every Gmail user in the world.

   - **Session duration:** 24 hours is a reasonable default.

   If the one-click option is not offered, create the application by hand:
   Zero Trust → Access → Applications → **Add an application → Self-hosted**,
   domain `alpaca-dashboard.<subdomain>.workers.dev`, path empty, same policy.

7. **Give the Worker the Access settings.** From the application's
   **Overview** (or Basic information) copy the **Application Audience (AUD)
   Tag**, then store both values as Worker **secrets**:

   ```sh
   cd apps/dashboard
   pnpm exec wrangler secret put ACCESS_TEAM_DOMAIN   # https://<team>.cloudflareaccess.com
   pnpm exec wrangler secret put ACCESS_AUD           # the AUD tag
   ```

   (Or use Worker → Settings → **Variables and Secrets → Add**, type
   **Secret**.) They are secrets on purpose: `wrangler.jsonc` declares no
   `vars`, so a CI deploy never resets them. Never set `DEV_AUTH_BYPASS` here.

8. **Verify from outside.** Without a cookie, every path must redirect to the
   Access login, never return data:

   ```sh
   U=https://alpaca-dashboard.<subdomain>.workers.dev
   curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' "$U/"
   curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' "$U/api/snapshot"
   curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' "$U/media/history.json"
   # each: 302 https://<team>.cloudflareaccess.com/cdn-cgi/access/login/...
   ```

   Then open `$U` in a browser, enter your email and the one-time PIN. The
   terminal should show the account with "as of N s ago". A request that
   reaches the Worker with no or a forged `Cf-Access-Jwt-Assertion` is
   answered 401.

9. **GitHub deploys, from a protected environment.** The deploy job runs in
   a GitHub Environment named `production`, and the Cloudflare secrets live
   there, not in the repository. GitHub then hands them only to a run that
   the environment's rules admit, whatever a workflow file on another branch
   asks for. (The repository is public, so environments with branch rules and
   required reviewers are available on the free plan.)

   1. **Create the environment first:** Settings → **Environments → New
      environment** → `production`. If the deploy job ran before it existed,
      GitHub would create it with no rules.
   2. **Deployment branches and tags → Selected branches and tags → Add
      deployment branch or tag rule** → `main`. Only runs on `main` can use
      the environment and its secrets.
   3. Optional: **Required reviewers** → yourself. Every deploy then waits for
      your approval in the Actions tab. If you are the only reviewer, leave
      "Prevent self-review" off, or nobody can approve your own pushes.
   4. **Environment secrets → Add environment secret** (in this environment,
      NOT under Settings → Secrets and variables → Actions → Repository
      secrets, which any workflow on any branch can read; delete them there
      if you created them earlier):
      - `CLOUDFLARE_API_TOKEN`: a token that can deploy Workers on this one
        account and do nothing else. In the Cloudflare dashboard go to My Profile → API
        Tokens → **Create Token**, start from the **Edit Cloudflare Workers**
        template, then trim it before creating it:
        - **Account Resources:** Include → your account only (never "All
          accounts").
        - **Permissions:** keep **Account → Workers Scripts → Edit**
          (uploads the Worker and turns on its `workers.dev` route) and
          **Account → Account Settings → Read**. Delete every other row,
          including the template's read-only **User → User Details → Read**
          and **User → Memberships → Read**: a deploy does not need them, and
          on a failed login wrangler uses them to look up your email and your
          memberships. This repository's Actions logs are public; the deploy
          step runs wrangler at `WRANGLER_LOG=warn` so those lines never
          print, and without the User rows wrangler cannot read your email
          either. Delete in particular:
          - **Zone → Workers Routes:** the dashboard is on `*.workers.dev`
            and uses no zone. With it, a leaked token could put a Worker in
            front of any site on the account.
          - **Workers R2 Storage:** with it, a leaked token could read or
            write every bucket, including the `approvals/<date>.json` the
            home box applies. The Worker reaches its bucket through its
            binding, not through this token.
          - **Workers KV Storage**, **Workers Tail**, and any Pages, Builds
            or Observability rows the template carries.
        - With no zone permission left, there are no **Zone Resources** to
          choose. Optionally set a **TTL** (for example one year) and renew
          it.
        - If a deploy then fails with an authentication error, add back only
          the permission the error names, never the User rows. To see which
          user and accounts a token reaches, run `wrangler whoami` locally,
          not in CI.
        - Rename the account to something neutral (Manage Account →
          Configurations): a new account is named "<your email>'s Account",
          and on a failed login wrangler lists the name of every account the
          token can read, User rows or not.
      - `CLOUDFLARE_ACCOUNT_ID`: the account id.

   `.github/workflows/dashboard.yml` then lints, typechecks, tests and builds
   every pull request that touches `apps/dashboard/`, and deploys on every push
   to `main`. Without these two secrets the deploy job skips its steps and
   still passes.

10. **Install the publisher cron** on the home box. It must run in the same
    install as the live `.env` and data, with boto3 installed (step 3):

    ```cron
    * * * * * cd <that install> && PYTHONPATH=. .venv/bin/python -m web.publisher --once >> agent/publisher.log 2>&1
    ```

    Exit codes: 0 means ok, or another run still held the lock; 1 means a
    step failed (see the log); 2 means the configuration is missing (R2
    variables or boto3).

11. **Keep the local dashboard on loopback.** `web/server.py` keeps running
    on 127.0.0.1:8787, because the renderer needs it. The dashboards need no
    inbound port on the home box: the publisher only pushes.

    If the local dashboard was ever published some other way, for example
    through a reverse proxy on ports 80 and 443 with the router forwarding
    those ports to the box, close that once the cloud dashboard works:
    - remove the router's port forwards for 80 and 443;
    - stop and disable the proxy (for Caddy,
      `sudo systemctl disable --now caddy`) and delete the dashboard's site
      block from its configuration;
    - check that nothing listens on a public interface any more
      (`ss -ltn` should show 8787 on 127.0.0.1 only, and no `*:80` or
      `*:443`).

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| Banner "Home box not reporting since ..." | The publisher has not uploaded a snapshot for 3+ minutes. Check `agent/publisher.log` and the cron line. Exit 2 means R2 settings or boto3 are missing. A crashed builder only turns its own section into an error; it does not stop the snapshot. |
| `/api/*` gives 503 "authentication is not configured" | `ACCESS_TEAM_DOMAIN` or `ACCESS_AUD` is not set as a Worker secret (step 7). |
| 503 "authentication is not configured" although both secrets are set | `ACCESS_TEAM_DOMAIN` is not a plain https origin. It must be exactly `https://<team>.cloudflareaccess.com` (a trailing `/` is fine; no path, port or query). |
| 503 "cannot verify identity right now" | The Worker could not fetch the Access signing keys from `<team domain>/cdn-cgi/access/certs`: wrong team name, or a Cloudflare incident. |
| 401 "invalid Access token" while logged in | `ACCESS_AUD` does not match the application's AUD tag, or the application was re-created (a new AUD). Copy it again. |
| The SPA says "Signed out — reload to sign in via Cloudflare Access" | The Access session expired; requests are redirected to the login page. Reload to sign in. |
| Sign-off stays "waiting for the home box" | The publisher has not run since the request; it applies requests on its next cycle. If the page says **Not applied**, the home box refused it: `rejected` means a malformed request, or one that reached the home box after the date's 20:00 ET render had started; `superseded` means the look record was changed at home (on :8787, or redrawn) after the request. Before 20:00 ET, send it again if you still want it. After the render, the page no longer offers sign-off; if the video will be rendered again, sign off on the local dashboard. |
| Video will not play or seek | Check `media/<date>/<slot>/` in the bucket. Only editions within the retention window are uploaded, and a file still being written (under 2 minutes old) waits for the next run. |
| A chart says "No chart has been published" | The symbol is enabled but its first build has not happened, or it has no cached bars at home. Charts are rebuilt at most every 5 minutes. |
| The chart panel says no symbols are enabled, and the engine grid marks none "disabled — not polled" | The publisher could not read `sigengine/config.json` (missing, half-written or not JSON); `agent/publisher.log` says so. It then sends each engine symbol as `enabled: null` (unknown), and the grid shows every symbol's real lag rather than hiding it. Fix the file at home; the next run corrects both panels. |
| The deploy job ran but did nothing | `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ACCOUNT_ID` are missing from the `production` environment (step 9); the run shows a "Deploy skipped" notice. Repository secrets of the same name are not what the job is meant to read: move them into the environment. |
| The deploy job waits for approval, or says the branch is not allowed | The `production` environment's rules (step 9): approve the run in the Actions tab, or deploy from `main`. |
| Installing the app (PWA) fails | Install from a tab where you are signed in: the page requests the manifest with the Access cookie (`crossorigin="use-credentials"`). If the browser still cannot install it, use its "Add to Home screen" (a shortcut needs no manifest). Do **not** add a Bypass policy to the dashboard's Access application to make the manifest or icons public: policies apply to every path of their application, so a Bypass there turns off the edge gate for the whole hostname, SPA and API included. |
| R2 costs rising | Lower `--retention-days`, and lower the `media/` lifecycle rule with it (to retention + 2 days). The rule (step 2) bounds `media/` even when the home box stops pruning. |

## Local development

See [`apps/dashboard/README.md`](../apps/dashboard/README.md). In short:
`cp .dev.vars.example .dev.vars && pnpm seed:local && pnpm dev` serves
synthetic fixtures at http://localhost:5173. To look at real data locally
without it entering the repository, write the layout with
`python -m web.publisher --out-dir DIR` and serve it with
`pnpm seed:local --from-dir DIR --persist-to <scratch>` and
`ALPACA_DASHBOARD_STATE_DIR=<scratch> pnpm dev`.
