# Alpaca Trading System: public portfolio site

A public, static site about the paper trading system in this repository: what
it is, how it has done (from the account's own figures), the rules it trades
by and how it is built. It is separate from the private ops dashboard in
`apps/dashboard`, which stays behind Cloudflare Access; the two share no code.

- **Pages** (one SPA, client routes): Overview `/`, Trades `/trades`,
  Strategy & risk `/strategy`, How it's built `/how-its-built`, Recaps
  `/recaps`. Unknown paths render a not-found page.
- **Data**: one static file, `/data/portfolio.json`, written on the home box by
  `web/public_export.py` and gated there by
  [`schema/portfolio.schema.json`](schema/portfolio.schema.json). The page
  checks it again on load (`src/data/portfolio.ts`) and shows nothing from a
  file that fails.
- **Hosting**: a Cloudflare Worker named `alpaca-portfolio` with static assets
  only: no script, no bindings, no routes beyond `workers.dev`
  ([`wrangler.jsonc`](wrangler.jsonc)). Every request is a static-asset request.

## What is and is not shown

Owner decisions, enforced by the exporter's whitelist, the schema and this
page's own check. They cover the data file and the site's own pages and
charts; the recap videos it embeds are described in the last point.

- Only the account's own figures: equity curve, P&L, round trips at its own fill
  prices, the research board's bias, priority, size and standing orders, and
  the levels the system set itself (stops, put strikes). **No third-party
  market data** in the file or on the site's own pages and charts: no candles,
  bars, quotes, current or last prices, or market values.
- **Daily equity only**: one point per session and no intraday series, which
  with a position open would trace that symbol's price through the day.
- **No equity from a session still in progress**: equity, returns and the
  curve stop at the last completed session. With a position's size and entry
  price, the current equity would give back that symbol's live price; the
  published one gives back only its price at the end of that session.
- **No model-written text** in the file or on the site's own pages: no
  research notes, contingency reasons or recap titles (each recap is labelled
  by its date). No logs, account or order ids, paths, host names, or presenter
  data in the file.
- Recaps: English editions only, as YouTube embeds, and only those whose recorded
  YouTube privacy is `public`. The site never lists the zh cut. The videos
  themselves are AI-generated and are not covered by the points above: a
  language model writes their scripts and YouTube titles, an AI-generated
  presenter reads them on weekdays, and most of them show the live dashboard,
  including market candles and current prices. The Recaps page says so.
- No licence is granted for this repository (all rights reserved).

## Develop

Node ≥ 22.22 and pnpm (pinned in `package.json` `packageManager`;
`corepack enable` picks it up).

```sh
pnpm install
pnpm dev            # http://localhost:5173, data from fixtures/portfolio.json
```

`pnpm dev` serves `/data/portfolio.json` from the synthetic fixture. To look at
a real export, point it at a file kept **outside** the repository:

```sh
PORTFOLIO_DATA=/some/scratch/dir/portfolio.json pnpm dev
```

| Script | What it does |
| --- | --- |
| `pnpm dev` | Vite dev server with the fixture (or `PORTFOLIO_DATA`) at `/data/portfolio.json`. No security headers. |
| `pnpm build` | `tsc -b`, then `vite build` into `dist/`. The build carries no data. It writes `dist/third-party-licenses.txt`, the licences of every bundled package plus the notices their npm packages leave out (`scripts/licenseNotices.ts`), and fails if it cannot complete that file. |
| `pnpm check-licenses [DIST]` | Check that DIST (default `dist/`) has a complete `third-party-licenses.txt`: every runtime dependency listed, every listed package with a licence text, the TradingView NOTICE present. CI runs it after the build. |
| `pnpm check-data FILE` | Gate FILE with `schema/portfolio.schema.json` (`scripts/schemaGate.ts`, the CI gate). Refuses an object with the same key twice (`scripts/strictJson.ts`). Prints JSON pointers and rules, never values. |
| `pnpm stage-data --from FILE` | Read FILE strictly, check it with the page's own validator and WRITE the checked value (re-serialised, never FILE's bytes) to `dist/data/portfolio.json`. `--dist DIR` stages into another build; `--quiet` prints no counts. |
| `pnpm dev:assets` | `wrangler dev` over `dist/`, exactly as deployed, `_headers` included. Build and stage first. Port 8787 by default; add `--port` where that is taken. |
| `pnpm lint` | oxlint (with jsx-a11y), warnings fail. |
| `pnpm typecheck` | `tsc -b` over the app, the scripts and the build config. |
| `pnpm test` | Vitest: project `app` (jsdom, `src/**`) and project `config` (Node, `scripts/**`: site config, headers, the schema gate and the workflow invariants). |

The fixture (`fixtures/portfolio.json`) is synthetic: invented tickers and
numbers, generated by the real exporter from the Python test suite's fake
broker. Never commit a real export.

## Deploy (owner only)

The figures are part of the deployed assets, so refreshing them means
deploying again. Normally GitHub Actions does it
(`.github/workflows/portfolio.yml`): on every push to `main`, on a schedule
(about every 30 minutes through the US session, every 6 hours otherwise), and
on demand. Job `build` builds the site with no secrets and hands it on as an
artifact; job `deploy`, which installs nothing from this lockfile, downloads
that build and `portfolio.json` from the R2 bucket `alpaca-public` with a
read-only token, gates the file with `scripts/check-portfolio.ts` (fails
closed), writes the checked value in with `scripts/stage-data.ts` and runs
`wrangler deploy` (wrangler installed on its own, at the locked version).
Setup, schedule, platform limits and how to take the site down:
[`docs/public-portfolio.md`](../../docs/public-portfolio.md).

By hand, from the home box, after the exporter has run:

```sh
# 1. In the trading install: write the file somewhere outside the repository.
PYTHONPATH=. .venv/bin/python -m web.public_export --out-dir /path/to/out

# 2. In apps/portfolio: build, gate, stage, deploy.
pnpm install --frozen-lockfile
pnpm build
pnpm check-data /path/to/out/portfolio.json
pnpm stage-data --from /path/to/out/portfolio.json
pnpm exec wrangler deploy
```

`check-data` and `stage-data` each refuse a file that fails (exit 1);
`check-data` exits 2 when the schema itself is missing or not strict enough.
After the first deploy, put the `workers.dev` origin in `site.config.json`
`siteUrl` so the canonical link and the social-card image get absolute URLs.

CI (the same workflow's `build` job) lints, typechecks, tests and builds on
every change, then rehearses the deploy's gates on the fixture and a copy of
the build.

## site.config.json

| Field | Meaning |
| --- | --- |
| `title` | Page and social title. |
| `description` | Meta and social description. Keep it generic. |
| `siteUrl` | The deployed origin, `https://…`, no trailing slash. Empty until known; social image tags are left out while it is empty. |
| `repoUrl` | The public repository. |
| `author.name` | Display name for the footer's copyright line. Empty shows no name. |
| `author.links` | `[{label, url}]`, https only. Entries with an empty label or URL are skipped. |

The file is checked at build time and in the bundle (`src/lib/siteConfig.ts`);
an unknown key or a non-https URL fails the build.

## Security and privacy

- **CSP** (`public/_headers`): `default-src 'none'`, scripts and data from this
  origin only, no inline script (the theme is applied by `public/theme-init.js`).
  `style-src` allows one hash and no `'unsafe-inline'`: the `<style>` that
  lightweight-charts injects for its TradingView attribution logo, which stays
  on. `scripts/config.test.ts` recomputes that hash from the installed library,
  so an upgrade that changes it fails the tests.
- **YouTube**: recaps are click-to-play facades. Nothing is requested from
  YouTube (not even a thumbnail) until play is pressed; then the player loads
  from `youtube-nocookie.com`, the only `frame-src`. `Referrer-Policy` is
  `strict-origin-when-cross-origin` because YouTube's embedded player refuses
  to play without a referrer; only the origin is sent.
- **Network**: the page fetches `/data/portfolio.json` and nothing else
  (`connect-src 'self'`). The tests check every page for third-party requests.

## Design

The account book of a trading machine, set like a columnar ledger pad: pale
green-grey paper with green rules (and the same pad at night). Gains are set in
ink and losses in red ink, always with their sign; a single rule over a total
and a double rule under it mark totals. Pencil blue is the one interactive
colour. One family, [Archivo](https://github.com/Omnibus-Type/Archivo)
(variable weight and width, SIL OFL 1.1, `public/fonts/OFL.txt`): condensed for
headings and figures, normal width for reading, tabular figures in tables.

Accessibility: WCAG AA contrast in both themes, a theme toggle that follows the
system setting until used, keyboard-scrollable table regions, sortable column
headers with `aria-sort`, focus moved to the page heading on navigation, and a
text summary plus a full data table for every chart.
