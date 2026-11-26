/**
 * The R2 contract between the home box and the cloud dashboard.
 *
 * Data flows ONE way. `web/publisher.py` on the home box runs the read-only
 * builders in `web/server.py` and uploads their output to the R2 bucket; the
 * Worker (worker/index.ts) only reads it, plus the single approval object it
 * writes. Nothing in the cloud ever calls the home box.
 *
 *   snapshot/latest.json             Snapshot
 *   chart/<KEY>.json                 ChartPayload   KEY = chartKey(symbol)
 *   look/<date>/plate.png            wardrobe plate image
 *   look/<date>/keyframe.png         stage keyframe image
 *   media/history.json               History        (URLs rewritten to /media/)
 *   media/<date>/<slot>/meta.json    EditionMeta (zh fields stripped)
 *   media/<date>/<slot>/<cover>.png  the EN cover: the poster for either player
 *   media/<date>/<slot>/<video>.mp4  the EN recap, ONLY as the fallback for an
 *                                    edition that did not reach YouTube
 *                                    (Edition.briefing)
 *   approvals/<date>.json            ApprovalRequest, written by the Worker,
 *                                    applied at home with set_look_approval()
 *
 * RECAP VIDEOS play from YouTube (Edition.youtube_id), embedded from
 * youtube-nocookie.com only after the viewer presses play. The Chinese (zh)
 * cut is NEVER uploaded: it is rendered and archived at home and shown on the
 * local :8787 page only, so no zh video, cover, title or field exists anywhere
 * in the bucket, in history.json or in an uploaded meta.json. The per-scene
 * clip_*.mp4 files are never uploaded either. Each media sync deletes every
 * other object under media/<date>/ for the dates in its retention window
 * (and whole dates past it), so the bucket converges to exactly this set.
 *
 * Most shapes mirror a Python builder field for field, and each interface
 * names its source. Snake_case is kept on purpose: these objects are JSON the
 * Python side writes, and renaming fields here would hide the mapping.
 *
 * UNITS used throughout:
 *   EpochSeconds  seconds since 1970-01-01T00:00:00Z, TRUE UTC, may be
 *                 fractional. Never milliseconds. Format for display in the
 *                 viewer's zone (or a chosen one); never read the UTC getters
 *                 as wall clock.
 *   IsoUtc        ISO-8601 timestamp with an explicit offset, written by
 *                 Python as e.g. "2026-03-02T21:05:00+00:00".
 *   IsoDate       "YYYY-MM-DD". Session dates are the US/Eastern trading date.
 *   Fraction      a ratio: 0.0123 means 1.23%. Multiply by 100 to show a %.
 *   USD           a plain dollar amount (not cents).
 *
 * CHART TIME: backtest/chart_view.py stamps candles with Melbourne wall clock
 * re-tagged as UTC ("fake UTC") because the old dashboard displayed with UTC
 * getters. The publisher undoes that, so every `time` in a ChartPayload is a
 * true UTC epoch and `time_basis` is "utc". The SPA must format chart times in
 * an explicit zone (lightweight-charts `localization.timeFormatter` /
 * `tickMarkFormatter`), not rely on the library's UTC display.
 *
 * Free text (journal reasons, research notes, stylist rationale, titles) is
 * written by LLMs and by the engine. Render it as text, never as HTML.
 */

// ---------------------------------------------------------------- primitives

/** Seconds since the Unix epoch, true UTC. Not milliseconds. */
export type EpochSeconds = number
/** ISO-8601 timestamp with explicit offset, e.g. "2026-03-02T21:05:00+00:00". */
export type IsoUtc = string
/** Calendar date "YYYY-MM-DD". */
export type IsoDate = string
/** Ratio: 0.0123 = 1.23%. */
export type Fraction = number
/** US dollars. */
export type USD = number
/**
 * A same-origin media URL served by the Worker from R2:
 * "/media/<date>/<slot>/<file>?v=<first 12 hex of the file's SHA-256>". The
 * version makes a re-rendered file (same name, new bytes) a new URL; the
 * Worker picks the object by the path alone. Older publishers sent no `v`.
 */
export type MediaUrl = `/media/${string}`

/** Research bias for a symbol (decisions.json). */
export type Bias = 'long' | 'short' | 'flat'

/**
 * Every Python builder reports failure by RETURNING this instead of raising
 * (broker unreachable, no decisions file, no cached bars...). A section of the
 * snapshot can therefore be an error while the rest is fine; render the error
 * in that panel only.
 */
export interface BuilderError {
  error: string
}

/** True when a builder section (or an API body) is an error object. */
export function isBuilderError(value: unknown): value is BuilderError {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as { error?: unknown }).error === 'string'
  )
}

// ------------------------------------------------------------------ snapshot

/** Current contract version of snapshot/latest.json. */
export const SNAPSHOT_SCHEMA = 1

/**
 * snapshot/latest.json. Re-sent by the publisher on EVERY run (every minute),
 * changed or not: `as_of` is the only sign that the home box is alive.
 */
export interface Snapshot {
  schema: typeof SNAPSHOT_SCHEMA
  /** When the publisher assembled this snapshot. Drives every "as of / N min ago" label. */
  as_of: IsoUtc
  publisher: PublisherInfo
  portfolio: Portfolio | BuilderError
  board: Board | BuilderError
  /**
   * build_journal(). It degrades to empty rows rather than failing, but the
   * publisher wraps every builder, so an unexpected exception still arrives
   * as {error}.
   */
  journal: Journal | BuilderError
  executions: Executions | BuilderError
  /**
   * Enabled symbols from sigengine/config.json, in config order, in their
   * TRADING spelling (e.g. "AAPL", "BTC/USD"). One chart/<chartKey(s)>.json
   * exists per entry once the publisher has built it.
   */
  symbols: string[]
  /** build_look(<US/Eastern session date at publish time>). */
  look: Look | BuilderError
  /**
   * Optional: the weekly cash-secured-put sleeve, which build_portfolio()
   * filters out of `portfolio.positions` (web/options_view.py build_options()).
   * Absent when the publisher does not report it; null when the sleeve has no
   * policy file (decisions/csp.json) on the home box. See OptionsSleeve.
   */
  options?: OptionsSleeve | BuilderError | null
}

export interface PublisherInfo {
  /** Publisher version string (e.g. a semver or a git describe). */
  version: string
  /** A short label for the publishing machine. Not a network address. */
  host: string
  /**
   * Optional: what the home box did with the newest approval request it saw
   * for `look.date`. Lets the SPA tell "still waiting" from "the home box
   * refused or could not apply it". Absent until a request has been handled.
   */
  approval?: PublisherApproval | null
}

/**
 * The publisher's record of the approvals/<date>.json it handled last.
 * `requested_at` identifies the request (compare it with
 * `approval_request.requested_at`). Outcomes:
 *   "applied"                  written to the look record
 *   "already approved" / "already not approved"  nothing to do
 *   "superseded: ..."          the look record changed at home after the request
 *   "rejected: ..."            malformed, or it reached the home box at or after
 *                              the date's 20:00 ET render (RENDER_HOUR_ET); never applied
 *   "error: ..."               the write failed; `done` false while it retries
 * Once `done` is true for a request, the home box has finished with it: from
 * then on the look record alone says whether the look is signed off, even if
 * it is changed again at home.
 */
export interface PublisherApproval {
  date: IsoDate
  approved: boolean | null
  requested_at: IsoUtc | null
  outcome: string
  /** False while a failed apply will be retried. */
  done: boolean
  /** When the home box handled it. */
  at: IsoUtc | null
}

// ------------------------------------------------------------------ portfolio

/** web/server.py build_portfolio(): Alpaca account + open EQUITY positions. */
export interface Portfolio {
  equity: USD
  /** Equity at the previous close (Alpaca last_equity). */
  last_equity: USD
  cash: USD
  buying_power: USD
  /** equity - last_equity. */
  day_pl: USD
  /** equity / last_equity - 1. */
  day_pl_pct: Fraction
  /** True on a paper account. The old UI badges `false` as "LIVE" in red. */
  paper: boolean
  /** Alpaca account status enum name, e.g. "ACTIVE". */
  status: string
  /** Option positions are filtered out. Sorted by market_value, largest first. */
  positions: Position[]
  halt: DailyLossHalt
}

export interface Position {
  symbol: string
  side: 'long' | 'short' | null
  /** Shares (fractional for crypto). */
  qty: number
  avg_entry_price: USD
  current_price: USD
  market_value: USD
  cost_basis: USD
  unrealized_pl: USD
  unrealized_plpc: Fraction
  /** Configured synthetic stop distance below entry (e.g. 0.02); null when none. */
  stop_pct: Fraction | null
  /** avg_entry_price * (1 - stop_pct). The stop is enforced by the engine in code; no order rests at the broker. */
  stop_price: USD | null
  /** current_price / stop_price - 1. Negative means price is already through the stop. */
  stop_distance_pct: Fraction | null
  /** decisions.json priority for this symbol, or null when it has none. */
  priority: number | null
  bias: Bias | null
}

/** Mirror of relay.risk_filter's daily circuit breaker. BUYS ONLY; exits are never blocked. */
export interface DailyLossHalt {
  /** Configured limit clamped by hard_cap, e.g. 0.05. */
  limit: Fraction
  hard_cap: Fraction
  /** True when equity / last_equity - 1 <= -limit: new buys are blocked today. */
  tripped: boolean
  /** Share of today's loss budget used, clamped to 0..1 (for a meter). */
  used_frac: Fraction
  /** Human sentence, currently "buys only — exits always allowed". */
  applies_to: string
}

// ---------------------------------------------------------------------- board

/** web/server.py build_board(): decisions.json as a rotation board. */
export interface Board {
  /** The research date written into decisions.json. */
  date: IsoDate | null
  /** Highest priority first, then symbol. priority > 0 = live rotation candidate. */
  rows: BoardRow[]
  max_open_positions: number | null
  daily_loss_limit: Fraction | null
  allow_extended_hours: boolean | null
  /**
   * Research notes keyed by symbol; the key "_" is the market overview. LLM
   * text. (A non-object `notes` in decisions.json arrives as {"_": text}.)
   */
  notes: Record<string, string>
  /** decisions.json modification time. */
  mtime: EpochSeconds | null
}

export interface BoardRow {
  symbol: string
  /** "flat" when the symbol has no bias entry. */
  bias: Bias
  /** 0 when absent. Higher = more preferred. */
  priority: number
  /**
   * Share of equity the engine would deploy on entry (0..1), derived from bias.
   * The engine applies it to equity MINUS the put sleeve's reserve
   * (decisions/csp.json reserve_frac) while the sleeve is enabled.
   */
  suggested_size: Fraction | null
  /** Max shares (a safety cap; the engine sizes off equity). */
  max_position: number | null
  /** Pre-authorised price triggers; [] when none. */
  contingencies: Contingency[]
}

/**
 * A pre-approved override (agent/decisions_schema.py): when a bar CLOSES past
 * `level`, the engine may enter `action` even though bias says flat/short.
 */
export interface Contingency {
  condition: 'close_above' | 'close_below'
  level: USD
  /** Only "long" is supported. */
  action: 'long'
  /** Expiry; at most 48 h after the research run. */
  valid_until: IsoUtc
  /** LLM text, at most 500 chars. */
  reason: string
}

// -------------------------------------------------------------------- journal

/** web/server.py build_journal(): the trades.jsonl tail plus engine liveness. */
export interface Journal {
  /** Up to 60 most recent rows, NEWEST FIRST. */
  rows: JournalRow[]
  engine: EngineStatus
}

/**
 * One trades.jsonl event. The journal is open-ended: every event type carries
 * its own extra fields (quotes, strikes, sizing...), so only the common ones
 * are typed and the rest stay reachable through the index signature.
 */
export interface JournalRow {
  /** e.g. "order_ok", "risk_block", "signal_skip", "rotation_close", "reconcile_close", "correction", "csp_sell"... */
  event: string
  /** Raw timestamp as written: an epoch float, or an ISO string on legacy rows. Prefer `_ts`. */
  ts: number | string
  /** `ts` normalised to EpochSeconds by the server; null when unparseable. */
  _ts: EpochSeconds | null
  /** Trading symbol the row is about. Most rows have it. */
  ticker?: string
  /** Some rows (option events) name the contract here; show `symbol ?? ticker`. */
  symbol?: string
  /** "buy" | "close" | "sell" ... */
  action?: string
  /** Free text from the engine or relay. */
  reason?: string
  /** e.g. "skip", "blocked", "closed", "submitted_market", "closed_limit_extended"; often null/absent. */
  status?: string | null
  qty?: number | null
  /** Strategy that produced a signal row, e.g. "macd_sma", "bollinger_rsi". */
  strategy?: string
  /** Bar time of a signal row (ISO string). */
  bar_ts?: string
  [extra: string]: unknown
}

export interface EngineStatus {
  /** Per-symbol newest cached bar, alphabetical. */
  symbols: EngineSymbol[]
  /** Newest bar across all symbols. */
  newest_bar: EpochSeconds | null
  /** sigengine/state.json mtime = end of the last completed poll. */
  last_poll: EpochSeconds | null
  /** A sigengine process exists on the home box (presence only). */
  running: boolean
  /** A poll completed within stale_after_sec. "live" in the UI = running && polling. */
  polling: boolean
  /** Seconds after which a missing poll means stalled (900). */
  stale_after_sec: number
  log_mtime: EpochSeconds | null
  /** Last 12 lines of the engine log. */
  log_tail: string[]
  /** Symbols whose recent polls fell back to yfinance (IBKR unavailable). */
  yfinance_fallback: string[]
}

export interface EngineSymbol {
  symbol: string
  last_bar: EpochSeconds | null
  /** Seconds behind newest_bar. About one bar of lag on one symbol is an expected transient. */
  behind_s: number | null
  /**
   * Whether sigengine/config.json has the symbol enabled, added by the
   * publisher. false: the engine does not poll it (disabled, or no longer
   * configured), so its last bar is old by design and `behind_s` is not lag.
   * null: the publisher could not read the config, so it does not know; show
   * the row as polled, with its lag. Absent from older publishers; treat
   * absent like null.
   */
  enabled?: boolean | null
}

// ----------------------------------------------------------------- executions

/** web/server.py build_executions(): broker fills paired into round trips (average cost). */
export interface Executions {
  /** Up to 60 fills, NEWEST FIRST. */
  rows: ExecutionRow[]
  /** Sum of realized P&L over every closed order fetched (up to 500), not just `rows`. */
  realized_pnl: USD
  /** Number of sells that realized a P&L. */
  round_trips: number
  /** Winning share of round_trips (0..1); null when there are none. */
  win_rate: Fraction | null
}

export interface ExecutionRow {
  /** Broker fill time; null when the broker reported none. */
  ts: EpochSeconds | null
  /** Equity ("AAPL"), crypto ("BTC/USD") or an OCC option symbol ("XYZ261218P00042000"). */
  symbol: string
  side: 'buy' | 'sell'
  qty: number
  /** Executed average fill price. */
  price: USD
  /** Buy: the fill price. Sell: the average cost being closed against (null if unknown). */
  entry_price: USD | null
  /** Sell only. */
  exit_price: USD | null
  /** Realized P&L in dollars; sells only. */
  pnl: USD | null
  /** exit_price / entry_price - 1; sells only. */
  pnl_pct: Fraction | null
  /** "market" | "limit" | ... */
  order_type: string
  /** Position size left after this fill. */
  held_after: number
}

// ---------------------------------------------------------------------- chart

/**
 * chart/<KEY>.json: build_chart(symbol, 220) with every `time` converted to a
 * TRUE UTC epoch, plus a small envelope (schema, as_of, time_basis).
 */
export type ChartPayload = ChartData | ChartError

export interface ChartEnvelope {
  schema: typeof SNAPSHOT_SCHEMA
  /** When the publisher built this chart (charts are rebuilt at most every 5 min). */
  as_of: IsoUtc
  /** Marker that the Melbourne "fake UTC" shift has been undone. Always "utc". */
  time_basis: 'utc'
}

export interface ChartData extends ChartEnvelope {
  /** Exactly one entry, keyed by the TRADING symbol (e.g. "BTC/USD", not "BTC-USD"). */
  symbols: Record<string, SymbolChart>
}

/** build_chart() had nothing to draw (e.g. no cached bars yet). */
export interface ChartError extends ChartEnvelope, BuilderError {}

/** One symbol's chart (backtest/chart_view.py _build_chart_data). */
export interface SymbolChart {
  /** Bar interval, e.g. "1h". */
  interval: string
  /** Strictly ascending by time, one per bar, up to 220. */
  candles: Candle[]
  /** Sorted by time. Each `time` equals some candle's time. */
  markers: ChartMarker[]
  /** Bollinger bands; [] when the symbol has no bollinger_rsi strategy. Warm-up NaNs are dropped. */
  bbUpper: LinePoint[]
  bbBasis: LinePoint[]
  bbLower: LinePoint[]
  /** RSI 0..100; [] without bollinger_rsi. */
  rsi: LinePoint[]
  /** MACD histogram; [] without macd_sma. */
  macdHist: HistogramPoint[]
  /** Pane labels, e.g. "BB 100/2.0  RSI 6" and "MACD 8/35/9  SMA100"; "" when absent. */
  bbLabel: string
  macdLabel: string
}

/** lightweight-charts CandlestickData with a UTCTimestamp time. */
export interface Candle {
  time: EpochSeconds
  open: USD
  high: USD
  low: USD
  close: USD
}

/** lightweight-charts LineData. */
export interface LinePoint {
  time: EpochSeconds
  value: number
}

/** lightweight-charts HistogramData: green "#34b892" at or above zero, red "#e2596b" below. */
export interface HistogramPoint extends LinePoint {
  color: string
}

/**
 * lightweight-charts SeriesMarker. Three families, told apart by shape/color:
 *   strategy signals   arrowUp belowBar "#34b892" (buy) / arrowDown aboveBar "#e2596b" (exit);
 *                      text = strategy label "MACD" | "BB" | "DON" | "EMA"
 *   account exits      arrowDown aboveBar "#f2a93b"; text "ROT>SYM" (rotation) or "RECONCILE"
 *   broker fills       circle; "#7b8fff" belowBar (buy) / "#c58fff" aboveBar (sell);
 *                      text e.g. "BUY 12 @ 101.50"
 */
export interface ChartMarker {
  time: EpochSeconds
  position: 'belowBar' | 'aboveBar'
  color: string
  shape: 'arrowUp' | 'arrowDown' | 'circle'
  text: string
}

// ----------------------------------------------------------------------- look

/**
 * web/server.py build_look(date): the presenter's outfit for one session date
 * and what tonight's render will use. Discriminated on `exists`.
 */
export type Look = LookAbsent | LookPresent

export interface LookBase {
  /** The date this look is for. */
  date: IsoDate
  /** Today's US/Eastern session date when the snapshot was built. */
  session_date: IsoDate
  /** Display name of the presenting host. */
  host: string
  /**
   * The presenter config file the render uses: briefing/config.json
   * `host.host_json`, or BRIEFING_HOST_JSON when set. Informational only.
   */
  host_json: string
  /** False = the stylist is off and the locked keyframe always renders. */
  stylist_enabled: boolean
  /** Repo-relative path of the host's locked fallback keyframe; informational only. */
  locked_keyframe: string | null
  /** Every date with a look record, newest first. May not include session_date yet. */
  dates: IsoDate[]
}

export interface LookAbsent extends LookBase {
  exists: false
  /** No look: the render falls back to the locked keyframe. */
  renders_with: 'locked'
}

export interface LookPresent extends LookBase {
  exists: true
  weather: LookWeather | null
  style: LookStyle | null
  /** Face-identity similarity of the plate / keyframe to the host (0..1). */
  plate_identity: number | null
  keyframe_identity: number | null
  /**
   * TRI-STATE. true = signed off; null = nobody has judged it yet. (A revoke
   * removes the key, so it reads null again; false only appears if something
   * else wrote it.) Render "unjudged" and "refused" differently.
   */
  approved: boolean | null
  approved_at: IsoUtc | null
  /** "look" only when approved; otherwise the locked keyframe renders. */
  renders_with: 'look' | 'locked'
  /**
   * Same-origin image URLs; the Worker serves them from look/<date>/<kind>.png:
   * "/api/look/asset?date=<date>&kind=<kind>&v=<12 hex>". `v` is the start of
   * the image's SHA-256, so a redraw for the same date is a NEW URL (the
   * Worker ignores it). It is absent while the image is not in the bucket.
   * The publisher uploads the images before the snapshot that names them.
   */
  plate_url: string
  keyframe_url: string
}

export interface LookWeather {
  /** Normally the forecast date; test presets use a label such as "mild-autumn". */
  date: string
  tmax_c: number
  tmin_c: number
  precip_mm: number
  sky: string
  season: string
}

/** The stylist's pick for the configured presenter (`host_json`). LLM text. */
export interface LookStyle {
  slug: string
  outfit: string
  rationale: string
  hair?: string
  jewellery?: string
}

/** Which look image to fetch. */
export type LookAssetKind = 'plate' | 'keyframe'

// ------------------------------------------------------------------ approvals

/**
 * approvals/<date>.json, written by the Worker on POST /api/look/approve. The
 * publisher applies it at home with set_look_approval(date, approved); the
 * cloud never edits the look record itself. The object stays in R2 after it
 * is applied.
 *
 * A request is still PENDING while the snapshot's look for that date does
 * not reflect it yet:
 *   approved === true   pending until look.approved === true
 *   approved === false  pending until look.approved !== true
 * Show "requested, waiting for home box" while pending.
 */
export interface ApprovalRequest {
  date: IsoDate
  approved: boolean
  requested_at: IsoUtc
  /** The Access identity: JWT `email`, or `common_name` for a service token. */
  requested_by: string
}

// -------------------------------------------------------------------- history

/** media/history.json: every archived edition, newest session first. */
export interface History {
  generated_at: IsoUtc
  /** Date of the newest render. */
  current: IsoDate | null
  /**
   * Sorted by (date, generated_at) descending. Only editions that can be
   * played: each has `youtube_id`, or `briefing` (the fallback mp4), never
   * neither.
   */
  briefings: Edition[]
}

/**
 * A YouTube video id: exactly 11 characters of [A-Za-z0-9_-]. Anything else
 * is never put into an embed URL.
 */
export const YOUTUBE_ID_RE = /^[A-Za-z0-9_-]{11}$/

/**
 * The publisher gives an edition without an embeddable YouTube id this long
 * after its `generated_at` to get one (the upload runs right after the
 * render) before it uploads the EN mp4 as the fallback instead: 3 hours.
 * Mirrors YOUTUBE_GRACE_SEC in web/publisher.py; kept a plain literal so a
 * Python test can read it from this file, as it reads FRESHNESS.
 */
export const YOUTUBE_GRACE_SEC = 10_800

/**
 * media/<date>/<slot>/meta.json: one rendered edition as briefing/generate.py
 * archived it (file NAMES only, no URLs), minus what the publisher never
 * uploads: every zh field (the Chinese cut stays at home), `desk` (the
 * account digest the DESK slide was drawn from) and `youtube` (the raw upload
 * records; history.json carries `youtube_id` instead).
 */
export interface EditionMeta {
  date: IsoDate
  generated_at: IsoUtc
  /**
   * Full English video file name. Named even when that file is not in R2
   * (an edition that plays from YouTube).
   */
  briefing_file: string
  /** Runtime of the EN video in seconds. */
  duration_s: number
  mode: 'factual' | 'story'
  /** Edition label, e.g. "Post-market recap", "Midday briefing". */
  label: string | null
  /** Story-mode renderer ("wan" | "hyperframes" | "h3"); null in factual mode. */
  renderer: string | null
  /** Headline; null on older renders. */
  title: string | null
  model: string | null
  images: boolean
  subtitles: boolean
  director: string | null
  /** TTS voice id, e.g. "en-US-AndrewNeural". */
  voice: string | null
  /** The running order: one entry per scene (or story beat). */
  clips: EditionClip[]
  /** "slideshow" when slot starts with "slideshow", else "video". */
  kind: 'video' | 'slideshow'
  /** Archive subdirectory: "video" or "slideshow-<label-slug>". */
  slot: string
  cover_head?: CoverHead
  cover_file?: string
}

/**
 * One history.json entry: the archived meta plus how to play it. Every URL
 * points at an object the publisher actually uploaded; a URL whose file was
 * not uploaded is removed, never left dangling.
 *
 * Playback: `youtube_id` when the edition is on YouTube, else `briefing`. The
 * publisher never sends both, and never lists an edition with neither; the
 * SPA drops such an entry itself too (features/briefing/editions.ts).
 */
export interface Edition extends EditionMeta {
  /**
   * The EN cut on YouTube (YOUTUBE_ID_RE). Present iff meta.json's
   * `youtube.en` upload record has an id AND its privacy is "public" or
   * "unlisted": a private video cannot be embedded for a viewer.
   */
  youtube_id?: string
  /**
   * The EN mp4 in R2: the FALLBACK for an edition still without an
   * embeddable YouTube id YOUTUBE_GRACE_SEC after its `generated_at`
   * (web/publisher.py). Before that the edition is not listed at all. A
   * refused or failed YouTube publish records nothing in meta.json, so the
   * wait is the only signal. When an id appears later (a manual retry), the
   * publisher switches the edition to `youtube_id` and deletes this mp4.
   */
  briefing?: MediaUrl
  /** The EN cover PNG, the poster for either player. Uploaded for every listed edition that has one. */
  cover?: MediaUrl
  /** Always false in the cloud copy. */
  local: boolean
}

/**
 * One scene of the running order. Per-scene clip_*.mp4 files are NOT
 * uploaded, so `file` survives only when it names the uploaded full video
 * (a legacy story render served from the fallback mp4, where `start_s` is the
 * chapter offset). Without `file`, a row is a label and plays nothing on its
 * own.
 */
export interface EditionClip {
  /** "OVERVIEW", "DESK", a ticker, or a character name in story renders. */
  symbol: string
  clip_file: string
  duration_s: number
  bias: Bias | null
  headline: string
  mood?: string
  /** Seconds into the full EN video where this scene starts (legacy story renders). */
  start_s?: number
  file?: MediaUrl
}

export interface CoverHead {
  title: string
  keywords: string[]
  art: string
}

// ------------------------------------------------------------ Worker HTTP API

/**
 * GET /api/snapshot: snapshot/latest.json plus the approval request for the
 * snapshot's look date, if one exists in R2.
 */
export interface SnapshotResponse extends Snapshot {
  approval_request?: ApprovalRequest | null
}

/** GET /api/chart?symbol=SYM returns ChartPayload. */
export type ChartResponse = ChartPayload

/** POST /api/look/approve request body. */
export interface ApproveBody {
  date: IsoDate
  approved: boolean
}

/** POST /api/look/approve success body: what was recorded in R2. */
export interface ApproveResponse {
  request: ApprovalRequest
}

/** Body of every non-2xx JSON response from /api. */
export type ApiError = BuilderError

/** CSRF defence on POST /api/look/approve. The SPA must send this header. */
export const CSRF_HEADER = 'X-Requested-With'
export const CSRF_VALUE = 'alpaca-dashboard'

/** Approvals are accepted for [ET today - before, ET today + after] days. */
export const APPROVAL_WINDOW_DAYS = { before: 1, after: 3 } as const

/**
 * The nightly recap renders at this hour, US/Eastern, on the look's date, and
 * reads the sign-off as it starts. The publisher refuses (outcome "rejected:
 * arrived after the 20:00 ET render ...") any request for date D that reaches
 * the home box at or after 20:00 ET on D, so the SPA stops offering it then.
 */
export const RENDER_HOUR_ET = 20

// ----------------------------------------------------------------- freshness

export const FRESHNESS = {
  /** SPA polls /api/snapshot this often while the tab is visible. */
  snapshotPollMs: 30_000,
  /** SPA polls the open chart this often while the tab is visible. */
  chartPollMs: 60_000,
  /**
   * Older than this, show "home box not reporting since ...". A healthy box
   * uploads a snapshot every minute, so the oldest `as_of` a page can show
   * is about 60 s + cron jitter + build time + one poll (~100 s): 180 s is
   * two missed runs. tests/test_publisher.py reads this value and holds the
   * publisher's cadence to it.
   */
  staleAfterMs: 180_000,
  /** The publisher rebuilds charts at most this often. */
  chartRebuildMs: 300_000,
} as const

// ------------------------------------------------------------------ R2 keys

/** YYYY-MM-DD, nothing else. */
export const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}$/

/**
 * A trading symbol as config spells it: upper-case letters and digits with an
 * optional "/", ".", or "-" inside (e.g. "AAPL", "BRK.B", "BTC/USD").
 */
export const SYMBOL_RE = /^[A-Z0-9]{1,15}(?:[/.-][A-Z0-9]{1,15})?$/

/** R2 key segment for a symbol: "/" becomes "-" (BTC/USD -> BTC-USD). */
export function chartKey(symbol: string): string {
  return symbol.replaceAll('/', '-')
}

export const R2_KEYS = {
  snapshot: 'snapshot/latest.json',
  history: 'media/history.json',
  chart: (symbol: string) => `chart/${chartKey(symbol)}.json`,
  lookAsset: (date: IsoDate, kind: LookAssetKind) => `look/${date}/${kind}.png`,
  approval: (date: IsoDate) => `approvals/${date}.json`,
  /** Prefix under which /media/<path> is served; the R2 key is "media/" + path. */
  mediaPrefix: 'media/',
} as const

// ------------------------------------------------------------ options sleeve
//
// build_portfolio() drops option positions (its table is equity-shaped), so
// the sleeve travels as its own snapshot section, `snapshot.options`. The
// publisher builds it with web/options_view.py build_options(), a READ-ONLY
// mirror of relay.csp_manager.assignment_state() (which it cannot import: that
// module can place orders) plus the option's prices and the underlying's last
// trade. Every field the broker may leave unset is nullable.

/** The weekly cash-secured put sleeve (relay/csp_manager.py). */
export interface OptionsSleeve {
  /** decisions/csp.json `enabled`. */
  enabled: boolean
  /** True = the weekly run chooses and journals a trade but sends nothing. */
  shadow: boolean
  /** The underlying the sleeve sells puts on, e.g. "XYZ". */
  underlying: string
  /** The open short put, or null when none is open. */
  short_put: ShortPut | null
  /** Shares a put was assigned into, or null. */
  assigned: AssignedShares | null
}

export interface ShortPut {
  /** OCC option symbol, e.g. "XYZ261218P00042000". */
  symbol: string
  /** Contracts short, as a POSITIVE number. */
  contracts: number
  strike: USD | null
  expiry: IsoDate | null
  /** Premium received per share (Alpaca avg_entry_price). */
  avg_entry_price?: USD | null
  /** Current mark per share (Alpaca current_price). */
  current_price?: USD | null
  /** Alpaca market_value: negative for a short position. */
  market_value: USD | null
  /** Alpaca unrealized_pl in dollars. */
  unrealized: USD | null
  /** The underlying's last price when the snapshot was built. */
  underlying_price?: USD | null
  /**
   * underlying_price / strike - 1: how far the underlying can fall before the
   * put is in the money. The sleeve has NO stop by design; this is its risk
   * distance. Negative = already in the money.
   */
  strike_distance_pct?: Fraction | null
}

/** assignment_state()["assigned"]: shares that arrived through assignment. */
export interface AssignedShares {
  symbol: string
  qty: number | null
  avg_price: USD | null
  unrealized: USD | null
  unrealized_pct: Fraction | null
  /** FALSE means nobody manages these shares: an operator must act. */
  managed_by_engine: boolean
  adopted: boolean
  /** The strike: what the shares cost. */
  basis: USD | null
  /** The price they arrived at. */
  anchor: USD | null
  /** The weekly run that will sell them (display only). */
  sell_by: IsoDate | null
}
