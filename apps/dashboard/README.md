# Alpaca dashboard (cloud)

A read-only view of the trading system that runs on the home box, hosted as one
Cloudflare Worker on `*.workers.dev`:

- **SPA**: Vite + React 19 + TypeScript, served from Workers Static Assets.
- **Worker** (`worker/index.ts`): answers `/api/*` and `/media/*`, reading an R2
  bucket bound as `DATA`. No Durable Objects, no KV.
- **Access**: Cloudflare Access protects the whole Worker at the edge, and the
  Worker verifies the `Cf-Access-Jwt-Assertion` header itself, failing closed
  when `ACCESS_TEAM_DOMAIN` / `ACCESS_AUD` are unset.

Data only ever flows **home → cloud**. `web/publisher.py` on the home box runs
the read-only builders in `web/server.py` and uploads the results to R2 over
the S3 API. The cloud never calls the home box, and broker keys never leave it.
The local dashboard (`web/server.py` on 127.0.0.1:8787) keeps running; the video
renderer screenshots it.

The R2 layout, every payload shape and the Worker API types are in
[`src/contract.ts`](src/contract.ts). Units: epoch **seconds** in true UTC,
ratios as fractions (0.0123 = 1.23%).

## Recap videos

The Briefing page plays the English recap **from YouTube**; R2 holds no copy
of a video that is on YouTube.

- `history.json` gives an edition `youtube_id` when its EN upload is public or
  unlisted (a private video cannot be embedded). The page shows the edition's
  R2 cover (or a plain placeholder) as a play button, and **nothing is
  requested from YouTube until it is pressed**: no script, no thumbnail. The
  click creates an iframe from `https://www.youtube-nocookie.com/embed/<id>`
  (`src/lib/youtube.ts`, ported from the public portfolio). A chapter in the
  running order reloads that player at its start (`&start=<s>`).
- An edition still without an embeddable id three hours after its render
  gets its EN mp4 uploaded as a **fallback** instead (a refused or failed
  YouTube publish records nothing, so the wait is the only signal): `briefing`
  in `history.json`, played in a plain `<video>` as before. Until then the
  edition is not listed. The publisher removes that mp4 once an id appears.
  An entry with neither is never listed, and the page drops one itself.
- **The Chinese cut stays local.** It is still rendered and archived at home
  and shown on the local `:8787` page, but no zh video, cover, title or field
  is uploaded to R2, YouTube or this dashboard, and the page has no language
  switch.
- `public/_headers` allows exactly one third-party source,
  `frame-src https://www.youtube-nocookie.com` (`devserver/headers.test.ts`
  holds the whole CSP to its exact string). The page keeps
  `Referrer-Policy: no-referrer`; the player's iframe alone sends
  `strict-origin-when-cross-origin`, because YouTube's embed will not play
  without a referrer, so YouTube learns this origin (never a path), and only
  once play is pressed.

## Develop

Requires Node ≥ 22.22 and pnpm (pinned in `package.json` `packageManager`;
`corepack enable` picks it up).

```sh
pnpm install
cp .dev.vars.example .dev.vars   # local only: DEV_AUTH_BYPASS=1
pnpm seed:local                   # synthetic fixtures into the local R2 simulation
pnpm dev                          # Vite + workerd, http://localhost:5173
```

| Script | What it does |
| --- | --- |
| `pnpm dev` | Vite dev server; the Worker runs in workerd with local R2 under `.wrangler/state`. |
| `pnpm build` | `tsc -b`, then builds the client and the Worker into `dist/`. |
| `pnpm preview` | Build, then serve the production build locally. The dev auth bypass does not apply there (the API answers 503 without Access). |
| `pnpm lint` | oxlint, warnings fail. |
| `pnpm typecheck` | `tsc -b` over the app, the Worker and the build config. |
| `pnpm test` | Vitest: project `app` (jsdom, `src/**/*.test.ts(x)`) and project `worker` (Node, `worker/**/*.test.ts`). |
| `pnpm cf-typegen` | Regenerate `worker-configuration.d.ts` after changing `wrangler.jsonc`. |
| `pnpm cf-typegen:check` | Fail if `worker-configuration.d.ts` is out of date. |
| `pnpm seed:local` | Load `fixtures/` into local R2 (see `fixtures/README.md`). |

`worker-configuration.d.ts` is generated with `--env-file=/dev/null`, so a local
`.dev.vars` never leaks into it; `DEV_AUTH_BYPASS` is declared (optional) in
`worker/env.d.ts`.

### The dev auth bypass

`DEV_AUTH_BYPASS=1` (from `.dev.vars` only) skips the Access JWT check so the
API works under `pnpm dev`. It takes effect only when the value is exactly `1`,
`ACCESS_TEAM_DOMAIN` and `ACCESS_AUD` are both unset, AND the request comes
straight from this machine through `vite dev`:

- **It passed through `vite dev`, from a loopback TCP peer.** The Host header
  proves nothing (any client can send `Host: localhost`), so `vite dev` runs
  `devserver/peerStamp.ts` first. It makes a random key per dev server process
  and gives it to the Worker as the var `DEV_PEER_KEY`. On every request it
  overwrites `X-Alpaca-Dev-Peer` with the connection's peer address and
  `X-Alpaca-Dev-Peer-Key` with the key, whatever the client sent. The Worker
  (`worker/devPeer.ts`, `worker/auth.ts`) wants both: the right key and a
  loopback peer. Starting the server with `--host` to try the phone layout is
  therefore safe, because other devices get 503, not data. Runtimes without
  the middleware get no key, so forged headers fail there too. That covers
  `wrangler dev`, which reads the same `.dev.vars`, and `pnpm preview`, whose
  build also has `DEV_AUTH_BYPASS` stripped from its `.dev.vars` copy.
- **Nothing relayed it.** A request carrying `X-Forwarded-For`, `Forwarded`,
  `X-Real-IP`, `True-Client-IP` or a non-loopback `CF-Connecting-IP` is
  refused, so a tunnel or reverse proxy on this machine cannot hand the
  bypass to the internet.
- **Its host is loopback** (`localhost`, `127.0.0.1`, `[::1]`,
  `*.localhost`), which also keeps it off any `*.workers.dev` hostname.

Once the Access variables are set it is ignored. Responses served under it
carry `X-Dev-Auth-Bypass: 1`.

### Real data locally

`python -m web.publisher --out-dir DIR` (repo root) writes the bucket layout
from the live builders. Serve it without letting real data into the repo:

```sh
pnpm seed:local --from-dir DIR --persist-to /some/scratch/state
ALPACA_DASHBOARD_STATE_DIR=/some/scratch/state pnpm dev
```

## Layout

```
worker/            the Worker: index.ts (routing, status codes), auth.ts (Access
                   JWT), devPeer.ts (the dev bypass's loopback-peer rule),
                   routes.ts (the five routes), objects.ts (R2 reads with
                   Range/ETag), validate.ts (path + parameter allowlists),
                   dates.ts (US/Eastern approval window), http.ts (headers)
devserver/         peerStamp.ts: the Vite plugins behind the dev bypass (stamp
                   each request's TCP peer and a per-process key under vite
                   dev; keep DEV_AUTH_BYPASS out of builds), a test that
                   every icon index.html and the manifest name is in public/,
                   and one that holds public/_headers' CSP to its exact string
worker/test/       in-memory R2 (fake-r2.ts), runtime-minted Access tokens
                   (access.ts), a request driver (call.ts)
src/contract.ts    the R2 contract and API types, shared by both halves
src/app/           providers, the shell (header, freshness bar, stale banner),
                   routes: / (terminal), /briefing, /look
src/features/      terminal/ (account, positions + options sleeve, board,
                   engine, chart, executions, journal), briefing/, look/
src/lib/           fetch wrapper + error classes (api.ts), TanStack Query hooks
                   and polling (queries.ts), freshness, time zones, formatting,
                   the YouTube embed URL (youtube.ts)
src/test/          fixtures.ts (typed fixtures), mockApi.ts (simulates the
                   Worker routes from the fixtures for component tests)
public/_headers    CSP and security headers for the static assets
fixtures/          synthetic data for tests and `pnpm seed:local`
```

Polling: the snapshot every 30 s and the open chart every 60 s, paused while
the tab is hidden. Every panel shows the snapshot's age, and a banner appears
when it is older than 3 minutes. Back from a hidden tab, the page asks again
first and shows "checking…" beside the age until that answer arrives (at most
10 s); only an answer that is still old raises the banner.

## Deploy

CI deploys on a push to `main` from the GitHub Environment `production`,
which holds the Cloudflare secrets and admits only `main` (see
`.github/workflows/dashboard.yml` and step 9 of `docs/cloud-dashboard.md`).
Deploying needs the R2 bucket, the Access application and the
`ACCESS_TEAM_DOMAIN` / `ACCESS_AUD` Worker secrets first. `DEV_AUTH_BYPASS`
must never be set on a deployed Worker.

Installing the app (PWA) works from a signed-in tab: the manifest is requested
with the Access cookie. Never add an Access Bypass policy to the dashboard's
application to make it public; that switches the edge gate off for every
path.
