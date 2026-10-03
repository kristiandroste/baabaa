// Updates and restarts: the notice for accounts that may act on them, the update dialog, the banner while a
// restart waits for running replies, the screen while baabaa restarts, "What's new" after an update, and the
// Updates section of Settings > About.
import { S, changed } from '../app.js';
import { get, post, patch, del, on } from '../api.js';
import { h, clear, icon, ago } from '../dom.js';
import { modal, toast, errorToast, segmented, switchEl, keepDraft, confirmDialog } from '../ui.js';

export const U = { st: null, loaded: null };
const SHOWN = ['available', 'ready', 'installed', 'changed'];

export async function initUpdates(version) {
  U.loaded = version;
  on('update', () => refresh());
  on('restart', d => {
    if (U.st) { U.st.pending = d.pending; U.st.restart_error = d.error; }
    if (d.error) toast(d.error, 'error', 12000);
    drawBanner();
    changed('update');
    if (!d.pending) refresh();
  });
  on('hello', d => { if (U.loaded && d.version && d.version !== U.loaded) reloadInto(d.version); });
  await refresh();
  whatsNew();
}

export async function refresh() {
  try {
    U.st = await get('/api/update');
  } catch { return; }
  drawBanner();
  changed('update');
}

// a dot on the sidebar button, so a phone (where the sidebar is closed) shows that something waits there
function markButton() {
  const st = U.st;
  document.body.classList.toggle('has-update', !!(st && st.can_act && !st.pending && SHOWN.includes(st.state)));
}

// the notice in the sidebar ----------------------------------------------------------------------------------
export function updateNotice() {
  const st = U.st;
  if (!st || !st.can_act || st.pending || !SHOWN.includes(st.state)) return null;
  const text = { available: `baabaa ${st.target} is available`, ready: `baabaa ${st.target} is ready`,
    installed: `baabaa ${st.target} is installed`, changed: 'baabaa’s files changed' }[st.state];
  return h('button', { class: 'sb-update', type: 'button', title: 'About this update', onclick: () => openUpdateDialog() },
    icon(st.state === 'available' ? 'download' : 'refresh', 16), h('span', { class: 'sb-update-text' }, text),
    h('span', { class: 'sb-update-action' }, st.state === 'available' ? 'Install' : 'Restart'));
}

function workText(w) {
  if (!w) return '';
  const parts = [];
  if (w.replies) {
    const yours = w.yours.length ? ` (yours: ${w.yours.map(t => `“${t}”`).join(', ')})` : '';
    parts.push(`${w.replies === 1 ? 'a reply is' : `${w.replies} replies are`} being written${yours}`);
  }
  for (const j of w.jobs || []) parts.push(j.kind === 'pull' ? `${j.model} is being installed` : j.kind === 'fit' ? `${j.model} is being tested` : 'a model search is running');
  return parts.join('; ');
}

// The dialog: what the update is, what is running, and Install or Restart (now, or when the work is done).
export async function openUpdateDialog(plainRestart = false) {
  await refresh();
  const st = U.st;
  if (!st) return;
  const state = plainRestart || !SHOWN.includes(st.state) ? 'restart' : st.state;
  const title = { available: `baabaa ${st.target} is available`, ready: `baabaa ${st.target} is ready to install`,
    installed: `baabaa ${st.target} is installed`, changed: 'baabaa’s files changed', restart: 'Restart baabaa' }[state];
  const m = modal(title);
  const lead = {
    available: `This computer runs baabaa ${st.running}. Installing downloads ${st.target}, checks it, and restarts baabaa.`,
    ready: `It is downloaded and checked. Restarting switches baabaa from ${st.running} to ${st.target}.`,
    installed: `The server still runs ${st.running}. Restarting starts ${st.target}.`,
    changed: 'The program files on this computer changed since baabaa started (after a git pull, for example). Restarting runs them.',
    restart: 'baabaa stops and starts again. Open windows reconnect by themselves.',
  }[state];
  m.body.append(h('p', { class: 'dialog-text' }, lead));
  const notes = state === 'restart' || state === 'changed' ? [] : st.target_notes || [];
  if (notes.length) m.body.append(h('h4', { class: 'update-notes-head' }, 'What’s new'), h('ul', { class: 'update-notes' }, notes.map(n => h('li', null, n))));
  const busy = st.work && (st.work.replies || (st.work.jobs || []).length);
  if (busy) m.body.append(h('p', { class: 'muted' }, `Right now ${workText(st.work)}. Waiting lets that finish; new replies wait for the restart.`));
  const verb = state === 'available' ? 'Install' : state === 'restart' ? 'Restart' : 'Restart to update';
  const go = async now => {
    try {
      if (state === 'restart') await post('/api/restart', { now });
      else {
        if (state === 'available') toast(`Downloading baabaa ${st.target}…`, 'info', 4000);
        await post('/api/update/install', { now });
      }
      m.close();
      refresh();
    } catch (e) { errorToast(e); refresh(); }
  };
  m.body.append(h('div', { class: 'dialog-actions' },
    h('button', { class: 'btn', type: 'button', onclick: () => m.close() }, 'Later'),
    busy ? h('button', { class: 'btn', type: 'button', onclick: () => go(true) }, `${verb} now`) : null,
    h('button', { class: 'btn btn-primary', type: 'button', onclick: () => go(false) }, busy ? `${verb} when they finish` : verb)));
}

// the banner while a restart waits, and the screen while it happens ---------------------------------------------
let banner = null, overlay = null, polling = false;

function drawBanner() {
  markButton();
  const p = U.st && U.st.pending;
  if (p && p.phase === 'restarting') { showOverlay(p); return; }
  if (!p) { if (banner) { banner.remove(); banner = null; } return; }
  if (!banner) { banner = h('div', { class: 'restart-banner', role: 'status' }); document.body.appendChild(banner); }
  clear(banner);
  const why = p.reason === 'update' ? `to start baabaa ${p.target}` : p.reason === 'changed' ? 'to run its changed files' : '';
  banner.append(icon('refresh', 16),
    h('span', null, p.now ? `baabaa is restarting ${why}…` : `baabaa will restart ${why} when the replies it is writing are finished.`),
    U.st.can_act && !p.now ? h('button', { class: 'btn small', type: 'button', onclick: async () => {
      try { await post('/api/restart', { now: true }); } catch (e) { errorToast(e); }
    } }, 'Restart now') : null,
    U.st.can_act && !p.now ? h('button', { class: 'btn small', type: 'button', onclick: async () => {
      try { await del('/api/restart'); refresh(); } catch (e) { errorToast(e); }
    } }, 'Cancel') : null);
}

function showOverlay(p) {
  if (banner) { banner.remove(); banner = null; }
  // after a change of who can connect, baabaa comes back at another address
  const next = p && p.next_url && new URL(p.next_url).origin !== location.origin ? p.next_url : null;
  const local = ['localhost', '127.0.0.1'].includes(location.hostname);
  const away = next && new URL(next).hostname === 'localhost' && !local;  // this device will not reach it
  if (!overlay) {
    overlay = h('div', { class: 'restart-overlay', role: 'alert' },
      h('div', { class: 'restart-box' }, away ? null : h('div', { class: 'restart-spin' }),
        h('p', null, p && p.reason === 'update' ? `Restarting baabaa into ${p.target}…` : 'Restarting baabaa…'),
        next ? h('p', { class: 'muted small' }, away ? 'Afterwards it accepts only the computer it runs on, at ' : 'Afterwards it is at ',
          h('a', { href: next }, next), '.')
          : h('p', { class: 'muted small' }, 'This window reconnects by itself.')));
    document.body.appendChild(overlay);
  }
  if (!away) waitForServer(next);
}

async function waitForServer(next) {
  if (polling) return;
  polling = true;
  const t0 = Date.now();
  await new Promise(r => setTimeout(r, 1500));  // the old server is still stopping
  while (next) {  // another address: go there once it answers
    try { await fetch(new URL('api/health', next), { mode: 'no-cors', cache: 'no-store' }); location.href = next; return; } catch { /* not yet */ }
    await new Promise(r => setTimeout(r, 1000));
  }
  while (true) {
    try {
      const res = await fetch('/api/health', { cache: 'no-store' });
      if (res.ok) {
        const d = await res.json();
        if (d.version !== U.loaded) return reloadInto(d.version);
        if (Date.now() - t0 > 4000) { polling = false; overlay && overlay.remove(); overlay = null; refresh(); return; }
      }
    } catch { /* not back yet */ }
    if (Date.now() - t0 > 180000 && overlay) {
      clear(overlay.firstChild).append(h('p', null, 'baabaa has not come back yet.'),
        h('p', { class: 'muted small' }, 'On the computer that runs it: baabaa status'));
    }
    await new Promise(r => setTimeout(r, 1000));
  }
}

function reloadInto(version) {
  keepDraft();
  if (!overlay) {
    overlay = h('div', { class: 'restart-overlay' }, h('div', { class: 'restart-box' }, h('div', { class: 'restart-spin' }),
      h('p', null, `baabaa ${version} is running. Loading it…`)));
    document.body.appendChild(overlay);
  }
  setTimeout(() => location.reload(), 300);
}

// What's new, once per account after an update ----------------------------------------------------------------
function cmp(a, b) {
  const pa = String(a || '').split(/[.-]/), pb = String(b || '').split(/[.-]/);
  for (let i = 0; i < 3; i++) { const d = (parseInt(pa[i]) || 0) - (parseInt(pb[i]) || 0); if (d) return d; }
  return 0;
}

async function whatsNew() {
  const st = U.st;
  if (!st || !S.me) return;
  const seen = S.me.settings.seen_version;
  if (seen === st.running) return;
  const save = () => patch('/api/me', { settings: { seen_version: st.running } }).then(d => { S.me = d.account; }).catch(() => {});
  if (!seen || cmp(st.running, seen) <= 0 || !(st.running_notes || []).length) { save(); return; }
  const m = modal(`What’s new in baabaa ${st.running}`, { onClose: save });
  m.body.append(h('ul', { class: 'update-notes' }, st.running_notes.map(n => h('li', null, n))),
    h('div', { class: 'dialog-actions' }, h('button', { class: 'btn btn-primary', type: 'button', onclick: () => m.close() }, 'Got it')));
}

// Settings > About: Updates ------------------------------------------------------------------------------------
export async function updatesSection(section, row) {
  await refresh();
  const st = U.st;
  if (!st) return null;
  const kindText = st.kind === 'installed' ? `installed, ${st.channel} channel`
    : st.kind === 'git' ? 'runs from a git checkout: update it with git pull' : 'runs from a folder of its own';
  const stateText = {
    up_to_date: st.kind !== 'installed' ? '' : st.published === false ? 'No releases are published yet.'
      : `Up to date.${st.checked_ms ? ` Checked ${ago(st.checked_ms)}.` : ''}`,
    available: `baabaa ${st.target} is available.`, ready: `baabaa ${st.target} is downloaded and ready.`,
    installed: `baabaa ${st.target} is installed; a restart starts it.`, changed: 'The program files changed since baabaa started.',
  }[st.state] || '';
  const buttons = h('div', { class: 'update-actions' },
    st.can_act && SHOWN.includes(st.state) ? h('button', { class: 'btn btn-primary', type: 'button', onclick: () => openUpdateDialog() },
      st.state === 'available' ? 'Install' : 'Restart to update') : null,
    st.owner && st.kind === 'installed' && !st.disabled ? h('button', { class: 'btn', type: 'button', disabled: st.busy ? true : null, onclick: async e => {
      e.target.disabled = true;
      try { U.st = await post('/api/update/check', {}); changed('update'); toast(U.st.latest ? `baabaa ${U.st.latest.version} is available` : 'baabaa is up to date'); }
      catch (err) { errorToast(err); }
      reopen();
    } }, st.busy === 'checking' ? 'Checking…' : 'Check now') : null,
    st.can_act ? h('button', { class: 'btn', type: 'button', onclick: () => openUpdateDialog(true) }, 'Restart baabaa') : null);
  const rows = [
    h('p', null, `baabaa ${st.running} (${kindText}). ${stateText}`),
    st.error ? h('p', { class: 'muted small error-text' }, st.error) : null,
    buttons,
  ];
  if (st.owner) {
    if (st.kind === 'installed' && !st.disabled) {
      rows.push(row('Release channel', segmented([{ value: 'stable', label: 'Stable' }, { value: 'latest', label: 'Latest' }], st.channel,
        v => save({ channel: v })), 'Stable gets a release after it has been out a week without problems; Latest gets each one at once.'));
      rows.push(row('Check for updates every day', switchEl(st.checks, v => save({ checks: v })),
        st.auto_check ? 'A check downloads only the list of releases from GitHub; nothing about you is sent.' : 'Turned off on this computer (BAABAA_DISABLE_AUTOUPDATER).'));
    }
    rows.push(row('Who can restart and update baabaa', segmented([{ value: 'all', label: 'Everyone' }, { value: 'owners', label: 'Owners only' }],
      st.restart_by, v => save({ restart_by: v })), 'Restarts wait for running replies unless someone chooses Restart now.'));
    if (st.previous) {
      rows.push(row(`Go back to baabaa ${st.previous}`, h('button', { class: 'btn', type: 'button', onclick: async () => {
        if (!await confirmDialog(`Go back to baabaa ${st.previous}?`, `baabaa restarts into ${st.previous}. Your conversations and settings stay as they are.`, 'Go back')) return;
        try { await post('/api/update/previous', {}); refresh(); } catch (e) { errorToast(e); }
      } }, 'Use previous version')));
    }
  }
  return section('Updates', ...rows);
}

// Settings > About: who can connect (owners) ------------------------------------------------------------------------
export async function networkSection(section, row) {
  if (!S.me || S.me.role !== 'owner') return null;
  let n;
  try { n = await get('/api/network'); } catch { return null; }
  const what = m => m === 'local' ? 'this computer only' : 'other devices on your network too';
  const rows = [row('Who can connect', segmented([{ value: 'local', label: 'This computer only' }, { value: 'lan', label: 'Other devices too' }], n.saved,
    async v => { try { await patch('/api/network', { mode: v }); } catch (e) { errorToast(e); } reopen(); }),
    'This computer only: baabaa is at http://localhost and nothing else can reach it. Other devices too: phones and computers on your network can open it over HTTPS.')];
  if (n.forced) {
    rows.push(h('p', { class: 'muted small' }, 'baabaa was started with --bind, which chooses its addresses itself; this setting applies when it starts without --bind.'));
  } else if (n.saved !== n.running) {
    rows.push(h('p', null, `baabaa accepts ${what(n.running)} until it restarts${n.next_urls ? `; then it is at ${n.next_urls[0]}` : ''}.`),
      h('div', { class: 'update-actions' }, h('button', { class: 'btn btn-primary', type: 'button', onclick: () => openUpdateDialog(true) }, 'Restart now')));
  }
  if (n.saved === 'lan' && n.passwordless.length) {
    rows.push(h('p', { class: 'small error-text' }, `These accounts have no password, so anyone on your network could open them: ${n.passwordless.join(', ')}.`));
  }
  if (n.saved === 'local' && n.remote) {
    rows.push(h('p', { class: 'small error-text' }, 'You are connected from another device. After the restart, only the computer baabaa runs on can open it.'));
  }
  return section('Network', ...rows);
}

let reopenFn = null;
export function onReopen(fn) { reopenFn = fn; }
function reopen() { if (reopenFn) reopenFn(); }

async function save(body) {
  try { U.st = await patch('/api/update/settings', body); changed('update'); toast('Saved'); } catch (e) { errorToast(e); }
  reopen();
}
