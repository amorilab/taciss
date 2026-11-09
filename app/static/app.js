/* TACISS self-hosted frontend. No build step, no CDN: works fully offline. */
'use strict';

const S = { me: null, publicRead: false, schema: null, settings: {}, cache: {}, timers: [], mapState: {}, mine: new Set() };
const RANK = { viewer: 1, operator: 2, admin: 3 };
const LEVELS = {
  normal: 'STATUS NORMAL', monitoring: 'ENHANCED MONITORING',
  partial: 'PARTIAL ACTIVATION', full: 'FULL ACTIVATION',
};
const COLORS = { ok: '#22c55e', warn: '#eab308', orange: '#f97316', bad: '#ef4444', info: '#3b82f6', off: '#6b7280' };
const TONE = {
  operational: 'ok', available: 'ok', active: 'bad', active_event: 'bad', standby: 'warn', monitoring: 'warn',
  inactive: 'off', closed: 'off', out_of_service: 'off', assigned: 'info',
  critical: 'bad', high: 'orange', normal: 'warn', low: 'off',
  checked_in: 'ok', checked_out: 'off', routine: 'info', priority: 'orange', emergency: 'bad',
  major: 'bad', moderate: 'orange', minor: 'warn', info: 'info',
  restricted: 'orange', open: 'ok',
};

/* ---------- tiny helpers ---------- */
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  let value;
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'class') el.className = v;
    else if (k === 'value') value = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const k of kids.flat(Infinity)) {
    if (k == null || k === false) continue;
    el.append(k instanceof Node ? k : String(k));
  }
  if (value !== undefined) el.value = value;
  return el;
}
// display form of a stored choice value: "severe_weather" -> "Severe Weather"
const NICE = { hazmat: 'HazMat', sitrep: 'SITREP', go_kit: 'Go-Kit', search_rescue: 'Search & Rescue',
  ems: 'EMS', eoc: 'EOC', ics: 'ICS', lsr: 'LSR', nws: 'NWS' };
const nice = (s) => (s == null ? '' : NICE[s] || String(s).replace(/_/g, ' ').replace(/\b[a-z]/g, (c) => c.toUpperCase()));
const badge = (v) => v ? h('span', { class: 'badge c-' + (TONE[v] || 'off') }, nice(v)) : '';
const color = (v) => COLORS[TONE[v] || 'off'];
function fmtTime(iso, withDate = true) {
  if (!iso) return '';
  const d = new Date(iso);
  const t = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
  return withDate ? d.toLocaleDateString([], { month: '2-digit', day: '2-digit' }) + ' ' + t : t;
}
function ago(iso) {
  const s = (Date.now() - new Date(iso)) / 1000;
  if (s < 60) return 'just now';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
}
function toast(msg, isErr) {
  const t = h('div', { class: 'toast' + (isErr ? ' error' : '') }, msg);
  document.getElementById('toasts').append(t);
  setTimeout(() => t.remove(), isErr ? 7000 : 4000);
}
function store(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch { return null; } }

async function api(method, path, body) {
  const opt = { method, headers: { 'X-Requested-With': 'TACISS' }, credentials: 'same-origin' };
  if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  const r = await fetch('/api/' + path, opt);
  let data = null;
  try { data = await r.json(); } catch { /* empty */ }
  if (!r.ok) { const e = new Error((data && data.error) || 'HTTP ' + r.status); e.status = r.status; throw e; }
  return data;
}

const can = (role) => !!S.me && RANK[S.me.role] >= RANK[role];
const canDo = (entity, action) => can(S.schema.entities[entity][action]);
const canRead = (entity) => !!S.me || (S.publicRead && S.schema.entities[entity].public);

function load(entity) {
  if (!canRead(entity)) return Promise.resolve([]);
  if (!S.cache[entity]) {
    S.cache[entity] = api('GET', entity).catch((e) => { delete S.cache[entity]; throw e; });
  }
  return S.cache[entity];
}
function refName(rows, id) {
  const r = (rows || []).find((x) => x.id === id);
  return r ? (r.title || r.name || '#' + id) : (id ? '#' + id : '');
}
const refNames = (rows, ids) => (ids || []).map((i) => refName(rows, i)).join(', ');
const isLocked = (inc) => inc && inc.visibility === 'restricted';
const lockIcon = (inc) => isLocked(inc) ? h('span', { class: 'lock', title: 'Restricted: only assigned programs / served agencies can see this' }, '🔒') : null;
// incidents/programs that serve one of my agencies (user may belong to several)
const servesMe = (r) => !!S.me && (r.agencies || []).some((a) => (S.me.agencies || []).includes(a));

/* Multi-select checklist. items: [{id, label, sub}]. Read the choice with pickValue(el). */
function pickList(name, items, selected, disabled, emptyText) {
  const sel = new Set((selected || []).map(Number));
  const box = h('div', { class: 'pick', 'data-pick': name });
  const list = h('div', { class: 'pick-list' });
  const q = items.length > 8 ? h('input', { type: 'search', placeholder: 'Filter…', class: 'pick-q',
    oninput: () => { const t = q.value.toLowerCase(); list.querySelectorAll('label').forEach((l) => { l.hidden = t && !l.textContent.toLowerCase().includes(t); }); } }) : null;
  if (!items.length) list.append(h('div', { class: 'muted' }, emptyText || 'Nothing to choose yet.'));
  for (const it of items) {
    list.append(h('label', { class: 'inline-check' },
      h('input', { type: 'checkbox', value: String(it.id), checked: sel.has(it.id), disabled }),
      h('span', {}, it.label, it.sub ? h('span', { class: 'muted' }, ' · ', it.sub) : null)));
  }
  if (q) box.append(q);
  box.append(list);
  return box;
}
const pickValue = (el) => [...el.querySelectorAll('input[type=checkbox]:checked')].map((c) => +c.value);

/* ---------- chrome: clocks, status bar, user box, nav ---------- */
function tickClocks() {
  const d = new Date();
  document.getElementById('clock-local').textContent = d.toLocaleTimeString([], { hour12: false });
  document.getElementById('clock-utc').textContent = d.toISOString().slice(11, 19) + 'Z';
}

function renderChrome() {
  const st = S.settings;
  document.getElementById('site-name').textContent = st.site_name || 'TACISS';
  // The signed-in user's program under the name, so it reads as their program's board
  const mine = (S.me && S.me.program_info) || [];
  const pe = document.getElementById('site-program');
  pe.hidden = !mine.length;
  pe.textContent = mine.length ? mine[0].name + (mine.length > 1 ? ` +${mine.length - 1}` : '') : '';
  pe.title = mine.map((p) => p.name).join('\n');
  document.title = (mine.length ? mine[0].name + ' · ' : '') + (st.site_name || 'TACISS');
  const lvl = st.seoc_level || 'normal';
  const bar = document.getElementById('status-bar');
  bar.className = 'status-bar lvl-' + lvl;
  document.getElementById('status-text').textContent = 'SEOC: ' + (LEVELS[lvl] || lvl);
  document.getElementById('status-banner').textContent = st.banner || '';
  const ctl = document.getElementById('status-control');
  ctl.replaceChildren();
  if (can('operator')) {
    ctl.append(h('select', {
      title: 'Change SEOC activation level', value: lvl,
      onchange: async (e) => {
        try { S.settings = await api('PUT', 'settings', { seoc_level: e.target.value }); renderChrome(); }
        catch (err) { toast(err.message, true); }
      },
    }, Object.entries(LEVELS).map(([k, v]) => h('option', { value: k }, v))),
    h('button', { class: 'small ghost', style: 'color:#fff', onclick: editBanner }, 'Edit banner'));
  }

  const ub = document.getElementById('user-box');
  ub.replaceChildren();
  if (S.me) {
    ub.append(
      h('button', { class: 'ghost', onclick: profileModal, title: 'Profile' },
        (S.me.callsign ? S.me.callsign + ' · ' : '') + (S.me.display_name || S.me.username)),
      h('button', { onclick: logout }, 'Sign out'));
  } else {
    ub.append(h('a', { class: 'btn primary', href: '#/login' }, 'Sign in'));
  }

  const nav = document.getElementById('nav');
  const items = [
    ['dashboard', 'Dashboard', true],
    ['incidents', 'Incidents', canRead('incidents')],
    ['reports', '🛰 SITREP War Room', canRead('reports')],
    ['map', 'Map', canRead('incidents')],
    ['weather', '⛈ Weather', !!S.me || S.publicRead],
    ['programs', 'Programs', canRead('programs')],
    ['agencies', 'Agencies', canRead('agencies')],
    ['resources', 'Resources', canRead('resources')],
    ['chat', 'Chat', canRead('messages')],
    ['aprs', '📡 APRS', !!S.me],
    ['mesh', '🕸 Mesh', can('admin')],
    ['admin', 'Admin', can('admin')],
  ];
  const cur = route().view;
  nav.replaceChildren(...items.filter((i) => i[2]).map(([k, label]) =>
    h('a', { href: '#/' + k, class: cur === k ? 'active' : '' }, label)));
  nav.style.display = (S.me || S.publicRead) ? '' : 'none';
  if (typeof renderTicker === 'function') renderTicker();
}

function editBanner() {
  const ta = h('textarea', { value: S.settings.banner || '', placeholder: 'e.g. Tornado Watch #123 until 21:00 CDT' });
  const close = modal('Status banner', h('label', {}, 'Message shown in the status bar', ta), [
    h('button', { onclick: () => close() }, 'Cancel'),
    h('button', { class: 'primary', onclick: async () => {
      try { S.settings = await api('PUT', 'settings', { banner: ta.value }); renderChrome(); close(); }
      catch (e) { toast(e.message, true); }
    } }, 'Save'),
  ]);
}

async function logout() {
  await api('POST', 'logout').catch(() => {});
  location.hash = '#/dashboard';
  location.reload();
}

/* ---------- modal & forms ---------- */
function modal(title, content, actions) {
  const onKey = (e) => { if (e.key === 'Escape') close(); };
  const bg = h('div', { class: 'modal-bg', onmousedown: (e) => { if (e.target === bg) close(); } },
    h('div', { class: 'modal', role: 'dialog', 'aria-modal': 'true' },
      h('header', {}, h('h3', {}, title), h('button', { class: 'ghost', onclick: () => close(), 'aria-label': 'Close' }, '✕')),
      h('div', { class: 'modal-body' }, content),
      actions ? h('footer', {}, actions) : null));
  function close() { bg.remove(); document.removeEventListener('keydown', onKey); }
  document.addEventListener('keydown', onKey);
  document.body.append(bg);
  const first = bg.querySelector('input, select, textarea');
  if (first) setTimeout(() => first.focus(), 30);
  return close;
}

function fieldInput(f, value, refs, disabled) {
  const common = { name: f.name, disabled, required: f.required };
  if (f.type === 'textarea') return h('textarea', { ...common, value: value ?? '' });
  if (f.type === 'refs') {
    return pickList(f.name, (refs[f.name] || []).map((r) => ({ id: r.id, label: r.title || r.name || '#' + r.id, sub: r.kind })),
      value, disabled, `No ${f.ref} defined yet.`);
  }
  if (f.type === 'ref') {
    return h('select', { ...common, value: value == null ? '' : String(value) },
      h('option', { value: '' }, '— none —'),
      (refs[f.name] || []).map((r) => h('option', { value: String(r.id) }, r.title || r.name || '#' + r.id)));
  }
  if (f.choices) {
    return h('select', { ...common, value: value ?? '' },
      f.required ? null : h('option', { value: '' }, '—'),
      f.choices.map((c) => h('option', { value: c }, nice(c))));
  }
  if (f.type === 'real') return h('input', { ...common, type: 'number', step: 'any', value: value ?? '' });
  return h('input', { ...common, type: 'text', value: value ?? '', maxlength: 2000 });
}

async function editRecord(entity, rec, defaults = {}) {
  const spec = S.schema.entities[entity];
  const isNew = !rec;
  const editable = isNew ? canDo(entity, 'create') : canDo(entity, 'edit');
  const refs = {};
  for (const f of spec.fields) if (f.type === 'ref' || f.type === 'refs') refs[f.name] = await load(f.ref).catch(() => []);
  const data = rec || { ...Object.fromEntries(spec.fields.map((f) => [f.name, f.default])), ...defaults };
  const err = h('div', { class: 'err' });
  const form = h('form', { class: 'grid-form', onsubmit: (e) => { e.preventDefault(); save(); } });
  for (const f of spec.fields) {
    if (f.hidden) continue;
    const full = f.type === 'textarea' || f.type === 'refs';
    // <div> (not <label>) around checklists so clicking the caption doesn't toggle the first box
    form.append(h(f.type === 'refs' ? 'div' : 'label', { class: (full ? 'full ' : '') + (f.type === 'refs' ? 'field' : '') },
      f.label + (f.required ? ' *' : ''), fieldInput(f, data[f.name], refs, !editable)));
    if (entity === 'incidents' && f.name === 'visibility') {
      form.append(h('div', { class: 'full hint' },
        '🔒 Restricted incidents (with their log, check-ins, reports and chat) are visible only to admins, the person who opened it, ' +
        'members of the assigned programs, and users of the served agencies. Open incidents are visible to everyone signed in.'));
    }
  }
  if (editable && spec.fields.some((f) => f.name === 'lat')) {
    form.append(h('div', { class: 'full' }, h('button', { type: 'button', class: 'small', onclick: () => {
      if (!navigator.geolocation) return toast('Geolocation unavailable', true);
      navigator.geolocation.getCurrentPosition((p) => {
        form.elements.lat.value = p.coords.latitude.toFixed(5);
        form.elements.lon.value = p.coords.longitude.toFixed(5);
      }, (e) => toast('Location: ' + e.message, true), { enableHighAccuracy: true, timeout: 10000 });
    } }, '📍 Use my location')));
  }
  if (rec && rec.nws_sent) form.append(h('div', { class: 'full ok-note' }, '✅ Reported to NWS: ', rec.nws_sent));
  if (rec && entity === 'reports' && ['storm_report', 'damage'].includes(rec.kind) && !rec.nws_sent && S.me &&
      (rec.created_by === S.me.username || can('operator')) && typeof nwsModal === 'function') {
    form.append(h('div', { class: 'full' }, h('button', { type: 'button', class: 'small', onclick: () => { close(); nwsModal(rec); } }, '📤 Report to NWS')));
  }
  const meta = rec ? h('div', { class: 'meta' },
    `Created ${fmtTime(rec.created_at)} by ${rec.created_by || '?'} · Updated ${fmtTime(rec.updated_at)} by ${rec.updated_by || '?'}`) : null;

  async function save() {
    const body = {};
    for (const f of spec.fields) {
      if (f.hidden) continue;
      body[f.name] = f.type === 'refs' ? pickValue(form.querySelector(`[data-pick="${f.name}"]`)) : form.elements[f.name].value;
    }
    if (entity === 'incidents' && body.visibility === 'restricted' && !body.programs.length && !body.agencies.length &&
        !confirm('This incident is restricted but has no assigned programs or served agencies, so only admins and its creator will see it. Save anyway?')) return;
    try {
      const saved = isNew ? await api('POST', entity, body) : await api('PUT', `${entity}/${rec.id}`, body);
      S.mine.add(entity + ':' + saved.id);
      delete S.cache[entity];
      close();
      toast(`${spec.singular} saved`);
      if (isNew && entity === 'incidents') location.hash = '#/incidents/' + saved.id;
      else render();
    } catch (e) { err.textContent = e.message; }
  }
  async function del() {
    if (!confirm(`Delete this ${spec.singular.toLowerCase()}? This cannot be undone.`)) return;
    try {
      await api('DELETE', `${entity}/${rec.id}`);
      delete S.cache[entity];
      close();
      toast(`${spec.singular} deleted`);
      if (entity === 'incidents') location.hash = '#/incidents'; else render();
    } catch (e) { err.textContent = e.message; }
  }
  const actions = [];
  if (rec && canDo(entity, 'delete')) actions.push(h('button', { class: 'danger left', onclick: del }, 'Delete'));
  actions.push(h('button', { onclick: () => close() }, editable ? 'Cancel' : 'Close'));
  if (editable) actions.push(h('button', { class: 'primary', onclick: save }, isNew ? 'Create' : 'Save'));
  const extra = rec && (entity === 'programs' || entity === 'agencies') && S.me ? membersPanel(entity, rec) : null;
  const close = modal((isNew ? 'New ' : editable ? 'Edit ' : '') + spec.singular, [form, extra, err, meta], actions);
}

/* Program / served-agency members (users), plus for an agency what is serving it. */
function membersPanel(kind, rec) {
  const box = h('div', { class: 'members' }, h('h4', {}, kind === 'programs' ? '👥 Program members' : '👥 Agency users'),
    h('div', { class: 'muted' }, 'Loading…'));
  (async () => {
    const [members, inc, prog] = await Promise.all([
      api('GET', `members/${kind}/${rec.id}`).catch(() => []),
      kind === 'agencies' ? load('incidents').catch(() => []) : [],
      kind === 'agencies' ? load('programs').catch(() => []) : [],
    ]);
    const kids = [box.firstChild];
    const who = (u) => [u.callsign, u.display_name || u.username].filter(Boolean).join(' · ');
    kids.push(members.length ? h('div', { class: 'chips' }, members.map((u) => h('span', { class: 'chip' }, who(u))))
      : h('div', { class: 'muted' }, 'No users assigned yet.'));
    if (can('admin')) {
      kids.push(h('button', { type: 'button', class: 'small', onclick: async () => {
        const users = await api('GET', 'users');
        const pl = pickList('m', users.map((u) => ({ id: u.id, label: u.username, sub: [u.callsign, u.role].filter(Boolean).join(' ') })),
          members.map((m) => m.id));
        const close = modal(`Members: ${rec.name}`, [h('p', { class: 'muted' }, kind === 'programs'
          ? 'Members can see restricted incidents this program is assigned to.'
          : 'Users of this agency see the programs and incidents serving it, including restricted ones.'), pl], [
          h('button', { onclick: () => close() }, 'Cancel'),
          h('button', { class: 'primary', onclick: async () => {
            try { await api('PUT', `members/${kind}/${rec.id}`, { user_ids: pickValue(pl) }); close(); toast('Members saved'); box.replaceWith(membersPanel(kind, rec)); }
            catch (e) { toast(e.message, true); }
          } }, 'Save'),
        ]);
      } }, '✎ Edit members'));
    }
    if (kind === 'agencies') {
      const p = prog.filter((x) => (x.agencies || []).includes(rec.id));
      const i = inc.filter((x) => (x.agencies || []).includes(rec.id) && x.status !== 'closed');
      kids.push(h('h4', {}, '📡 Programs serving this agency'),
        p.length ? h('div', { class: 'chips' }, p.map((x) => h('span', { class: 'chip' }, x.name))) : h('div', { class: 'muted' }, 'None linked.'),
        h('h4', {}, '⚠ Open incidents serving this agency'),
        i.length ? h('ul', { class: 'plain' }, i.map((x) => h('li', {}, lockIcon(x), ' ',
          h('a', { href: '#/incidents/' + x.id, onclick: () => document.querySelectorAll('.modal-bg').forEach((m) => m.remove()) }, x.title), ' ', badge(x.priority))))
          : h('div', { class: 'muted' }, 'None you can see.'));
    }
    box.replaceChildren(...kids);
  })();
  return box;
}

function profileModal() {
  const cur = h('input', { type: 'password', autocomplete: 'current-password' });
  const nw = h('input', { type: 'password', autocomplete: 'new-password', minlength: 10 });
  const err = h('div', { class: 'err' });
  const close = modal('Profile', [
    h('dl', { class: 'kv' }, h('dt', {}, 'Username'), h('dd', {}, S.me.username),
      h('dt', {}, 'Callsign'), h('dd', {}, S.me.callsign || '—'), h('dt', {}, 'Role'), h('dd', {}, S.me.role)),
    h('h4', {}, 'Change password'),
    h('div', { class: 'grid-form' }, h('label', {}, 'Current password', cur), h('label', {}, 'New password (10+ chars)', nw)),
    err,
  ], [
    h('button', { onclick: () => close() }, 'Close'),
    h('button', { class: 'primary', onclick: async () => {
      try { await api('POST', 'me/password', { current: cur.value, new: nw.value }); toast('Password changed'); close(); }
      catch (e) { err.textContent = e.message; }
    } }, 'Change password'),
  ]);
}

/* ---------- maps ---------- */
function makeMap(el, key) {
  const st = S.settings;
  const saved = S.mapState[key];
  const m = L.map(el, { worldCopyJump: true }).setView(
    saved ? saved.c : [+st.map_lat || 35.86, +st.map_lon || -86.35], saved ? saved.z : (+st.map_zoom || 7));
  if (st.tile_url) L.tileLayer(st.tile_url, { maxZoom: 18, attribution: st.tile_attribution || '' }).addTo(m);
  m.on('moveend', () => { S.mapState[key] = { c: m.getCenter(), z: m.getZoom() }; });
  // Maps are built before the view is attached to the page (size 0), so re-measure whenever
  // the container actually gets (or changes) size; otherwise only a few tiles load.
  if (window.ResizeObserver) {
    const ro = new ResizeObserver(() => { if (el.isConnected && el.clientWidth) m.invalidateSize(); else if (!el.isConnected && el.__seen) ro.disconnect(); el.__seen = el.isConnected || el.__seen; });
    ro.observe(el);
  } else setTimeout(() => m.invalidateSize(), 300);
  return m;
}

function markerLayer(rows, colorOf, popup, opts = {}) {
  const g = L.layerGroup();
  for (const r of rows) {
    if (r.lat == null || r.lon == null) continue;
    L.circleMarker([r.lat, r.lon], {
      radius: opts.radius || 8, color: '#0b1120', weight: 2, fillColor: colorOf(r), fillOpacity: 0.95,
    }).bindPopup(popup(r)).addTo(g);
  }
  return g;
}

async function mapLayers(map, withControl) {
  const [inc, rep, prog, ag] = await Promise.all(['incidents', 'reports', 'programs', 'agencies'].map((e) => load(e).catch(() => [])));
  const layers = {
    'Incidents': markerLayer(inc.filter((i) => i.status !== 'closed'), (r) => color(r.priority), (r) =>
      h('div', {}, h('b', {}, r.title), h('br'), badge(r.status), ' ', badge(r.priority), h('br'),
        h('a', { href: '#/incidents/' + r.id }, 'Open incident')), { radius: 11 }),
    'Reports (24h)': markerLayer(rep.filter((r) => Date.now() - new Date(r.created_at) < 864e5), (r) => color(r.severity), (r) =>
      h('div', {}, h('b', {}, r.title), h('br'), nice(r.kind), ' · ', ago(r.created_at), r.body ? h('p', {}, r.body.slice(0, 240)) : null), { radius: 6 }),
    'Programs': markerLayer(prog, (r) => color(r.status), (r) =>
      h('div', {}, h('b', {}, r.name), h('br'), badge(r.status), ' ', r.kind || '',
        r.coordinator ? h('div', {}, 'Coord: ', r.coordinator, ' ', r.coordinator_call || '') : null,
        r.frequencies ? h('div', { class: 'mono' }, r.frequencies) : null)),
    'Agencies': markerLayer(ag, () => COLORS.info, (r) =>
      h('div', {}, h('b', {}, r.name), h('br'), r.kind || '', r.phone ? h('div', {}, r.phone) : null), { radius: 6 }),
  };
  for (const l of Object.values(layers)) l.addTo(map);
  if (withControl) {
    // NEXRAD radar from Iowa Environmental Mesonet. Only works when the server/clients have internet.
    const radar = L.tileLayer.wms('https://mesonet.agron.iastate.edu/cgi-bin/wms/nexrad/n0q.cgi', {
      layers: 'nexrad-n0q-900913', format: 'image/png', transparent: true, opacity: 0.6,
      attribution: 'NEXRAD: NWS / Iowa State Mesonet',
    });
    S.timers.push(setInterval(() => radar.setParams({ _t: Date.now() }), 120000));
    L.control.layers(null, { ...layers, 'NEXRAD Radar (online only)': radar }, { collapsed: false }).addTo(map);
  }
}

function legend() {
  const item = (c, t) => h('span', {}, h('span', { class: 'dot', style: 'background:' + COLORS[c] }), t);
  return h('div', { class: 'legend' }, item('bad', 'Active event / Critical'), item('orange', 'High / Moderate'),
    item('warn', 'Standby / Normal / Minor'), item('ok', 'Operational'), item('info', 'Agency'), item('off', 'Inactive'));
}

/* ---------- views ---------- */
const VIEWS = {};

VIEWS.login = async (root) => {
  const u = h('input', { name: 'username', autocomplete: 'username', required: true });
  const p = h('input', { name: 'password', type: 'password', autocomplete: 'current-password', required: true });
  const err = h('div', { class: 'err' });
  const btn = h('button', { class: 'primary' }, 'Sign in');
  root.append(h('div', { class: 'login-page' },
    h('div', { class: 'login-brand' },
      h('img', { src: '/static/logo-512.png?v=5', alt: 'TACISS: Tactical Amateur Communication Information Sharing System', width: 190, height: 190 }),
      h('div', { class: 'login-name' }, S.settings.site_name || 'TACISS'),
      h('div', { class: 'muted' }, S.settings.org_name || '')),
    h('div', { class: 'card login' },
      h('h2', {}, 'Sign in'),
      h('form', { onsubmit: async (e) => {
        e.preventDefault();
        err.textContent = '';
        btn.disabled = true;
        try {
          const r = await api('POST', 'login', { username: u.value, password: p.value });
          S.me = r.user; S.cache = {}; loadNodes();
          const me = await api('GET', 'me').catch(() => null);
          if (me && me.user) { S.me = me.user; S.update = me.update; } // includes saved weather prefs
          connectStream();
          refreshWx(false);
          // Go to the page they asked for (or the dashboard). Setting the hash to the value it
          // already has fires no hashchange, so render directly in that case.
          const target = route().view === 'login' ? '#/dashboard' : (location.hash || '#/dashboard');
          if (location.hash !== target) location.hash = target;
          else render();
        } catch (ex) { err.textContent = ex.message; btn.disabled = false; }
      } }, h('label', {}, 'Username', u), h('label', {}, 'Password', p), btn, err)),
    h('footer', { class: 'login-footer' }, '© 2026 AmoriLabs - W3FDE')));
};

VIEWS.setup = async (root) => {
  const f = h('form', { onsubmit: async (e) => {
    e.preventDefault();
    if (f.elements.password.value !== f.elements.password2.value) { err.textContent = 'Passwords do not match.'; return; }
    try {
      const r = await api('POST', 'setup', {
        code: f.elements.code.value, username: f.elements.username.value, display_name: f.elements.display_name.value,
        callsign: f.elements.callsign.value, password: f.elements.password.value,
      });
      S.me = r.user; S.needsSetup = false; S.cache = {};
      connectStream();
      refreshWx(false);
      toast('Admin account created. Welcome!');
      location.hash = '#/admin';
      render();
    } catch (ex) { err.textContent = ex.message; }
  } },
  h('label', {}, 'Setup code (shown on the server screen)', h('input', { name: 'code', required: true, autocomplete: 'off', placeholder: 'XXXX-XXXX-XXXX', class: 'mono' })),
  h('label', {}, 'Admin username', h('input', { name: 'username', required: true, autocomplete: 'username' })),
  h('label', {}, 'Your name', h('input', { name: 'display_name' })),
  h('label', {}, 'Callsign (optional)', h('input', { name: 'callsign' })),
  h('label', {}, 'Password (10+ characters)', h('input', { name: 'password', type: 'password', required: true, minlength: 10, autocomplete: 'new-password' })),
  h('label', {}, 'Repeat password', h('input', { name: 'password2', type: 'password', required: true, autocomplete: 'new-password' })),
  h('button', { class: 'primary' }, 'Create admin account'));
  const err = h('div', { class: 'err' });
  root.append(h('div', { class: 'card login' }, h('h2', {}, 'First-time setup'),
    h('p', { class: 'muted' }, 'No accounts exist yet. Create the first administrator. The setup code is printed on the TACISS server’s console (or run ', h('code', {}, 'sudo taciss setup-code'), ').'),
    f, err));
};

VIEWS.dashboard = async (root) => {
  const [inc, rep, prog, res, ag] = await Promise.all(
    ['incidents', 'reports', 'programs', 'resources', 'agencies'].map((e) => load(e).catch(() => [])));
  const active = inc.filter((i) => i.status !== 'closed');
  const day = rep.filter((r) => Date.now() - new Date(r.created_at) < 864e5);
  const stat = (n, l, tone) => h('div', { class: 'card stat' }, h('div', { class: 'n c-' + tone }, n), h('div', { class: 'l' }, l));
  // Banner for the user's own program(s) (assigned in Admin → Users)
  const mine = S.me ? prog.filter((p) => (S.me.programs || []).includes(p.id))
    .sort((a, b) => String(a.name).localeCompare(String(b.name))) : [];
  if (mine.length) root.append(h('div', { class: 'program-hero' }, mine.map((p) =>
    h('div', { class: 'ph-item', title: 'Open program details', onclick: () => editRecord('programs', p) },
      h('div', { class: 'ph-kicker' }, (p.kind || 'Program') + (mine.length > 1 ? ' · One of your programs' : ' · Your program')),
      h('div', { class: 'ph-name' }, p.name, ' ', badge(p.status)),
      h('div', { class: 'ph-meta' }, [p.counties && 'Counties: ' + p.counties, p.region && 'Region: ' + p.region,
        p.coordinator && `Coordinator: ${p.coordinator} ${p.coordinator_call || ''}`.trim(),
        p.frequencies && 'Frequencies: ' + p.frequencies].filter(Boolean).join('  ·  '))))));
  root.append(h('div', { class: 'stats' },
    stat(active.length, 'Open incidents', active.length ? 'bad' : 'ok'),
    stat(day.length, 'Reports (24h)', day.length ? 'warn' : 'ok'),
    stat(prog.filter((p) => p.status === 'operational' || p.status === 'active_event').length + '/' + prog.length, 'Programs operational', 'ok'),
    canRead('resources') ? stat(res.filter((r) => r.status === 'available').length + '/' + res.length, 'Resources available', 'info') : null,
    stat(ag.filter((a) => a.status === 'active').length, 'Served agencies', 'info')));

  const mapEl = h('div', { class: 'map' });
  const incList = active.length ? h('ul', { class: 'items' }, active.map((i) =>
    h('li', { onclick: () => { location.hash = '#/incidents/' + i.id; } },
      h('div', { class: 't' }, lockIcon(i), isLocked(i) ? ' ' : '', i.title, ' ', badge(i.priority)),
      h('div', { class: 's' }, nice(i.kind), ' · ', i.location || '—', ' · ', ago(i.updated_at)))))
    : h('div', { class: 'empty' }, 'No open incidents');

  // Served-agency users: everything that is serving their agency, front and center
  const myAgencies = S.me ? ag.filter((a) => (S.me.agencies || []).includes(a.id)) : [];
  const servingCard = myAgencies.length ? (() => {
    const progs = prog.filter(servesMe);
    const incs = inc.filter(servesMe).sort((a, b) => (a.status === 'closed') - (b.status === 'closed'));
    return h('div', { class: 'card serving' },
      h('h3', {}, '🏛 Serving ', myAgencies.map((a) => a.name).join(', ')),
      h('div', { class: 'card-body' },
        h('h4', {}, `⚠ Incidents (${incs.filter((i) => i.status !== 'closed').length} open)`),
        incs.length ? h('ul', { class: 'items' }, incs.slice(0, 12).map((i) =>
          h('li', { onclick: () => { location.hash = '#/incidents/' + i.id; } },
            h('div', { class: 't' }, lockIcon(i), isLocked(i) ? ' ' : '', i.title, ' ', badge(i.status), ' ', badge(i.priority)),
            h('div', { class: 's' }, [nice(i.kind), i.location, refNames(prog, i.programs)].filter(Boolean).join(' · ')))))
          : h('div', { class: 'empty' }, 'No incidents are serving your agency right now.'),
        h('h4', {}, `📡 Programs (${progs.length})`),
        progs.length ? h('ul', { class: 'items' }, progs.map((p) =>
          h('li', { onclick: () => editRecord('programs', p) },
            h('div', { class: 't' }, p.name, ' ', badge(p.status)),
            h('div', { class: 's' }, [p.kind, p.coordinator && `${p.coordinator} ${p.coordinator_call || ''}`, p.frequencies].filter(Boolean).join(' · ')))))
          : h('div', { class: 'empty' }, 'No programs are linked to your agency yet.')));
  })() : null;
  const repList = rep.length ? h('ul', { class: 'items' }, rep.slice(0, 8).map((r) =>
    h('li', { onclick: () => editRecord('reports', r) },
      h('div', { class: 't' }, r.title, ' ', badge(r.severity)),
      h('div', { class: 's' }, nice(r.kind), ' · ', r.location || '—', ' · ', ago(r.created_at), ' · ', r.created_by))))
    : h('div', { class: 'empty' }, 'No recent reports');
  const progList = prog.length ? h('ul', { class: 'items' }, prog.map((p) =>
    h('li', { onclick: () => editRecord('programs', p) },
      h('div', { class: 't' }, p.name, ' ', badge(p.status)),
      h('div', { class: 's' }, [p.kind, p.counties, p.coordinator && `${p.coordinator} ${p.coordinator_call || ''}`, p.frequencies].filter(Boolean).join(' · ')))))
    : h('div', { class: 'empty' }, 'No programs yet');

  root.append(h('div', { class: 'grid cols-dash' },
    h('div', { class: 'grid' },
      servingCard,
      typeof wxDashboardCard === 'function' ? wxDashboardCard() : null,
      h('div', { class: 'card' }, h('h3', {}, '⚠ Open Incidents',
        canDo('incidents', 'create') ? h('button', { class: 'small right', onclick: () => editRecord('incidents') }, '+ New') : null), incList),
      h('div', { class: 'card' }, h('h3', {}, '📝 Recent Reports',
        S.me && canDo('reports', 'create') ? h('button', { class: 'small right', onclick: () => editRecord('reports') }, '+ Report') : null), repList)),
    h('div', { class: 'grid' },
      h('div', { class: 'card' }, h('h3', {}, '📍 Program Locations'), mapEl, legend()),
      h('div', { class: 'card' }, h('h3', {}, '📡 Programs'), progList))));
  const map = makeMap(mapEl, 'dash');
  await mapLayers(map, false);
};

function listView(entity, opts = {}) {
  return async (root) => {
    const spec = S.schema.entities[entity];
    const rows = await load(entity);
    const cols = spec.fields.filter((f) => f.list).slice(0, 7);
    const refs = {};
    for (const f of cols) if (f.type === 'ref' || f.type === 'refs') refs[f.name] = await load(f.ref).catch(() => []);
    const statusField = spec.fields.find((f) => f.name === 'status');
    const q = h('input', { type: 'search', placeholder: 'Search…', 'data-keep': entity + '-q', oninput: draw });
    const sf = statusField ? h('select', { 'data-keep': entity + '-s', onchange: draw },
      h('option', { value: '' }, 'All statuses'), statusField.choices.map((c) => h('option', { value: c }, nice(c)))) : null;
    const body = h('div', { class: 'card' });
    root.append(h('div', { class: 'toolbar' }, h('h2', {}, spec.label), q, sf,
      canDo(entity, 'create') ? h('button', { class: 'primary', onclick: () => editRecord(entity) }, '+ New ' + spec.singular) : null), body);

    function cell(f, r) {
      const v = r[f.name];
      if (f.type === 'ref') return refName(refs[f.name], v);
      if (f.type === 'refs') return refNames(refs[f.name], v);
      if (f.name === 'visibility') return v === 'restricted' ? badge('restricted') : h('span', { class: 'muted' }, 'open');
      if (f.name === 'title' && entity === 'incidents') return [lockIcon(r), isLocked(r) ? ' ' : '', v];
      if (f.name === 'status' || f.name === 'priority' || f.name === 'severity') return badge(v);
      if (f.choices) return nice(v);
      return v ?? '';
    }
    function draw() {
      const term = q.value.trim().toLowerCase();
      const st = sf ? sf.value : '';
      const shown = rows.filter((r) => (!st || r.status === st) &&
        (!term || Object.values(r).some((v) => v != null && String(v).toLowerCase().includes(term))));
      body.replaceChildren(shown.length ? h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
        h('thead', {}, h('tr', {}, cols.map((c) => h('th', {}, c.label)), h('th', {}, 'Updated'))),
        h('tbody', {}, shown.map((r) => h('tr', { class: 'click', onclick: () => (opts.open ? opts.open(r) : editRecord(entity, r)) },
          cols.map((c) => h('td', {}, cell(c, r))), h('td', { class: 'muted' }, ago(r.updated_at), ' ', originTag(r)))))))
        : h('div', { class: 'empty' }, rows.length ? 'Nothing matches.' : `No ${spec.label.toLowerCase()} yet.`));
    }
    draw();
  };
}

VIEWS.programs = listView('programs');
VIEWS.agencies = listView('agencies');
VIEWS.resources = listView('resources');

VIEWS.incidents = async (root, params) => {
  if (params[0]) return incidentDetail(root, +params[0]);
  return listView('incidents', { open: (r) => { location.hash = '#/incidents/' + r.id; } })(root);
};

async function incidentDetail(root, id) {
  let inc;
  try { inc = await api('GET', 'incidents/' + id); } catch (e) {
    root.append(h('div', { class: 'empty' }, e.message, ' ', h('a', { href: '#/incidents' }, 'Back')));
    return;
  }
  const [log, reps, res, checkins, allCheckins] = await Promise.all([
    canRead('activity') ? api('GET', 'activity?incident_id=' + id).catch(() => []) : [],
    load('reports').catch(() => []), load('resources').catch(() => []),
    canRead('checkins') ? api('GET', 'checkins?incident_id=' + id).catch(() => []) : null,
    load('checkins').catch(() => []),
  ]);
  const myReps = reps.filter((r) => r.incident_id === id);
  const myRes = res.filter((r) => r.incident_id === id);
  const [progRows, agRows] = await Promise.all([load('programs').catch(() => []), load('agencies').catch(() => [])]);
  const kv = h('dl', { class: 'kv' });
  for (const f of S.schema.entities.incidents.fields) {
    const v = inc[f.name];
    if (['title', 'description'].includes(f.name) || v == null || v === '' || (Array.isArray(v) && !v.length)) continue;
    let shown;
    if (f.type === 'refs') shown = refNames(f.ref === 'programs' ? progRows : agRows, v);
    else if (f.name === 'visibility') shown = v === 'restricted' ? [badge('restricted'), ' assigned programs & served agencies only'] : 'Open to all users';
    else shown = ['status', 'priority'].includes(f.name) ? badge(v) : f.choices ? nice(v) : v;
    kv.append(h('dt', {}, f.label), h('dd', {}, shown));
  }
  kv.append(h('dt', {}, 'Opened'), h('dd', {}, fmtTime(inc.created_at), ' by ', inc.created_by || '?', ' ', originTag(inc)));

  const entry = h('textarea', { placeholder: 'Log entry (ICS-214 style): what happened, who, actions taken…', 'data-keep': 'log-' + id });
  const logCard = h('div', { class: 'card' }, h('h3', {}, '🕑 Activity Log',
    h('button', { class: 'small right no-print', onclick: () => window.print() }, 'Print')),
  canDo('activity', 'create') ? h('form', { class: 'card-body no-print', onsubmit: async (e) => {
    e.preventDefault();
    if (!entry.value.trim()) return;
    try { await api('POST', 'activity', { incident_id: id, body: entry.value }); entry.value = ''; render(); }
    catch (ex) { toast(ex.message, true); }
  } }, entry, h('div', { style: 'margin-top:8px' }, h('button', { class: 'primary' }, 'Add entry'))) : null,
  log.length ? h('ul', { class: 'timeline' }, log.map((l) => h('li', {},
    h('div', { class: 'when' }, fmtTime(l.created_at), h('br'), l.created_by), h('div', { class: 'body' }, l.body))))
    : h('div', { class: 'empty' }, canRead('activity') ? 'No log entries yet.' : 'Sign in to view the activity log.'));

  const mapEl = h('div', { class: 'map' });
  root.append(
    h('div', { class: 'toolbar' }, h('a', { href: '#/incidents', class: 'btn no-print' }, '← Incidents'),
      h('h2', {}, lockIcon(inc), isLocked(inc) ? ' ' : '', inc.title, ' ', badge(inc.status)),
      canRead('messages') ? h('a', { class: 'btn no-print', href: '#/chat/incident-' + id }, '💬 Incident chat') : null,
      S.me && inc.status === 'closed' ? h('a', { class: 'btn primary no-print', href: `/api/incidents/${id}/debrief.pdf`,
        download: '', title: 'Download a PDF packet of everything recorded for this incident' }, '📄 Debrief kit (PDF)') : null,
      S.me && canDo('reports', 'create') ? h('button', { onclick: () => editRecord('reports', null, { incident_id: id, lat: inc.lat, lon: inc.lon }) }, '+ Report') : null,
      h('button', { class: canDo('incidents', 'edit') ? 'primary' : '', onclick: () => editRecord('incidents', inc) }, canDo('incidents', 'edit') ? 'Edit' : 'Details')),
    h('div', { class: 'grid cols-2' },
      h('div', { class: 'grid' },
        h('div', { class: 'card' }, h('h3', {}, 'Overview'), h('div', { class: 'card-body' }, kv,
          inc.description ? h('p', { style: 'white-space:pre-wrap' }, inc.description) : null)),
        logCard),
      h('div', { class: 'grid' },
        inc.lat != null ? h('div', { class: 'card no-print' }, h('h3', {}, 'Location'), mapEl) : null,
        h('div', { class: 'card' }, h('h3', {}, 'Reports (' + myReps.length + ')'),
          myReps.length ? h('ul', { class: 'items' }, myReps.map((r) => h('li', { onclick: () => editRecord('reports', r) },
            h('div', { class: 't' }, r.title, ' ', badge(r.severity)), h('div', { class: 's' }, nice(r.kind), ' · ', fmtTime(r.created_at), ' · ', r.created_by))))
            : h('div', { class: 'empty' }, 'No reports linked.')),
        canRead('resources') ? h('div', { class: 'card' }, h('h3', {}, 'Assigned Resources (' + myRes.length + ')'),
          myRes.length ? h('ul', { class: 'items' }, myRes.map((r) => h('li', { onclick: () => editRecord('resources', r) },
            h('div', { class: 't' }, r.name, ' ', badge(r.status)), h('div', { class: 's' }, nice(r.kind), ' · ', r.contact || '', ' · ', r.location || ''))))
            : h('div', { class: 'empty' }, 'No resources assigned.')) : null)),
    checkins ? netCard(id, checkins, allCheckins) : null);
  if (inc.lat != null) {
    const m = makeMap(mapEl, 'inc' + id);
    if (!S.mapState['inc' + id]) m.setView([inc.lat, inc.lon], 10);
    await mapLayers(m, false);
  }
}

/* Net control check-in roster for one incident. Check-ins/outs are mirrored into the activity log. */
function duration(fromIso, toIso) {
  const m = Math.max(0, Math.round(((toIso ? new Date(toIso) : Date.now()) - new Date(fromIso)) / 60000));
  return m < 60 ? m + 'm' : Math.floor(m / 60) + 'h ' + (m % 60) + 'm';
}

function netCard(incId, rows, allRows) {
  const editable = canDo('checkins', 'create');
  const onNet = rows.filter((r) => r.status !== 'checked_out').sort((a, b) => a.id - b.id);
  const off = rows.filter((r) => r.status === 'checked_out');
  const withTraffic = onNet.filter((r) => r.traffic && r.traffic !== 'none');
  // remember names/assignments from earlier nets so NCS can auto-fill by callsign
  const known = {};
  for (const r of allRows.slice().sort((a, b) => a.id - b.id)) known[r.callsign] = r;

  const logEntry = (text) => api('POST', 'activity', { incident_id: incId, body: text }).catch(() => {});
  const refresh = async (focusCall) => {
    delete S.cache.checkins;
    const typing = document.activeElement && document.activeElement.closest && document.activeElement.closest('.net-form');
    await render();
    // after a check-in, go back to the callsign box unless NCS already moved on to another field
    if (focusCall && !typing) { const el = document.querySelector('[data-keep="ci-call-' + incId + '"]'); if (el) el.focus(); }
  };
  const upd = async (r, body, logText) => {
    try { await api('PUT', 'checkins/' + r.id, body); if (logText) await logEntry(logText); refresh(); }
    catch (e) { toast(e.message, true); }
  };
  const who = (r) => r.callsign + (r.name ? ` (${r.name})` : '');

  let form = null;
  if (editable) {
    const call = h('input', { placeholder: 'Callsign *', class: 'mono', autocomplete: 'off', list: 'ci-calls-' + incId,
      'data-keep': 'ci-call-' + incId, 'aria-label': 'Callsign',
      oninput: () => {
        const pos = call.selectionStart;
        call.value = call.value.toUpperCase();
        try { call.setSelectionRange(pos, pos); } catch { /* not focused */ }
        const k = known[call.value.trim()];
        if (k) { if (!name.value) name.value = k.name || ''; if (!asg.value) asg.value = k.assignment || ''; }
      } });
    const name = h('input', { placeholder: 'Name', 'data-keep': 'ci-name-' + incId, 'aria-label': 'Name' });
    const asg = h('input', { placeholder: 'Location / assignment', 'data-keep': 'ci-asg-' + incId, 'aria-label': 'Location or assignment' });
    const traf = h('select', { 'data-keep': 'ci-traf-' + incId, 'aria-label': 'Traffic', title: 'Traffic to pass' },
      ['none', 'routine', 'priority', 'emergency'].map((t) => h('option', { value: t }, t === 'none' ? 'No traffic' : nice(t) + ' traffic')));
    form = h('form', { class: 'net-form no-print', onsubmit: async (e) => {
      e.preventDefault();
      const cs = call.value.trim().toUpperCase();
      if (!cs) return call.focus();
      if (onNet.some((r) => r.callsign === cs)) { toast(cs + ' is already on the net', true); return; }
      // snapshot + clear immediately so NCS can type the next station while this one saves
      const v = { callsign: cs, name: name.value.trim(), assignment: asg.value.trim(), traffic: traf.value };
      call.value = name.value = asg.value = ''; traf.value = 'none';
      call.focus();
      try {
        await api('POST', 'checkins', { incident_id: incId, ...v });
        await logEntry(`${cs}${v.name ? ' (' + v.name + ')' : ''} checked in` +
          (v.assignment ? ' — ' + v.assignment : '') + (v.traffic !== 'none' ? ` — has ${v.traffic} traffic` : ''));
        refresh(true);
      } catch (ex) {
        toast(ex.message, true);
        if (!call.value) { call.value = v.callsign; name.value = v.name; asg.value = v.assignment; traf.value = v.traffic; }
      }
    } },
    call, name, asg, traf, h('button', { class: 'primary' }, '＋ Check in'),
    h('datalist', { id: 'ci-calls-' + incId }, Object.values(known).map((k) => h('option', { value: k.callsign }, k.name || ''))));
  }

  const statusCell = (r) => editable && r.status !== 'checked_out'
    ? h('select', { class: 'mini', value: r.status, onchange: (e) => upd(r, { status: e.target.value }, `${who(r)} now ${nice(e.target.value)}`) },
      ['checked_in', 'standby', 'assigned'].map((s) => h('option', { value: s }, nice(s))))
    : badge(r.status);
  const trafficCell = (r) => editable && r.status !== 'checked_out'
    ? h('select', { class: 'mini traffic-' + (r.traffic || 'none'), value: r.traffic || 'none', onchange: (e) => {
      const t = e.target.value;
      upd(r, { traffic: t }, t === 'none' ? `${r.callsign} traffic passed` : `${who(r)} has ${t} traffic`);
    } }, ['none', 'routine', 'priority', 'emergency'].map((t) => h('option', { value: t }, t === 'none' ? '—' : nice(t))))
    : (r.traffic && r.traffic !== 'none' ? badge(r.traffic) : '—');
  const actions = (r) => !editable ? null : h('td', { class: 'no-print', style: 'white-space:nowrap' },
    r.status !== 'checked_out'
      ? h('button', { class: 'small', title: 'Check out', onclick: () => upd(r, { status: 'checked_out', checked_out_at: new Date().toISOString() }, `${who(r)} checked out`) }, 'Check out')
      : h('button', { class: 'small', title: 'Check this station back in', onclick: async () => {
        if (onNet.some((x) => x.callsign === r.callsign)) return toast(r.callsign + ' is already on the net', true);
        try {
          await api('POST', 'checkins', { incident_id: incId, callsign: r.callsign, name: r.name, assignment: r.assignment });
          await logEntry(`${who(r)} checked back in`); refresh();
        } catch (e) { toast(e.message, true); }
      } }, 'Re-check in'),
    ' ', h('button', { class: 'small ghost', title: 'Edit details', onclick: () => editRecord('checkins', r) }, '✎'));

  const row = (r, i) => h('tr', { class: [r.status === 'checked_out' ? 'out' : '', r.status !== 'checked_out' && r.traffic && r.traffic !== 'none' ? 'has-' + r.traffic : ''].join(' ') },
    h('td', { class: 'muted mono' }, i == null ? '' : i + 1),
    h('td', { class: 'mono', style: 'font-weight:700' }, r.callsign),
    h('td', {}, r.name || ''),
    h('td', {}, r.assignment || '', r.notes ? h('div', { class: 'muted', style: 'font-size:12px' }, r.notes) : null),
    h('td', {}, statusCell(r)),
    h('td', {}, trafficCell(r)),
    h('td', { class: 'mono muted' }, fmtTime(r.created_at, false)),
    h('td', { class: 'mono muted' }, r.status === 'checked_out'
      ? fmtTime(r.checked_out_at || r.updated_at, false) + ' (' + duration(r.created_at, r.checked_out_at || r.updated_at) + ')'
      : duration(r.created_at)),
    actions(r));

  return h('div', { class: 'card', style: 'margin-top:16px' },
    h('h3', {}, '📻 Net Check-ins',
      h('span', { class: 'muted', style: 'font-weight:500;font-size:13px' },
        ` ${onNet.length} on net` + (withTraffic.length ? ` · ${withTraffic.length} with traffic` : '') + (off.length ? ` · ${off.length} checked out` : ''))),
    form,
    rows.length ? h('div', { class: 'table-wrap' }, h('table', { class: 'data net-table' },
      h('thead', {}, h('tr', {}, ['#', 'Callsign', 'Name', 'Location / Assignment', 'Status', 'Traffic', 'In', 'Out / On net'].map((t) => h('th', {}, t)),
        editable ? h('th', { class: 'no-print' }, '') : null)),
      h('tbody', {}, onNet.map((r, i) => row(r, i)), off.map((r) => row(r, null)))))
      : h('div', { class: 'empty' }, editable ? 'No stations checked in yet. Enter a callsign above and press Enter.' : 'No stations checked in yet.'));
}

// VIEWS.reports (SITREP war room) lives in warroom.js

VIEWS.map = async (root) => {
  const el = h('div', { class: 'map tall' });
  root.append(el, h('div', { class: 'muted', style: 'font-size:12px;margin-top:6px' },
    'Base map and radar need internet. Offline, markers still show; point “Map tile URL” in Admin at a local tile server for offline maps.'));
  const m = makeMap(el, 'full');
  await mapLayers(m, true);
};

VIEWS.chat = async (root, params) => {
  const channel = params[0] || 'general';
  const [msgs, inc] = await Promise.all([load('messages'), load('incidents').catch(() => [])]);
  const chans = [['general', '# general'], ['ops', '# ops'], ['net-control', '# net-control'],
    ...inc.filter((i) => i.status !== 'closed').map((i) => ['incident-' + i.id, '⚠ ' + i.title])];
  if (!chans.some((c) => c[0] === channel)) chans.push([channel, '# ' + channel]);
  const list = h('div', { class: 'msgs' });
  const mine = msgs.filter((m) => (m.channel || 'general') === channel).slice().reverse();
  list.append(...(mine.length ? mine.map((m) => h('div', { class: 'msg' },
    h('div', { class: 'who' }, m.created_by, h('span', { class: 'muted' }, fmtTime(m.created_at))),
    h('div', { class: 'txt' }, m.body))) : [h('div', { class: 'empty' }, 'No messages yet.')]));
  const ta = h('textarea', { placeholder: 'Message #' + channel + ' (Enter to send, Shift+Enter for newline)', 'data-keep': 'chat-' + channel,
    onkeydown: (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } } });
  async function send() {
    if (!ta.value.trim()) return;
    try { await api('POST', 'messages', { channel, body: ta.value }); ta.value = ''; delete S.cache.messages; render(); }
    catch (e) { toast(e.message, true); }
  }
  root.append(h('div', { class: 'chat' },
    h('div', { class: 'card channels card-body' }, chans.map(([k, l]) =>
      h('a', { href: '#/chat/' + encodeURIComponent(k), class: k === channel ? 'active' : '' }, l))),
    h('div', { class: 'card chat-main' }, h('h3', {}, '💬 ', channel), list,
      canDo('messages', 'create') ? h('div', { class: 'chat-form' }, ta, h('button', { class: 'primary', onclick: send }, 'Send')) : null)));
  list.scrollTop = list.scrollHeight;
};

VIEWS.admin = async (root) => {
  if (!can('admin')) { root.append(h('div', { class: 'empty' }, 'Admins only.')); return; }
  const [users, audit, progRows, agRows] = await Promise.all([api('GET', 'users'), api('GET', 'audit?limit=200'),
    load('programs').catch(() => []), load('agencies').catch(() => [])]);
  const opts = (rows) => rows.map((r) => ({ id: r.id, label: r.name, sub: r.kind }));
  const st = S.settings;
  const keys = [['site_name', 'Site name'], ['org_name', 'Organization / subtitle'], ['map_lat', 'Map center latitude'],
    ['map_lon', 'Map center longitude'], ['map_zoom', 'Map zoom'], ['tile_url', 'Map tile URL'], ['tile_attribution', 'Tile attribution'],
    ['wx_states', 'NWS alert states (e.g. TN or TN,KY)'], ['wx_offices', 'NWS offices for radar/products (e.g. MRX,OHX,MEG)'],
    ['wx_contact', 'Contact email sent to NWS API (recommended)'], ['wx_enabled', 'NWS integration on (1) / off (0)'],
    ['feeds_enabled', 'War-room live feeds on (1) / off (0)'], ['lsr_hours', 'Storm reports: hours to show (1-168)'],
    ['feeds_bbox', 'Spotter Network area: south,west,north,east'], ['nws_report_email', 'NWS spotter-report e-mail (your office)'],
    ['windy_api_key', 'Windy.com webcams API key (free, api.windy.com)'], ['tdot_api_key', 'TDOT SmartWay camera API key (from TDOT)'],
    ['nws_report_links', 'Report-to-NWS links (one per line: Name|https://…)'], ['outage_links', 'Outage map links (one per line: Name|https://…)'],
    ['aprs_enabled', 'APRS live feed on (1) / off (0)'], ['aprs_range_km', 'APRS area radius around map center (km)'],
    ['aprs_server', 'APRS-IS server (host:port)'], ['aprs_tx_enabled', 'Let users send to APRS on (1) / off (0)'],
    ['aprsfi_api_key', 'aprs.fi API key (free, aprs.fi → Account)'], ['aprs_filter_extra', 'Extra APRS-IS filter (advanced)']];
  const MULTI = new Set(['nws_report_links', 'outage_links']);
  const SECRET = new Set(['windy_api_key', 'tdot_api_key', 'aprsfi_api_key']);
  const sform = h('form', { class: 'grid-form card-body', onsubmit: async (e) => {
    e.preventDefault();
    const body = Object.fromEntries(keys.map(([k]) => [k, sform.elements[k].value]));
    try { S.settings = await api('PUT', 'settings', body); renderChrome(); toast('Settings saved'); }
    catch (ex) { toast(ex.message, true); }
  } }, keys.map(([k, l]) => h('label', { class: k.startsWith('tile') || MULTI.has(k) ? 'full' : '' }, l,
    MULTI.has(k) ? h('textarea', { name: k, value: st[k] || '', rows: 4, class: 'mono small' })
      : h('input', { name: k, value: st[k] || '', type: SECRET.has(k) ? 'password' : 'text', autocomplete: 'off', placeholder: SECRET.has(k) ? 'not set' : '' }))),
  h('div', { class: 'full' }, h('button', { class: 'primary' }, 'Save settings')));

  const userForm = (u) => {
    const f = h('form', { class: 'grid-form' },
      h('label', {}, 'Username', h('input', { name: 'username', value: u ? u.username : '', disabled: !!u, required: true })),
      h('label', {}, 'Display name', h('input', { name: 'display_name', value: u ? u.display_name || '' : '' })),
      h('label', {}, 'Callsign', h('input', { name: 'callsign', value: u ? u.callsign || '' : '' })),
      h('label', {}, 'Role', h('select', { name: 'role', value: u ? u.role : 'viewer' }, S.schema.roles.map((r) => h('option', { value: r }, r)))),
      h('label', {}, u ? 'New password (leave blank to keep)' : 'Password (10+ chars)', h('input', { name: 'password', type: 'password', autocomplete: 'new-password' })),
      u ? h('label', {}, 'Active', h('select', { name: 'active', value: u.active ? '1' : '0' }, h('option', { value: '1' }, 'Active'), h('option', { value: '0' }, 'Disabled'))) : null,
      h('div', { class: 'field' }, 'Programs', pickList('programs', opts(progRows), u ? u.programs : [], false, 'No programs yet.')),
      h('div', { class: 'field' }, 'Served agency', pickList('agencies', opts(agRows), u ? u.agencies : [], false, 'No agencies yet.')),
      h('div', { class: 'full hint' }, 'Program members and agency users can see restricted incidents assigned to their program or serving their agency. ',
        'Agency users also get a “Serving” panel on their dashboard. Use role “viewer” for agency staff who should only watch.'));
    const err = h('div', { class: 'err' });
    const close = modal(u ? 'Edit user' : 'New user', [f, err], [
      u ? h('button', { class: 'danger left', onclick: async () => {
        if (!confirm('Delete user ' + u.username + '?')) return;
        try { await api('DELETE', 'users/' + u.id); close(); render(); } catch (e) { err.textContent = e.message; }
      } }, 'Delete') : null,
      h('button', { onclick: () => close() }, 'Cancel'),
      h('button', { class: 'primary', onclick: async () => {
        const b = { display_name: f.elements.display_name.value, callsign: f.elements.callsign.value, role: f.elements.role.value,
          programs: pickValue(f.querySelector('[data-pick=programs]')), agencies: pickValue(f.querySelector('[data-pick=agencies]')) };
        if (f.elements.password.value) b.password = f.elements.password.value;
        try {
          if (u) { b.active = f.elements.active.value === '1'; await api('PUT', 'users/' + u.id, b); }
          else { b.username = f.elements.username.value; await api('POST', 'users', b); }
          close(); toast('User saved'); render();
        } catch (e) { err.textContent = e.message; }
      } }, 'Save'),
    ]);
  };

  root.append(h('div', { class: 'toolbar' }, h('h2', {}, 'Administration'),
    h('a', { class: 'btn', href: '/api/export', download: '' }, '⬇ Export all data (JSON)')),
  h('div', { class: 'grid cols-2' },
    h('div', { class: 'grid' },
      h('div', { class: 'card' }, h('h3', {}, 'Site settings'), sform),
      h('div', { class: 'card' }, h('h3', {}, 'Users', h('button', { class: 'small right', onclick: () => userForm() }, '+ Add user')),
        h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
          h('thead', {}, h('tr', {}, ['User', 'Callsign', 'Role', 'Programs / Agency', 'Status', 'Last login'].map((t) => h('th', {}, t)))),
          h('tbody', {}, users.map((u) => h('tr', { class: 'click', onclick: () => userForm(u) },
            h('td', {}, u.username, h('div', { class: 'muted' }, u.display_name || '')), h('td', { class: 'mono' }, u.callsign || ''),
            h('td', {}, u.role),
            h('td', {}, refNames(progRows, u.programs), u.agencies && u.agencies.length ? h('div', { class: 'muted' }, '🏛 ', refNames(agRows, u.agencies)) : null),
            h('td', {}, u.active ? badge('active') : badge('inactive')), h('td', { class: 'muted' }, u.last_login ? ago(u.last_login) : 'never')))))))),
    h('div', { class: 'card' }, h('h3', {}, 'Audit log (latest 200)'),
      h('div', { class: 'table-wrap', style: 'max-height:70vh;overflow:auto' }, h('table', { class: 'data' },
        h('thead', {}, h('tr', {}, ['Time', 'User', 'Action', 'Target', 'Detail'].map((t) => h('th', {}, t)))),
        h('tbody', {}, audit.map((a) => h('tr', {}, h('td', { class: 'mono' }, fmtTime(a.ts)), h('td', {}, a.username || '—'),
          h('td', {}, a.action), h('td', {}, a.entity ? `${a.entity}#${a.entity_id ?? ''}` : ''), h('td', { class: 'muted' }, a.detail || '')))))))));
};

/* ---------- router / render ---------- */
function route() {
  const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean).map(decodeURIComponent);
  return { view: parts[0] || 'dashboard', params: parts.slice(1) };
}

let rendering = null;
async function render() {
  if (rendering) { rendering.again = true; return; }
  const job = rendering = { again: false };
  try {
    const { view, params } = route();
    const root = document.getElementById('view');
    S.timers.forEach(clearInterval); S.timers = [];

    let v = VIEWS[view] ? view : 'dashboard';
    if (!S.me && !S.publicRead) v = 'login';
    if (S.needsSetup && !S.me) v = 'setup';
    if (v === 'login' && S.me) v = 'dashboard';
    renderChrome();
    const next = h('div');
    if (S.update && S.me && can('admin') && !S.updateHidden) {
      next.append(h('div', { class: 'card update-note' },
        `⬆ TACISS ${S.update.version} is available (this server runs ${S.version || 'an older version'}). `,
        'Install it on the server with ', h('code', {}, 'sudo taciss update'), '. ',
        h('a', { href: S.update.url, target: '_blank', rel: 'noopener' }, 'What’s new'), ' ',
        h('button', { class: 'small ghost', onclick: () => { S.updateHidden = true; render(); } }, 'Hide')));
    }
    try { await VIEWS[v](next, params); } catch (e) {
      next.replaceChildren(h('div', { class: 'card empty' }, 'Could not load: ', e.message,
        e.status === 401 ? h('div', {}, h('a', { href: '#/login' }, 'Sign in')) : null));
    }
    // keep typed text, focus & scroll across live re-renders. Captured right before the swap
    // (not when the render started) so nothing typed while data was loading is lost.
    const kept = {};
    root.querySelectorAll('[data-keep]').forEach((el) => { kept[el.dataset.keep] = el.value; });
    const focusKey = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.keep : null;
    const scroll = window.scrollY;
    root.replaceChildren(...next.childNodes);
    // Leaflet needs the element attached before it can size itself
    window.dispatchEvent(new Event('resize'));
    root.querySelectorAll('[data-keep]').forEach((el) => {
      if (kept[el.dataset.keep] !== undefined) { el.value = kept[el.dataset.keep]; el.dispatchEvent(new Event('input')); el.dispatchEvent(new Event('change')); }
      if (focusKey && el.dataset.keep === focusKey) el.focus();
    });
    if (scroll) window.scrollTo(0, scroll);
  } finally {
    rendering = null;
    if (job.again) render();
  }
}

/* ---------- live updates ---------- */
let es = null;
let renderTimer = null;
function connectStream() {
  if (es) es.close();
  const dot = document.getElementById('conn');
  if (!S.me && !S.publicRead) return;
  es = new EventSource('/api/stream');
  es.onopen = () => { dot.className = 'conn on'; dot.title = 'Live updates connected'; };
  es.onerror = () => { dot.className = 'conn off'; dot.title = 'Live updates disconnected — retrying'; };
  es.onmessage = async (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch { return; }
    if (ev.entity === 'settings') {
      S.settings = await api('GET', 'settings');
      renderChrome();
      return;
    }
    if (ev.entity === 'weather') { refreshWx(true); return; }
    if (ev.entity === 'aprs') { if (S.aprsRefresh) S.aprsRefresh(); return; }
    if (ev.entity === 'mesh') {
      if (ev.action === 'pair') loadNodes();
      if (route().view === 'mesh') { clearTimeout(renderTimer); renderTimer = setTimeout(render, 400); }
      return;
    }
    if (ev.entity === 'feeds') {
      if (S.feeds) delete S.feeds.storm;
      toast(`🌪 ${ev.new || 'New'} new storm report${ev.new === 1 ? '' : 's'} from NWS / spotters`);
      if (route().view === 'reports') { clearTimeout(renderTimer); renderTimer = setTimeout(render, 250); }
      return;
    }
    if (ev.entity === 'members') {
      // someone's program/agency assignments changed: my access may have too
      const me = await api('GET', 'me').catch(() => null);
      if (me && me.user) S.me = me.user;
      S.cache = {};
      clearTimeout(renderTimer);
      renderTimer = setTimeout(render, 250);
      return;
    }
    delete S.cache[ev.entity];
    if (ev.entity === 'programs' && S.me && (S.me.programs || []).includes(Number(ev.id))) {
      const me = await api('GET', 'me').catch(() => null); // my program was renamed/edited: refresh the top bar
      if (me && me.user) { S.me = me.user; renderChrome(); }
    }
    const key = ev.entity + ':' + ev.id;
    if (ev.action === 'create' && !S.mine.has(key)) {
      if (ev.entity === 'incidents') toast('⚠ New incident opened');
      else if (ev.entity === 'reports') toast('📝 New report posted');
      else if (ev.entity === 'messages' && route().view !== 'chat') toast('💬 New chat message');
    }
    clearTimeout(renderTimer);
    renderTimer = setTimeout(render, 250);
  };
}

/* ---------- boot ---------- */
async function boot() {
  const theme = store('taciss-theme');
  if (theme) document.documentElement.dataset.theme = theme;
  document.getElementById('theme-btn').addEventListener('click', () => {
    const t = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    document.documentElement.dataset.theme = t;
    store('taciss-theme', t);
  });
  tickClocks();
  setInterval(tickClocks, 1000);
  try {
    const [me, schema, settings] = await Promise.all([api('GET', 'me'), api('GET', 'schema'), api('GET', 'settings')]);
    S.me = me.user; S.publicRead = me.public_read; S.needsSetup = me.needs_setup; S.schema = schema; S.settings = settings;
    S.update = me.update; S.version = me.version;
  } catch (e) {
    document.getElementById('view').replaceChildren(h('div', { class: 'card empty' }, 'Server unreachable: ' + e.message));
    return;
  }
  window.addEventListener('hashchange', render);
  await loadNodes();
  connectStream();
  render();
  if (S.me || S.publicRead) { loadCounties().then(renderTicker); refreshWx(false); }
}
document.addEventListener('DOMContentLoaded', boot); // after weather.js has loaded too
