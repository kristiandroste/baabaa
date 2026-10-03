// baabaa web app: boot, shared state, layout and routing.
window.__errors = [];
window.addEventListener('error', e => window.__errors.push(String(e.message || e)));
window.addEventListener('unhandledrejection', e => window.__errors.push(String(e.reason && e.reason.message || e.reason)));
import { api, get, post, setCsrf, connectEvents, disconnectEvents, on, ApiError } from './api.js';
import { h, $, clear, icon, logo } from './dom.js';
import { toast, errorToast } from './ui.js';

export const S = {
  me: null, modes: [], models: [], defaultModel: null, convs: [], conv: null, thread: [], turn: null,
  pending: [], artifacts: [], context: null, todos: [], queue: { busy: false, waiting: 0 }, jobs: new Map(),
  connected: false, sidebarOpen: window.innerWidth > 900, panel: null, trusted: null, shells: [],
};

const listeners = new Set();
export function onState(fn) { listeners.add(fn); return () => listeners.delete(fn); }
export function changed(what) { for (const fn of listeners) { try { fn(what); } catch (e) { console.error(e); } } }

// Renderers written as classic scripts; loaded for their globals. The app still works without them.
async function loadRenderers() {
  for (const f of ['./markdown.js', './highlight.js', './mermaid-lite.js']) {
    try { await import(f); } catch (e) { console.warn('renderer missing', f); }
  }
}

export function applyTheme() {
  const pref = (S.me && S.me.settings.theme) || 'auto';
  const dark = pref === 'dark' || (pref === 'auto' && window.matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  document.documentElement.dataset.font = (S.me && S.me.settings.font) || 'serif';
  document.documentElement.dataset.density = (S.me && S.me.settings.density) || 'normal';
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = dark ? '#1f1e1c' : '#faf9f6';
}
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyTheme);

export async function loadModels() {
  try {
    const d = await get('/api/models');
    S.models = d.models;
    S.imageModels = d.images || [];
    S.imageDefault = d.image_default || null;
    S.sttModel = d.stt || null;
    S.defaultModel = d.default;
    S.modelsFull = d;
    if (d.jobs) for (const j of d.jobs) S.jobs.set(j.id, j);
    changed('models');
  } catch (e) { errorToast(e); }
}

export async function loadStatus() {
  try { const st = await get('/api/status'); S.queue = st.queue; changed('queue'); } catch { /* the events stream will tell */ }
}

export async function loadConversations() {
  try {
    const d = await get('/api/conversations?limit=200');
    S.convs = d.conversations;
    changed('convs');
  } catch (e) { if (!(e instanceof ApiError && e.status === 401)) errorToast(e); }
}

async function boot() {
  applyTheme();
  const root = $('#app');
  const setup = /#setup=([\w-]+)/.exec(location.hash);
  let me = null;
  try {
    me = await get('/api/me');
  } catch (e) {
    if (!(e instanceof ApiError) || e.status !== 401) {
      clear(root).appendChild(h('div', { class: 'boot-error' }, logo(48), h('p', null, e.message)));
      return;
    }
  }
  await loadRenderers();
  if (!me) {
    const { renderLogin } = await import('./views/login.js');
    renderLogin(root, setup ? setup[1] : null, () => location.reload());
    return;
  }
  S.me = me.account;
  S.modes = me.modes;
  setCsrf(me.csrf);
  applyTheme();
  await loadModels();
  await loadStatus();
  await renderLayout(root);
  connectEvents(ok => { S.connected = ok; changed('connection'); if (ok) resync(); });
  wireEvents();
  import('./views/update.js').then(m => m.initUpdates(me.version)).catch(e => console.warn('updates', e));
  registerServiceWorker();
  window.addEventListener('hashchange', route);
  await route();
}

let resyncing = false;
async function resync() {
  if (resyncing) return;
  resyncing = true;
  try {
    await loadConversations();
    await loadStatus();
    if (S.conv) {
      const { reloadConversation } = await import('./views/chat.js');
      await reloadConversation();
    }
  } finally { resyncing = false; }
}

function wireEvents() {
  on('conv', d => {
    const c = d.conversation;
    const i = S.convs.findIndex(x => x.id === c.id);
    if (i >= 0) S.convs[i] = { ...S.convs[i], ...c };
    else if (!c.incognito) S.convs.unshift(c);
    if (S.conv && S.conv.id === c.id) { S.conv = { ...S.conv, ...c }; changed('conv'); }
    changed('convs');
  });
  on('conv.deleted', d => {
    S.convs = S.convs.filter(c => c.id !== d.conv_id);
    changed('convs');
    if (S.conv && S.conv.id === d.conv_id) location.hash = '#/';
  });
  on('queue', d => { S.queue = d; changed('queue'); });
  on('job', j => {
    const before = S.jobs.get(j.id);
    S.jobs.set(j.id, j);
    changed('jobs');
    if (j.status === 'error' && (!before || before.status !== 'error') && (j.kind === 'pull' || j.kind === 'fit')) {
      toast(`${j.kind === 'pull' ? 'Installing' : 'Testing'} ${(j.params || {}).model} failed: ${j.error || 'no reason given'}`, 'error', 9000);
    }
  });
  on('models.updated', () => loadModels());
  on('turn', d => {
    const c = S.convs.find(x => x.id === d.conv_id);
    if (c) { c.running = d.state !== 'idle'; changed('convs'); }
  });
  on('msg.done', d => {
    const c = S.convs.find(x => x.id === d.conv_id);
    if (c) { c.updated_ms = Date.now(); changed('convs'); }
    else loadConversations();
  });
  on('pending', p => {
    if (p.for_owner && p.account_id !== S.me.id && (!S.conv || S.conv.id !== p.conv_id)) {
      toast(`${p.account_name} needs your approval: ${p.tool}`, 'warn', 8000);
    }
    const what = p.kind === 'approval' ? `wants to run ${p.tool}` : p.kind === 'plan' ? 'has a plan for you to review' : 'has a question';
    notify(`baabaa ${what}`, convTitle(p.conv_id), p.conv_id);
  });
  on('msg.done', d => {
    if (d.message && d.message.role === 'assistant' && d.message.status === 'ok') notify('baabaa replied', convTitle(d.conv_id), d.conv_id);
  });
  on('share', d => { toast(`${d.from} shared “${d.title || 'a conversation'}” with you`, 'info', 6000); notify(`${d.from} shared a conversation`, d.title || '', null); });
}

function convTitle(cid) {
  const c = S.convs.find(x => x.id === cid);
  return (c && c.title) || 'A conversation';
}

// Desktop notifications, only while baabaa is in the background and only if the person turned them on.
export function notify(title, body, convId) {
  if (!S.me || !S.me.settings.notify || !('Notification' in window) || Notification.permission !== 'granted') return;
  if (!document.hidden) return;
  const opts = { body, icon: '/img/icon-192.png', tag: convId || 'baabaa', data: { url: convId ? `/#/c/${convId}` : '/' } };
  const fallback = () => { try { const n = new Notification(title, opts); n.onclick = () => { window.focus(); if (convId) location.hash = `#/c/${convId}`; n.close(); }; } catch { /* not allowed here */ } };
  if (navigator.serviceWorker && navigator.serviceWorker.controller) navigator.serviceWorker.ready.then(r => r.showNotification(title, opts)).catch(fallback);
  else fallback();
}

function registerServiceWorker() {
  if ('serviceWorker' in navigator && window.isSecureContext) {
    navigator.serviceWorker.register('/sw.js').catch(e => console.warn('service worker', e));
  }
}

async function renderLayout(root) {
  clear(root);
  const sidebar = h('aside', { class: 'sidebar', id: 'sidebar' });
  const main = h('main', { class: 'main', id: 'main' });
  const panel = h('aside', { class: 'panel', id: 'panel', hidden: true });
  const layout = h('div', { class: 'layout' + (S.sidebarOpen ? '' : ' sidebar-closed'), id: 'layout' }, sidebar, main, panel);
  root.appendChild(layout);
  const { renderSidebar } = await import('./views/sidebar.js');
  renderSidebar(sidebar);
  onState(what => { if (what === 'sidebar') layout.classList.toggle('sidebar-closed', !S.sidebarOpen); });
  document.addEventListener('keydown', globalKeys);
}

export function toggleSidebar(open) {
  S.sidebarOpen = open === undefined ? !S.sidebarOpen : open;
  changed('sidebar');
}

function globalKeys(e) {
  const mod = e.ctrlKey || e.metaKey;
  if (mod && e.key.toLowerCase() === 'k') { e.preventDefault(); import('./views/sidebar.js').then(m => m.openSearch()); }
  else if (mod && e.shiftKey && e.key.toLowerCase() === 'o') { e.preventDefault(); location.hash = '#/'; }
  else if (mod && e.key === '.') { e.preventDefault(); toggleSidebar(); }
  else if (mod && e.key === ',') { e.preventDefault(); location.hash = '#/settings/general'; }
}

let lastMain = null;
export async function route() {
  const hash = location.hash || '#/';
  if (!$('#main')) return;
  const settings = /^#\/settings(?:\/(\w+))?/.exec(hash);
  if (settings) {
    if (lastMain === null) { lastMain = '#/'; await renderMain('#/'); }
    const { openSettings } = await import('./views/settings.js');
    openSettings(settings[1] || 'general', () => {
      if (location.hash.startsWith('#/settings')) history.replaceState(null, '', lastMain);
    });
    return;
  }
  lastMain = hash;
  import('./views/settings.js').then(s => s.closeSettings && s.closeSettings());
  await renderMain(hash);
}

async function renderMain(hash) {
  const main = $('#main');
  if (window.innerWidth <= 900) toggleSidebar(false);
  const conv = /^#\/c\/([\w]+)/.exec(hash);
  const project = /^#\/p\/([\w]+)/.exec(hash);
  const chat = await import('./views/chat.js');
  if (conv) await chat.openConversation(main, conv[1]);
  else if (project) await (await import('./views/projects.js')).renderProject(main, project[1]);
  else if (hash.startsWith('#/projects')) await (await import('./views/projects.js')).renderProjects(main);
  else if (hash.startsWith('#/scheduled')) await (await import('./views/scheduled.js')).renderScheduled(main);
  else if (/^#\/s\/\w+/.test(hash)) await (await import('./views/shared.js')).renderShare(main, hash.split('/')[2]);
  else if (hash.startsWith('#/shared')) await (await import('./views/shared.js')).renderShared(main);
  else if (hash.startsWith('#/artifacts')) (await import('./views/gallery.js')).renderGallery(main);
  else if (hash.startsWith('#/images')) await (await import('./views/images.js')).renderImages(main);
  else if (hash.startsWith('#/code')) chat.renderHome(main, { code: true });
  else chat.renderHome(main, {});
  changed('route');
}

export async function logout() {
  try { await post('/api/logout'); } catch { /* already gone */ }
  disconnectEvents();
  location.hash = '';
  location.reload();
}

boot().catch(e => { console.error(e); toast(e.message || String(e), 'error', 10000); });
