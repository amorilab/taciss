/* TACISS MeshCore tab: MeshCore LoRa mesh as seen by internet-connected observers.
   Node directory from the public MeshCore map, plus live packets / channels / observers
   from a CoreScope analyzer when an admin has set one up, plus links and an embedded web analyzer. */
'use strict';

const MC_COLORS = { repeater: '#f97316', companion: '#3b82f6', room: '#22c55e', sensor: '#a855f7', other: '#94a3b8' };
const MC_LABELS = { repeater: 'Repeater', companion: 'Companion', room: 'Room server', sensor: 'Sensor', other: 'Other' };
const MC_ICONS = { repeater: '📡', companion: '📱', room: '💬', sensor: '🌡️', other: '•' };

VIEWS.meshcore = async (root) => {
  let st;
  try { st = await api('GET', 'meshcore/status'); } catch (ex) {
    root.append(h('div', { class: 'card pad' }, ex.message)); return;
  }
  const state = { days: st.days, types: new Set(['repeater', 'companion', 'room', 'sensor', 'other']), q: '', nodes: [], live: new Map(), shown: false };

  // ---- header
  const info = h('span', { class: 'muted small' }, 'Loading nodes…');
  root.append(h('div', { class: 'toolbar' }, h('h2', {}, 'MeshCore'), info,
    h('span', { class: 'right btn-row' }, st.links.map((l) => h('a', { class: 'btn small', href: l.url, target: '_blank', rel: 'noopener' }, l.name + ' ↗')))));
  if (!st.analyzer) {
    root.append(h('div', { class: 'card pad muted small' },
      'Showing the MeshCore node map for your area. For live packets and public-channel messages, ',
      can('admin') ? 'add a CoreScope analyzer address in Admin → Settings (MeshCore analyzer URL)' : 'an admin can add a CoreScope analyzer address in Admin → Settings',
      '. A regional MeshCore group may already run one; otherwise use the embedded web analyzer below.'));
  }

  // ---- map + list
  const mapEl = h('div', { class: 'map aprs-map' });
  const list = h('ul', { class: 'items aprs-list' });
  const search = h('input', { type: 'search', placeholder: 'Find a node…', oninput: () => { state.q = search.value.trim().toLowerCase(); paint(); } });
  const days = h('select', { onchange: () => { state.days = +days.value; loadNodes(); } },
    [[1, 'Heard today'], [3, 'Last 3 days'], [7, 'Last week'], [14, 'Last 2 weeks'], [30, 'Last month'], [90, 'Last 3 months'], [365, 'Last year']]
      .map(([v, l]) => h('option', { value: v }, l)));
  days.value = String(state.days);
  const typeBoxes = h('div', { class: 'mc-types' }, ['repeater', 'companion', 'room', 'sensor'].map((t) => h('label', { class: 'inline-check' },
    h('input', { type: 'checkbox', checked: true, onchange: (e) => { e.target.checked ? state.types.add(t) : state.types.delete(t); paint(); } }),
    h('span', { class: 'mc-dot', style: `background:${MC_COLORS[t]}` }), ' ', MC_LABELS[t])));
  root.append(h('div', { class: 'grid cols-aprs' },
    h('div', { class: 'card' }, h('h3', {}, '🗺 Nodes in your area', h('span', { class: 'muted small right' }, 'from observers and node adverts')), mapEl),
    h('div', { class: 'card' }, h('h3', {}, '📋 Node list'), h('div', { class: 'card-body aprs-tools' }, search, days), h('div', { class: 'card-body' }, typeBoxes), list)));
  const map = makeMap(mapEl, 'meshcore');
  const nodeLayer = L.layerGroup().addTo(map);
  const obsLayer = L.layerGroup().addTo(map);
  const advLayer = L.layerGroup().addTo(map);
  const box = st.bbox;
  // zoom to the area once the map actually has a size on screen (views are built before they're shown)
  const fitArea = () => { if (state.fitted || S.mapState.meshcore) return; if (!mapEl.clientWidth) return setTimeout(fitArea, 150);
    map.invalidateSize(); map.fitBounds([[box[0], box[1]], [box[2], box[3]]]); state.fitted = true; };
  fitArea();

  const merged = () => {
    const out = new Map(state.nodes.map((n) => [n.key, { ...n }]));
    for (const n of state.live.values()) {
      const cur = out.get(n.key);
      if (cur) { if ((n.last || '') > (cur.last || '')) cur.last = n.last; cur.live = true; }
      else if (n.lat != null) out.set(n.key, { ...n, type: MC_COLORS[n.type] ? n.type : 'other', live: true });
    }
    return [...out.values()];
  };
  const popup = (n) => h('div', { class: 'aprs-pop' },
    h('div', {}, h('b', {}, n.name || '(no name)'), ' ', h('span', { class: 'muted' }, MC_LABELS[n.type] || n.type)),
    n.last ? h('div', {}, 'Last heard ', ago(n.last), h('span', { class: 'muted small' }, ' · ' + fmtTime(n.last))) : null,
    n.radio ? h('div', { class: 'muted small' }, n.radio) : null,
    h('div', { class: 'muted small mono' }, (n.key || '').slice(0, 16) + '…'),
    n.live ? h('div', { class: 'muted small' }, '✓ also heard by the analyzer') : null);

  function paint() {
    const rows = merged().filter((n) => state.types.has(n.type) && (!state.q || (n.name || '').toLowerCase().includes(state.q)));
    nodeLayer.clearLayers();
    for (const n of rows) {
      L.circleMarker([n.lat, n.lon], { radius: n.type === 'repeater' ? 6 : 5, color: '#0b1120', weight: 1, fillColor: MC_COLORS[n.type] || MC_COLORS.other, fillOpacity: 0.9 })
        .bindPopup(() => popup(n)).bindTooltip(n.name || '', { direction: 'top' }).addTo(nodeLayer);
    }
    const sorted = rows.slice().sort((a, b) => ((a.last || '') < (b.last || '') ? 1 : -1)).slice(0, 400);
    list.replaceChildren(...(sorted.length ? sorted.map((n) => h('li', { onclick: () => map.setView([n.lat, n.lon], Math.max(map.getZoom(), 12)) },
      h('div', { class: 't' }, MC_ICONS[n.type] || '•', ' ', n.name || '(no name)'),
      h('div', { class: 's' }, [MC_LABELS[n.type], n.last && ago(n.last), n.radio].filter(Boolean).join(' · '))))
      : [h('li', { class: 'empty' }, 'No nodes match.')]));
    info.textContent = `${rows.length} nodes shown` + (state.total != null ? ` (${state.total} in this area on the MeshCore map)` : '') +
      (state.updated ? ` · map data ${ago(state.updated)}` : '');
  }

  async function loadNodes() {
    try {
      const r = await api('GET', 'meshcore/nodes?days=' + state.days);
      state.nodes = r.data || []; state.total = r.total_in_area; state.updated = r.updated;
      if (r.error) toast('MeshCore map: ' + r.error, true);
      paint();
    } catch (ex) { info.textContent = ex.message; }
  }

  // ---- live analyzer cards
  const cards = [];
  let packets = null; let channels = null; let observers = null;
  if (st.analyzer) {
    packets = buildPackets(); channels = buildChannels(); observers = buildObservers();
    cards.push(h('div', { class: 'grid cols-2' }, packets.card, channels.card), observers.card);
  }
  if (st.embed) cards.push(buildEmbed());
  root.append(...cards);
  loadNodes();
  refreshLive();
  const timer = setInterval(refreshLive, 20000);

  async function refreshLive() {
    if (!mapEl.isConnected) {
      if (state.shown) clearInterval(timer); else setTimeout(refreshLive, 200);
      return;
    }
    state.shown = true;
    if (!st.analyzer) return;
    await Promise.all([packets.refresh(), observers.refresh(), channels.refresh(),
      api('GET', 'meshcore/live-nodes').then((r) => { state.live = new Map((r.data || []).map((n) => [n.key, n])); paint(); }).catch(() => {})]);
  }

  function buildPackets() {
    const body = h('tbody');
    const note = h('div', { class: 'muted small card-body' });
    const card = h('div', { class: 'card' }, h('h3', {}, '⚡ Live packets', h('span', { class: 'muted small right' }, st.regions ? 'areas ' + st.regions : 'all areas')),
      h('div', { class: 'table-wrap', style: 'max-height:460px;overflow:auto' }, h('table', { class: 'data' },
        h('thead', {}, h('tr', {}, ['Time', 'Type', 'What', 'Heard by', 'Hops', 'SNR'].map((t) => h('th', {}, t)))), body)), note);
    return {
      card,
      async refresh() {
        try {
          const r = await api('GET', 'meshcore/packets');
          advLayer.clearLayers();
          body.replaceChildren(...(r.data || []).slice(0, 100).map((p) => {
            if (p.type === 'ADVERT' && p.lat != null) {
              L.circleMarker([p.lat, p.lon], { radius: 10, color: '#facc15', weight: 2, fill: false }).bindTooltip('Advert: ' + p.summary).addTo(advLayer);
            }
            return h('tr', {}, h('td', { class: 'mono' }, fmtTime(p.t, false)), h('td', {}, h('span', { class: 'badge c-' + (p.type === 'ADVERT' ? 'warn' : p.type.startsWith('GRP') ? 'info' : 'off') }, p.type)),
              h('td', {}, p.summary || ''), h('td', { class: 'muted' }, [p.observer, p.iata].filter(Boolean).join(' · ') + (p.seen > 1 ? ` (+${p.seen - 1})` : '')),
              h('td', {}, p.hops ?? ''), h('td', {}, p.snr ?? ''));
          }));
          note.textContent = (r.error ? r.error + ' ' : '') + (r.updated ? `Updated ${ago(r.updated)} · refreshes every 20 s. Private messages and most channels are encrypted; the analyzer decodes public channels.` : '');
        } catch (ex) { note.textContent = ex.message; }
      },
    };
  }

  function buildChannels() {
    const chList = h('ul', { class: 'items' });
    const msgs = h('div');
    let current = null;
    async function open(c) {
      current = c;
      msgs.replaceChildren(h('div', { class: 'muted' }, 'Loading ' + c.name + '…'));
      try {
        const r = await api('GET', 'meshcore/messages?channel=' + encodeURIComponent(c.hash));
        msgs.replaceChildren(h('h4', {}, c.name, ' ', h('button', { class: 'small ghost', onclick: () => { current = null; msgs.replaceChildren(); } }, '✕')),
          (r.data || []).length ? h('ul', { class: 'items' }, r.data.map((m) => h('li', {},
            h('div', { class: 't' }, h('b', {}, m.sender || '?'), ' ', h('span', { class: 'muted small' }, ago(m.timestamp) + (m.hops != null ? ` · ${m.hops} hops` : ''))),
            h('div', { class: 's mc-text' }, m.text || ''))))
            : h('div', { class: 'empty' }, 'No messages.'));
      } catch (ex) { msgs.replaceChildren(h('div', { class: 'err' }, ex.message)); }
    }
    return {
      card: h('div', { class: 'card' }, h('h3', {}, '💬 Public channels', st.channels ? h('span', { class: 'muted small right', title: 'Set in Admin → Settings' }, 'showing ' + st.channels.split(',').join(', ')) : null), h('div', { class: 'card-body' }, msgs, chList)),
      async refresh() {
        try {
          const r = await api('GET', 'meshcore/channels');
          chList.replaceChildren(...(r.data || []).slice(0, 25).map((c) => h('li', { onclick: () => open(c) },
            h('div', { class: 't' }, c.name || c.hash, h('span', { class: 'muted small' }, `  ${c.messageCount ?? ''} msgs`)),
            h('div', { class: 's' }, [c.lastSender, c.lastMessage && c.lastMessage.slice(0, 80), c.lastActivity && ago(c.lastActivity)].filter(Boolean).join(' · ')))));
          if (current) open(current);
        } catch { /* keep last */ }
      },
    };
  }

  function buildObservers() {
    const body = h('div', { class: 'chips' });
    return {
      card: h('div', { class: 'card' }, h('h3', {}, '👁 Observers in your area'), h('div', { class: 'card-body' }, body,
        h('p', { class: 'muted small' }, 'Observers are MeshCore radios with internet that report what they hear. Run one to improve coverage of your area.'))),
      async refresh() {
        try {
          const r = await api('GET', 'meshcore/observers');
          obsLayer.clearLayers();
          const now = Date.now();
          body.replaceChildren(...((r.data || []).length ? r.data.map((o) => {
            const online = o.last_seen && now - new Date(o.last_seen) < 15 * 60000;
            if (o.lat != null) {
              L.marker([o.lat, o.lon], { icon: L.divIcon({ className: 'aprs-marker', iconSize: null, html: h('div', {}, h('span', { class: 'aprs-ico' }, '👁️'), h('span', { class: 'aprs-lbl' }, o.name || '')) }) })
                .bindPopup(`${o.name || ''}<br>${online ? 'online' : 'offline'} · ${o.packetsLastHour ?? 0} packets/h`).addTo(obsLayer);
            }
            return h('span', { class: 'chip', title: [o.model, o.iata, o.noise_floor != null && `noise ${o.noise_floor} dBm`].filter(Boolean).join(' · ') },
              online ? '🟢 ' : '⚪ ', o.name || o.id.slice(0, 8), h('span', { class: 'muted' }, ` · ${o.packetsLastHour ?? 0}/h`));
          }) : [h('span', { class: 'muted' }, 'No observers in this area on the analyzer.')]));
        } catch { /* keep last */ }
      },
    };
  }

  function buildEmbed() {
    const holder = h('div', { class: 'mc-embed' });
    const d = h('details', { class: 'card', ontoggle: () => {
      if (d.open && !holder.firstChild) {
        holder.append(h('iframe', { src: st.embed, title: 'MeshCore web analyzer', loading: 'lazy', referrerpolicy: 'no-referrer',
          sandbox: 'allow-scripts allow-same-origin allow-popups allow-forms' }));
      }
    } }, h('summary', { class: 'pad' }, '🌐 Web analyzer (', new URL(st.embed).hostname, ')',
      h('a', { class: 'btn small right', href: st.embed, target: '_blank', rel: 'noopener', onclick: (e) => e.stopPropagation() }, 'Open in a new tab ↗')), holder);
    return d;
  }
};
