/* ==========================================================================
   Grid Load Forecast Accuracy -- dashboard front end

   NO BUILD STEP, NO FRAMEWORK, ON PURPOSE.
   The entire site is three static files plus JSON. GitHub Pages serves it for
   free with no CI build, nothing to keep patched, and no way for a dependency
   release to break the page at 3am. A React build would add a toolchain, a
   lockfile and a deploy step to a page that renders eight charts. Choosing the
   smaller tool when it is sufficient is an engineering judgement worth making
   deliberately rather than by habit.
   ========================================================================== */

'use strict';

const DATA = 'data/';
const FILES = [
  'meta', 'balancing_authorities', 'daily', 'hourly_recent',
  'profile_hour', 'profile_temperature', 'scatter_temp_demand',
  'alerts', 'outlook'
];

const C = {
  accent: '#38bdf8', accent2: '#fbbf24', good: '#34d399',
  warn: '#f59e0b', danger: '#f43f5e', cool: '#60a5fa',
  grid: '#1f2c3a', text: '#8fa3b5', faint: '#5c6f80'
};
// A fixed per-BA palette. Assigning colours by index would let a BA change
// colour whenever the sort order changes, which silently breaks the reader's
// mental map between one chart and the next.
const BA_COLORS = {
  PJM: '#38bdf8', MISO: '#34d399', ERCO: '#f43f5e',
  CISO: '#fbbf24', ISNE: '#a78bfa', NYIS: '#fb923c'
};

let D = {};             // loaded datasets, columnar
let state = { ba: 'PJM', win: 7 };
const charts = {};      // live Chart instances, keyed by canvas id

/* ---------- columnar helpers -------------------------------------------
   The JSON arrives as {col: [values]} rather than [{...}, {...}] because that
   form is several times smaller on the wire. These two helpers are the only
   place that shape is dealt with; everything above them works in plain rows. */
function rows(ds) {
  if (!ds) return [];
  const keys = Object.keys(ds);
  if (!keys.length) return [];
  const n = ds[keys[0]].length;
  const out = new Array(n);
  for (let i = 0; i < n; i++) {
    const o = {};
    for (const k of keys) o[k] = ds[k][i];
    out[i] = o;
  }
  return out;
}
const byBA = (ds, ba) => rows(ds).filter(r => r.ba_code === ba);

/* ---------- formatting ---------- */
const pct  = (v, d = 2) => (v === null || v === undefined || isNaN(v)) ? '—' : (v * 100).toFixed(d) + '%';
const sPct = (v, d = 2) => (v === null || v === undefined || isNaN(v)) ? '—' : (v >= 0 ? '+' : '') + (v * 100).toFixed(d) + '%';
const mw   = v => (v === null || v === undefined || isNaN(v)) ? '—' : Math.round(v).toLocaleString();
const sMw  = v => (v === null || v === undefined || isNaN(v)) ? '—' : (v >= 0 ? '+' : '') + Math.round(v).toLocaleString();
const num  = (v, d = 1) => (v === null || v === undefined || isNaN(v)) ? '—' : Number(v).toFixed(d);
const shortDate = s => s ? new Date(String(s).slice(0, 10) + 'T00:00:00').toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : '—';
/** Trim a serialised DATE to its date portion as TEXT.
 *  Deliberately string slicing, not date parsing: new Date('2026-06-28') is
 *  interpreted as UTC midnight and then displayed in the viewer's local zone,
 *  which shows the previous day for anyone west of Greenwich. */
const dateOnly = s => s ? String(s).slice(0, 10) : '—';

function localTimeLabel(isoish) {
  if (!isoish) return '—';
  // local_datetime is already the BA's wall-clock time and carries no offset.
  // Formatting it with the browser's timezone would re-shift it -- so it is
  // sliced as text instead. Displaying a timestamp is not the same operation as
  // converting one, and conflating the two is how off-by-one-hour bugs happen.
  const s = String(isoish).replace('T', ' ');
  return s.slice(5, 16);
}

/* ---------- boot ---------- */
async function boot() {
  try {
    const loaded = await Promise.all(
      FILES.map(f => fetch(DATA + f + '.json', { cache: 'no-cache' }).then(r => {
        if (!r.ok) throw new Error(f + '.json -> HTTP ' + r.status);
        return r.json();
      }))
    );
    FILES.forEach((f, i) => { D[f] = loaded[i]; });
  } catch (err) {
    document.getElementById('boot').innerHTML =
      '<div class="empty"><strong>Could not load dashboard data.</strong><br><br>' +
      '<span class="mono">' + err.message + '</span><br><br>' +
      'Run <span class="mono">python -m src.build_web_data</span>, then serve this folder over HTTP ' +
      '(<span class="mono">python -m http.server</span>). Opening index.html directly from disk ' +
      'will not work, because browsers block fetch() on file:// URLs.</div>';
    return;
  }

  applyChartDefaults();
  initHeader();
  initTabs();
  initControls();

  document.getElementById('boot').hidden = true;
  document.getElementById('app').hidden = false;

  renderBrief();
  renderAccuracy();
  renderDrivers();
  renderOutlook();
  renderQuality();
}

function applyChartDefaults() {
  Chart.defaults.color = C.text;
  Chart.defaults.borderColor = C.grid;
  Chart.defaults.font.family = "'Times New Roman', Times, serif";
  Chart.defaults.font.size = 11;
  Chart.defaults.plugins.legend.labels.boxWidth = 10;
  Chart.defaults.plugins.legend.labels.boxHeight = 10;
  Chart.defaults.plugins.legend.labels.usePointStyle = true;
  Chart.defaults.maintainAspectRatio = false;
  Chart.defaults.animation.duration = 320;
  Chart.defaults.plugins.tooltip.backgroundColor = '#161f2b';
  Chart.defaults.plugins.tooltip.borderColor = '#2c3e50';
  Chart.defaults.plugins.tooltip.borderWidth = 1;
  Chart.defaults.plugins.tooltip.padding = 10;
}

/** Destroy before recreate. Chart.js keeps a registry keyed by canvas, and
 *  re-instantiating on a live canvas leaks the old instance and its listeners.
 *  With a BA selector that redraws on every change, that leak compounds fast. */
function draw(id, cfg) {
  if (charts[id]) charts[id].destroy();
  const el = document.getElementById(id);
  if (!el) return;
  charts[id] = new Chart(el.getContext('2d'), cfg);
}

/* ---------- header ---------- */
function initHeader() {
  const m = D.meta;
  if (m.is_demo_data) document.getElementById('demoBanner').hidden = false;

  const h = m.hours_behind;
  const dot = document.getElementById('freshDot');
  if (h === null || h === undefined) dot.className = 'dot bad';
  else if (h <= 6) dot.className = 'dot';
  else if (h <= 24) dot.className = 'dot stale';
  else dot.className = 'dot bad';

  const latest = m.latest_actual_hour_utc ? String(m.latest_actual_hour_utc).slice(0, 16).replace('T', ' ') : 'unknown';
  document.getElementById('freshText').textContent =
    'actuals through ' + latest + ' UTC' + (h != null ? '  (' + num(h) + 'h ago)' : '');

  document.getElementById('footMeta').textContent =
    'Built ' + String(m.generated_utc).slice(0, 19).replace('T', ' ') + ' UTC · ' +
    m.fact_rows.toLocaleString() + ' fact rows · ' +
    'history ' + dateOnly(m.history_start) + ' → ' + dateOnly(m.history_end) + ' · ' +
    m.dq_pass + '/' + m.dq_total + ' data quality checks passing';
}

/* ---------- tabs ---------- */
function initTabs() {
  document.querySelectorAll('nav.tabs button').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('nav.tabs button').forEach(b => b.setAttribute('aria-selected', 'false'));
      btn.setAttribute('aria-selected', 'true');
      document.querySelectorAll('.panel-page').forEach(p => p.classList.remove('active'));
      document.getElementById('page-' + btn.dataset.page).classList.add('active');
      // Chart.js cannot measure a display:none canvas, so charts drawn while a
      // tab was hidden come out zero-height. Resizing on reveal fixes it.
      Object.values(charts).forEach(c => c.resize());
    });
  });
}

/* ---------- controls ---------- */
function initControls() {
  const sel = document.getElementById('baSelect');
  const bas = rows(D.balancing_authorities);
  sel.innerHTML = bas.map(b => `<option value="${b.ba_code}">${b.ba_name}</option>`).join('');
  state.ba = bas[0] ? bas[0].ba_code : 'PJM';
  sel.value = state.ba;
  sel.addEventListener('change', e => { state.ba = e.target.value; renderBrief(); });

  document.querySelectorAll('#winSeg button').forEach(b => {
    b.addEventListener('click', () => {
      document.querySelectorAll('#winSeg button').forEach(x => x.classList.remove('on'));
      b.classList.add('on');
      state.win = +b.dataset.win;
      renderBrief();
    });
  });
}

/* ======================= PAGE: DAILY BRIEF ======================= */
function renderBrief() {
  const ba = state.ba;
  const d = byBA(D.daily, ba).slice(-state.win);
  const all = byBA(D.daily, ba);
  const latest = d[d.length - 1];

  // ---- KPI cards ----
  const avg = k => { const v = d.map(r => r[k]).filter(x => x != null); return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null; };
  const prev = all.slice(-state.win * 2, -state.win);
  const prevAvg = k => { const v = prev.map(r => r[k]).filter(x => x != null); return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null; };

  const mape = avg('mape'), mapePrev = prevAvg('mape');
  const bias = avg('bias_pct');
  const peakMape = avg('peak_hour_ape');
  const misses = d.reduce((a, r) => a + (r.material_miss_hours || 0), 0);
  const worstDay = d.reduce((a, r) => (a && a.mape >= (r.mape || 0)) ? a : r, null);

  const deltaCls = (mape != null && mapePrev != null)
    ? (mape > mapePrev * 1.05 ? 'up' : mape < mapePrev * 0.95 ? 'down' : 'flat') : 'flat';
  const deltaTxt = (mape != null && mapePrev != null)
    ? (mape > mapePrev ? '▲ ' : '▼ ') + Math.abs((mape / mapePrev - 1) * 100).toFixed(0) + '% vs prior ' + state.win + 'd'
    : 'no prior period';

  const band = mape == null ? '' : mape > 0.04 ? 'danger' : mape > 0.028 ? 'warn' : 'good';

  document.getElementById('briefKpis').innerHTML = `
    <div class="kpi ${band}">
      <div class="kpi-label">MAPE · last ${state.win}d</div>
      <div class="kpi-value">${mape != null ? (mape * 100).toFixed(2) : '—'}<span class="unit">%</span></div>
      <div class="kpi-delta ${deltaCls}">${deltaTxt}</div>
    </div>
    <div class="kpi ${bias == null ? '' : Math.abs(bias) > 0.01 ? 'warn' : 'good'}">
      <div class="kpi-label">Systematic bias</div>
      <div class="kpi-value">${sPct(bias)}</div>
      <div class="kpi-note">${bias == null ? '' : bias > 0 ? 'tends to under-forecast' : 'tends to over-forecast'}</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Peak-hour MAPE</div>
      <div class="kpi-value">${peakMape != null ? (peakMape * 100).toFixed(2) : '—'}<span class="unit">%</span></div>
      <div class="kpi-note">accuracy when it matters most</div>
    </div>
    <div class="kpi ${misses > state.win * 2 ? 'warn' : ''}">
      <div class="kpi-label">Material misses (&gt;5% APE)</div>
      <div class="kpi-value">${misses}<span class="unit">hrs</span></div>
      <div class="kpi-note">of ${d.length * 24} hours</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Peak load · latest day</div>
      <div class="kpi-value">${latest ? mw(latest.peak_demand_mw) : '—'}<span class="unit">MW</span></div>
      <div class="kpi-note">${latest && latest.peak_hour_local != null ? 'at ' + String(latest.peak_hour_local).padStart(2, '0') + ':00 local' : ''}</div>
    </div>
    <div class="kpi ${worstDay && worstDay.mape > 0.05 ? 'danger' : ''}">
      <div class="kpi-label">Worst day in window</div>
      <div class="kpi-value">${worstDay ? (worstDay.mape * 100).toFixed(2) : '—'}<span class="unit">%</span></div>
      <div class="kpi-note">${worstDay ? shortDate(worstDay.date_key) : ''}</div>
    </div>`;

  // ---- narratives, newest first ----
  const nar = d.slice().reverse().slice(0, 5).map(r => `
    <div class="narrative ${(r.status_band || '').toLowerCase().replace(/\s+/g, '')}">
      <div class="who">${shortDate(r.date_key)} · ${r.day_type}${r.holiday_name ? ' · ' + r.holiday_name : ''}
        · <span class="pill ${r.status_band === 'Attention' ? 'Attention' : r.status_band === 'Watch' ? 'Watch' : 'Normal'}">${r.status_band || '—'}</span></div>
      ${r.daily_narrative || ''}
    </div>`).join('');
  document.getElementById('narratives').innerHTML = nar || '<div class="empty">No days in this window.</div>';

  // ---- alert table (all BAs, not just the selected one: an operations view
  //      should surface the worst problem anywhere, not hide it behind a filter)
  const al = rows(D.alerts);
  document.querySelector('#alertTable tbody').innerHTML = al.length ? al.slice(0, 25).map(r => `
    <tr>
      <td><span class="pill ${r.severity}">${r.severity}</span></td>
      <td>${r.ba_code}</td>
      <td class="mono">${localTimeLabel(r.local_datetime)}</td>
      <td class="num">${mw(r.demand_mw)}</td>
      <td class="num">${mw(r.forecast_mw)}</td>
      <td class="num" style="color:${r.forecast_error_mw > 0 ? C.danger : C.cool}">${sMw(r.forecast_error_mw)}</td>
      <td class="num">${pct(r.ape)}</td>
      <td class="num">${num(r.ape_zscore, 1)}σ</td>
      <td style="font-size:11.5px">${r.direction}</td>
      <td class="num">${num(r.temp_f, 0)}</td>
    </tr>`).join('')
    : '<tr><td colspan="10" class="empty">No hours exceeded 2σ in the last 3 days. The forecast is behaving normally.</td></tr>';

  // ---- hourly charts ----
  const h = byBA(D.hourly_recent, ba);
  const labels = h.map(r => localTimeLabel(r.local_datetime));

  draw('chartAvF', {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: 'Actual demand', data: h.map(r => r.demand_mw), borderColor: C.accent, backgroundColor: 'rgba(56,189,248,.08)', borderWidth: 1.8, pointRadius: 0, fill: true, tension: .25, spanGaps: true },
        { label: 'Day-ahead forecast', data: h.map(r => r.forecast_mw), borderColor: C.accent2, borderWidth: 1.4, borderDash: [4, 3], pointRadius: 0, fill: false, tension: .25, spanGaps: true }
      ]
    },
    options: {
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: { ticks: { maxTicksLimit: 8, maxRotation: 0 }, grid: { display: false } },
        y: { title: { display: true, text: 'MW' }, grid: { color: C.grid } }
      },
      plugins: { tooltip: { callbacks: { label: c => c.dataset.label + ': ' + mw(c.parsed.y) + ' MW' } } }
    }
  });

  draw('chartErr', {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        label: 'Forecast error (MW)',
        data: h.map(r => r.forecast_error_mw),
        // Colour encodes DIRECTION, so short vs long is readable at a glance
        // without consulting the axis sign.
        backgroundColor: h.map(r => r.forecast_error_mw > 0 ? 'rgba(244,63,94,.72)' : 'rgba(96,165,250,.72)'),
        borderWidth: 0
      }]
    },
    options: {
      scales: {
        x: { ticks: { maxTicksLimit: 8, maxRotation: 0 }, grid: { display: false } },
        y: { title: { display: true, text: 'MW  (+ short / − long)' }, grid: { color: C.grid } }
      },
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: c => sMw(c.parsed.y) + ' MW · ' + (c.parsed.y > 0 ? 'short' : 'long') } }
      }
    }
  });
}

/* ======================= PAGE: ACCURACY ======================= */
function renderAccuracy() {
  const bas = rows(D.balancing_authorities);

  // A generated headline, so the page states a conclusion rather than leaving
  // the reader to derive one. A dashboard that only shows numbers makes its
  // user do the analysis; a good one has already done it.
  const worst = bas[0];
  const best = bas[bas.length - 1];
  const mostBiased = bas.slice().sort((a, b) => Math.abs(b.bias_30d || 0) - Math.abs(a.bias_30d || 0))[0];
  document.getElementById('accuracyInsight').innerHTML = `
    <h3>Headline</h3>
    <p>Over the last 30 days <strong>${worst.ba_name}</strong> carries the highest day-ahead error at
    <strong>${pct(worst.mape_30d)}</strong> MAPE, against <strong>${pct(best.mape_30d)}</strong> for
    ${best.ba_name} &mdash; a spread of
    ${((worst.mape_30d / best.mape_30d - 1) * 100).toFixed(0)}%.
    The largest <em>systematic</em> component sits with <strong>${mostBiased.ba_name}</strong> at
    ${sPct(mostBiased.bias_30d)} bias, meaning it
    ${mostBiased.bias_30d > 0 ? 'consistently under-forecasts and runs short' : 'consistently over-forecasts and runs long'}.
    Unlike random error, that portion is addressable through model recalibration.</p>`;

  // ---- MAPE trend, one line per BA ----
  const dailyAll = rows(D.daily);
  const dates = [...new Set(dailyAll.map(r => r.date_key))].sort();
  draw('chartMapeTrend', {
    type: 'line',
    data: {
      labels: dates.map(shortDate),
      datasets: bas.map(b => {
        const m = new Map(byBA(D.daily, b.ba_code).map(r => [r.date_key, r.mape]));
        return {
          label: b.ba_code,
          data: dates.map(d => { const v = m.get(d); return v == null ? null : v * 100; }),
          borderColor: BA_COLORS[b.ba_code], borderWidth: 1.5,
          pointRadius: 0, tension: .3, fill: false, spanGaps: true
        };
      })
    },
    options: {
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: { ticks: { maxTicksLimit: 12, maxRotation: 0 }, grid: { display: false } },
        y: { title: { display: true, text: 'Daily MAPE (%)' }, grid: { color: C.grid }, beginAtZero: true }
      },
      plugins: { tooltip: { callbacks: { label: c => c.dataset.label + ': ' + num(c.parsed.y, 2) + '%' } } }
    }
  });

  // ---- MAPE vs bias, dual axis ----
  draw('chartMapeBias', {
    type: 'bar',
    data: {
      labels: bas.map(b => b.ba_code),
      datasets: [
        { label: 'MAPE 30d (%)', data: bas.map(b => (b.mape_30d || 0) * 100), backgroundColor: 'rgba(56,189,248,.62)', borderWidth: 0, yAxisID: 'y', order: 2 },
        { label: 'Bias 30d (%)', data: bas.map(b => (b.bias_30d || 0) * 100), type: 'line', borderColor: C.accent2, backgroundColor: C.accent2, borderWidth: 2, pointRadius: 4, yAxisID: 'y1', order: 1 }
      ]
    },
    options: {
      scales: {
        x: { grid: { display: false } },
        y:  { position: 'left',  title: { display: true, text: 'MAPE %' }, grid: { color: C.grid }, beginAtZero: true },
        // grid off on the right axis so two gridline sets do not overlay and
        // create a moire that makes both harder to read.
        y1: { position: 'right', title: { display: true, text: 'Bias %' }, grid: { display: false } }
      }
    }
  });

  // ---- overall vs peak-hour ----
  draw('chartPeakVsAll', {
    type: 'bar',
    data: {
      labels: bas.map(b => b.ba_code),
      datasets: [
        { label: 'MAPE, all hours', data: bas.map(b => (b.mape_all || 0) * 100), backgroundColor: 'rgba(143,163,181,.55)', borderWidth: 0 },
        { label: 'MAPE, peak hour', data: bas.map(b => (b.mape_peak_hour || 0) * 100), backgroundColor: 'rgba(244,63,94,.78)', borderWidth: 0 }
      ]
    },
    options: {
      scales: { x: { grid: { display: false } }, y: { title: { display: true, text: 'MAPE %' }, grid: { color: C.grid }, beginAtZero: true } },
      plugins: { tooltip: { callbacks: { label: c => c.dataset.label + ': ' + num(c.parsed.y, 2) + '%' } } }
    }
  });

  // ---- scorecard ----
  document.querySelector('#scorecard tbody').innerHTML = bas.map(b => `
    <tr>
      <td><strong>${b.ba_name}</strong><br><span class="hint">${b.market}</span></td>
      <td>${b.region}</td>
      <td style="font-size:11.5px">${b.size_band}</td>
      <td class="num">${pct(b.mape_7d)}</td>
      <td class="num"><strong>${pct(b.mape_30d)}</strong></td>
      <td class="num">${pct(b.mape_all)}</td>
      <td class="num" style="color:${Math.abs(b.bias_30d || 0) > 0.01 ? C.warn : C.text}">${sPct(b.bias_30d)}</td>
      <td class="num">${pct(b.mape_peak_hour)}</td>
      <td class="num">${mw(b.avg_demand_mw)}</td>
      <td class="num">${b.material_misses ?? '—'}</td>
    </tr>`).join('');
}

/* ======================= PAGE: ROOT CAUSE ======================= */
function renderDrivers() {
  const tp = rows(D.profile_temperature);

  // Aggregate the CDH buckets across all BAs, weighting by hour count so a
  // small operator's thin bucket does not carry the same weight as a large
  // one's thick bucket. An unweighted mean of means is a real and common error.
  const buckets = [...new Set(tp.map(r => r.cdh_bucket))].sort((a, b) => a - b);
  const agg = buckets.map(bk => {
    const g = tp.filter(r => r.cdh_bucket === bk);
    const n = g.reduce((a, r) => a + r.hours, 0);
    return {
      bucket: bk,
      hours: n,
      mape: g.reduce((a, r) => a + r.mape * r.hours, 0) / n,
      bias: g.reduce((a, r) => a + r.bias * r.hours, 0) / n
    };
  });

  const hot = agg.filter(a => a.bucket >= 15);
  const mild = agg.filter(a => a.bucket < 5);
  const hotBias = hot.length ? hot.reduce((a, r) => a + r.bias * r.hours, 0) / hot.reduce((a, r) => a + r.hours, 0) : null;
  const mildBias = mild.length ? mild.reduce((a, r) => a + r.bias * r.hours, 0) / mild.reduce((a, r) => a + r.hours, 0) : null;
  const hotMape = hot.length ? hot.reduce((a, r) => a + r.mape * r.hours, 0) / hot.reduce((a, r) => a + r.hours, 0) : null;
  const mildMape = mild.length ? mild.reduce((a, r) => a + r.mape * r.hours, 0) / mild.reduce((a, r) => a + r.hours, 0) : null;

  document.getElementById('driverInsight').innerHTML = `
    <h3>The finding</h3>
    <p>In mild conditions (under 5 cooling degree hours) the day-ahead forecast is
    <strong>essentially unbiased</strong> at ${sPct(mildBias)}, with ${pct(mildMape)} MAPE.
    Above 15 cooling degree hours, bias moves to <strong>${sPct(hotBias)}</strong> and MAPE rises to
    <strong>${pct(hotMape)}</strong>.
    The forecast does not simply become noisier when it is hot &mdash; it becomes
    <em>directionally wrong</em>, consistently ${hotBias > 0 ? 'under' : 'over'}-predicting load.
    That asymmetry points at an under-modelled air-conditioning response rather than at irreducible
    uncertainty, and it recurs on the days when being wrong is most expensive.</p>`;

  draw('chartCdhBias', {
    type: 'bar',
    data: {
      labels: agg.map(a => a.bucket >= 20 ? '20+' : a.bucket + '–' + (a.bucket + 2.5)),
      datasets: [
        {
          label: 'Bias (%)', data: agg.map(a => a.bias * 100),
          backgroundColor: agg.map(a => a.bias > 0 ? 'rgba(244,63,94,.78)' : 'rgba(96,165,250,.78)'),
          borderWidth: 0, yAxisID: 'y', order: 2
        },
        {
          label: 'MAPE (%)', data: agg.map(a => a.mape * 100), type: 'line',
          borderColor: C.accent2, backgroundColor: C.accent2, borderWidth: 2, pointRadius: 3, yAxisID: 'y1', order: 1
        }
      ]
    },
    options: {
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: { title: { display: true, text: 'Cooling degree hours (°F above 65°F base)' }, grid: { display: false } },
        y:  { position: 'left',  title: { display: true, text: 'Bias %  (+ under-forecast)' }, grid: { color: C.grid } },
        y1: { position: 'right', title: { display: true, text: 'MAPE %' }, grid: { display: false }, beginAtZero: true }
      },
      plugins: {
        tooltip: {
          callbacks: {
            label: c => c.dataset.label + ': ' + num(c.parsed.y, 2) + '%',
            afterBody: items => 'n = ' + agg[items[0].dataIndex].hours.toLocaleString() + ' hours'
          }
        }
      }
    }
  });

  // ---- hour-of-day profile ----
  const hp = rows(D.profile_hour);
  const hours = [...Array(24).keys()];
  const bas = rows(D.balancing_authorities);
  draw('chartHourProfile', {
    type: 'line',
    data: {
      labels: hours.map(h => String(h).padStart(2, '0') + ':00'),
      datasets: bas.map(b => {
        const m = new Map(hp.filter(r => r.ba_code === b.ba_code).map(r => [r.local_hour, r.mape]));
        return {
          label: b.ba_code,
          data: hours.map(h => { const v = m.get(h); return v == null ? null : v * 100; }),
          borderColor: BA_COLORS[b.ba_code], borderWidth: 1.5, pointRadius: 0, tension: .35, fill: false
        };
      })
    },
    options: {
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: { ticks: { maxTicksLimit: 12 }, grid: { display: false }, title: { display: true, text: 'Local hour' } },
        y: { title: { display: true, text: 'MAPE %' }, grid: { color: C.grid }, beginAtZero: true }
      }
    }
  });

  // ---- load vs temperature scatter ----
  const sc = rows(D.scatter_temp_demand);
  draw('chartScatter', {
    type: 'scatter',
    data: {
      datasets: bas.map(b => {
        const g = sc.filter(r => r.ba_code === b.ba_code);
        // Normalise each BA's load to a share of its own maximum. Without this
        // PJM's 150 GW would compress every other operator into a flat line at
        // the bottom of the axis and the shared U-shape would be invisible.
        const mx = Math.max(...g.map(r => r.demand_mw || 0)) || 1;
        return {
          label: b.ba_code,
          data: g.map(r => ({ x: r.temp_f, y: (r.demand_mw / mx) * 100 })),
          backgroundColor: BA_COLORS[b.ba_code] + '55',
          pointRadius: 1.6, borderWidth: 0
        };
      })
    },
    options: {
      scales: {
        x: { title: { display: true, text: 'Population-weighted temperature (°F)' }, grid: { color: C.grid } },
        y: { title: { display: true, text: 'Load, % of that BA’s own peak' }, grid: { color: C.grid } }
      },
      plugins: {
        tooltip: { callbacks: { label: c => c.dataset.label + ': ' + num(c.parsed.x, 0) + '°F, ' + num(c.parsed.y, 0) + '% of peak' } }
      }
    }
  });

  // ---- day type ----
  const daily = rows(D.daily);
  const types = ['Weekday', 'Weekend', 'Holiday'];
  const byType = types.map(t => {
    const g = daily.filter(r => r.day_type === t && r.mape != null);
    return g.length ? g.reduce((a, r) => a + r.mape, 0) / g.length : null;
  });
  const counts = types.map(t => daily.filter(r => r.day_type === t && r.mape != null).length);
  draw('chartDayType', {
    type: 'bar',
    data: {
      labels: types.map((t, i) => t + '  (n=' + counts[i] + ')'),
      datasets: [{
        label: 'Mean daily MAPE (%)',
        data: byType.map(v => v == null ? null : v * 100),
        backgroundColor: ['rgba(56,189,248,.7)', 'rgba(52,211,153,.7)', 'rgba(244,63,94,.8)'],
        borderWidth: 0
      }]
    },
    options: {
      indexAxis: 'y',
      scales: { x: { title: { display: true, text: 'MAPE %' }, grid: { color: C.grid }, beginAtZero: true }, y: { grid: { display: false } } },
      plugins: { legend: { display: false } }
    }
  });
}

/* ======================= PAGE: OUTLOOK ======================= */
function renderOutlook() {
  const ol = rows(D.outlook);
  if (!ol.length) {
    document.getElementById('chartOutlook').parentElement.innerHTML =
      '<div class="empty">No forward-looking hours available.</div>';
    return;
  }
  const bas = rows(D.balancing_authorities);
  const stamps = [...new Set(ol.map(r => r.period_utc))].sort();

  draw('chartOutlook', {
    type: 'line',
    data: {
      labels: stamps.map(s => String(s).slice(5, 16).replace('T', ' ')),
      datasets: bas.map(b => {
        const m = new Map(ol.filter(r => r.ba_code === b.ba_code).map(r => [r.period_utc, r.temp_vs_recent_max]));
        return {
          label: b.ba_code,
          data: stamps.map(s => m.get(s) ?? null),
          borderColor: BA_COLORS[b.ba_code], borderWidth: 1.6,
          pointRadius: 0, tension: .3, fill: false, spanGaps: true
        };
      })
    },
    options: {
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: { ticks: { maxTicksLimit: 10, maxRotation: 0 }, grid: { display: false }, title: { display: true, text: 'UTC' } },
        y: { title: { display: true, text: '°F above / below trailing 30-day max' }, grid: { color: C.grid } }
      },
      plugins: {
        tooltip: { callbacks: { label: c => c.dataset.label + ': ' + (c.parsed.y >= 0 ? '+' : '') + num(c.parsed.y, 1) + '°F vs 30d max' } }
      }
    }
  });

  // Daily roll-up of the forward window.
  const key = r => r.ba_code + '|' + r.date_key;
  const grouped = {};
  ol.forEach(r => {
    const k = key(r);
    if (!grouped[k]) grouped[k] = { ba_code: r.ba_code, ba_name: r.ba_name, date_key: r.date_key, temps: [], cdh: 0, fc: [], recent: r.recent_max_temp };
    if (r.temp_f != null) grouped[k].temps.push(r.temp_f);
    grouped[k].cdh += r.cooling_degree_hours || 0;
    if (r.forecast_mw != null) grouped[k].fc.push(r.forecast_mw);
  });

  const outRows = Object.values(grouped)
    .map(g => {
      const maxT = g.temps.length ? Math.max(...g.temps) : null;
      const vs = (maxT != null && g.recent != null) ? maxT - g.recent : null;
      return { ...g, maxT, vs, peakFc: g.fc.length ? Math.max(...g.fc) : null };
    })
    .sort((a, b) => (b.vs ?? -99) - (a.vs ?? -99));

  document.querySelector('#outlookTable tbody').innerHTML = outRows.map(r => {
    const risk = r.vs == null ? 'Unknown' : r.vs > 3 ? 'Elevated' : r.vs > 0 ? 'Watch' : 'Normal';
    return `<tr>
      <td><strong>${r.ba_code}</strong></td>
      <td>${shortDate(r.date_key)}</td>
      <td class="num">${num(r.maxT, 1)}</td>
      <td class="num" style="color:${r.vs > 0 ? C.danger : C.text}">${r.vs == null ? '—' : (r.vs >= 0 ? '+' : '') + num(r.vs, 1)}</td>
      <td class="num">${num(r.cdh, 0)}</td>
      <td class="num">${mw(r.peakFc)}</td>
      <td><span class="pill ${risk === 'Elevated' ? 'High' : risk === 'Watch' ? 'Elevated' : 'Normal'}">${risk}</span></td>
    </tr>`;
  }).join('');
}

/* ======================= PAGE: DATA QUALITY ======================= */
function renderQuality() {
  const m = D.meta;
  const dq = rows(m.dq_checks);
  const fails = dq.filter(r => r.severity === 'FAIL' && r.result !== 'PASS').length;
  const warns = dq.filter(r => r.result === 'WARN').length;

  document.getElementById('dqKpis').innerHTML = `
    <div class="kpi ${fails ? 'danger' : 'good'}">
      <div class="kpi-label">Checks passing</div>
      <div class="kpi-value">${m.dq_pass}<span class="unit">/ ${m.dq_total}</span></div>
      <div class="kpi-note">${fails ? fails + ' hard failure(s)' : 'no blocking failures'}</div>
    </div>
    <div class="kpi ${warns ? 'warn' : 'good'}">
      <div class="kpi-label">Warnings</div>
      <div class="kpi-value">${warns}</div>
      <div class="kpi-note">tolerated, disclosed</div>
    </div>
    <div class="kpi ${m.hours_behind > 6 ? 'warn' : 'good'}">
      <div class="kpi-label">Data lag</div>
      <div class="kpi-value">${num(m.hours_behind, 1)}<span class="unit">h</span></div>
      <div class="kpi-note">behind real time</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Fact rows</div>
      <div class="kpi-value">${(m.fact_rows / 1000).toFixed(1)}<span class="unit">k</span></div>
      <div class="kpi-note">BA-hours in the model</div>
    </div>`;

  document.querySelector('#dqTable tbody').innerHTML = dq.map(r => `
    <tr>
      <td><span class="pill ${r.result}">${r.result}</span></td>
      <td class="mono" style="font-size:11.5px">${r.check_name}</td>
      <td>${r.severity}</td>
      <td class="num">${r.observed ?? '—'}</td>
      <td class="num">${r.expected ?? '—'}</td>
      <td style="font-size:11.5px;color:var(--text-dim)">${r.description}</td>
    </tr>`).join('');

  const bas = rows(D.balancing_authorities);
  draw('chartCoverage', {
    type: 'bar',
    data: {
      labels: bas.map(b => b.ba_code),
      datasets: [{
        label: 'Gradeable hours',
        data: bas.map(b => b.gradeable_hours),
        backgroundColor: bas.map(b => BA_COLORS[b.ba_code] + 'bb'),
        borderWidth: 0
      }]
    },
    options: {
      scales: { x: { grid: { display: false } }, y: { grid: { color: C.grid }, beginAtZero: true, title: { display: true, text: 'hours' } } },
      plugins: { legend: { display: false } }
    }
  });

  const p = m.pipeline || {};
  document.querySelector('#metaTable tbody').innerHTML = Object.entries({
    'Latest actual hour (UTC)': m.latest_actual_hour_utc,
    'Latest data hour (UTC)': m.latest_data_hour_utc,
    'History start': dateOnly(m.history_start),
    'History end': dateOnly(m.history_end),
    'Payload generated (UTC)': m.generated_utc,
    'Raw region files': p.raw_region_files,
    'Balancing authorities': (p.ba_codes || []).join(', '),
    'Synthetic data present': m.is_demo_data ? 'YES — not for publication' : 'no'
  }).map(([k, v]) => `<tr><td style="color:var(--text-faint)">${k}</td><td class="mono num">${v ?? '—'}</td></tr>`).join('');
}

boot();
