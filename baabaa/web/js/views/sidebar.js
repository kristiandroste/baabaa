// The sidebar: new chat, navigation, search, the conversation list, GPU queue and account menu.
import { S, onState, loadConversations, toggleSidebar, logout } from '../app.js';
import { get, patch, del } from '../api.js';
import { h, clear, icon, logo, avatar, debounce, esc, bytes } from '../dom.js';
import { menu, modal, promptDialog, confirmDialog, errorToast } from '../ui.js';
import { updateNotice } from './update.js';

let listEl, footerEl, filter = '';

export function renderSidebar(el) {
  clear(el);
  const head = h('div', { class: 'sb-head' },
    h('a', { class: 'brand', href: '#/', title: 'baabaa' }, logo(26), h('span', { class: 'brand-name' }, 'baabaa')),
    h('button', { class: 'icon-btn', type: 'button', title: 'Close sidebar (Ctrl+.)', onclick: () => toggleSidebar(false) }, icon('sidebar')));
  const newBtn = h('a', { class: 'sb-new', href: '#/', title: 'New chat (Ctrl+Shift+O)' }, icon('plus', 18), h('span', null, 'New chat'));
  const nav = h('nav', { class: 'sb-nav' },
    navItem('#/', 'list', 'Chats'),
    navItem('#/projects', 'box', 'Projects'),
    navItem('#/code', 'terminal', 'Code'),
    navItem('#/scheduled', 'clock', 'Scheduled'),
    navItem('#/shared', 'users', 'Shared'),
    navItem('#/artifacts', 'code', 'Artifacts'),
    navItem('#/images', 'image', 'Images'));
  const search = h('input', { class: 'sb-search', type: 'search', placeholder: 'Search chats  (Ctrl+K)', 'aria-label': 'Search conversations' });
  search.addEventListener('input', debounce(() => { filter = search.value.trim().toLowerCase(); renderList(); }, 120));
  search.addEventListener('keydown', e => { if (e.key === 'Enter' && search.value.trim()) openSearch(search.value.trim()); });
  listEl = h('div', { class: 'sb-list', role: 'list' });
  footerEl = h('div', { class: 'sb-foot' });
  el.append(head, newBtn, nav, h('div', { class: 'sb-search-wrap' }, icon('search', 16), search), listEl, footerEl);
  renderList();
  renderFooter();
  loadConversations();
  onState(what => {
    if (what === 'convs' || what === 'route' || what === 'conv') renderList();
    if (what === 'queue' || what === 'jobs' || what === 'connection' || what === 'me' || what === 'update') renderFooter();
    if (what === 'route') for (const a of nav.children) a.classList.toggle('active', matches(a.getAttribute('href')));
  });
}

function navItem(href, ic, label) {
  return h('a', { class: 'sb-nav-item' + (matches(href) ? ' active' : ''), href }, icon(ic, 17), h('span', null, label));
}

function matches(href) {
  const hash = location.hash || '#/';
  if (href === '#/') return hash === '#/' || hash === '' || hash.startsWith('#/c/');
  if (href === '#/projects') return hash.startsWith('#/projects') || hash.startsWith('#/p/');
  if (href === '#/shared') return hash.startsWith('#/shared') || hash.startsWith('#/s/');
  return hash.startsWith(href);
}

function groupOf(ms) {
  const d = new Date(ms), now = new Date();
  const day = 86400000;
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  if (ms >= start) return 'Today';
  if (ms >= start - day) return 'Yesterday';
  if (ms >= start - 7 * day) return 'Previous 7 days';
  if (ms >= start - 30 * day) return 'Previous 30 days';
  return d.toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
}

function renderList() {
  if (!listEl) return;
  clear(listEl);
  const current = S.conv && S.conv.id;
  const code = (location.hash || '').startsWith('#/code');
  let convs = S.convs.filter(c => !c.archived);
  if (code) convs = convs.filter(c => c.folder);
  if (filter) convs = convs.filter(c => (c.title || 'untitled').toLowerCase().includes(filter));
  if (!convs.length) {
    listEl.appendChild(h('div', { class: 'sb-empty' }, filter ? 'No matching chats. Press Enter to search messages.' : 'No conversations yet.'));
    return;
  }
  const pinned = convs.filter(c => c.pinned);
  const rest = convs.filter(c => !c.pinned);
  if (pinned.length) {
    listEl.appendChild(h('div', { class: 'sb-group' }, 'Pinned'));
    for (const c of pinned) listEl.appendChild(item(c, current));
  }
  let group = null;
  for (const c of rest) {
    const g = groupOf(c.updated_ms);
    if (g !== group) { group = g; listEl.appendChild(h('div', { class: 'sb-group' }, g)); }
    listEl.appendChild(item(c, current));
  }
}

function item(c, current) {
  const more = h('button', { class: 'icon-btn sb-more', type: 'button', title: 'More', onclick: e => { e.preventDefault(); e.stopPropagation(); convMenu(more, c); } }, icon('more', 16));
  return h('a', { class: 'sb-item' + (c.id === current ? ' active' : ''), href: `#/c/${c.id}`, role: 'listitem', title: c.title || 'Untitled' },
    c.folder ? icon('folder', 14, 'sb-item-icon') : c.project_id ? icon('box', 14, 'sb-item-icon') : null,
    h('span', { class: 'sb-title' }, c.title || 'Untitled'),
    c.running ? h('span', { class: 'sb-running', title: 'Working' }) : null,
    more);
}

export function convMenu(anchor, c, opts = {}) {
  menu(anchor, [
    { label: 'Rename', icon: 'edit', onClick: async () => {
      const t = await promptDialog('Rename conversation', 'Title', c.title || '');
      if (t !== null) try { await patch(`/api/conversations/${c.id}`, { title: t }); } catch (e) { errorToast(e); }
    } },
    { label: c.pinned ? 'Unpin' : 'Pin', icon: 'pin', onClick: async () => {
      try { await patch(`/api/conversations/${c.id}`, { pinned: !c.pinned }); } catch (e) { errorToast(e); }
    } },
    ...(opts.extra || []),
    c.incognito ? null : { label: 'Share with…', icon: 'users', onClick: () => import('./shared.js').then(m => m.shareDialog(c)) },
    'sep',
    { label: 'Delete', icon: 'trash', danger: true, onClick: async () => {
      if (await confirmDialog('Delete conversation?', `“${c.title || 'Untitled'}” and its files and artifacts will be deleted. This cannot be undone.`, 'Delete', true)) {
        try { await del(`/api/conversations/${c.id}`); } catch (e) { errorToast(e); }
      }
    } },
  ], opts);
}

// Full-text search across all messages -----------------------------------------------------------
export function openSearch(initial = '') {
  const m = modal('Search', { wide: true });
  const input = h('input', { class: 'input search-input', type: 'search', value: initial, placeholder: 'Search messages and titles' });
  const results = h('div', { class: 'search-results' });
  m.body.append(input, results);
  const run = debounce(async () => {
    const q = input.value.trim();
    if (!q) { clear(results); return; }
    try {
      const d = await get(`/api/search?q=${encodeURIComponent(q)}`);
      clear(results);
      if (!d.results.length) results.appendChild(h('div', { class: 'muted' }, 'Nothing found.'));
      for (const r of d.results) {
        const snip = esc(r.snippet).replace(/\[\[/g, '<mark>').replace(/\]\]/g, '</mark>');
        results.appendChild(h('a', { class: 'search-hit', href: `#/c/${r.conv_id}`, onclick: () => m.close() },
          h('div', { class: 'search-title' }, r.title || 'Untitled'), h('div', { class: 'search-snippet', html: snip })));
      }
    } catch (e) { errorToast(e); }
  }, 200);
  input.addEventListener('input', run);
  setTimeout(() => input.focus(), 0);
  if (initial) run();
}

// Footer: GPU queue, installs, account -----------------------------------------------------------
function renderFooter() {
  if (!footerEl || !S.me) return;
  clear(footerEl);
  const q = S.queue || {};
  const jobs = [...S.jobs.values()].filter(j => j.status === 'running');
  for (const j of jobs) footerEl.appendChild(jobChip(j));
  const paused = q.paused;
  const status = h('div', { class: 'sb-status', title: paused ? `Paused by ${paused.by}${paused.reason ? `: ${paused.reason}` : ''}` : 'The GPU queue is shared by every account' },
    h('span', { class: 'dot ' + (!S.connected ? 'dot-off' : paused ? 'dot-off' : q.busy ? 'dot-busy' : 'dot-ok') }),
    h('span', null, !S.connected ? 'Reconnecting…' : paused ? `GPU paused${q.waiting ? ` · ${q.waiting} waiting` : ''}` : q.busy
      ? `GPU: ${q.running ? (q.running.label || q.running.kind) : 'busy'}${q.waiting ? ` · ${q.waiting} waiting` : ''}` : 'GPU idle'));
  const acct = h('button', { class: 'sb-account', type: 'button', onclick: () => accountMenu(acct) },
    avatar(S.me, 30), h('span', { class: 'sb-account-name' }, S.me.display_name,
      h('span', { class: 'sb-account-role' }, S.me.role === 'owner' ? 'Owner' : 'Member')), icon('chevron', 16));
  const notice = updateNotice();
  if (notice) footerEl.appendChild(notice);
  footerEl.append(status, acct);
}

function jobChip(j) {
  const p = j.progress || {};
  let text = j.kind === 'pull' ? `Installing ${j.params.model}` : j.kind === 'fit' ? `GPU fit test: ${j.params.model}` : 'Checking for newer models';
  let pct = null;
  if (j.kind === 'pull' && p.total) pct = Math.round(100 * p.completed / p.total);
  if (j.kind === 'fit' && p.of) pct = Math.round(100 * ((p.steps || []).length) / (p.of + 1));
  return h('a', { class: 'sb-job', href: '#/settings/models', title: text },
    h('span', { class: 'sb-job-text' }, text, pct !== null ? ` · ${pct}%` : ''),
    h('span', { class: 'bar' }, h('span', { class: 'bar-fill', style: { width: (pct ?? 30) + '%' } })),
    j.kind === 'pull' && p.rate ? h('span', { class: 'sb-job-rate' }, `${bytes(p.rate)}/s`) : null);
}

function accountMenu(anchor) {
  const owner = S.me.role === 'owner';
  menu(anchor, [
    { heading: S.me.display_name },
    { label: 'Settings', icon: 'gear', hint: 'Ctrl+,', onClick: () => { location.hash = '#/settings/general'; } },
    { label: 'Models', icon: 'cpu', onClick: () => { location.hash = '#/settings/models'; } },
    { label: 'Usage', icon: 'chart', onClick: () => { location.hash = '#/settings/usage'; } },
    { label: 'Permissions', icon: 'shield', onClick: () => { location.hash = '#/settings/permissions'; } },
    owner ? { label: 'Accounts', icon: 'users', onClick: () => { location.hash = '#/settings/accounts'; } } : null,
    'sep',
    { label: 'Switch account', icon: 'user', onClick: logout },
    { label: 'Sign out', icon: 'logout', onClick: logout },
  ].filter(Boolean), { above: true });
}
