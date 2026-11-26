/* Alpaca Terminal — dashboard client.
 *
 * Polls the read-only JSON API and paints four panels. All text is written via
 * textContent / DOM nodes rather than innerHTML: journal reasons and research
 * notes are free-form strings authored by an LLM, so they are treated as data,
 * never as markup.
 */
'use strict';

const $ = (id) => document.getElementById(id);
const TZ = 'Australia/Melbourne';

/* Panels refresh on their own cadence: the broker call is a network round-trip,
 * the files are local, the charts recompute indicators over full history. */
const POLL = { portfolio: 10000, board: 60000, journal: 20000, executions: 20000, chart: 60000 };

// ---------------------------------------------------------------- formatting
const money = (v, dp = 2) =>
  v == null || Number.isNaN(v) ? '—'
    : v.toLocaleString(undefined, { style: 'currency', currency: 'USD', minimumFractionDigits: dp, maximumFractionDigits: dp });

const pct = (v, dp = 2) => (v == null || Number.isNaN(v) ? '—' : `${(v * 100).toFixed(dp)}%`);
const signed = (v, fn) => (v == null || Number.isNaN(v) ? '—' : (v > 0 ? '+' : '') + fn(v));
const dirClass = (v) => (v == null || v === 0 ? '' : v > 0 ? 'up' : 'down');
const qtyFmt = (v) => (v == null ? '—' : Number.isInteger(v) ? String(v) : v.toFixed(4));

const clock = (epoch) =>
  epoch == null ? '—'
    : new Date(epoch * 1000).toLocaleString('en-AU', { timeZone: TZ, day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false });

const ago = (epoch) => {
  if (epoch == null) return '—';
  const s = Math.max(0, Math.round(Date.now() / 1000 - epoch));
  if (s < 90) return `${s}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 172800) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
};

const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

/** Cell helper: `num` right-aligns via the .num class the stylesheet keys on. */
const td = (text, cls) => el('td', cls, text);

async function getJSON(path) {
  const r = await fetch(path, { credentials: 'same-origin' });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

function setNote(id, msg, bad) {
  const n = $(id);
  n.textContent = msg || '';
  n.style.color = bad ? 'var(--down)' : '';
}

// ---------------------------------------------------------------- portfolio
async function loadPortfolio() {
  let d;
  try {
    d = await getJSON('/api/portfolio');
  } catch (e) {
    setNote('portfolioNote', `unreachable: ${e.message}`, true);
    return;
  }
  if (d.error) {
    setNote('portfolioNote', d.error, true);
    $('acctBadge').textContent = 'broker error';
    $('acctBadge').className = 'badge badge--bad';
    return;
  }
  setNote('portfolioNote', '');

  $('equity').textContent = money(d.equity);
  const delta = $('equityDelta');
  delta.textContent = `${signed(d.day_pl, (v) => money(v))}  (${signed(d.day_pl_pct, (v) => pct(v))}) today`;
  delta.className = `hero__delta ${dirClass(d.day_pl)}`;

  $('cash').textContent = money(d.cash);
  $('bp').textContent = money(d.buying_power);
  $('lastEquity').textContent = money(d.last_equity);

  const acct = $('acctBadge');
  acct.textContent = `${d.paper ? 'paper' : 'LIVE'} · ${d.status || '—'}`;
  acct.className = `badge ${d.paper ? 'badge--ok' : 'badge--bad'}`;

  // Halt meter: fill tracks how much of the day's loss budget is spent.
  const h = d.halt || {};
  const used = h.used_frac || 0;
  const fill = $('haltFill');
  fill.style.width = `${Math.round(used * 100)}%`;
  fill.className = 'meter__fill' + (h.tripped || used >= 1 ? ' meter__fill--crit' : used >= 0.6 ? ' meter__fill--warn' : '');
  $('haltText').textContent = `${pct(-Math.min(d.day_pl_pct, 0), 2)} of ${pct(h.limit, 0)}`;

  const hb = $('haltBadge');
  hb.textContent = h.tripped ? 'HALTED — buys blocked' : `halt at ${pct(h.limit, 0)}`;
  hb.className = `badge ${h.tripped ? 'badge--bad' : used >= 0.6 ? 'badge--warn' : 'badge--muted'}`;

  renderPositions(d.positions || []);
  $('updated').textContent = `updated ${new Date().toLocaleTimeString('en-AU', { timeZone: TZ, hour12: false })}`;
}

function renderPositions(rows) {
  const body = $('posTable').tBodies[0];
  body.replaceChildren();
  $('posEmpty').hidden = rows.length > 0;
  $('posTable').hidden = rows.length === 0;
  setNote('posNote', rows.length ? `${rows.length} open` : '');

  for (const p of rows) {
    const tr = el('tr');
    const sym = el('td');
    sym.append(el('span', 'sym', p.symbol));
    if (p.bias) {
      sym.append(' ', el('span', `tag tag--${p.bias}`, p.bias));
    }
    tr.append(sym);
    tr.append(td(qtyFmt(p.qty), 'num'));
    tr.append(td(money(p.avg_entry_price), 'num'));
    tr.append(td(money(p.current_price), 'num'));
    tr.append(td(money(p.market_value), 'num'));

    // Sign is always printed, so P&L direction never depends on colour alone.
    const pl = td(`${signed(p.unrealized_pl, (v) => money(v))} (${signed(p.unrealized_plpc, (v) => pct(v))})`, `num ${dirClass(p.unrealized_pl)}`);
    tr.append(pl);

    tr.append(td(p.stop_price ? money(p.stop_price) : '—', 'num muted'));
    const dist = p.stop_distance_pct;
    tr.append(td(dist == null ? '—' : pct(dist), `num ${dist != null && dist < 0.005 ? 'down' : ''}`));
    body.append(tr);
  }
}

// ---------------------------------------------------------------- board
async function loadBoard() {
  let d;
  try {
    d = await getJSON('/api/board');
  } catch (e) {
    setNote('boardNote', `unreachable: ${e.message}`, true);
    return;
  }
  if (d.error) { setNote('boardNote', d.error, true); return; }

  setNote('boardNote', `${d.date || '—'} · ${ago(d.mtime)}`);
  const body = $('boardTable').tBodies[0];
  body.replaceChildren();

  for (const r of d.rows) {
    const tr = el('tr');
    // priority > 0 means a live rotation candidate; 0 is watch-only.
    tr.append(td(String(r.priority ?? 0), `num pri ${r.priority > 0 ? 'pri--live' : ''}`));
    tr.append(td(r.symbol, 'sym'));

    const bias = el('td');
    bias.append(el('span', `tag tag--${r.bias}`, r.bias));
    tr.append(bias);

    tr.append(td(r.suggested_size != null ? pct(r.suggested_size, 0) : '—', 'num'));
    tr.append(td(r.max_position != null ? r.max_position.toLocaleString() : '—', 'num muted'));

    const c = el('td', 'muted');
    if (r.contingencies && r.contingencies.length) {
      const first = r.contingencies[0];
      c.textContent = `${(first.condition || '').replace('_', ' ')} ${first.level ?? ''}`;
      c.title = first.reason || '';
      c.className = '';
    } else {
      c.textContent = '—';
    }
    tr.append(c);
    body.append(tr);
  }

  const nb = $('notesBody');
  nb.replaceChildren();
  const notes = d.notes || {};
  const keys = Object.keys(notes);
  $('notesBox').hidden = keys.length === 0;
  for (const k of keys) {
    const p = el('p');
    if (k !== '_') p.append(el('span', 'notes__key', `${k} `));
    p.append(document.createTextNode(String(notes[k])));
    nb.append(p);
  }
  $('footNote').textContent = `max_open_positions ${d.max_open_positions ?? '—'} · extended hours ${d.allow_extended_hours ? 'on' : 'off'}`;
}

// ---------------------------------------------------------------- journal
async function loadJournal() {
  let d;
  try {
    d = await getJSON('/api/journal');
  } catch (e) {
    setNote('journalNote', `unreachable: ${e.message}`, true);
    return;
  }
  const eng = d.engine || {};

  const badge = $('engineBadge');
  // Three states, not two. `running` is only "a process exists"; `polling` is
  // "a poll finished recently" (state-file mtime). A process that is present but
  // wedged — the fetch-phase hang, or the ushmds farm returning zero bars — used
  // to render a confident green "engine live", which is the worst possible
  // answer at exactly the moment someone is checking because something is wrong.
  const live = eng.running && eng.polling;
  const label = live ? 'engine live' : (eng.running ? 'engine stalled' : 'engine down');
  badge.replaceChildren(el('span', 'dot'), document.createTextNode(label));
  badge.className = `badge ${live ? 'badge--ok' : 'badge--bad'}`;

  // Engine health chips: one per symbol, flagged when it trails the newest bar.
  const health = $('health');
  health.replaceChildren();
  const poll = el('span', 'chip', '');
  poll.append(el('span', null, 'last poll'), el('span', 'chip__val', ago(eng.last_poll)));
  health.append(poll);

  for (const s of eng.symbols || []) {
    // ~1 bar of lag on a single name is the expected transient (yfinance
    // fallback); the engine re-pulls it via _backfill_gaps. Flag, don't alarm.
    const lagging = s.behind_s != null && s.behind_s > 60;
    const chip = el('span', `chip ${lagging ? 'chip--lag' : 'chip--ok'}`);
    chip.append(el('span', null, s.symbol), el('span', 'chip__val', lagging ? `−${Math.round(s.behind_s / 60)}m` : '✓'));
    chip.title = `last bar ${clock(s.last_bar)}`;
    health.append(chip);
  }
  if ((eng.yfinance_fallback || []).length) {
    const f = el('span', 'chip chip--lag');
    f.append(el('span', null, 'yfinance fallback'), el('span', 'chip__val', String(eng.yfinance_fallback.length)));
    f.title = 'IBKR unavailable for these polls — data source fell back';
    health.append(f);
  }

  setNote('journalNote', `${d.rows.length} recent · bars to ${clock(eng.newest_bar)}`);

  const body = $('journalTable').tBodies[0];
  body.replaceChildren();
  for (const r of d.rows) {
    const tr = el('tr');
    tr.append(td(clock(r._ts), 'muted'));
    tr.append(td(r.event || '—'));
    tr.append(td(r.symbol || r.ticker || '—', 'sym'));

    const detail = [r.action, r.reason, r.qty != null ? `qty ${qtyFmt(r.qty)}` : null].filter(Boolean).join(' · ');
    const dcell = td(detail || '—');
    dcell.title = detail;
    tr.append(dcell);

    const st = r.status || '—';
    const cell = el('td');
    const ok = /ok|closed|filled/.test(st);
    const bad = /error|reject|fail/.test(st);
    cell.append(el('span', `tag ${ok ? 'tag--long' : bad ? 'tag--short' : 'tag--flat'}`, st));
    tr.append(cell);
    body.append(tr);
  }
}

// ---------------------------------------------------------------- executions
async function loadExecutions() {
  let d;
  try {
    d = await getJSON('/api/executions');
  } catch (e) {
    setNote('execNote', `unreachable: ${e.message}`, true);
    return;
  }
  if (d.error) { setNote('execNote', d.error, true); return; }

  const rows = d.rows || [];
  $('execEmpty').hidden = rows.length > 0;
  $('execTable').hidden = rows.length === 0;

  if (rows.length) {
    const wr = d.win_rate == null ? '—' : `${Math.round(d.win_rate * 100)}% win`;
    setNote('execNote', `realized ${signed(d.realized_pnl, (v) => money(v))} · ${d.round_trips} round trip${d.round_trips === 1 ? '' : 's'} · ${wr}`);
  } else {
    setNote('execNote', '');
  }

  const body = $('execTable').tBodies[0];
  body.replaceChildren();
  for (const r of rows) {
    const tr = el('tr');
    tr.append(td(clock(r.ts), 'muted'));
    tr.append(td(r.symbol, 'sym'));

    const side = el('td');
    side.append(el('span', `tag tag--${r.side === 'buy' ? 'long' : 'short'}`, r.side));
    tr.append(side);
    tr.append(td(qtyFmt(r.qty), 'num'));

    // On a buy, "entry" is the fill itself and there is no exit yet. On a sell,
    // entry is the average cost being closed against.
    tr.append(td(r.entry_price != null ? money(r.entry_price) : '—', 'num'));
    tr.append(td(r.exit_price != null ? money(r.exit_price) : '—', 'num'));

    if (r.pnl == null) {
      // A buy realizes nothing — P&L lands on the exit that closes it. Don't
      // label it "open": the row is a historical fill, and the position it
      // opened may well have been closed by a later sell in this same table.
      tr.append(td('—', 'num muted'));
    } else {
      tr.append(td(`${signed(r.pnl, (v) => money(v))} (${signed(r.pnl_pct, (v) => pct(v))})`,
        `num ${dirClass(r.pnl)}`));
    }
    body.append(tr);
  }
}

// ---------------------------------------------------------------- charts
let charts = null;
let currentSymbol = null;

function buildCharts() {
  const PRICE_SCALE_WIDTH = 80; // pin all panes to one plot width so they align
  const common = {
    layout: { background: { type: 'solid', color: '#0b0e14' }, textColor: '#7c889c', fontFamily: "'JBMono', ui-monospace, monospace", fontSize: 11 },
    grid: { vertLines: { color: 'rgba(35,40,56,0.6)' }, horzLines: { color: 'rgba(35,40,56,0.6)' } },
    // autoScale is on by default and rescales to whatever is in view as you pan
    // or zoom; the margins stop the candles touching the pane edges.
    rightPriceScale: {
      borderColor: '#232838', minimumWidth: PRICE_SCALE_WIDTH,
      autoScale: true, scaleMargins: { top: 0.12, bottom: 0.12 },
    },
    timeScale: { borderColor: '#232838', timeVisible: true, secondsVisible: false },
    crosshair: { vertLine: { color: '#4d5568', labelBackgroundColor: '#171c28' }, horzLine: { color: '#4d5568', labelBackgroundColor: '#171c28' } },
    autoSize: true,
  };
  const price = LightweightCharts.createChart($('chartPrice'), common);
  const rsi = LightweightCharts.createChart($('chartRsi'), common);
  const macd = LightweightCharts.createChart($('chartMacd'), common);

  const c = {
    price, rsi, macd,
    candles: price.addCandlestickSeries({
      upColor: '#34b892', downColor: '#e2596b',
      borderUpColor: '#34b892', borderDownColor: '#e2596b',
      wickUpColor: '#2a8f73', wickDownColor: '#b8465a',
    }),
    // The Bollinger bands are drawn but EXCLUDED from autoscale
    // (autoscaleInfoProvider -> null). With a 100-200 bar BB length the bands
    // sit far from price, so letting them drive the scale stretched the axis to
    // ~225-260 while the candles lived in 245-250 — squashing the candles into a
    // thin band. Now the price scale fits the candles, and a band simply clips
    // off-pane on the stretches where it is that far away.
    bbU: price.addLineSeries({ color: '#7b8fff', lineWidth: 1, priceLineVisible: false, lastValueVisible: false, autoscaleInfoProvider: () => null }),
    bbB: price.addLineSeries({ color: '#4d5568', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, priceLineVisible: false, lastValueVisible: false, autoscaleInfoProvider: () => null }),
    bbL: price.addLineSeries({ color: '#7b8fff', lineWidth: 1, priceLineVisible: false, lastValueVisible: false, autoscaleInfoProvider: () => null }),
    rsiS: rsi.addLineSeries({ color: '#f2a93b', lineWidth: 2, lastValueVisible: false }),
    macdS: macd.addHistogramSeries({ lastValueVisible: false }),
    all: [price, rsi, macd],
  };
  c.rsiS.createPriceLine({ price: 50, color: '#4d5568', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: '50' });
  c.macdS.createPriceLine({ price: 0, color: '#4d5568', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: false });

  // Keep the three panes panned/zoomed together.
  let syncing = false;
  c.all.forEach((ch) => {
    ch.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      if (syncing || !range) return;
      syncing = true;
      c.all.forEach((o) => { if (o !== ch) o.timeScale().setVisibleLogicalRange(range); });
      syncing = false;
    });
  });

  // One shared crosshair readout over the price pane, fed by whichever pane the
  // pointer is in — otherwise each pane only tooltips its own series.
  c.byTime = { candle: new Map(), rsi: new Map(), macd: new Map() };
  c.all.forEach((ch) => ch.subscribeCrosshairMove((param) => updateOhlc(c, param)));
  return c;
}

/* _chart_ts (backtest/chart_view.py) shifts each bar so Melbourne wall-clock is
 * encoded as if it were UTC — so read it back with UTC getters, not local ones. */
function fmtBarTime(t) {
  const d = new Date(t * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
}

function updateOhlc(c, param) {
  const box = $('ohlc');
  const t = param && param.time != null ? param.time : c.lastTime;
  if (t == null) { box.replaceChildren(); return; }
  const k = c.byTime.candle.get(t);
  if (!k) return;
  box.replaceChildren();
  const add = (label, val, cls) => {
    const s = el('span');
    s.append(el('span', 'muted', `${label} `), el('span', cls, val));
    box.append(s);
  };
  const dir = k.close >= k.open ? 'up' : 'down';
  add('', fmtBarTime(t), 'muted');
  add('O', k.open.toFixed(2));
  add('H', k.high.toFixed(2));
  add('L', k.low.toFixed(2));
  add('C', k.close.toFixed(2), dir);
  const r = c.byTime.rsi.get(t);
  if (r != null) add('RSI', r.toFixed(1));
  const m = c.byTime.macd.get(t);
  if (m != null) add('MACD', m.toFixed(3), m >= 0 ? 'up' : 'down');
}

async function loadChart(symbol) {
  setNote('chartNote', 'loading…');
  let d;
  try {
    d = await getJSON(`/api/chart?symbol=${encodeURIComponent(symbol)}`);
  } catch (e) {
    setNote('chartNote', `unreachable: ${e.message}`, true);
    return;
  }
  if (d.error) {
    setNote('chartNote', d.error, true);
    $('chartEmpty').hidden = false;
    $('chartEmpty').textContent = d.error;
    return;
  }
  $('chartEmpty').hidden = true;
  const s = d.symbols[symbol];
  if (!s) { setNote('chartNote', 'no data', true); return; }

  if (!charts) charts = buildCharts();
  charts.candles.setData(s.candles);
  charts.bbU.setData(s.bbUpper || []);
  charts.bbB.setData(s.bbBasis || []);
  charts.bbL.setData(s.bbLower || []);
  charts.rsiS.setData(s.rsi || []);
  charts.macdS.setData((s.macdHist || []).map((p) => ({
    time: p.time, value: p.value, color: p.value >= 0 ? '#34b892' : '#e2596b',
  })));
  charts.candles.setMarkers(s.markers || []);

  charts.byTime.candle = new Map(s.candles.map((k) => [k.time, k]));
  charts.byTime.rsi = new Map((s.rsi || []).map((p) => [p.time, p.value]));
  charts.byTime.macd = new Map((s.macdHist || []).map((p) => [p.time, p.value]));
  charts.lastTime = s.candles.length ? s.candles[s.candles.length - 1].time : null;
  updateOhlc(charts, null);

  charts.all.forEach((ch) => ch.timeScale().fitContent());
  $('rsiTag').textContent = s.bbLabel || 'RSI';
  $('macdTag').textContent = s.macdLabel || 'MACD';
  const last = s.candles[s.candles.length - 1];
  setNote('chartNote', `${s.interval} · ${s.candles.length} bars · last ${last ? last.close : '—'} · ${(s.markers || []).length} signals`);
}

async function loadSymbols() {
  const d = await getJSON('/api/symbols');
  const nav = $('symbolNav');
  nav.replaceChildren();
  for (const sym of d.symbols) {
    const b = el('button', 'sym-btn', sym);
    b.type = 'button';
    b.setAttribute('aria-pressed', String(sym === currentSymbol));
    b.addEventListener('click', () => selectSymbol(sym));
    nav.append(b);
  }
  if (!currentSymbol && d.symbols.length) selectSymbol(d.symbols[0]);
}

function selectSymbol(sym) {
  currentSymbol = sym;
  for (const b of $('symbolNav').children) b.setAttribute('aria-pressed', String(b.textContent === sym));
  loadChart(sym);
}

// ---------------------------------------------------------------- boot
async function refreshAll() {
  const btn = $('refreshBtn');
  btn.setAttribute('aria-busy', 'true');
  await Promise.allSettled([loadPortfolio(), loadBoard(), loadJournal(), loadExecutions(),
                            currentSymbol ? loadChart(currentSymbol) : null]);
  btn.removeAttribute('aria-busy');
}

$('refreshBtn').addEventListener('click', refreshAll);

loadSymbols().catch((e) => setNote('chartNote', e.message, true));
loadPortfolio();
loadBoard();
loadJournal();
loadExecutions();

setInterval(loadPortfolio, POLL.portfolio);
setInterval(loadBoard, POLL.board);
setInterval(loadJournal, POLL.journal);
setInterval(loadExecutions, POLL.executions);
setInterval(() => { if (currentSymbol) loadChart(currentSymbol); }, POLL.chart);

// Catch up immediately when the tab comes back rather than waiting a full tick.
document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshAll(); });
