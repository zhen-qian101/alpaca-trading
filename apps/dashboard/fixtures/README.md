# Fixtures

Everything in this directory is **synthetic**: invented symbols (`ACME`, `NOVA`,
`ORBT`, `QBIT/USD`, `OFFL`), invented prices, fills, notes and people. It is not data
from any real account, and nothing here may be replaced with captured data.

Each file conforms to `src/contract.ts`; `src/contract.test.ts` checks that it
does. Tests should load them through `src/test/fixtures.ts`, which casts them
to the contract types once.

| File | R2 key it stands in for | Notes |
| --- | --- | --- |
| `snapshot.json` | `snapshot/latest.json` | `as_of` 2026-03-04T21:05Z (after the US close). One open position (ACME), a contingency on NOVA, journal rows of several event types including one legacy ISO `ts`, an option fill, a crypto round trip, an unjudged look (`approved: null`), an options sleeve with one live short ORBT put, and in the engine block a symbol the engine no longer polls (`OFFL`, `enabled: false`, last bar 2158 h old). |
| `chart-ACME.json` | `chart/ACME.json` | 220 hourly bars, BB + RSI + MACD, signal arrows, an account exit, a fill. |
| `chart-NOVA.json` | `chart/NOVA.json` | 160 bars, MACD only: Bollinger/RSI series are empty and `bbLabel` is `""`. |
| `chart-QBIT-USD.json` | `chart/QBIT-USD.json` | Crypto, 24/7 bars, keyed `QBIT/USD` inside the file. |
| `history.json` | `media/history.json` | Three editions, English only (the Chinese cut never reaches the cloud): a recap on YouTube (`youtube_id` `FAKEyt00001`, a cover, no mp4 in R2), a slideshow that did not reach YouTube and plays its **fallback** mp4 (`briefing` + cover), and a legacy story render on YouTube (`FAKEyt00002`) with no cover, whose chapters carry recorded `start_s`. The YouTube ids are fake: nothing here may name a real video. Every URL ends in `?v=` + the first 12 hex of the SHA-256 of the sample file `pnpm seed:local` uploads for it. |
| `approval-request.json` | `approvals/<date>.json` | A pending sign-off request. |
| `look/plate.png`, `look/keyframe.png` | `look/<date>/{plate,keyframe}.png` | Test patterns. |
| `media/sample.mp4`, `media/cover.png` | the fallback mp4 and every cover in `history.json` | One 2-second test-pattern video and one cover, reused for all editions. |

`ORBT` is in `snapshot.symbols` but has **no** chart fixture on purpose: it
exercises the "chart not published yet" state.

All chart times are TRUE UTC epoch seconds. Equity bars sit on 09:00–15:00
US/Eastern, which the contract test uses to catch a Melbourne-shifted time.

## Local R2

`pnpm seed:local` uploads these files into the local R2 simulation that
`pnpm dev` serves (`.wrangler/state`), at the keys above. By default it stamps
`as_of` with the current time and re-dates the look to today's US/Eastern
session date so the approve flow is inside its date window; `--as-is` uploads
the files unchanged and `--with-approval` also writes a pending approval.

`pnpm seed:local --from-dir DIR` uploads a publisher `--out-dir` tree instead
of these fixtures. That tree holds REAL account data, so pair it with
`--persist-to <scratch dir>` and start `pnpm dev` with
`ALPACA_DASHBOARD_STATE_DIR=<scratch dir>`: the data then never lands in
`.wrangler/` inside the repository.
