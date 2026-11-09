/* TACISS SITREP war room: our SITREPs + live NWS/SPC/Spotter Network storm reports, NWS warnings,
   radar, cameras, power and internet outages on one screen. All feeds come through the TACISS server. */
'use strict';

const HAZ = {  // NWS storm report color conventions
  tornado: ['#ef4444', 'T'], funnel_cloud: ['#f97316', 'F'], wall_cloud: ['#f97316', 'W'], rotation: ['#f97316', 'R'],
  hail: ['#22c55e', 'H'], wind_damage: ['#3b82f6', 'D'], high_wind: ['#3b82f6', 'G'], flash_flood: ['#14b8a6', 'FF'],
  flood: ['#14b8a6', 'Fl'], heavy_rain: ['#0ea5e9', 'R'], lightning_damage: ['#eab308', 'L'], snow_ice: ['#a5b4fc', 'S'],
  other: ['#94a3b8', '•'],
};
const hazOf = (k) => HAZ[k] || HAZ.other;
const STORM_KINDS = ['storm_report', 'damage'];
S.feeds = S.feeds || {};

async function loadFeed(name, maxAge) {
  const f = S.feeds[name];
  if (f && Date.now() - f.at < maxAge) return f.data;
  try {
    const data = await api('GET', 'feeds/' + name);
    S.feeds[name] = { at: Date.now(), data };
    return data;
  } catch (e) {
    if (f) return f.data;
    return { error: e.message, failed: true };
  }
}

function hazIcon(hz, extra) {
  const [c, t] = hazOf(hz);
  return L.divIcon({ className: 'hz-icon' + (extra ? ' ' + extra : ''), iconSize: [22, 22],
    html: `<span style="background:${c}">${t}</span>` });
}

function stormLine(r) {
  return [r.event, r.mag].filter(Boolean).join(' ') + ' — ' + [r.city, r.county && r.county + ' Co.', r.state].filter(Boolean).join(', ');
}

/* ---------- report-to-NWS hand-off ---------- */
function spotterText(r) {
  const when = r.event_time || fmtTime(r.created_at);
  return [
    `SPOTTER REPORT — ${nice(r.hazard || r.kind).toUpperCase()}`,
    r.magnitude ? `Magnitude: ${r.magnitude}` : null,
    `Time of event: ${when}`,
    `Location: ${r.location || '(not given)'}`,
    r.lat != null ? `Lat/Lon: ${(+r.lat).toFixed(4)}, ${(+r.lon).toFixed(4)}` : null,
    r.body ? `Remarks: ${r.body}` : null,
    `Reported by: ${r.created_by}${S.me && S.me.callsign ? ' / relayed by ' + S.me.callsign : ''} via ${S.settings.site_name || 'TACISS'}`,
  ].filter(Boolean).join('\n');
}

function nwsModal(r) {
  const text = spotterText(r);
  const ta = h('textarea', { readonly: true, rows: 9, class: 'mono', value: text });
  const links = (S.settings.nws_report_links || '').split('\n').map((l) => l.split('|')).filter((x) => x[1] && /^https?:\/\//.test(x[1].trim()));
  const email = (S.settings.nws_report_email || '').trim();
  const via = h('select', {}, ['mPING', 'E-mail to NWS', 'Phone to NWS', 'Social media (#tspotter)', 'Amateur radio / Skywarn net', 'Other']
    .map((v) => h('option', { value: v }, v)));
  const close = modal('📤 Report to the National Weather Service', [
    h('p', { class: 'muted' }, 'The NWS and spotter networks have no open API for third-party submissions, so TACISS formats the report for you. ',
      'Send it by one of the routes below. NWS-accepted reports come back into the war room automatically as Local Storm Reports.'),
    ta,
    h('div', { class: 'btn-row' },
      h('button', { onclick: async () => { try { await navigator.clipboard.writeText(text); toast('Copied'); } catch { ta.select(); document.execCommand('copy'); toast('Copied'); } } }, '📋 Copy report'),
      email ? h('a', { class: 'btn', href: `mailto:${encodeURIComponent(email)}?subject=${encodeURIComponent('Spotter report: ' + nice(r.hazard || r.kind) + ' ' + (r.location || ''))}&body=${encodeURIComponent(text)}` }, '✉ E-mail NWS') : null,
      links.map(([n, u]) => h('a', { class: 'btn', href: u.trim(), target: '_blank', rel: 'noopener' }, '↗ ', n.trim()))),
    r.nws_sent ? h('p', { class: 'ok-note' }, '✅ Already reported: ', r.nws_sent) : null,
    h('label', {}, 'After sending, record how it went', via),
  ], [
    h('button', { onclick: () => close() }, 'Close'),
    h('button', { class: 'primary', onclick: async () => {
      try { await api('POST', `reports/${r.id}/nws`, { via: via.value }); delete S.cache.reports; close(); toast('Marked as reported to NWS'); render(); }
      catch (e) { toast(e.message, true); }
    } }, 'Mark as reported'),
  ]);
}

/* ---------- small renderers ---------- */
function sparkline(points, w = 220, hgt = 40) {
  if (!points || points.length < 2) return h('span', { class: 'muted' }, 'no data');
  const xs = points.map((p) => p[0]), ys = points.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys) * 0.98, y1 = Math.max(...ys) * 1.02 || 1;
  const d = points.map((p, i) => `${i ? 'L' : 'M'}${((p[0] - x0) / (x1 - x0 || 1) * w).toFixed(1)},${(hgt - (p[1] - y0) / (y1 - y0 || 1) * hgt).toFixed(1)}`).join('');
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', `0 0 ${w} ${hgt}`); svg.setAttribute('class', 'spark'); svg.setAttribute('preserveAspectRatio', 'none');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', d);
  svg.append(path);
  return svg;
}

function kpi(n, label, tone, sub, onclick) {
  return h('div', { class: 'card stat' + (onclick ? ' click' : ''), onclick },
    h('div', { class: 'n c-' + tone }, n), h('div', { class: 'l' }, label), sub ? h('div', { class: 'sub muted' }, sub) : null);
}

function feedNote(f, source) {
  if (!f) return null;
  return h('div', { class: 'feed-note' + (f.error ? ' warn' : '') },
    f.error ? '⚠ ' + f.error + ' · ' : '', source ? source + ' · ' : '', f.updated ? 'updated ' + ago(f.updated) : '');
}

function camModal(c) {
  const img = h('img', { class: 'cam-big', src: c.img + '?t=' + Date.now(), alt: c.name });
  const t = setInterval(() => { if (!img.isConnected) return clearInterval(t); img.src = c.img + '?t=' + Date.now(); }, 30000);
  modal('📷 ' + c.name, [img, h('div', { class: 'muted' }, [c.src, c.county, nice(c.kind)].filter(Boolean).join(' · '), ' · refreshes every 30 s'),
    c.page ? h('p', {}, h('a', { href: c.page, target: '_blank', rel: 'noopener' }, '↗ Open live stream / source page')) : null]);
}

/* ---------- the view ---------- */
VIEWS.reports = async (root) => {
  const live = !!S.me;  // live feeds are for signed-in (networked) users
  const [rows, inc, storm, power, net, cams] = await Promise.all([
    load('reports'), load('incidents').catch(() => []),
    live ? loadFeed('storm', 60000) : null, live ? loadFeed('power', 120000) : null,
    live ? loadFeed('internet', 120000) : null, live ? loadFeed('cameras', 120000) : null,
  ]);
  const day = rows.filter((r) => Date.now() - new Date(r.created_at) < 864e5);
  const alerts = (S.wx && S.wx.alerts) || [];
  const warnings = alerts.filter((a) => /Warning/.test(a.event || ''));
  const stormRows = (storm && storm.reports) || [];
  const camRows = (cams && cams.cameras) || [];
  const netWorst = net && net.regions && net.regions.length
    ? net.regions.reduce((w, r) => (['normal', 'degraded', 'outage'].indexOf(r.status) > ['normal', 'degraded', 'outage'].indexOf(w) ? r.status : w), 'normal') : null;

  /* toolbar */
  const kind = h('select', { 'data-keep': 'rep-k', onchange: () => draw() }, h('option', { value: '' }, 'All SITREP types'),
    S.schema.entities.reports.fields.find((f) => f.name === 'kind').choices.map((c) => h('option', { value: c }, nice(c))));
  const q = h('input', { type: 'search', placeholder: 'Search reports…', 'data-keep': 'rep-q', oninput: () => draw() });
  const tab = store('taciss-feed-tab') || 'all';
  root.append(h('div', { class: 'toolbar' }, h('h2', {}, '🛰 SITREP War Room'), q, kind,
    S.me && canDo('reports', 'create') ? h('button', { class: 'primary', onclick: () => editRecord('reports', null, { kind: 'storm_report' }) }, '+ New Report') : null,
    h('button', { title: 'Full-screen war-room display', onclick: () => {
      document.body.classList.toggle('warroom-mode');
      if (document.body.classList.contains('warroom-mode')) document.documentElement.requestFullscreen?.().catch(() => {});
      else if (document.fullscreenElement) document.exitFullscreen();
    } }, '⛶ War-room mode')));

  /* KPI strip */
  root.append(h('div', { class: 'stats wr-kpis' },
    kpi(warnings.length, 'NWS warnings', warnings.length ? 'bad' : 'ok', alerts.length ? `${alerts.length} alerts total` : 'none active', () => { location.hash = '#/weather'; }),
    live ? kpi(stormRows.length, 'Storm reports', stormRows.length ? 'orange' : 'ok', `NWS / SPC / spotters, ${storm && storm.hours || 24} h`) : null,
    kpi(day.length, 'Our SITREPs (24h)', day.length ? 'warn' : 'ok', `${day.filter((r) => STORM_KINDS.includes(r.kind)).length} storm/damage`),
    live ? kpi(power && power.total != null ? power.total.toLocaleString() : '—', 'Customers out (power)', power && power.total > 1000 ? 'bad' : power && power.total > 0 ? 'warn' : 'ok',
      power && power.counties ? `${power.counties.length} counties` : 'no data') : null,
    live ? kpi(netWorst ? netWorst.toUpperCase() : '—', 'Internet', netWorst === 'outage' ? 'bad' : netWorst === 'degraded' ? 'orange' : 'ok', 'IODA, statewide') : null,
    live ? kpi(camRows.length, 'Cameras', 'info', camRows.length ? 'click a pin or tile' : 'add in Manage cameras') : null));

  /* map + live feed */
  const mapEl = h('div', { class: 'map wr-map' });
  const feedList = h('div', { class: 'wr-feed' });
  const tabs = h('div', { class: 'seg' }, [['all', 'All'], ['ours', 'Our SITREPs'], ['storm', 'NWS / Spotters'], ['alerts', 'Warnings']].map(([k, l]) =>
    h('button', { class: 'small' + (tab === k ? ' primary' : ''), onclick: () => { store('taciss-feed-tab', k); render(); } }, l)));
  root.append(h('div', { class: 'wr-main' },
    h('div', { class: 'card' }, h('h3', {}, '🗺 Situation map'), mapEl,
      h('div', { class: 'legend' }, Object.entries({ tornado: 'Tornado', funnel_cloud: 'Funnel/Wall cloud', hail: 'Hail', wind_damage: 'Wind/Damage', flash_flood: 'Flood', other: 'Other' })
        .map(([k, l]) => h('span', {}, h('span', { class: 'dot', style: 'background:' + hazOf(k)[0] }), l)),
        h('span', {}, '◆ ours'), h('span', {}, '📷 camera'))),
    h('div', { class: 'card wr-feed-card' }, h('h3', {}, '📡 Live feed'), tabs, feedList,
      live && storm ? feedNote(storm, 'IEM/NWS LSR · SPC · Spotter Network') : null)));

  // timeline items
  const items = [];
  if (tab === 'all' || tab === 'ours') for (const r of rows.slice(0, 80)) items.push({ t: new Date(r.created_at).getTime(), el: () =>
    h('div', { class: 'fi ours sev-' + (r.severity || 'info'), onclick: () => editRecord('reports', r) },
      h('div', { class: 'fi-h' }, h('b', {}, '◆ ', nice(r.kind)), ' ', badge(r.severity), h('span', { class: 'muted right' }, ago(r.created_at))),
      h('div', {}, r.title), h('div', { class: 'muted' }, [r.location, r.created_by].filter(Boolean).join(' · '))) });
  if (tab === 'all' || tab === 'storm') for (const r of stormRows) items.push({ t: r.ts * 1000, el: () =>
    h('div', { class: 'fi storm', onclick: () => { if (S.wrMap) S.wrMap.setView([r.lat, r.lon], 10); } },
      h('div', { class: 'fi-h' }, h('span', { class: 'hz', style: 'background:' + hazOf(r.hazard)[0] }, hazOf(r.hazard)[1]), ' ',
        h('b', {}, r.event || 'Report'), r.mag ? ' ' + r.mag : '', h('span', { class: 'muted right' }, ago(r.time))),
      h('div', {}, [r.city, r.county && r.county + ' Co.', r.state].filter(Boolean).join(', ')),
      r.remark ? h('div', { class: 'muted fi-r' }, r.remark.length > 220 ? r.remark.slice(0, 220) + '…' : r.remark) : null,
      h('div', { class: 'muted small' }, r.src, r.reporter ? ' · ' + r.reporter : '', r.wfo ? ' · ' + r.wfo : '')) });
  if (tab === 'all' || tab === 'alerts') for (const a of alerts) items.push({ t: new Date(a.sent).getTime(), el: () =>
    h('div', { class: 'fi alert', onclick: () => alertModal(a) }, h('div', { class: 'fi-h' }, chip(a), h('span', { class: 'muted right' }, ago(a.sent))),
      h('div', { class: 'muted' }, a.area)) });
  items.sort((a, b) => b.t - a.t);
  feedList.append(...(items.length ? items.slice(0, 150).map((i) => i.el())
    : [h('div', { class: 'empty' }, tab === 'storm' && !live ? 'Sign in to see live storm reports.' : 'Quiet — nothing in this feed right now.')]));

  /* cameras wall, power, internet */
  if (live) {
    const wallIds = JSON.parse(store('taciss-wall') || 'null');
    const wall = (wallIds ? camRows.filter((c) => wallIds.includes(c.id)) : camRows).filter((c) => c.img).slice(0, 12);
    const camCard = h('div', { class: 'card' }, h('h3', {}, '📷 Camera wall',
      h('span', { class: 'right btn-row' },
        camRows.length ? h('button', { class: 'small', onclick: () => pickWall(camRows, wallIds) }, 'Choose cameras') : null,
        canDo('cameras', 'create') ? h('a', { class: 'btn small', href: '#/cameras' }, 'Manage cameras') : null)),
      wall.length ? h('div', { class: 'cam-wall' }, wall.map((c) => h('figure', { class: 'cam', onclick: () => camModal(c) },
        h('img', { src: c.img + '?t=' + Math.floor(Date.now() / 60000), alt: c.name, loading: 'lazy',
          onerror: (e) => { e.target.replaceWith(h('div', { class: 'cam-off' }, 'No image')); } }),
        h('figcaption', {}, c.name, h('span', { class: 'muted' }, ' · ', c.src)))))
        : h('div', { class: 'empty' }, 'No cameras yet. Add weather or traffic cameras (any snapshot-image URL) in Manage cameras, ',
          'or add a free Windy.com key / a TDOT SmartWay key in Admin to pull them in automatically.'),
      cams && cams.error ? feedNote(cams) : null);

    const pw = power || {};
    const powerCard = h('div', { class: 'card' }, h('h3', {}, '⚡ Power outages'),
      pw.counties && pw.counties.length ? h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
        h('thead', {}, h('tr', {}, ['County', 'Customers out', 'Utilities', 'Est. restore'].map((t) => h('th', {}, t)))),
        h('tbody', {}, pw.counties.slice(0, 40).map((c) => h('tr', { class: c.out >= 1000 ? 'has-emergency' : '' },
          h('td', {}, c.county, h('span', { class: 'muted' }, ' ', c.state !== 'Tennessee' ? c.state : '')),
          h('td', { class: 'num' }, c.out.toLocaleString()), h('td', { class: 'muted' }, c.utilities.join(', ')),
          h('td', { class: 'muted' }, c.etr ? fmtTime(c.etr) : '—'))))))
        : h('div', { class: 'empty' }, pw.failed ? pw.error : 'No outages reported by participating utilities.'),
      pw.links && pw.links.length ? h('div', { class: 'link-row' }, pw.links.map((l) => h('a', { href: l.url, target: '_blank', rel: 'noopener' }, '↗ ', l.name))) : null,
      feedNote(pw, pw.source));

    const nt = net || {};
    const netCard = h('div', { class: 'card' }, h('h3', {}, '🌐 Internet connectivity'),
      (nt.regions || []).length ? (nt.regions || []).map((r) => h('div', { class: 'net-region' },
        h('div', { class: 'fi-h' }, h('b', {}, r.name), ' ', badge(r.status === 'normal' ? 'operational' : r.status === 'degraded' ? 'high' : 'critical'),
          ' ', h('span', { class: 'muted' }, r.status), h('a', { class: 'right', href: r.link, target: '_blank', rel: 'noopener' }, 'IODA ↗')),
        h('div', { class: 'net-sigs' }, Object.entries({ bgp: 'BGP (routed networks)', 'ping-slash24': 'Active probing (reachable blocks)', 'merit-nt': 'Telescope (background traffic)' })
          .filter(([k]) => r.series[k]).map(([k, l]) => h('div', { class: 'net-sig' },
            h('div', { class: 'muted small' }, l, ' · ', Math.round(r.series[k].ratio * 100) + '% of 24 h median'),
            sparkline(r.series[k].points)))),
        r.alerts.length ? h('ul', { class: 'plain small' }, r.alerts.slice(-5).map((a) => h('li', {}, fmtTime(new Date(a.time * 1000).toISOString()), ' · ', a.datasource, ' · ', a.level, ' ', a.condition))) : null))
        : h('div', { class: 'empty' }, nt.failed ? nt.error : 'No data yet.'),
      feedNote(nt, nt.source));

    root.append(camCard, h('div', { class: 'grid cols-2' }, powerCard, netCard));
  }

  /* our SITREP board */
  const board = h('div', { class: 'board' });
  root.append(h('h3', { class: 'wr-section' }, '📝 SITREP Board'), board);
  function draw() {
    const t = q.value.trim().toLowerCase();
    const shown = rows.filter((r) => (!kind.value || r.kind === kind.value) &&
      (!t || [r.title, r.body, r.location, r.created_by].some((v) => v && v.toLowerCase().includes(t))));
    board.replaceChildren(...(shown.length ? shown.map((r) => {
      const stormy = STORM_KINDS.includes(r.kind);
      const mayForward = stormy && S.me && (r.created_by === S.me.username || can('operator'));
      return h('div', { class: 'card report sev-' + (r.severity || 'info'), onclick: () => editRecord('reports', r) },
        h('div', { class: 'muted', style: 'font-size:12px' }, nice(r.kind).toUpperCase(), ' · ', fmtTime(r.created_at), ' · ', r.created_by,
          r.incident_id ? ' · ' + refName(inc, r.incident_id) : ''),
        h('h4', {}, r.hazard ? h('span', { class: 'hz', style: 'background:' + hazOf(r.hazard)[0] }, hazOf(r.hazard)[1]) : null, r.hazard ? ' ' : '',
          r.title, ' ', badge(r.severity)),
        r.hazard || r.magnitude ? h('div', { class: 's' }, [nice(r.hazard), r.magnitude, r.event_time && 'at ' + r.event_time].filter(Boolean).join(' · ')) : null,
        r.location ? h('div', { class: 's muted' }, '📍 ', r.location) : null,
        r.body ? h('p', {}, r.body.length > 400 ? r.body.slice(0, 400) + '…' : r.body) : null,
        r.nws_sent ? h('div', { class: 'ok-note' }, '✅ Reported to NWS: ', r.nws_sent)
          : mayForward ? h('button', { class: 'small', onclick: (e) => { e.stopPropagation(); nwsModal(r); } }, '📤 Report to NWS') : null);
    }) : [h('div', { class: 'card empty' }, 'No reports.')]));
  }
  draw();

  /* map layers */
  const map = S.wrMap = makeMap(mapEl, 'warroom');
  const layers = {};
  layers['Our SITREPs'] = L.layerGroup(rows.filter((r) => r.lat != null && Date.now() - new Date(r.created_at) < 3 * 864e5).map((r) =>
    L.marker([r.lat, r.lon], { icon: hazIcon(r.hazard || (r.kind === 'damage' ? 'wind_damage' : 'other'), 'ours') })
      .bindPopup(() => h('div', {}, h('b', {}, r.title), h('br'), nice(r.kind), ' · ', ago(r.created_at), ' · ', r.created_by,
        r.body ? h('p', {}, r.body.slice(0, 240)) : null))));
  if (live) {
    layers['Storm reports (NWS/SPC/spotters)'] = L.layerGroup(stormRows.map((r) =>
      L.marker([r.lat, r.lon], { icon: hazIcon(r.hazard) }).bindPopup(() => h('div', {}, h('b', {}, r.event, ' ', r.mag || ''), h('br'),
        [r.city, r.county && r.county + ' Co.', r.state].filter(Boolean).join(', '), h('br'), fmtTime(r.time), ' · ', r.src,
        r.reporter ? ' · ' + r.reporter : '', r.remark ? h('p', {}, r.remark.slice(0, 300)) : null))));
    layers['Cameras'] = L.layerGroup(camRows.filter((c) => c.lat != null && c.lon != null).map((c) =>
      L.marker([c.lat, c.lon], { icon: L.divIcon({ className: 'cam-pin', html: '📷', iconSize: [20, 20] }) })
        .bindPopup(() => h('div', {}, h('b', {}, c.name), h('br'),
          c.img ? h('img', { src: c.img + '?t=' + Date.now(), class: 'cam-pop', onclick: () => camModal(c) }) : null,
          h('div', { class: 'muted' }, c.src), c.page ? h('a', { href: c.page, target: '_blank', rel: 'noopener' }, 'Live ↗') : null))));
  }
  const polys = alerts.filter((a) => a.geometry);
  if (polys.length) {
    layers['NWS warnings'] = L.geoJSON({ type: 'FeatureCollection', features: polys.map((a) => ({ type: 'Feature', geometry: a.geometry, properties: { id: a.id } })) }, {
      style: (f) => { const a = polys.find((x) => x.id === f.properties.id); return { color: alertColor(a), weight: 2, fillOpacity: 0.15 }; },
      onEachFeature: (f, l) => { const a = polys.find((x) => x.id === f.properties.id); l.on('click', () => alertModal(a)); },
    });
  }
  for (const l of Object.values(layers)) l.addTo(map);
  const radar = L.tileLayer.wms('https://mesonet.agron.iastate.edu/cgi-bin/wms/nexrad/n0q.cgi', {
    layers: 'nexrad-n0q-900913', format: 'image/png', transparent: true, opacity: 0.55, attribution: 'NEXRAD: NWS / Iowa State Mesonet' });
  if (live) radar.addTo(map);
  S.timers.push(setInterval(() => radar.setParams({ _t: Date.now() }), 120000));
  L.control.layers(null, { ...layers, 'NEXRAD radar': radar }, { collapsed: true }).addTo(map);

  // keep feeds fresh while the war room is open
  S.timers.push(setInterval(() => { if (live) { delete S.feeds.power; delete S.feeds.internet; delete S.feeds.cameras; render(); } }, 5 * 60000));
  S.timers.push(setInterval(() => {
    document.querySelectorAll('.cam-wall img').forEach((img) => { img.src = img.src.replace(/\?t=\d+/, '?t=' + Date.now()); });
  }, 60000));
};

function pickWall(cams, current) {
  const pl = pickList('wall', cams.filter((c) => c.img).map((c, i) => ({ id: i, label: c.name, sub: c.src })),
    cams.filter((c) => c.img).map((c, i) => (!current || current.includes(c.id) ? i : -1)).filter((i) => i >= 0));
  const withImg = cams.filter((c) => c.img);
  const close = modal('Choose cameras for the wall (up to 12)', [pl], [
    h('button', { onclick: () => { store('taciss-wall', 'null'); close(); render(); } }, 'Show all'),
    h('button', { class: 'primary', onclick: () => { store('taciss-wall', JSON.stringify(pickValue(pl).map((i) => withImg[i].id))); close(); render(); } }, 'Save'),
  ]);
}

VIEWS.cameras = listView('cameras');
