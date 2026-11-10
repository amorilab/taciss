/* TACISS mesh: pair with other TACISS nodes (Raspberry Pi kits, EOC servers) and watch sync status. */
'use strict';

const LINK_HELP = {
  lan: 'Same network or Wi-Fi. Use http://<pi-address>:8080.',
  aredn: 'Amateur radio mesh. Part 97 forbids encryption, so traffic is plain http:// — TACISS signs every sync so it can’t be forged or altered, but anyone can read it. Keep chat on-topic for amateur radio rules.',
  vpn: 'Tailscale or WireGuard. The VPN encrypts everything; use the node’s VPN address, e.g. http://100.x.y.z:8080 or http://kit3.tailnet.ts.net:8080.',
  internet: 'Across the open internet. Requires https:// (Caddy or nginx in front of TACISS).',
};

// "from Knox EOC" tag for records that came from another node
function originTag(r) {
  if (!r || !r.mesh_origin || !S.nodes || r.mesh_origin === S.nodes.self) return null;
  const name = S.nodes.nodes[r.mesh_origin] || 'another node';
  return h('span', { class: 'origin', title: 'Created on ' + name + ' and shared over the mesh' }, '⇄ ' + name);
}

async function loadNodes() {
  if (!S.me) return;
  try { S.nodes = await api('GET', 'mesh/nodes'); } catch { S.nodes = null; }
}

function linkSelect(name, value) {
  const help = h('div', { class: 'hint' }, LINK_HELP[value || 'lan']);
  const sel = h('select', { name, value: value || 'lan', onchange: () => { help.textContent = LINK_HELP[sel.value]; } },
    Object.entries(S.meshLinks || {}).map(([k, v]) => h('option', { value: k }, v)));
  return [sel, help];
}

function peerState(p, interval) {
  const fresh = (iso, mult) => iso && (Date.now() - Date.parse(iso)) / 1000 < Math.max(90, interval * mult);
  if (!p.enabled) return ['off', 'Paused'];
  if (p.url && p.last_error && !fresh(p.last_seen, 3)) return ['down', 'Unreachable'];
  if (fresh(p.last_ok, 3) || fresh(p.last_seen, 3)) return ['up', 'Online'];
  if (!p.url) return ['wait', 'Waiting for them to connect'];
  return ['wait', 'Not synced yet'];
}

VIEWS.mesh = async (root) => {
  if (!can('admin')) { root.append(h('div', { class: 'empty' }, 'Admins only.')); return; }
  const st = await api('GET', 'mesh');
  S.meshLinks = st.links;
  const interval = +st.node.interval || 30;
  const reload = () => render();

  // ---- this node
  const nform = h('form', { class: 'grid-form', onsubmit: async (e) => {
    e.preventDefault();
    try {
      S.settings = await api('PUT', 'settings', {
        mesh_node_name: nform.elements.mesh_node_name.value, mesh_url: nform.elements.mesh_url.value,
        mesh_enabled: nform.elements.mesh_enabled.value, mesh_interval: nform.elements.mesh_interval.value,
        mesh_discoverable: nform.elements.mesh_discoverable.value });
      toast('Saved'); await loadNodes(); reload();
    } catch (ex) { toast(ex.message, true); }
  } },
  h('label', {}, 'Node name (what other nodes see)', h('input', { name: 'mesh_node_name', value: S.settings.mesh_node_name || '', placeholder: st.node.name, maxlength: 60 })),
  h('label', {}, 'This node’s address (as other nodes reach it)', h('input', { name: 'mesh_url', value: st.node.url || '', placeholder: 'http://10.54.3.21:8080' })),
  h('label', {}, 'Mesh sync', h('select', { name: 'mesh_enabled', value: st.node.enabled ? '1' : '0' },
    h('option', { value: '1' }, 'On'), h('option', { value: '0' }, 'Off'))),
  h('label', {}, 'Background sync every (seconds)', h('input', { name: 'mesh_interval', type: 'number', min: 5, max: 3600, value: interval })),
  h('label', {}, 'Let nearby nodes find this one', h('select', { name: 'mesh_discoverable', value: st.node.discoverable ? '1' : '0' },
    h('option', { value: '1' }, 'Yes — announce name and address'), h('option', { value: '0' }, 'No — hidden'))),
  h('div', { class: 'full' }, h('button', { class: 'primary' }, 'Save')));

  // ---- invite another node
  const inviteOut = h('div');
  const [invLink, invHelp] = linkSelect('link', 'lan');
  const iform = h('form', { class: 'grid-form', onsubmit: async (e) => {
    e.preventDefault();
    try {
      const r = await api('POST', 'mesh/invites', { link: iform.elements.link.value, note: iform.elements.note.value,
        hours: +iform.elements.hours.value, share_restricted: iform.elements.share_restricted.checked });
      inviteOut.replaceChildren(h('div', { class: 'invite-code' },
        h('div', { class: 'muted' }, 'Pairing code — good for ', r.hours, ' h, one use'),
        h('div', { class: 'code mono' }, r.code),
        h('ol', { class: 'plain-ol' },
          h('li', {}, 'On the other node open the ', h('b', {}, 'TACISS Mesh'), ' tab → ', h('b', {}, 'Join a node'), '.'),
          h('li', {}, 'Address: ', h('code', {}, r.node.url || '(set “This node’s address” above first)')),
          h('li', {}, 'Enter this code and pick the same link type.')),
        h('button', { class: 'small', type: 'button', onclick: () => navigator.clipboard && navigator.clipboard.writeText(r.code).then(() => toast('Code copied')) }, '⧉ Copy code')));
    } catch (ex) { toast(ex.message, true); }
  } },
  h('label', {}, 'Link type', invLink), h('div', { class: 'full' }, invHelp),
  h('label', {}, 'Note (who it’s for)', h('input', { name: 'note', placeholder: 'e.g. Blount County Pi kit' })),
  h('label', {}, 'Expires after (hours)', h('input', { name: 'hours', type: 'number', min: 1, max: 168, value: 24 })),
  h('label', { class: 'full check' }, h('input', { type: 'checkbox', name: 'share_restricted' }),
    ' Also share 🔒 restricted incidents with this node (only if you trust everyone who uses it)'),
  h('div', { class: 'full' }, h('button', { class: 'primary' }, 'Create pairing code')));

  // ---- join another node
  const [joinLink, joinHelp] = linkSelect('link', 'lan');
  const joinErr = h('div', { class: 'err' });
  const jform = h('form', { class: 'grid-form', onsubmit: async (e) => {
    e.preventDefault();
    joinErr.textContent = '';
    const btn = jform.querySelector('button.primary');
    btn.disabled = true; btn.textContent = 'Connecting…';
    try {
      const r = await api('POST', 'mesh/peers', { url: jform.elements.url.value, code: jform.elements.code.value,
        link: jform.elements.link.value, share_restricted: jform.elements.share_restricted.checked });
      toast(`Paired with ${r.peer.name}` + (r.sync && r.sync.pulled != null ? ` — received ${r.sync.pulled}, sent ${r.sync.pushed}` : ''));
      await loadNodes(); reload();
    } catch (ex) { joinErr.textContent = ex.message; btn.disabled = false; btn.textContent = 'Join'; }
  } },
  h('label', {}, 'Other node’s address', h('input', { name: 'url', required: true, placeholder: 'http://10.54.3.21:8080' })),
  h('label', {}, 'Pairing code', h('input', { name: 'code', required: true, class: 'mono', placeholder: 'ABCD-EFGH-JKMN-PQRS-TUVW', autocomplete: 'off' })),
  h('label', {}, 'Link type', joinLink), h('div', { class: 'full' }, joinHelp),
  h('label', { class: 'full check' }, h('input', { type: 'checkbox', name: 'share_restricted' }),
    ' Also share 🔒 restricted incidents with this node'),
  joinErr, h('div', { class: 'full' }, h('button', { class: 'primary' }, 'Join')));

  // ---- nearby nodes (discovery)
  const VIA = { lan: ['LAN', 'lan'], aredn: ['AREDN', 'aredn'], tailscale: ['Tailscale', 'vpn'] };
  const paired = new Set(st.peers.map((p) => p.node_id));
  const pairWith = (d) => {
    jform.elements.url.value = d.url;
    jform.elements.link.value = VIA[d.via][1];
    jform.elements.link.dispatchEvent(new Event('change'));
    jform.scrollIntoView({ behavior: 'smooth', block: 'center' });
    jform.elements.code.focus();
    toast(`Ask ${d.name} for a pairing code (TACISS Mesh → Invite another node), then enter it here.`);
  };
  const nearbyBody = h('div');
  const drawNearby = (list, status) => {
    nearbyBody.replaceChildren(
      list.length ? h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
        h('thead', {}, h('tr', {}, ['Node', 'Found via', 'Address', 'Heard', ''].map((t) => h('th', {}, t)))),
        h('tbody', {}, list.map((d) => h('tr', {},
          h('td', {}, h('b', {}, d.name), d.version ? h('span', { class: 'muted small' }, ' v', d.version) : null),
          h('td', {}, h('span', { class: 'badge link-' + VIA[d.via][1] }, VIA[d.via][0])),
          h('td', { class: 'mono small' }, d.url),
          h('td', { class: 'muted' }, ago(new Date(d.seen * 1000).toISOString())),
          h('td', {}, paired.has(d.node_id) ? h('span', { class: 'muted' }, '✓ paired')
            : h('button', { class: 'small', onclick: () => pairWith(d) }, 'Pair…')))))))
        : h('div', { class: 'empty' }, 'No other TACISS nodes heard yet.'),
      h('p', { class: 'hint' }, 'LAN: ', status.lan, ' · AREDN: ', status.aredn, ' · Tailscale: ', status.tailscale, '. ',
        'Finding a node only fills in its address — you still need its pairing code, so nobody joins just by being nearby. ',
        'On AREDN, advertise TACISS as a service on your node (name containing “TACISS”) so other nodes can find it across the mesh.'));
  };
  drawNearby(st.discovered || [], st.discovery || {});
  const scanBtn = h('button', { class: 'small right', onclick: async () => {
    scanBtn.disabled = true; scanBtn.textContent = 'Looking…';
    try { const r = await api('POST', 'mesh/scan'); drawNearby(r.discovered, r.discovery); } catch (e) { toast(e.message, true); }
    scanBtn.disabled = false; scanBtn.textContent = '⌕ Look now';
  } }, '⌕ Look now');

  // ---- peers
  const peerEdit = (p) => {
    const [ls] = linkSelect('link', p.link);
    const f = h('form', { class: 'grid-form' },
      h('label', {}, 'Name', h('input', { name: 'name', value: p.name || '' })),
      h('label', {}, 'Address', h('input', { name: 'url', value: p.url || '', placeholder: 'blank = they connect to us' })),
      h('label', {}, 'Link type', ls),
      h('label', {}, 'Sync', h('select', { name: 'enabled', value: p.enabled ? '1' : '0' }, h('option', { value: '1' }, 'On'), h('option', { value: '0' }, 'Paused'))),
      h('label', { class: 'full check' }, h('input', { type: 'checkbox', name: 'share_restricted', checked: p.share_restricted }),
        ' Share 🔒 restricted incidents with this node'),
      h('div', { class: 'full hint mono' }, 'Node ID ', p.node_id));
    const err = h('div', { class: 'err' });
    const close = modal('Peer: ' + p.name, [f, err], [
      h('button', { class: 'danger left', onclick: async () => {
        if (!confirm(`Unpair ${p.name}? Records already shared stay; new changes stop flowing. They’ll need a new pairing code to reconnect.`)) return;
        try { await api('DELETE', 'mesh/peers/' + p.id); close(); reload(); } catch (e) { err.textContent = e.message; }
      } }, 'Unpair'),
      h('button', { onclick: () => close() }, 'Cancel'),
      h('button', { class: 'primary', onclick: async () => {
        try {
          await api('PUT', 'mesh/peers/' + p.id, { name: f.elements.name.value, url: f.elements.url.value, link: f.elements.link.value,
            enabled: f.elements.enabled.value === '1', share_restricted: f.elements.share_restricted.checked });
          close(); reload();
        } catch (e) { err.textContent = e.message; }
      } }, 'Save')]);
  };
  const syncNow = async (p, btn) => {
    btn.disabled = true; btn.textContent = '…';
    try {
      const r = await api('POST', `mesh/peers/${p.id}/sync`);
      if (r.sync.error) toast(r.sync.error, true);
      else if (r.sync.busy) toast('Already syncing');
      else toast(`Synced ${p.name}: received ${r.sync.pulled}, sent ${r.sync.pushed}`);
    } catch (e) { toast(e.message, true); }
    reload();
  };
  const peerRows = st.peers.map((p) => {
    const [cls, label] = peerState(p, interval);
    const skew = p.clock_skew != null && Math.abs(p.clock_skew) > 120
      ? h('div', { class: 'warn-text' }, `⚠ clock off by ${Math.round(Math.abs(p.clock_skew) / 60)} min`) : null;
    const btn = h('button', { class: 'small', onclick: (e) => { e.stopPropagation(); syncNow(p, btn); } }, '⟳ Sync');
    return h('tr', { class: 'click', onclick: () => peerEdit(p) },
      h('td', {}, h('span', { class: 'peer-dot ' + cls, title: label }), ' ', h('b', {}, p.name),
        p.share_restricted ? h('span', { title: 'Restricted incidents shared' }, ' 🔒') : null,
        h('div', { class: 'muted small' }, label)),
      h('td', {}, h('span', { class: 'badge link-' + p.link }, (st.links[p.link] || p.link).split(' (')[0])),
      h('td', { class: 'mono small' }, p.url || h('span', { class: 'muted' }, 'they connect to us')),
      h('td', { class: 'muted' }, p.last_ok ? ago(p.last_ok) : (p.last_seen ? 'seen ' + ago(p.last_seen) : 'never'),
        p.last_error ? h('div', { class: 'err small' }, p.last_error) : null, skew),
      h('td', { class: 'mono' }, '↓', p.received, ' ↑', p.sent),
      h('td', {}, p.url ? btn : null));
  });

  const invites = st.invites.length ? h('div', { class: 'card' }, h('h3', {}, 'Unused pairing codes'),
    h('ul', { class: 'plain' }, st.invites.map((i) => h('li', {},
      h('span', { class: 'badge link-' + i.link }, i.link), ' ', i.note || 'no note', ' · by ', i.created_by,
      h('span', { class: 'muted' }, ' · expires ', fmtTime(new Date(i.expires_at * 1000).toISOString())), ' ',
      h('button', { class: 'small ghost', onclick: async () => { await api('DELETE', 'mesh/invites/' + i.id); reload(); } }, 'Cancel'))))) : null;

  root.append(...[
    h('div', { class: 'toolbar' }, h('h2', {}, 'TACISS Mesh — linked TACISS nodes'),
      h('span', { class: 'muted' }, `${st.log.records} shared records · ${st.peers.length} peer${st.peers.length === 1 ? '' : 's'}`,
        st.pending ? ` · ${st.pending} waiting on related records` : ''),
      h('button', { class: 'right', onclick: reload }, '⟳ Refresh')),
    !st.node.enabled ? h('div', { class: 'card warn-text' }, 'Mesh sync is OFF on this node. Turn it on below.') : null,
    h('div', { class: 'card' }, h('h3', {}, 'Peers'),
      st.peers.length ? h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
        h('thead', {}, h('tr', {}, ['Node', 'Link', 'Address', 'Last sync', 'Records', ''].map((t) => h('th', {}, t)))),
        h('tbody', {}, peerRows)))
        : h('div', { class: 'empty' }, 'Not linked to any other node yet. Create a pairing code here and enter it on the other node, or join one below.'),
      h('p', { class: 'hint' }, 'Shared: incidents, activity logs, check-ins, SITREPs/reports, resources, programs, agencies, cameras and chat. ',
        'Never shared: user accounts, passwords, settings and API keys. Changes hop node to node, so a kit only needs to reach one other node. ',
        '🔒 Restricted incidents go only to peers you mark as trusted.')),
    st.node.enabled ? h('div', { class: 'card' }, h('div', { class: 'toolbar' }, h('h3', {}, '📡 Nearby nodes'), scanBtn), nearbyBody) : null,
    h('div', { class: 'grid cols-2' },
      h('div', { class: 'grid' },
        h('div', { class: 'card' }, h('h3', {}, 'This node'), h('div', { class: 'hint mono' }, 'Node ID ', st.node.id), nform),
        invites),
      h('div', { class: 'grid' },
        h('div', { class: 'card' }, h('h3', {}, 'Invite another node'), iform, inviteOut),
        h('div', { class: 'card' }, h('h3', {}, 'Join a node'), jform)))].filter(Boolean));
};
