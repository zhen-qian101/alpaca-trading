/* Look review — sign off the presenter's outfit before it becomes video.
 *
 * The stylist runs at 09:35 ET, the render at 20:00 ET. This page is what fills
 * the ~10h in between: it shows what was drawn, and it is the only place the
 * `approved` flag that host_briefing.day_keyframe reads gets written.
 *
 * Same house rule as app.js: every string from the API is written with
 * textContent, never innerHTML. The outfit prompt and the rationale are LLM
 * output and are treated as data.
 */
'use strict';

const $ = (id) => document.getElementById(id);

let STATE = null;

// ------------------------------------------------------------------ helpers
const show = (img, empty, url) => {
  if (url) {
    // Cache-bust: approving does not change the image, but a redraw for the
    // same date does, and a stale 200 from the disk cache would show yesterday's
    // outfit under today's verdict — the one mistake this page must not make.
    img.src = `${url}&t=${Date.now()}`;
    img.hidden = false;
    empty.hidden = true;
  } else {
    img.hidden = true;
    empty.hidden = false;
  }
};

const idScore = (v) => (v == null ? '—' : v.toFixed(3));

async function api(path, opts) {
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok && !body.error) body.error = `HTTP ${r.status}`;
  return body;
}

// -------------------------------------------------------------------- paint
function paint(d) {
  STATE = d;
  const badge = $('verdictBadge');
  const vb = $('verdict');
  const txt = $('verdictText');

  $('hostBadge').textContent = d.host ? `host · ${d.host}` : 'host —';

  // --- the verdict bar. Three distinct states, never collapsed into two: a
  // look nobody has judged is not the same as one that was judged and refused.
  vb.classList.remove('verdict--ok', 'verdict--wait', 'verdict--none');
  badge.className = 'badge';
  $('approveBtn').hidden = true;
  $('revokeBtn').hidden = true;

  if (d.error) {
    badge.classList.add('badge--bad');
    badge.textContent = 'error';
    txt.textContent = d.error;
    return;
  }
  if (!d.stylist_enabled) {
    vb.classList.add('verdict--none');
    badge.classList.add('badge--muted');
    badge.textContent = 'stylist off';
    txt.textContent = 'The stylist is switched off, so the locked keyframe is what renders. Nothing to review.';
  } else if (!d.exists) {
    vb.classList.add('verdict--none');
    badge.classList.add('badge--muted');
    badge.textContent = 'no look';
    txt.textContent = `No look was built for ${d.date}. Tonight's render wears the locked keyframe.`;
  } else if (d.approved) {
    vb.classList.add('verdict--ok');
    badge.classList.add('badge--ok');
    badge.textContent = 'signed off';
    txt.textContent = `Signed off — the ${d.date} look is what the render will use.`;
    $('revokeBtn').hidden = false;
  } else {
    vb.classList.add('verdict--wait');
    badge.classList.add('badge--warn');
    badge.textContent = 'awaiting sign-off';
    txt.textContent = `Not signed off yet, so tonight's render falls back to the locked keyframe. Sign off to use the ${d.date} look instead.`;
    $('approveBtn').hidden = false;
  }

  // --- the art
  show($('kfImg'), $('kfEmpty'), d.keyframe_url);
  show($('faceImg'), $('faceEmpty'), d.keyframe_url);
  show($('plateImg'), $('plateEmpty'), d.plate_url);

  // --- the numbers and the copy
  const st = d.style || {};
  const w = d.weather || {};
  $('rationale').textContent = st.rationale || '—';
  $('outfit').textContent = st.outfit || '—';
  $('slug').textContent = st.slug || '—';
  $('plateId').textContent = idScore(d.plate_identity);
  $('kfId').textContent = idScore(d.keyframe_identity);
  $('kfNote').textContent = d.approved_at ? `signed off ${d.approved_at}` : '';
  $('plateNote').textContent = st.slug || '';
  $('weatherNote').textContent = w.tmax_c == null ? ''
    : `${w.tmin_c}–${w.tmax_c}°C · ${w.sky || ''} · ${w.precip_mm ?? 0}mm · ${w.season || ''}`;
}

function paintDates(d) {
  const sel = $('dateSel');
  if (!d.dates) return;
  const want = d.date;
  // The session date always belongs in the list even before the stylist has
  // run — otherwise the page cannot show "no look yet for today", which is
  // exactly the state a reviewer checking in the morning needs to see.
  const dates = Array.from(new Set([d.session_date, ...d.dates]))
    .sort().reverse();
  if (sel.dataset.filled === dates.join(',')) { sel.value = want; return; }
  sel.textContent = '';
  for (const dt of dates) {
    const o = document.createElement('option');
    o.value = dt;
    o.textContent = dt === d.session_date ? `${dt} (today)` : dt;
    sel.appendChild(o);
  }
  sel.dataset.filled = dates.join(',');
  sel.value = want;
}

// ------------------------------------------------------------------- events
async function load(date) {
  const q = date ? `?date=${encodeURIComponent(date)}` : '';
  const d = await api(`/api/look${q}`);
  paintDates(d);
  paint(d);
}

async function setApproval(approved) {
  if (!STATE || !STATE.date) return;
  const btns = [$('approveBtn'), $('revokeBtn')];
  btns.forEach((b) => b.setAttribute('aria-busy', 'true'));
  try {
    const d = await api('/api/look/approve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ date: STATE.date, approved }),
    });
    paint(d);
  } finally {
    btns.forEach((b) => b.removeAttribute('aria-busy'));
  }
}

$('approveBtn').addEventListener('click', () => setApproval(true));
$('revokeBtn').addEventListener('click', () => setApproval(false));
$('dateSel').addEventListener('change', (e) => load(e.target.value));

load();
