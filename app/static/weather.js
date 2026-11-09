/* TACISS weather: NWS alerts, radar by forecast office, forecast, products, county alerting + ticker.
   All NWS data comes through the TACISS server (/api/weather/*), which polls and caches it. */
'use strict';

const WFO = {
  MRX: { name: 'Morristown, TN', radar: 'KMRX', lat: 36.17, lon: -83.40 },
  OHX: { name: 'Nashville, TN', radar: 'KOHX', lat: 36.25, lon: -86.56 },
  MEG: { name: 'Memphis, TN', radar: 'KNQA', lat: 35.34, lon: -89.87 },
  HUN: { name: 'Huntsville, AL', radar: 'KHTX', lat: 34.93, lon: -86.08 },
  JKL: { name: 'Jackson, KY', radar: 'KJKL', lat: 37.59, lon: -83.31 },
  PAH: { name: 'Paducah, KY', radar: 'KPAH', lat: 37.07, lon: -88.77 },
  LMK: { name: 'Louisville, KY', radar: 'KLVX', lat: 37.98, lon: -85.94 },
  FFC: { name: 'Peachtree City, GA', radar: 'KFFC', lat: 33.36, lon: -84.57 },
  GSP: { name: 'Greenville-Spartanburg, SC', radar: 'KGSP', lat: 34.88, lon: -82.22 },
  RNK: { name: 'Blacksburg, VA', radar: 'KFCX', lat: 37.02, lon: -80.27 },
  BMX: { name: 'Birmingham, AL', radar: 'KBMX', lat: 33.17, lon: -86.77 },
  JAN: { name: 'Jackson, MS', radar: 'KDGX', lat: 32.28, lon: -89.98 },
  LZK: { name: 'Little Rock, AR', radar: 'KLZK', lat: 34.84, lon: -92.26 },
};
const EXTRA_RADARS = { KHPX: 'Fort Campbell, KY', KGWX: 'Columbus AFB, MS', KLVX: 'Louisville, KY' };

// NWS hazard map colors for the common products; anything else falls back on severity.
const WX_COLORS = {
  'Tornado Warning': '#FF0000', 'Tornado Emergency': '#FF0000', 'Severe Thunderstorm Warning': '#FFA500',
  'Flash Flood Warning': '#8B0000', 'Flash Flood Emergency': '#8B0000', 'Flood Warning': '#00FF00',
  'Tornado Watch': '#FFFF00', 'Severe Thunderstorm Watch': '#DB7093', 'Flash Flood Watch': '#2E8B57',
  'Flood Watch': '#2E8B57', 'Flood Advisory': '#00FF7F', 'Winter Storm Warning': '#FF69B4',
  'Winter Storm Watch': '#4682B4', 'Winter Weather Advisory': '#7B68EE', 'Ice Storm Warning': '#8B008B',
  'Blizzard Warning': '#FF4500', 'Wind Advisory': '#D2B48C', 'High Wind Warning': '#DAA520',
  'Heat Advisory': '#FF7F50', 'Extreme Heat Warning': '#C71585', 'Excessive Heat Warning': '#C71585',
  'Dense Fog Advisory': '#708090', 'Freeze Warning': '#483D8B', 'Frost Advisory': '#6495ED',
  'Special Weather Statement': '#FFE4B5', 'Red Flag Warning': '#FF1493', 'Fire Weather Watch': '#FFDEAD',
  'Air Quality Alert': '#808080',
};
const EXERCISE_EVENTS = ['Tornado Warning', 'Severe Thunderstorm Warning', 'Flash Flood Warning', 'Tornado Watch',
  'Severe Thunderstorm Watch', 'Flood Warning', 'Winter Storm Warning', 'Wind Advisory'];

S.wx = null;          // latest /api/weather/alerts snapshot
S.wxSeen = new Set(); // alert ids already announced in this browser session
S.counties = null;    // [{same, name, state, cwa}]

/* ---------- helpers ---------- */
function alertColor(a) {
  return WX_COLORS[a.event] || { Extreme: '#FF0000', Severe: '#FF8C00', Moderate: '#FFD700' }[a.severity] || '#94A3B8';
}
function inkFor(hex) {
  const n = parseInt(hex.slice(1), 16);
  const lum = (0.299 * (n >> 16) + 0.587 * ((n >> 8) & 255) + 0.114 * (n & 255)) / 255;
  return lum > 0.55 ? '#111' : '#fff';
}
function alertLevel(a) {
  const e = a.event || '';
  if (/Warning|Emergency/.test(e)) return 'warning';
  if (/Watch/.test(e)) return 'watch';
  return 'advisory';
}
const LEVEL_RANK = { warning: 3, watch: 2, advisory: 1 };
function sortAlerts(list) {
  return list.slice().sort((a, b) => (LEVEL_RANK[alertLevel(b)] - LEVEL_RANK[alertLevel(a)]) || String(b.sent).localeCompare(String(a.sent)));
}
function until(a) {
  const t = a.ends || a.expires;
  if (!t) return '';
  const d = new Date(t);
  const sameDay = d.toDateString() === new Date().toDateString();
  return (sameDay ? '' : d.toLocaleDateString([], { weekday: 'short' }) + ' ') +
    d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}
function chip(a) {
  const c = alertColor(a);
  return h('span', { class: 'wx-chip', style: `background:${c};color:${inkFor(c)}` }, a.test ? '🧪 EXERCISE · ' : '', a.event);
}

function wxPrefs() {
  if (S.me) return S.me.prefs || {};
  try { return JSON.parse(store('taciss-wx') || '{}'); } catch { return {}; }
}
const myCounties = () => wxPrefs().wx_counties || [];
async function saveWxPrefs(p) {
  if (S.me) S.me.prefs = await api('PUT', 'me/prefs', p);
  else store('taciss-wx', JSON.stringify({ ...wxPrefs(), ...p }));
  renderTicker();
  render();
}
async function loadCounties() {
  if (!S.counties) S.counties = await api('GET', 'weather/counties').catch(() => null);
  return S.counties || [];
}
function countyName(same) {
  const c = (S.counties || []).find((x) => x.same === same);
  return c ? c.name : same;
}
const myAlertsFrom = (alerts) => {
  const mine = new Set(myCounties());
  return sortAlerts((alerts || []).filter((a) => (a.same || []).some((c) => mine.has(c))));
};
const myAlerts = () => myAlertsFrom(S.wx && S.wx.alerts);
function myAreaNames(a) {
  const mine = new Set(myCounties());
  const hits = (a.same || []).filter((c) => mine.has(c)).map(countyName);
  return hits.length ? hits.join(', ') : a.area;
}

/* ---------- live refresh + alarm ---------- */
async function refreshWx(live) {
  if (!(S.me || S.publicRead)) return;
  try { S.wx = await api('GET', 'weather/alerts'); } catch (e) { S.wx = S.wx || { alerts: [], error: e.message }; }
  const fresh = S.wx.alerts.filter((a) => !S.wxSeen.has(a.id));
  fresh.forEach((a) => S.wxSeen.add(a.id));
  if (live) {
    const hits = myAlertsFrom(fresh);
    if (hits.length) alarm(hits);
  }
  renderTicker();
  const v = route().view;
  if (live && (v === 'dashboard' || v === 'weather')) { clearTimeout(renderTimer); renderTimer = setTimeout(render, 250); }
}

function alarm(hits) {
  const worst = hits.some((a) => alertLevel(a) === 'warning') ? 'warning' : hits.some((a) => alertLevel(a) === 'watch') ? 'watch' : 'advisory';
  for (const a of hits.slice(0, 4)) toast(`${a.test ? '🧪 EXERCISE: ' : '⚠ '}${a.event} — ${myAreaNames(a)} until ${until(a)}`, alertLevel(a) === 'warning');
  document.body.classList.remove('wx-flash-warning', 'wx-flash-watch', 'wx-flash-advisory');
  void document.body.offsetWidth; // restart the animation
  document.body.classList.add('wx-flash-' + worst);
  setTimeout(() => document.body.classList.remove('wx-flash-' + worst), 9000);
  if (wxPrefs().wx_sound) beep(worst === 'warning' ? 3 : worst === 'watch' ? 2 : 1);
}

let audioCtx = null;
function beep(times) {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    for (let i = 0; i < times; i++) {
      const o = audioCtx.createOscillator();
      const g = audioCtx.createGain();
      const t = audioCtx.currentTime + i * 0.55;
      o.type = 'square';
      o.frequency.setValueAtTime(1050, t);
      o.frequency.setValueAtTime(853, t + 0.2);
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.25, t + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 0.42);
      o.connect(g).connect(audioCtx.destination);
      o.start(t); o.stop(t + 0.45);
    }
  } catch { /* audio blocked until the user interacts with the page */ }
}

/* ---------- ticker under the menu (dashboard) ---------- */
function renderTicker() {
  const el = document.getElementById('ticker');
  if (!el) return;
  const show = route().view === 'dashboard' && (S.me || S.publicRead) && !S.needsSetup;
  if (!show) { el.hidden = true; return; }
  el.hidden = false;
  const mine = myCounties();
  const pick = h('button', { class: 'ticker-btn', onclick: countyPicker, title: 'Choose which counties alert you' },
    mine.length ? `📍 ${mine.length} ${mine.length === 1 ? 'county' : 'counties'}` : '📍 Choose counties');
  const msg = (cls, ...kids) => { el.className = 'ticker ' + cls; el.replaceChildren(pick, h('div', { class: 'ticker-static' }, kids)); };
  if (!S.wx) return msg('ticker-idle', 'Loading NWS alerts…');
  if (!mine.length) return msg('ticker-idle', 'Choose your counties to see NWS watches and warnings for them here.');
  const list = myAlerts();
  if (!list.length) {
    const names = mine.slice(0, 6).map(countyName).join(', ') + (mine.length > 6 ? ` +${mine.length - 6}` : '');
    return msg('ticker-ok', `✓ No active NWS alerts for ${names}`,
      S.wx.updated ? h('span', { class: 'ticker-meta' }, ` · checked ${ago(S.wx.updated)}`) : null,
      S.wx.error ? h('span', { class: 'ticker-meta' }, ` · ⚠ ${S.wx.error}`) : null);
  }
  const worst = list.some((a) => alertLevel(a) === 'warning') ? 'warning' : list.some((a) => alertLevel(a) === 'watch') ? 'watch' : 'advisory';
  el.className = 'ticker ticker-' + worst;
  const item = (a) => h('a', { class: 'tick-item', href: '#/weather/' + encodeURIComponent(a.id) },
    chip(a), h('b', {}, myAreaNames(a)), ' until ' + until(a), a.headline && !a.test ? h('span', { class: 'ticker-meta' }, ' — ' + (a.sender || '')) : null);
  const one = h('div', { class: 'ticker-run' }, list.map(item));
  const two = one.cloneNode(true); // second copy makes the scroll loop seamless
  two.setAttribute('aria-hidden', 'true');
  const chars = list.reduce((n, a) => n + (a.event || '').length + myAreaNames(a).length + 30, 0);
  const track = h('div', { class: 'ticker-track', style: `--dur:${Math.max(18, Math.round(chars * 0.22))}s` }, one, two);
  el.replaceChildren(pick, h('div', { class: 'ticker-label' }, `${list.length} ALERT${list.length > 1 ? 'S' : ''}`), h('div', { class: 'ticker-viewport' }, track));
}

/* ---------- dashboard card ---------- */
function wxDashboardCard() {
  if (!(S.me || S.publicRead)) return null;
  const mine = myCounties();
  const list = myAlerts();
  const hot = list.some((a) => alertLevel(a) === 'warning');
  return h('div', { class: 'card wx-card' + (hot ? ' wx-hot' : list.length ? ' wx-warm' : '') },
    h('h3', {}, '⛈ Weather Alerts — My Counties',
      h('button', { class: 'small right', onclick: countyPicker }, mine.length ? 'Change counties' : 'Choose counties')),
    !mine.length ? h('div', { class: 'empty' }, 'Pick your counties to be alerted when the NWS issues a watch or warning for them.')
      : list.length ? h('ul', { class: 'items' }, list.map((a) => h('li', { onclick: () => alertModal(a) },
        h('div', { class: 't' }, chip(a)),
        h('div', { class: 's' }, myAreaNames(a), ' · until ', until(a), ' · ', a.sender || ''))))
        : h('div', { class: 'empty' }, '✓ No active alerts for your ', mine.length, ' ', mine.length === 1 ? 'county' : 'counties', '.'));
}

/* ---------- modals ---------- */
function alertModal(a) {
  const c = alertColor(a);
  const close = modal(a.event, [
    h('div', { class: 'wx-modal-head', style: `border-left:6px solid ${c}` },
      chip(a), ' ', badge((a.severity || '').toLowerCase() === 'extreme' ? 'critical' : null),
      h('div', { style: 'margin-top:6px;font-weight:600' }, a.headline || ''),
      h('div', { class: 'muted', style: 'font-size:13px;margin-top:4px' },
        `Issued ${fmtTime(a.sent)} · Until ${until(a)} · ${a.sender || ''} · Severity: ${a.severity || '?'} · Urgency: ${a.urgency || '?'}`)),
    h('h4', {}, 'Areas'), h('p', {}, a.area || ''),
    a.description ? [h('h4', {}, 'Details'), h('pre', { class: 'wx-pre' }, a.description)] : null,
    a.instruction ? [h('h4', {}, 'Precautionary / preparedness actions'), h('pre', { class: 'wx-pre' }, a.instruction)] : null,
  ], [h('button', { class: 'primary', onclick: () => close() }, 'Close')]);
}

async function countyPicker() {
  const counties = await loadCounties();
  if (!counties.length) { toast('County list unavailable — the server could not reach the NWS yet.', true); return; }
  const chosen = new Set(myCounties());
  const q = h('input', { type: 'search', placeholder: 'Filter counties…' });
  const grid = h('div', { class: 'county-grid' });
  const sound = h('input', { type: 'checkbox', checked: !!wxPrefs().wx_sound });
  const count = h('span', { class: 'muted' });
  function draw() {
    const t = q.value.trim().toLowerCase();
    grid.replaceChildren(...counties.filter((c) => !t || c.name.toLowerCase().includes(t) || c.cwa.join(' ').toLowerCase().includes(t))
      .map((c) => h('label', { class: 'county' + (chosen.has(c.same) ? ' on' : '') },
        h('input', { type: 'checkbox', checked: chosen.has(c.same), onchange: (e) => {
          if (e.target.checked) chosen.add(c.same); else chosen.delete(c.same);
          e.target.parentElement.classList.toggle('on', e.target.checked); updateCount();
        } }),
        c.name, h('span', { class: 'muted' }, ` ${c.state} · ${c.cwa.join('/')}`))));
  }
  function updateCount() { count.textContent = `${chosen.size} selected`; }
  q.addEventListener('input', draw);
  const offices = [...new Set(counties.flatMap((c) => c.cwa))].sort();
  const close = modal('My alert counties', [
    h('p', { class: 'muted', style: 'margin-top:0' }, 'You will be alerted on the dashboard (flash + ticker) when the NWS issues an alert covering any of these counties.',
      S.me ? ' Saved to your account.' : ' Saved in this browser.'),
    h('div', { class: 'toolbar' }, q,
      offices.map((o) => h('button', { type: 'button', class: 'small', title: `Add every county served by NWS ${o}`, onclick: () => {
        counties.filter((c) => c.cwa.includes(o)).forEach((c) => chosen.add(c.same)); draw(); updateCount();
      } }, '+ All ' + o)),
      h('button', { type: 'button', class: 'small', onclick: () => { chosen.clear(); draw(); updateCount(); } }, 'Clear')),
    grid,
    h('label', { class: 'inline-check' }, sound, 'Play an alert tone when a new alert hits my counties'),
  ], [count, h('button', { onclick: () => close() }, 'Cancel'),
    h('button', { class: 'primary', onclick: async () => {
      try { await saveWxPrefs({ wx_counties: [...chosen], wx_sound: sound.checked }); close(); toast('Alert counties saved'); if (sound.checked) beep(1); }
      catch (e) { toast(e.message, true); }
    } }, 'Save')]);
  draw(); updateCount();
}

async function exerciseModal() {
  const counties = await loadCounties();
  const chosen = new Set(myCounties());
  const ev = h('select', {}, EXERCISE_EVENTS.map((e) => h('option', { value: e }, e)));
  const mins = h('input', { type: 'number', min: 1, max: 240, value: 15 });
  const sel = h('select', { multiple: true, size: 10 }, counties.map((c) => h('option', { value: c.same, selected: chosen.has(c.same) }, `${c.name} (${c.state})`)));
  const err = h('div', { class: 'err' });
  const close = modal('Inject exercise alert', [
    h('p', { class: 'muted', style: 'margin-top:0' }, 'Creates a clearly-labelled EXERCISE alert inside TACISS only (nothing is sent to the NWS or the public) so you can drill your alert procedures. It is logged in the audit log.'),
    h('div', { class: 'grid-form' }, h('label', {}, 'Alert type', ev), h('label', {}, 'Lasts (minutes)', mins),
      h('label', { class: 'full' }, 'Counties (Ctrl/⌘-click for several)', sel)), err,
  ], [h('button', { onclick: () => close() }, 'Cancel'), h('button', { class: 'primary', onclick: async () => {
    const same = [...sel.selectedOptions].map((o) => o.value);
    try {
      await api('POST', 'weather/test', { event: ev.value, same, minutes: +mins.value, area: same.map(countyName).join('; ') });
      close(); toast('Exercise alert injected');
    } catch (e) { err.textContent = e.message; }
  } }, 'Inject exercise')]);
}

/* ---------- Weather tab ---------- */
function currentOffice() {
  const list = (S.settings.wx_offices || 'MRX,OHX,MEG').split(',').filter(Boolean);
  const saved = store('taciss-wx-office');
  return list.includes(saved) ? saved : list[0];
}
function radarFor(office) {
  const saved = store('taciss-wx-radar-' + office);
  return saved || (WFO[office] ? WFO[office].radar : 'K' + office);
}

VIEWS.weather = async (root, params) => {
  await loadCounties();
  if (!S.wx) await refreshWx(false);
  const offices = (S.settings.wx_offices || 'MRX,OHX,MEG').split(',').filter(Boolean);
  const office = currentOffice();
  const radar = radarFor(office);
  const wx = S.wx || { alerts: [] };
  const filter = store('taciss-wx-filter') || (myCounties().length ? 'mine' : 'all');
  const shown = filter === 'mine' ? myAlerts() : sortAlerts(wx.alerts);
  const tests = wx.alerts.filter((a) => a.test);

  // toolbar
  root.append(h('div', { class: 'toolbar' },
    h('h2', {}, 'Weather'),
    h('span', { class: 'muted', style: 'font-size:13px' },
      wx.updated ? `NWS alerts for ${wx.states || S.settings.wx_states} · updated ${ago(wx.updated)}` : 'Waiting for first NWS update…',
      wx.error ? h('span', { class: 'c-warn' }, ' · ⚠ ' + wx.error) : null),
    h('button', { onclick: countyPicker }, `📍 My counties (${myCounties().length})`),
    can('operator') ? h('button', { onclick: exerciseModal, title: 'Drill: create a labelled exercise alert' }, '🧪 Exercise alert') : null,
    can('operator') && tests.length ? h('button', { class: 'danger', onclick: async () => { await api('DELETE', 'weather/test'); toast('Exercise alerts cleared'); } }, `Clear ${tests.length} exercise`) : null));

  // alerts card
  const alertsCard = h('div', { class: 'card' },
    h('h3', {}, '⚠ Active Alerts',
      h('span', { class: 'right seg' },
        ['mine', 'all'].map((f) => h('button', { class: 'small' + (filter === f ? ' primary' : ''), onclick: () => { store('taciss-wx-filter', f); render(); } },
          f === 'mine' ? 'My counties' : 'All ' + (wx.states || S.settings.wx_states))))),
    shown.length ? h('ul', { class: 'items wx-list' }, shown.map((a) => h('li', { onclick: () => alertModal(a), style: `border-left:5px solid ${alertColor(a)}` },
      h('div', { class: 't' }, chip(a), ' ', h('span', { class: 'muted', style: 'font-weight:500;font-size:13px' }, 'until ' + until(a))),
      h('div', { class: 's' }, filter === 'mine' ? myAreaNames(a) : (a.area || '').slice(0, 220)),
      h('div', { class: 's' }, a.sender || ''))))
      : h('div', { class: 'empty' }, filter === 'mine' && !myCounties().length ? 'Choose your counties to filter alerts.'
        : '✓ No active alerts' + (filter === 'mine' ? ' for your counties.' : '.')));

  // radar card
  const loop = h('img', { class: 'radar-img', alt: `NWS radar loop ${radar}`,
    src: `https://radar.weather.gov/ridge/standard/${radar}_loop.gif?t=${Math.floor(Date.now() / 120000)}` });
  loop.addEventListener('error', () => loop.replaceWith(h('div', { class: 'empty' }, `Radar ${radar} unavailable (needs internet on this device).`)));
  S.timers.push(setInterval(() => { loop.src = `https://radar.weather.gov/ridge/standard/${radar}_loop.gif?t=${Math.floor(Date.now() / 120000)}`; }, 120000));
  const radarChoices = [...new Set([...offices.map((o) => (WFO[o] ? WFO[o].radar : 'K' + o)), ...Object.keys(EXTRA_RADARS)])];
  const radarCard = h('div', { class: 'card' },
    h('h3', {}, '📡 Radar',
      h('span', { class: 'right seg' }, offices.map((o) => h('button', {
        class: 'small' + (o === office ? ' primary' : ''), title: WFO[o] ? 'NWS ' + WFO[o].name : 'NWS ' + o,
        onclick: () => { store('taciss-wx-office', o); delete S.mapState.wx; render(); } }, o)))),
    h('div', { class: 'radar-bar' },
      h('span', { class: 'muted' }, WFO[office] ? `NWS ${WFO[office].name}` : `NWS ${office}`, ' · Radar '),
      h('select', { class: 'mini', value: radar, onchange: (e) => { store('taciss-wx-radar-' + office, e.target.value); render(); } },
        radarChoices.map((r) => h('option', { value: r }, r + (EXTRA_RADARS[r] ? ' – ' + EXTRA_RADARS[r] : '')))),
      h('a', { href: `https://radar.weather.gov/station/${radar.toLowerCase()}/standard`, target: '_blank', rel: 'noopener' }, 'Full NWS radar ↗'),
      h('a', { href: `https://www.weather.gov/${office.toLowerCase()}/`, target: '_blank', rel: 'noopener' }, `weather.gov/${office.toLowerCase()} ↗`)),
    h('div', { class: 'radar-wrap' }, loop));

  // alert polygon map
  const mapEl = h('div', { class: 'map' });
  const mapCard = h('div', { class: 'card' }, h('h3', {}, '🗺 Warning Polygons & Radar Mosaic',
    h('span', { class: 'muted right', style: 'font-weight:500;font-size:12px' }, 'Storm-based warnings are drawn; county/zone alerts are listed at left')), mapEl);

  // forecast + products + SPC
  const fcBody = h('div', { class: 'card-body' }, h('span', { class: 'muted' }, 'Loading forecast…'));
  const prodType = store('taciss-wx-product') || 'HWO';
  const prodBody = h('div', { class: 'card-body' }, h('span', { class: 'muted' }, 'Loading…'));
  root.append(
    h('div', { class: 'grid cols-dash' }, alertsCard, radarCard),
    h('div', { style: 'margin-top:16px' }, mapCard),
    h('div', { class: 'grid cols-2', style: 'margin-top:16px' },
      h('div', { class: 'card' }, h('h3', {}, '🌤 Forecast'), fcBody),
      h('div', { class: 'card' }, h('h3', {}, '📄 ', office, ' ',
        h('span', { class: 'right seg' }, ['HWO', 'AFD'].map((t) => h('button', { class: 'small' + (t === prodType ? ' primary' : ''), onclick: () => { store('taciss-wx-product', t); render(); } }, t)))), prodBody)),
    h('div', { class: 'card', style: 'margin-top:16px' }, h('h3', {}, '🌪 SPC Day 1 Convective Outlook',
      h('a', { class: 'right', href: 'https://www.spc.noaa.gov/products/outlook/day1otlk.html', target: '_blank', rel: 'noopener' }, 'spc.noaa.gov ↗')),
    h('div', { class: 'radar-wrap' }, h('img', { class: 'radar-img', alt: 'SPC Day 1 outlook', src: `https://www.spc.noaa.gov/products/outlook/day1otlk.png?t=${Math.floor(Date.now() / 600000)}` }))));

  // async fills (don't block the page)
  api('GET', 'weather/forecast').then((f) => {
    fcBody.replaceChildren(h('div', { class: 'muted', style: 'font-size:13px;margin-bottom:8px' }, `${f.place} · NWS ${f.office}` + (f.updated ? ` · updated ${ago(f.updated)}` : '')),
      h('div', { class: 'fc-grid' }, f.periods.slice(0, 8).map((p) => h('div', { class: 'fc' + (p.isDaytime ? '' : ' night'), title: p.detailedForecast },
        h('div', { class: 'fc-name' }, p.name), h('div', { class: 'fc-temp' }, `${p.temperature}°${p.temperatureUnit}`),
        h('div', { class: 'fc-short' }, p.shortForecast), h('div', { class: 'muted fc-meta' }, p.pop != null ? `💧 ${p.pop}%  ` : '', `${p.windDirection || ''} ${p.windSpeed || ''}`)))));
  }).catch((e) => fcBody.replaceChildren(h('div', { class: 'empty' }, e.message)));
  api('GET', `weather/product?type=${prodType}&office=${office}`).then((p) => {
    prodBody.replaceChildren(h('div', { class: 'muted', style: 'font-size:13px;margin-bottom:6px' }, `${p.name} · issued ${fmtTime(p.issued)}`), h('pre', { class: 'wx-pre product' }, p.text));
  }).catch((e) => prodBody.replaceChildren(h('div', { class: 'empty' }, e.message)));

  // map
  const m = makeMap(mapEl, 'wx');
  if (!S.mapState.wx && WFO[office]) m.setView([WFO[office].lat, WFO[office].lon], 7);
  const mosaic = L.tileLayer.wms('https://mesonet.agron.iastate.edu/cgi-bin/wms/nexrad/n0q.cgi', {
    layers: 'nexrad-n0q-900913', format: 'image/png', transparent: true, opacity: 0.55, attribution: 'NEXRAD: NWS / Iowa State Mesonet' }).addTo(m);
  S.timers.push(setInterval(() => mosaic.setParams({ _t: Date.now() }), 120000));
  const polys = L.layerGroup().addTo(m);
  for (const a of sortAlerts(wx.alerts).reverse()) {
    if (!a.geometry) continue;
    L.geoJSON(a.geometry, { style: { color: alertColor(a), weight: 3, fillOpacity: 0.12 } })
      .bindPopup(h('div', {}, h('b', {}, a.event), h('br'), 'until ', until(a), h('br'), (a.area || '').slice(0, 160)))
      .addTo(polys);
  }
  L.control.layers(null, { 'Radar mosaic': mosaic, 'Warning polygons': polys }, { collapsed: true }).addTo(m);

  // deep link from the ticker: #/weather/<alert id>
  if (params[0] && S.wxOpened !== params[0]) {
    S.wxOpened = params[0];
    const a = wx.alerts.find((x) => x.id === params[0]);
    if (a) setTimeout(() => alertModal(a), 50);
  } else if (!params[0]) S.wxOpened = null;
};

/* ---------- boot hooks ---------- */
(function initWeather() {
  // keep relative times / expiries fresh and recover if a live event was missed
  setInterval(renderTicker, 60000);
  setInterval(() => refreshWx(true), 120000);
})();
