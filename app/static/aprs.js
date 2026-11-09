/* TACISS APRS tab: live APRS-IS stations around the map center, aprs.fi lookups,
   and sending positions / objects / messages under the user's own callsign. */
'use strict';

const APRS_EMOJI = {
  '/>': '🚗', '/-': '🏠', '/_': '🌡️', '/[': '🚶', '/k': '🛻', '/v': '🚐', '/j': '🚙', '/a': '🚑', '/f': '🚒',
  '/!': '🚓', '/o': '🏛️', '/+': '➕', '/h': '🏥', '/r': '📡', '/y': '📶', '/#': '⭐', '/&': '💠', '/b': '🚲',
  '/^': '✈️', "/'": '🛩️', '/X': '🚁', '/s': '🚢', '/Y': '⛵', '/u': '🚛', '/R': '🚌', '/<': '🏍️', '/=': '🚆',
  '/;': '⛺', '/U': '🚌', '/O': '🎈', '/`': '📡', '/n': '🔺', '/l': '💻', '/$': '☎️',
  '\\z': '🏚️', '\\t': '🌪️', '\\T': '⛈️', '\\@': '🌀', '\\F': '🌊', '\\X': '⚠️', "\\'": '💥', '\\#': '⭐',
  '\\&': '💠', '\\>': '🚗', '\\-': '🏠', '\\_': '🌡️', '\\n': '🔺', '\\a': '🚑', '\\k': '🛻', '\\j': '🚙',
};
function aprsIcon(sym) {
  if (!sym) return '📍';
  const table = sym[0] === '/' ? '/' : '\\';  // overlay letters/digits use the alternate table
  return APRS_EMOJI[table + sym[1]] || APRS_EMOJI['/' + sym[1]] || '📍';
}
const kmh2mph = (v) => Math.round(v / 1.609);
const aprsfiUrl = (call) => 'https://aprs.fi/info/a/' + encodeURIComponent(call);
function wxText(wx) {
  if (!wx) return '';
  const p = [];
  if (wx.temp_f != null) p.push(wx.temp_f + '°F');
  if (wx.wind_dir != null || wx.wind_mph != null) p.push(`wind ${wx.wind_dir ?? '?'}° ${wx.wind_mph ?? '?'} mph` + (wx.gust_mph ? ` G${wx.gust_mph}` : ''));
  if (wx.humidity != null) p.push(wx.humidity + '% RH');
  if (wx.pressure_mb != null) p.push(wx.pressure_mb + ' mb');
  if (wx.rain_1h_in) p.push(`rain ${wx.rain_1h_in}"/h`);
  if (wx.rain_midnight_in) p.push(`${wx.rain_midnight_in}" today`);
  return p.join(' · ');
}
function motionText(s) {
  const p = [];
  if (s.speed_kmh) p.push(`${kmh2mph(s.speed_kmh)} mph` + (s.course != null ? ` @ ${s.course}°` : ''));
  if (s.alt_m != null) p.push(`${Math.round(s.alt_m * 3.281)} ft`);
  return p.join(' · ');
}

VIEWS.aprs = async (root) => {
  let st;
  try { st = await api('GET', 'aprs/status'); } catch (ex) {
    root.append(h('div', { class: 'card pad' }, ex.message)); return;
  }
  const me = st.me;
  const symbols = st.symbols || {};
  const state = { hours: 2, filter: '', stations: [], markers: new Map(), pick: null, follow: null };

  // ---- header
  const pill = h('span', { class: 'badge' });
  const info = h('span', { class: 'muted small' });
  const paintStatus = (s) => {
    pill.className = 'badge c-' + (s.connected ? 'ok' : s.enabled ? 'bad' : 'off');
    pill.textContent = s.connected ? 'Live' : s.enabled ? 'Not connected' : 'Off';
    info.textContent = s.connected
      ? `${s.stations} stations · ${s.packets} packets · within ${s.range_km} km of the map center · ${s.server || ''}`
      : (s.error || 'Waiting for APRS-IS…');
  };
  paintStatus(st);
  root.append(h('div', { class: 'toolbar' }, h('h2', {}, '📡 APRS'), pill, info,
    h('a', { class: 'btn right', target: '_blank', rel: 'noopener',
      href: `https://aprs.fi/#!lat=${st.center[0]}&lng=${st.center[1]}&z=9` }, 'Open area on aprs.fi ↗')));

  // ---- map + station list
  const mapEl = h('div', { class: 'map aprs-map' });
  const search = h('input', { type: 'search', placeholder: 'Find callsign or object…', oninput: () => { state.filter = search.value.trim().toUpperCase(); paintList(); } });
  const hoursSel = h('select', { onchange: () => { state.hours = +hoursSel.value; refresh(); } },
    [[1, 'Last hour'], [2, 'Last 2 hours'], [6, 'Last 6 hours'], [24, 'Last 24 hours']].map(([v, l]) => h('option', { value: v }, l)));
  hoursSel.value = '2';
  const list = h('ul', { class: 'items aprs-list' });
  root.append(h('div', { class: 'grid cols-aprs' },
    h('div', { class: 'card' }, h('h3', {}, '🗺 Stations heard', h('span', { class: 'muted small right' }, 'click the map to place a position or object')), mapEl),
    h('div', { class: 'card' }, h('h3', {}, '📋 Station list'),
      h('div', { class: 'card-body aprs-tools' }, search, hoursSel), list)));
  const map = makeMap(mapEl, 'aprs');
  const layer = L.layerGroup().addTo(map);
  const lookupLayer = L.layerGroup().addTo(map);
  let pickMarker = null;

  function popup(s) {
    return h('div', { class: 'aprs-pop' },
      h('div', {}, h('b', {}, s.call), ' ', h('span', { class: 'muted' }, symbols[s.sym] || ''),
        s.by ? h('span', { class: 'muted' }, ` · object by ${s.by}`) : null),
      s.comment ? h('div', {}, s.comment) : null,
      s.status ? h('div', { class: 'muted' }, '“' + s.status + '”') : null,
      s.wx ? h('div', {}, '🌡 ' + wxText(s.wx)) : null,
      motionText(s) ? h('div', {}, motionText(s)) : null,
      h('div', { class: 'muted small' }, `Heard ${ago(s.last)} · ${s.count} packet${s.count === 1 ? '' : 's'}` + (s.path ? ` · via ${s.path}` : '')),
      h('div', { class: 'btn-row' },
        h('a', { class: 'btn small', href: aprsfiUrl(s.call), target: '_blank', rel: 'noopener' }, 'aprs.fi ↗'),
        me.can_send && !s.by ? h('button', { class: 'small', onclick: () => sendForm.messageTo(s.call) }, '✉ Message') : null));
  }

  function paintMap() {
    const seen = new Set();
    for (const s of state.stations) {
      seen.add(s.call);
      const icon = L.divIcon({ className: 'aprs-marker', iconSize: null,
        html: h('div', {}, h('span', { class: 'aprs-ico' }, aprsIcon(s.sym)), h('span', { class: 'aprs-lbl' }, s.call)) });
      let m = state.markers.get(s.call);
      if (!m) {
        m = L.marker([s.lat, s.lon], { icon, title: s.call }).addTo(layer);
        state.markers.set(s.call, m);
      } else {
        m.setLatLng([s.lat, s.lon]);
        m.setIcon(icon);
      }
      m.bindPopup(() => popup(s));
    }
    for (const [call, m] of state.markers) if (!seen.has(call)) { layer.removeLayer(m); state.markers.delete(call); }
  }

  function paintList() {
    const rows = state.stations.filter((s) => !state.filter || s.call.includes(state.filter))
      .sort((a, b) => (a.last < b.last ? 1 : -1)).slice(0, 300);
    list.replaceChildren(...(rows.length ? rows.map((s) => h('li', { onclick: () => {
      map.setView([s.lat, s.lon], Math.max(map.getZoom(), 11));
      const m = state.markers.get(s.call);
      if (m) m.openPopup();
    } },
      h('div', { class: 't' }, aprsIcon(s.sym), ' ', s.call, s.wx ? h('span', { class: 'muted small' }, '  ' + wxText(s.wx)) : null),
      h('div', { class: 's' }, [symbols[s.sym], s.comment || s.status, motionText(s), ago(s.last)].filter(Boolean).join(' · '))))
      : [h('li', { class: 'empty' }, st.connected || state.stations.length ? 'No stations match.' : 'No stations heard yet.')]));
  }

  async function refresh() {
    if (!mapEl.isConnected) {
      // views are built before they're attached; only stop once we've been shown and then left
      if (state.shown) { S.aprsRefresh = null; clearInterval(timer); } else setTimeout(refresh, 150);
      return;
    }
    state.shown = true;
    try {
      const [stations, status] = await Promise.all([api('GET', 'aprs/stations?hours=' + state.hours), api('GET', 'aprs/status')]);
      state.stations = stations.filter((s) => s.lat != null);
      paintStatus(status);
      paintMap();
      paintList();
      msgs.refresh();
    } catch { /* keep showing the last data */ }
  }
  S.aprsRefresh = () => { clearTimeout(state.t); state.t = setTimeout(refresh, 300); };
  const timer = setInterval(refresh, 30000);  // fallback if the live stream is down

  map.on('click', (e) => {
    if (!me.can_send || !sendForm.wantsPoint()) return;
    sendForm.setPoint(e.latlng.lat, e.latlng.lng);
  });
  function showPick(lat, lon) {
    if (pickMarker) pickMarker.setLatLng([lat, lon]);
    else pickMarker = L.marker([lat, lon], { opacity: 0.85, title: 'Location to send' }).addTo(map);
  }

  // ---- send
  const sendForm = buildSendForm();
  const setup = buildSetup();
  const msgs = buildMessages();
  root.append(h('div', { class: 'grid cols-2' }, sendForm.card, setup));
  root.append(h('div', { class: 'grid cols-2' }, msgs.card, buildLookup()));
  root.append(buildRaw());
  refresh();

  function symbolSelect(name, value) {
    const sel = h('select', { name }, Object.entries(symbols).map(([k, l]) => h('option', { value: k }, `${aprsIcon(k)} ${l}`)));
    sel.value = value && symbols[value] ? value : '/-';
    return sel;
  }

  function buildSendForm() {
    if (!me.can_send) {
      return { card: h('div', { class: 'card' }, h('h3', {}, '📤 Send to APRS'),
        h('div', { class: 'card-body' }, h('p', {}, me.reason),
          h('p', { class: 'muted small' }, 'Once set up, anything you send goes into the APRS-IS network under your callsign and shows on aprs.fi within seconds.'))),
      wantsPoint: () => false, setPoint() {}, messageTo() { toast(me.reason, true); } };
    }
    let mode = 'position';
    const out = h('div', { class: 'muted small mono aprs-out' });
    const tabs = h('div', { class: 'seg' }, [['position', '📍 My position'], ['object', '🏷 Object'], ['message', '✉ Message']].map(([k, l]) =>
      h('button', { type: 'button', 'data-k': k, class: k === mode ? 'on' : '', onclick: () => { mode = k; paint(); } }, l)));
    const lat = h('input', { name: 'lat', inputmode: 'decimal', placeholder: 'Latitude, e.g. 36.1627' });
    const lon = h('input', { name: 'lon', inputmode: 'decimal', placeholder: 'Longitude, e.g. -86.7816' });
    const symbol = symbolSelect('symbol', me.symbol);
    const comment = h('input', { name: 'comment', maxlength: 43, value: me.comment, placeholder: 'Comment (optional, 43 characters)' });
    const name = h('input', { name: 'name', maxlength: 9, placeholder: 'Up to 9 characters, e.g. SHELTER2' });
    const kill = h('input', { type: 'checkbox', name: 'kill' });
    const to = h('input', { name: 'to', placeholder: 'Callsign, e.g. KD4XYZ-9', autocapitalize: 'characters' });
    const count = h('span', { class: 'muted small' }, '0/67');
    const text = h('input', { name: 'text', maxlength: 67, placeholder: 'Message (67 characters)', oninput: () => { count.textContent = text.value.length + '/67'; } });
    const incSel = h('select', { onchange: () => {
      const i = (state.incs || []).find((x) => String(x.id) === incSel.value);
      if (!i) return;
      name.value = (i.title || '').replace(/[^\x20-\x7e]/g, '').slice(0, 9);
      comment.value = (i.title || '').replace(/[^\x20-\x7e]/g, '').slice(0, 43);
      if (i.lat != null) setPoint(i.lat, i.lon, true);
    } }, h('option', { value: '' }, 'Fill from an open incident…'));
    load('incidents').then((rows) => {
      state.incs = rows.filter((i) => i.status !== 'closed');
      incSel.append(...state.incs.map((i) => h('option', { value: i.id }, i.title + (i.lat == null ? ' (no location)' : ''))));
    }).catch(() => {});
    const here = h('button', { type: 'button', class: 'small', onclick: () => {
      if (!navigator.geolocation) return toast('This browser can’t share its location.', true);
      navigator.geolocation.getCurrentPosition((p) => setPoint(p.coords.latitude, p.coords.longitude, true),
        (e) => toast('Location not available: ' + e.message + ' (needs https or localhost)', true), { enableHighAccuracy: true, timeout: 15000 });
    } }, '📍 Use my location');
    const posFields = h('div', { class: 'grid-form' }, h('label', {}, 'Latitude', lat), h('label', {}, 'Longitude', lon),
      h('div', { class: 'full muted small' }, here, '  or click the map above.'));
    const body = h('div');
    const form = h('form', { class: 'card-body', onsubmit: async (e) => {
      e.preventDefault();
      const payload = { type: mode, symbol: symbol.value, comment: comment.value };
      if (mode !== 'message') Object.assign(payload, { lat: lat.value, lon: lon.value });
      if (mode === 'object') Object.assign(payload, { name: name.value, kill: kill.checked });
      if (mode === 'message') Object.assign(payload, { to: to.value, text: text.value });
      const btn = form.querySelector('button.primary');
      btn.disabled = true;
      try {
        const r = await api('POST', 'aprs/send', payload);
        out.replaceChildren('Sent: ', r.packet, ' ', h('a', { href: r.aprsfi, target: '_blank', rel: 'noopener' }, 'view on aprs.fi ↗'));
        toast('Sent to APRS as ' + me.from);
        if (mode === 'message') { text.value = ''; count.textContent = '0/67'; }
        S.aprsRefresh && S.aprsRefresh();
      } catch (ex) { toast(ex.message, true); }
      finally { btn.disabled = false; }
    } }, tabs, body, h('div', { class: 'btn-row' }, h('button', { class: 'primary' }, 'Send to APRS'),
      h('span', { class: 'muted small' }, 'as ', h('b', {}, me.from))), out);
    function paint() {
      tabs.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.k === mode));
      if (mode === 'position') body.replaceChildren(h('p', { class: 'muted small' }, 'Puts your station on the APRS map at this spot.'),
        posFields, h('div', { class: 'grid-form' }, h('label', {}, 'Symbol', symbol), h('label', {}, 'Comment', comment)));
      if (mode === 'object') body.replaceChildren(h('p', { class: 'muted small' }, 'Marks something on the APRS map, such as a shelter, staging area or hazard. It shows under its own name, sent by you.'),
        h('label', { class: 'block' }, incSel), h('div', { class: 'grid-form' }, h('label', {}, 'Object name', name), h('label', {}, 'Symbol', symbol)),
        posFields, h('label', { class: 'block' }, 'Comment', comment),
        h('label', { class: 'check' }, kill, ' Remove this object from the map (sends a “killed” object)'));
      if (mode === 'message') body.replaceChildren(h('p', { class: 'muted small' }, 'An APRS text message. Many stations auto-acknowledge; replies show under Messages.'),
        h('div', { class: 'grid-form' }, h('label', {}, 'To', to), h('label', {}, 'Message ', count, text)));
    }
    function setPoint(la, lo, center) {
      lat.value = (+la).toFixed(5); lon.value = (+lo).toFixed(5);
      showPick(+la, +lo);
      if (center) map.setView([la, lo], Math.max(map.getZoom(), 12));
    }
    paint();
    return {
      card: h('div', { class: 'card' }, h('h3', {}, '📤 Send to APRS'), form,
        h('p', { class: 'card-body muted small' }, 'Packets go into APRS-IS and may be gated to RF by igates. Only licensed amateurs may send, under their own callsign. Keep it plain-language amateur traffic.')),
      wantsPoint: () => mode !== 'message',
      setPoint,
      messageTo(call) { mode = 'message'; paint(); to.value = call; text.focus(); form.scrollIntoView({ behavior: 'smooth', block: 'center' }); },
    };
  }

  function buildSetup() {
    const ssid = h('select', { name: 'ssid' }, h('option', { value: '' }, 'None (home station)'),
      [[1, 'Digi / home 2'], [5, 'Other network (D-Star, phone app)'], [7, 'Handheld'], [8, 'Boat / RV'], [9, 'Mobile'],
        [10, 'Internet / APRS-IS'], [12, 'Portable'], [13, 'Weather station'], [14, 'Truck'], [15, 'Other']]
        .map(([v, l]) => h('option', { value: v }, `-${v}  ${l}`)),
      [2, 3, 4, 6, 11].map((v) => h('option', { value: v }, `-${v}`)));
    ssid.value = me.ssid || '';
    const pass = h('input', { name: 'pass', type: 'password', autocomplete: 'off', inputmode: 'numeric',
      placeholder: me.passcode_set ? 'Saved. Type to replace.' : 'Your APRS-IS passcode (5 digits)' });
    const sym = symbolSelect('sym', me.symbol);
    const cmt = h('input', { name: 'cmt', maxlength: 43, value: me.comment, placeholder: 'e.g. Davidson ARES · 146.520' });
    const watch = h('input', { name: 'watch', value: (me.watch || []).join(', '), placeholder: 'e.g. KD4XYZ-9, W4NCS*' });
    const f = h('form', { class: 'card-body grid-form', onsubmit: async (e) => {
      e.preventDefault();
      const body = { aprs_ssid: ssid.value, aprs_symbol: sym.value, aprs_comment: cmt.value,
        aprs_watch: watch.value.split(/[\s,]+/).filter(Boolean) };
      if (pass.value) body.aprs_passcode = pass.value;
      try {
        const prefs = await api('PUT', 'me/prefs', body);
        S.me.prefs = prefs;
        toast('APRS setup saved');
        render();
      } catch (ex) { toast(ex.message, true); }
    } },
    h('label', {}, 'Callsign', h('input', { value: me.callsign || 'not set', disabled: true }),
      h('span', { class: 'hint' }, 'From your account. An admin sets it in Admin → Users.')),
    h('label', {}, 'SSID', ssid),
    h('label', {}, 'APRS-IS passcode', pass, h('span', { class: 'hint' }, me.passcode_set ? '✓ Saved and verified.' : 'The same one your APRS apps use. It’s checked against your callsign and never shown again.')),
    h('label', {}, 'Default symbol', sym),
    h('label', { class: 'full' }, 'Default comment', cmt),
    h('label', { class: 'full' }, 'Also watch these stations (comma separated, * = any SSID)', watch,
      h('span', { class: 'hint' }, 'They’re added to the live feed even when they’re outside the area.')),
    h('div', { class: 'full' }, h('button', { class: 'primary' }, 'Save APRS setup')));
    return h('div', { class: 'card' }, h('h3', {}, '⚙ My APRS setup'), f);
  }

  function buildMessages() {
    const mine = h('ul', { class: 'items' });
    const heard = h('ul', { class: 'items' });
    const bln = h('ul', { class: 'items' });
    const row = (m) => h('li', {},
      h('div', { class: 't' }, h('span', { class: 'mono' }, `${m.src} → ${m.to}`), ' ',
        m.acked ? h('span', { class: 'badge c-ok', title: 'Acknowledged by the recipient' }, '✓ ack') : null,
        me.can_send && m.mine && m.src.split('-')[0] !== me.callsign ? h('button', { class: 'small right', onclick: () => sendForm.messageTo(m.src) }, 'Reply') : null),
      h('div', { class: 's' }, m.text, ' · ', ago(m.t)));
    const inboxOut = h('div');
    const inboxBtn = st.aprsfi && me.callsign ? h('button', { class: 'small right', onclick: async () => {
      try {
        const rows = await api('GET', 'aprs/inbox');
        inboxOut.replaceChildren(h('h4', {}, 'aprs.fi: recent messages to you'),
          rows.length ? h('ul', { class: 'items' }, rows.map((m) => h('li', {},
            h('div', { class: 't mono' }, `${m.srccall} → ${m.dst}`), h('div', { class: 's' }, m.message, ' · ', ago(new Date(m.time * 1000).toISOString())))))
            : h('div', { class: 'empty' }, 'aprs.fi has no recent messages for you.'));
      } catch (ex) { toast(ex.message, true); }
    } }, 'Check aprs.fi') : null;
    const card = h('div', { class: 'card' }, h('h3', {}, '✉ Messages', inboxBtn),
      h('div', { class: 'card-body' }, h('h4', {}, me.callsign ? `To or from ${me.callsign}` : 'Yours'), mine, inboxOut,
        h('details', {}, h('summary', {}, 'Bulletins heard'), bln),
        h('details', {}, h('summary', {}, 'Other messages heard in the area'), heard)));
    return {
      card,
      async refresh() {
        try {
          const r = await api('GET', 'aprs/messages');
          mine.replaceChildren(...(r.mine.length ? r.mine.map(row) : [h('li', { class: 'empty' }, me.callsign ? 'No messages yet.' : 'Your account has no callsign yet.')]));
          heard.replaceChildren(...(r.heard.length ? r.heard.map(row) : [h('li', { class: 'empty' }, 'None heard.')]));
          bln.replaceChildren(...(r.bulletins.length ? r.bulletins.map(row) : [h('li', { class: 'empty' }, 'None heard.')]));
        } catch { /* ignore */ }
      },
    };
  }

  function buildLookup() {
    if (!st.aprsfi) {
      return h('div', { class: 'card' }, h('h3', {}, '🔎 aprs.fi lookup'), h('div', { class: 'card-body' },
        h('p', {}, 'Look up any callsign’s last position or weather anywhere in the world through aprs.fi.'),
        h('p', { class: 'muted small' }, can('admin')
          ? 'Add a free aprs.fi API key in Admin → Settings to turn this on. Find it on aprs.fi under Account → API key.'
          : 'An admin needs to add an aprs.fi API key in Admin → Settings to turn this on.')));
    }
    const names = h('input', { placeholder: 'Callsigns, e.g. KD4XYZ-9, W4NCS', autocapitalize: 'characters' });
    const what = h('select', {}, h('option', { value: 'loc' }, 'Position'), h('option', { value: 'wx' }, 'Weather'));
    const out = h('div');
    const f = h('form', { class: 'card-body aprs-tools', onsubmit: async (e) => {
      e.preventDefault();
      try {
        const rows = await api('GET', `aprs/lookup?what=${what.value}&name=${encodeURIComponent(names.value.replace(/\s+/g, ''))}`);
        lookupLayer.clearLayers();
        if (!rows.length) { out.replaceChildren(h('div', { class: 'empty' }, 'aprs.fi has nothing for that.')); return; }
        out.replaceChildren(h('ul', { class: 'items' }, rows.map((r) => {
          const when = r.lasttime || r.time;
          if (r.lat) {
            L.circleMarker([+r.lat, +r.lng], { radius: 9, color: '#a855f7', weight: 3, fillOpacity: 0.3 })
              .bindPopup(`${r.name}`).addTo(lookupLayer);
          }
          const wx = what.value === 'wx' ? [r.temp != null && `${Math.round(r.temp * 9 / 5 + 32)}°F`, r.humidity != null && `${r.humidity}% RH`,
            r.wind_speed != null && `wind ${r.wind_direction ?? '?'}° ${Math.round(r.wind_speed * 2.237)} mph`, r.pressure && `${r.pressure} mb`].filter(Boolean).join(' · ') : '';
          return h('li', { onclick: () => { if (r.lat) map.setView([+r.lat, +r.lng], 12); } },
            h('div', { class: 't' }, r.name, ' ', h('a', { href: aprsfiUrl(r.name), target: '_blank', rel: 'noopener', onclick: (ev) => ev.stopPropagation() }, 'aprs.fi ↗')),
            h('div', { class: 's' }, [wx, r.comment, r.speed && `${kmh2mph(+r.speed)} mph`, when && ago(new Date(when * 1000).toISOString()),
              r.lat && `${(+r.lat).toFixed(4)}, ${(+r.lng).toFixed(4)}`].filter(Boolean).join(' · ')));
        })));
        const pts = rows.filter((r) => r.lat).map((r) => [+r.lat, +r.lng]);
        if (pts.length) map.fitBounds(L.latLngBounds(pts).pad(0.5), { maxZoom: 12 });
      } catch (ex) { toast(ex.message, true); }
    } }, names, what, h('button', { class: 'primary' }, 'Look up'));
    return h('div', { class: 'card' }, h('h3', {}, '🔎 aprs.fi lookup'), f, h('div', { class: 'card-body' }, out,
      h('p', { class: 'muted small' }, 'Works for any station worldwide, not just this area. Data from ', h('a', { href: 'https://aprs.fi/', target: '_blank', rel: 'noopener' }, 'aprs.fi'), '.')));
  }

  function buildRaw() {
    const pre = h('pre', { class: 'mono small aprs-raw' }, 'Loading…');
    const d = h('details', { class: 'card', ontoggle: async () => {
      if (!d.open) return;
      try { pre.textContent = (await api('GET', 'aprs/packets')).map((p) => `${fmtTime(p.t)}  ${p.raw}`).join('\n') || 'Nothing heard yet.'; }
      catch (ex) { pre.textContent = ex.message; }
    } }, h('summary', { class: 'pad' }, '🧾 Raw packets (most recent first)'), pre);
    return d;
  }
};
