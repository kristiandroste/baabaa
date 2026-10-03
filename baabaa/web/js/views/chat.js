// The chat: the new-chat home, a conversation's thread with live updates, and the composer.
import { S, changed, onState, loadConversations, toggleSidebar } from '../app.js';
import { api, get, post, patch, on, upload, downloadFrom } from '../api.js';
import { h, clear, icon, logo, tokens, ctxk, toolNote, $, bytes } from '../dom.js';
import { menu, toast, errorToast, modal, confirmDialog, promptDialog, takeKeptDraft } from '../ui.js';
import { renderMessage, renderBlock, assistantFooter, pendingCard, enhance, attChip, copyCodeHandler, mdHTML } from './blocks.js';
import { convMenu } from './sidebar.js';

const MODE_ICON = { auto: 'bolt', manual: 'shield', accept_edits: 'pencil', plan: 'map' };
const MODE_HINT = {
  auto: 'Safe actions run; risky ones ask', manual: 'Asks before every edit and command',
  accept_edits: 'Edits in the folder run; commands ask', plan: 'Read-only research, then a plan',
};
let unsub = [];
let V = null; // the current view's elements and helpers

function teardown() {
  for (const u of unsub) u();
  unsub = [];
  V = null;
}

// Home -----------------------------------------------------------------------------------------------
export function renderHome(main, { code = false } = {}) {
  teardown();
  S.conv = null; S.thread = []; S.pending = []; S.artifacts = []; S.todos = []; S.turn = null; S.context = null;
  closePanel();
  clear(main);
  const draft = { folder: null, mode: null, incognito: false, model: null, think: null };
  const hour = new Date().getHours();
  const greet = hour < 5 ? 'Good evening' : hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening';
  const composer = createComposer({
    home: true, draft,
    placeholder: code ? 'Describe a coding task for the folder…' : 'How can I help you today?',
    onSend: async (text, atts) => {
      const body = { folder: draft.folder, mode: draft.mode || undefined, incognito: draft.incognito, model: draft.model || undefined };
      if (draft.think !== null) body.think = draft.think;
      const { conversation } = await post('/api/conversations', body);
      await post(`/api/conversations/${conversation.id}/messages`, { text, attachments: atts.map(a => a.id), research: !!draft.research,
        ...(draft.image ? { image: draft.image } : {}) });
      if (!conversation.incognito) loadConversations();
      location.hash = `#/c/${conversation.id}`;
    },
  });
  const suggestions = code
    ? ['Explain how this project is organized', 'Find and fix a failing test', 'Add a README with setup steps', 'Review the last changes for bugs']
    : ['Write a short poem about sheep on a hill', 'Explain how GPUs run language models', 'Plan a week of simple dinners', 'Draw a flowchart of making tea'];
  const home = h('div', { class: 'home' },
    h('div', { class: 'home-greet' }, logo(40), h('h1', null, `${greet}, ${S.me.display_name}`)),
    S.models.length ? null : firstModel(),
    composer.el,
    h('div', { class: 'suggestions' }, suggestions.map(s => h('button', { class: 'chip', type: 'button', onclick: () => composer.setText(s) }, s))),
    code ? h('p', { class: 'muted small center' }, 'Choose a folder with the folder button. Tools run in a sandbox that can change only that folder.') : null);
  main.append(topbar(null), h('div', { class: 'home-wrap' }, home));
  if (code && !draft.folder) setTimeout(() => composer.pickFolder(), 50);
  composer.focus();
}

// Before any model is approved: the owner picks one that fits the GPU; others wait for an owner.
function firstModel() {
  if (S.me.role !== 'owner') {
    return h('div', { class: 'first-model' }, h('p', null, 'baabaa has no model yet. An owner of this baabaa needs to install one first.'));
  }
  return h('div', { class: 'first-model' },
    h('div', null, h('strong', null, 'Pick your first model'),
      h('p', { class: 'muted' }, 'baabaa suggests models from the Ollama library that fit your GPU. Installing one downloads it, tests it on the GPU, and makes it ready to use.')),
    h('button', { class: 'btn btn-primary', type: 'button', onclick: () => { S.findModels = { category: 'general' }; location.hash = '#/settings/models'; } },
      icon('search', 15), ' Find a model'));
}

// Top bar ----------------------------------------------------------------------------------------------
function topbar(conv) {
  const left = h('div', { class: 'tb-left' },
    h('button', { class: 'icon-btn tb-sidebar', type: 'button', title: 'Sidebar (Ctrl+.)', onclick: () => toggleSidebar() }, icon('sidebar')));
  const right = h('div', { class: 'tb-right' });
  if (conv) {
    const title = h('button', { class: 'tb-title', type: 'button', title: 'Rename', onclick: async () => {
      const t = await promptDialog('Rename conversation', 'Title', conv.title || '');
      if (t !== null) try { await patch(`/api/conversations/${conv.id}`, { title: t }); } catch (e) { errorToast(e); }
    } }, conv.incognito ? icon('ghost', 15) : null, h('span', null, conv.title || 'New conversation'), icon('chevron', 14));
    if (S.project) left.appendChild(h('a', { class: 'tb-project', href: `#/p/${S.project.id}`, title: 'Open the project' }, icon('box', 14), S.project.name));
    left.appendChild(title);
    const wt = (conv.settings || {}).worktree;
    if (conv.folder) left.appendChild(h('span', { class: 'tb-folder', title: wt ? `${conv.folder} (worktree of ${wt.repo})` : conv.folder },
      icon(wt ? 'branch' : 'folder', 14), wt ? wt.branch : conv.folder.split('/').filter(Boolean).pop()));
    const artBtn = h('button', { class: 'icon-btn', type: 'button', title: 'Artifacts', hidden: !S.artifacts.length || null, onclick: () => togglePanelList() }, icon('panel'));
    const more = h('button', { class: 'icon-btn', type: 'button', title: 'More', onclick: () => moreMenu(more, conv) }, icon('more'));
    right.append(artBtn, more);
    unsub.push(onState(w => { if (w === 'artifacts') artBtn.hidden = !S.artifacts.length; }));
  }
  return h('header', { class: 'topbar' }, left, right);
}

function moreMenu(anchor, conv) {
  const c = S.conv || conv;
  convMenu(anchor, c, {
    alignRight: true,
    extra: [
      { label: 'Export as Markdown', icon: 'download', onClick: () => downloadFrom(`/api/conversations/${c.id}/export?format=md`, 'conversation.md') },
      { label: 'Export as web page', icon: 'download', onClick: () => downloadFrom(`/api/conversations/${c.id}/export?format=html`, 'conversation.html') },
      { label: 'Export as JSON', icon: 'download', onClick: () => downloadFrom(`/api/conversations/${c.id}/export?format=json`, 'conversation.json') },
      { label: 'Save conversation as image', icon: 'image', onClick: () => exportThreadPng() },
      { label: 'Fork from here', icon: 'fork', onClick: async () => {
        try { const d = await post(`/api/conversations/${c.id}/fork`, {}); loadConversations(); location.hash = `#/c/${d.conversation.id}`; } catch (e) { errorToast(e); }
      } },
      { label: 'Compact now', icon: 'compress', onClick: async () => {
        try { toast('Summarizing the conversation…'); await post(`/api/conversations/${c.id}/compact`, {}); } catch (e) { errorToast(e); }
      } },
      c.folder && !(c.settings || {}).worktree ? { label: 'Work in a new git worktree', icon: 'branch', onClick: async () => {
        try { const d = await post(`/api/conversations/${c.id}/worktree`, {}); toast(`Now on branch ${d.branch}, in its own worktree`); } catch (e) { errorToast(e); }
      } } : null,
      (c.settings || {}).worktree ? { label: 'Leave the worktree', icon: 'branch', onClick: async () => {
        try { const d = await api('DELETE', `/api/conversations/${c.id}/worktree`); toast(d.note, 'info', 6000); }
        catch (e) {
          if (e.status === 409 && await confirmDialog('Remove the worktree anyway?', `${e.message} Uncommitted changes in it will be lost.`, 'Remove', true)) {
            try { const d = await api('DELETE', `/api/conversations/${c.id}/worktree?force=1`); toast(d.note, 'info', 6000); } catch (e2) { errorToast(e2); }
          } else if (e.status !== 409) errorToast(e);
        }
      } } : null,
    ],
  });
}

// Conversation ----------------------------------------------------------------------------------------
export async function openConversation(main, id) {
  let d;
  try { d = await get(`/api/conversations/${id}`); }
  catch (e) { errorToast(e); location.hash = '#/'; return; }
  teardown();
  S.conv = d.conversation; S.thread = d.thread; S.turn = d.state; S.pending = (d.state && d.state.pending) || [];
  S.artifacts = d.artifacts; S.context = d.context; S.todos = d.conversation.settings.todos || []; S.trusted = d.trusted;
  S.shells = d.shells || [];
  S.project = d.project || null;
  changed('conv'); changed('artifacts');
  clear(main);
  const inner = h('div', { class: 'thread-inner' });
  const thread = h('div', { class: 'thread', id: 'thread' }, inner);
  thread.addEventListener('click', copyCodeHandler);
  const composer = createComposer({
    conv: S.conv, placeholder: 'Reply to baabaa…',
    onSend: async (text, atts, extra) => {
      if (V) V.stick = true;
      await post(`/api/conversations/${S.conv.id}/messages`, { text, attachments: atts.map(a => a.id), ...(extra || {}) });
    },
  });
  const todoEl = h('div', { class: 'todo-dock' });
  const queueEl = h('div', { class: 'queue-dock' });
  const bottom = h('div', { class: 'composer-dock' }, todoEl, queueEl, composer.el,
    h('div', { class: 'disclaimer' }, 'baabaa uses AI, which can make mistakes.'));
  const jump = h('button', { class: 'jump-bottom', type: 'button', title: 'Jump to the latest', hidden: true,
    onclick: () => { if (V) { V.stick = true; thread.scrollTo({ top: thread.scrollHeight, behavior: 'smooth' }); } } }, icon('arrow-down', 18));
  bottom.prepend(jump);
  main.append(topbar(S.conv), thread, bottom);
  V = { main, thread, inner, composer, todoEl, queueEl, jump, stick: true, raf: new Map() };
  followBottom(thread, jump);
  renderThread();
  renderTodos();
  renderQueue();
  composer.setRunning(!!S.turn);
  composer.setContext(S.context);
  wireConversationEvents();
  scrollBottom(true);
  composer.focus();
  if (S.conv.folder && S.trusted === false) askTrust(S.conv.folder);
}

export async function reloadConversation() {
  if (!S.conv || !V) return;
  const main = V.main;
  const keep = V.composer.getDraft();
  await openConversation(main, S.conv.id);
  if (V) V.composer.restoreDraft(keep);
}

function ctx() {
  return {
    hasFolder: !!(S.conv && S.conv.folder),
    branch: async mid => {
      try { const d = await post(`/api/conversations/${S.conv.id}/branch`, { message_id: mid }); S.thread = d.thread; S.conv = d.conversation; renderThread(); } catch (e) { errorToast(e); }
    },
    retry: async m => { try { await post(`/api/conversations/${S.conv.id}/regenerate`, { message_id: m.id }); } catch (e) { errorToast(e); } },
    edit: async (m, text) => {
      try { await post(`/api/conversations/${S.conv.id}/messages`, { text, parent_id: m.parent_id, edit: true }); await reloadConversation(); } catch (e) { errorToast(e); }
    },
    rerender: () => renderThread(),
    rewind: m => rewindDialog(m),
    png: m => exportMessagePng(m),
    feedback: async (m, rating, btn) => {
      try {
        await post(`/api/conversations/${S.conv.id}/feedback`, { message_id: m.id, rating });
        m.meta = { ...(m.meta || {}), feedback: rating };
        const row = btn.closest('.msg-actions');
        row.querySelectorAll('.icon-btn').forEach(b => b.classList.remove('active'));
        if (rating) btn.classList.add('active');
        toast(rating ? 'Thanks — noted locally' : 'Feedback removed', 'info', 1500);
      } catch (e) { errorToast(e); }
    },
    openArtifact: (aid, v) => openArtifact(aid, v),
    imageAgain: b => {
      if (!V) return;
      V.composer.setImageMode(true, b.width === b.height ? 'square' : b.width > b.height ? (b.width / b.height > 1.6 ? 'wide' : 'landscape') : 'portrait');
      V.composer.setText(b.prompt);
    },
    imageEdit: b => {
      if (!V) return;
      V.composer.setImageMode(true, 'square', true);
      V.composer.setText('');
      toast('Describe the change; baabaa edits the latest image', 'info', 3500);
    },
    speak: async (m, btn) => {
      const sp = await import('../speech.js');
      if (sp.speaking(m.id)) { sp.stop(); return; }
      const el = msgEl(m.id);
      const body = el && el.querySelector('.assistant-body');
      const text = body ? sp.textOf(body) : '';
      if (!text) return;
      const v = S.me.settings.voice || {};
      btn.classList.add('active');
      try {
        await sp.speak(text, { key: m.id, voiceURI: v.uri, rate: v.rate || 1, onend: () => btn.classList.remove('active') });
      } catch (e) { btn.classList.remove('active'); errorToast(e); }
    },
  };
}

function renderThread() {
  if (!V) return;
  clear(V.inner);
  const c = ctx();
  for (const m of S.thread) {
    const el = renderMessage(m, c);
    V.inner.appendChild(el);
    if (m.status !== 'streaming') enhance(el);
  }
  if (!S.thread.length) V.inner.appendChild(h('div', { class: 'empty-thread muted' }, 'Say hello to start.'));
}

function msgEl(id) { return V && V.inner.querySelector(`.msg[data-id="${id}"]`); }

function scrollBottom(force) {
  if (!V) return;
  if (force) V.stick = true;
  if (V.stick) V.thread.scrollTop = V.thread.scrollHeight;
}

// The thread follows a reply as it is written until the person scrolls up; it follows again once they are
// back at the bottom (or press the arrow). Upward intent is read from the wheel, touch, keys and the
// scrollbar, so text arriving below never pulls them back down.
function followBottom(thread, jump) {
  let touching = false, dragging = false, lastTop = 0;
  const unstick = () => { if (V) V.stick = false; };
  thread.addEventListener('wheel', e => { if (e.deltaY < 0) unstick(); }, { passive: true });
  thread.addEventListener('touchstart', () => { touching = true; }, { passive: true });
  thread.addEventListener('touchend', () => { touching = false; }, { passive: true });
  thread.addEventListener('keydown', e => { if (['PageUp', 'ArrowUp', 'Home'].includes(e.key)) unstick(); });
  thread.addEventListener('mousedown', e => { if (e.target === thread) dragging = true; });  // on its scrollbar
  window.addEventListener('mouseup', () => { dragging = false; });
  thread.addEventListener('scroll', () => {
    if (!V) return;
    const fromBottom = thread.scrollHeight - thread.scrollTop - thread.clientHeight;
    if (fromBottom < 8) V.stick = true;
    else if ((touching || dragging) && thread.scrollTop < lastTop) V.stick = false;
    lastTop = thread.scrollTop;
    jump.hidden = fromBottom < 160;
  }, { passive: true });
}

function upsertMessage(m) {
  const i = S.thread.findIndex(x => x.id === m.id);
  if (i >= 0) S.thread[i] = { ...S.thread[i], ...m };
  else {
    // a new message continues the branch on screen
    S.thread.push(m);
  }
  const old = msgEl(m.id);
  const el = renderMessage(S.thread[i >= 0 ? i : S.thread.length - 1], ctx());
  if (old) old.replaceWith(el); else { V.inner.querySelector('.empty-thread')?.remove(); V.inner.appendChild(el); }
  if (m.status !== 'streaming') enhance(el);
  scrollBottom();
}

function blockUpdate(msgId, index, block) {
  const m = S.thread.find(x => x.id === msgId);
  if (!m) return;
  m.blocks[index] = block;
  const el = msgEl(msgId);
  if (!el) return;
  const body = el.querySelector('.assistant-body');
  body.querySelector('.typing')?.remove();
  const old = body.querySelector(`:scope > [data-index="${index}"]`);
  const ne = renderBlock(block, m, ctx());
  if (!ne) return;
  ne.dataset.index = index;
  if (old) {
    if (old.tagName === 'DETAILS' && ne.tagName === 'DETAILS' && old.open !== ne.open && block.status !== 'waiting') ne.open = old.open;
    old.replaceWith(ne);
  } else body.appendChild(ne);
  scrollBottom();
}

function applyDelta(d) {
  const m = S.thread.find(x => x.id === d.msg_id);
  if (!m) return;
  const b = m.blocks[d.index];
  if (!b) { reloadConversation(); return; }
  const cur = b.text || '';
  if (cur.length >= d.len) return;              // already have it (the snapshot included it)
  if (cur.length + d.text.length !== d.len) { reloadConversation(); return; }  // missed something
  b.text = cur + d.text;
  if (V.raf.has(d.msg_id + ':' + d.index)) return;
  V.raf.set(d.msg_id + ':' + d.index, requestAnimationFrame(() => {
    V && V.raf.delete(d.msg_id + ':' + d.index);
    const el = msgEl(d.msg_id);
    if (!el) return;
    const node = el.querySelector(`.assistant-body > [data-index="${d.index}"]`);
    if (!node) { blockUpdate(d.msg_id, d.index, b); return; }
    if (b.type === 'text') node.innerHTML = mdHTML(b.text, true);
    else if (b.type === 'thinking') { const t = node.querySelector('.thinking-text'); if (t) t.textContent = b.text; }
    scrollBottom();
  }));
}

function imageProgress(d) {
  const m = S.thread.find(x => x.id === d.msg_id);
  if (!m || !m.blocks[d.index]) return;
  m.blocks[d.index].progress = d;
  blockUpdate(d.msg_id, d.index, m.blocks[d.index]);
}

function wireConversationEvents() {
  const mine = d => S.conv && d && d.conv_id === S.conv.id;
  unsub.push(on('msg.new', d => { if (mine(d)) upsertMessage(d.message); }));
  unsub.push(on('msg.block', d => { if (mine(d)) blockUpdate(d.msg_id, d.index, d.block); }));
  unsub.push(on('msg.delta', d => { if (mine(d)) applyDelta(d); }));
  unsub.push(on('image.progress', d => { if (mine(d)) imageProgress(d); }));
  unsub.push(on('msg.done', d => {
    if (!mine(d)) return;
    upsertMessage(d.message);
    const f = msgEl(d.message.id);
    if (f) { const a = f.querySelector('.assistant-actions'); if (a) a.classList.remove('hidden'); }
  }));
  unsub.push(on('turn', d => {
    if (!mine(d)) return;
    S.turn = d.state === 'idle' ? null : d;
    V.composer.setRunning(!!S.turn);
    renderQueue();
  }));
  unsub.push(on('pending', p => {
    if (!mine(p)) return;
    if (!S.pending.find(x => x.id === p.id)) S.pending.push(p);
    refreshBlockFor(p);
  }));
  unsub.push(on('pending.done', d => {
    if (!mine(d)) return;
    const p = S.pending.find(x => x.id === d.id);
    S.pending = S.pending.filter(x => x.id !== d.id);
    if (p) refreshBlockFor(p);
  }));
  unsub.push(on('context', d => {
    if (!mine(d)) return;
    if (d.used) { S.context = { used: d.used, num_ctx: d.num_ctx }; V.composer.setContext(S.context); }
    if (d.cleared) toast(`Cleared ${d.cleared} old tool output${d.cleared > 1 ? 's' : ''} to make room`);
  }));
  unsub.push(on('todos', d => { if (mine(d)) { S.todos = d.todos; renderTodos(); } }));
  unsub.push(on('artifact', d => {
    if (!mine(d)) return;
    const a = d.artifact;
    const i = S.artifacts.findIndex(x => x.id === a.id);
    if (i >= 0) S.artifacts[i] = a; else S.artifacts.push(a);
    changed('artifacts');
    openArtifact(a.id, a.version, true);
  }));
  unsub.push(on('tool.output', d => {
    if (!mine(d)) return;
    const pre = V.inner.querySelector(`[data-live="${d.block_id}"]`);
    if (pre) { pre.textContent += d.text; if (pre.textContent.length > 60000) pre.textContent = pre.textContent.slice(-50000); pre.scrollTop = pre.scrollHeight; scrollBottom(); }
  }));
  unsub.push(on('notice', d => { if (mine(d)) toast(d.text); }));
  unsub.push(on('conv.reload', d => { if (mine(d)) reloadConversation(); }));
  unsub.push(on('conv', d => {
    if (S.conv && d.conversation.id === S.conv.id) {
      S.conv = { ...S.conv, ...d.conversation };
      const t = V.main.querySelector('.tb-title span:not(.icon)');
      if (t) t.textContent = S.conv.title || 'New conversation';
      V.composer.update(S.conv);
    }
  }));
}

function refreshBlockFor(p) {
  for (const m of S.thread) {
    const i = m.blocks.findIndex(b => b.id === p.block_id);
    if (i >= 0) {
      if (S.pending.find(x => x.id === p.id)) m.blocks[i] = { ...m.blocks[i], status: 'waiting' };
      blockUpdate(m.id, i, m.blocks[i]);
      return;
    }
  }
  // the block is inside a helper agent or not rendered yet: show the card at the end of the thread
  const existing = V.inner.querySelector(`[data-pending="${p.id}"]`);
  if (existing) { if (!S.pending.find(x => x.id === p.id)) existing.remove(); return; }
  if (S.pending.find(x => x.id === p.id)) { V.inner.appendChild(pendingCard(p, ctx())); scrollBottom(); }
}

function renderTodos() {
  if (!V) return;
  clear(V.todoEl);
  const todos = S.todos || [];
  if (!todos.length) return;
  const done = todos.filter(t => t.status === 'completed').length;
  const allDone = done === todos.length;
  const d = h('details', { class: 'todo-card', open: !allDone || null },
    h('summary', null, icon('list', 15), ` Tasks · ${done} of ${todos.length} done`),
    h('ul', { class: 'todos' }, todos.map(t => h('li', { class: `todo-${t.status}` }, h('span', { class: 'todo-box' }), t.content))));
  V.todoEl.appendChild(d);
}

function renderQueue() {
  if (!V) return;
  clear(V.queueEl);
  const t = S.turn;
  if (!t) return;
  if (t.queue_position) V.queueEl.appendChild(h('div', { class: 'queue-note' }, icon('cpu', 14), ` Waiting for the GPU (${t.queue_position} ahead)…`));
  for (const q of t.queued || []) V.queueEl.appendChild(h('div', { class: 'queued' }, h('span', { class: 'queued-label' }, 'Queued'), h('span', null, q)));
}

async function rewindDialog(m) {
  const m2 = modal('Rewind to before this message', {});
  const text = m.blocks.filter(b => b.type === 'text').map(b => b.text).join('\n\n');
  m2.body.append(
    h('p', { class: 'dialog-text' }, 'The conversation goes back to just before this message, and its text returns to the message box. Later messages stay available as another version.'),
    h('div', { class: 'dialog-actions' },
      h('button', { class: 'btn', type: 'button', onclick: () => go(false) }, 'Conversation only'),
      h('button', { class: 'btn btn-primary', type: 'button', onclick: () => go(true) }, 'Conversation and files')));
  async function go(files) {
    m2.close();
    try {
      const r = await post(`/api/conversations/${S.conv.id}/rewind`, { message_id: m.id, files, conversation: true });
      if (r.files) toast(`Files restored: ${r.files.restored.length} changed, ${r.files.removed.length} removed`);
      setTimeout(() => V && V.composer.setText(text), 300);
    } catch (e) { errorToast(e); }
  }
}

function askTrust(folder) {
  const m = modal('Trust this folder?', { sticky: false });
  m.body.append(
    h('p', { class: 'dialog-text' }, `baabaa will work in ${folder}.`),
    h('p', { class: 'muted small' }, 'Trusting it lets baabaa read the folder’s own instructions (BAABAA.md or AGENTS.md). Only trust folders whose contents you know: instructions in a folder can steer the model.'),
    h('div', { class: 'dialog-actions' },
      h('button', { class: 'btn', type: 'button', onclick: () => m.close() }, 'Not now'),
      h('button', { class: 'btn btn-primary', type: 'button', onclick: async () => {
        try { await post('/api/folders/trust', { path: folder }); S.trusted = true; m.close(); } catch (e) { errorToast(e); }
      } }, 'Trust folder')));
}

// Composer -------------------------------------------------------------------------------------------------
export function createComposer(opts) {
  const draft = opts.draft || {};
  let running = false;
  let research = false;
  let imageMode = false, imageShape = 'square', imageEdit = false;
  let atts = [];
  const ta = h('textarea', { class: 'composer-input', rows: 1, placeholder: opts.placeholder, 'aria-label': 'Message' });
  const attRow = h('div', { class: 'att-row composer-atts', hidden: true });
  const fileInput = h('input', { type: 'file', multiple: true, hidden: true });
  const sendBtn = h('button', { class: 'send-btn', type: 'button', title: 'Send (Enter)', disabled: true }, icon('send', 18));
  const left = h('div', { class: 'composer-left' });
  const right = h('div', { class: 'composer-right' });
  const el = h('div', { class: 'composer' + (opts.home ? ' composer-home' : '') }, attRow,
    h('div', { class: 'composer-box' }, ta), h('div', { class: 'composer-bar' }, left, right), fileInput);
  const conv = () => S.conv;
  const hasFolder = () => opts.home ? !!draft.folder : !!(conv() && conv().folder);

  // slash commands: a small menu while typing "/name"
  let commands = null, cmdSel = 0;
  const cmdMenu = h('div', { class: 'cmd-menu', hidden: true });
  el.appendChild(cmdMenu);
  async function updateCommands() {
    const v = ta.value;
    const m = /^\/([\w.:-]*)$/.exec(v);
    if (!m) { cmdMenu.hidden = true; return; }
    if (commands === null) {
      commands = [];
      try { commands = (await get(`/api/extend${conv() ? `?conv_id=${conv().id}` : ''}`)).commands; } catch { commands = []; }
    }
    const hits = commands.filter(c => c.name.toLowerCase().startsWith(m[1].toLowerCase()));
    clear(cmdMenu);
    cmdMenu.hidden = !hits.length;
    cmdSel = Math.min(cmdSel, Math.max(0, hits.length - 1));
    hits.slice(0, 12).forEach((c, i) => cmdMenu.appendChild(h('button', { class: 'cmd-item' + (i === cmdSel ? ' sel' : ''), type: 'button',
      onmousedown: e => { e.preventDefault(); pickCommand(c.name); } }, h('span', { class: 'mono' }, '/' + c.name), h('span', { class: 'muted small' }, c.description))));
  }
  function pickCommand(name) { ta.value = `/${name} `; cmdMenu.hidden = true; autosize(); ta.focus(); }
  ta.addEventListener('input', updateCommands);
  ta.addEventListener('keydown', e => {
    if (cmdMenu.hidden) return;
    const items = cmdMenu.querySelectorAll('.cmd-item');
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      cmdSel = (cmdSel + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      items.forEach((b, i) => b.classList.toggle('sel', i === cmdSel));
    } else if ((e.key === 'Tab' || e.key === 'Enter') && items[cmdSel] && !e.shiftKey) {
      e.preventDefault();
      e.stopImmediatePropagation();
      pickCommand(items[cmdSel].querySelector('.mono').textContent.slice(1));
    } else if (e.key === 'Escape') { cmdMenu.hidden = true; }
  }, true);

  function autosize() {
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, Math.round(window.innerHeight * 0.4)) + 'px';
    sendBtn.disabled = !running && !ta.value.trim() && !atts.length;
  }
  ta.addEventListener('input', autosize);
  ta.addEventListener('keydown', e => {
    const sendKey = (S.me.settings.send_key || 'enter');
    const isSend = e.key === 'Enter' && !e.isComposing && (sendKey === 'enter' ? !e.shiftKey : (e.ctrlKey || e.metaKey));
    if (isSend) { e.preventDefault(); send(); }
    else if (e.key === 'Escape' && running) { e.preventDefault(); stop(); }
    else if (e.key === 'Tab' && e.shiftKey && hasFolder()) { e.preventDefault(); cycleMode(); }
  });
  ta.addEventListener('paste', e => {
    const files = Array.from(e.clipboardData?.files || []);
    if (files.length) { e.preventDefault(); addFiles(files); }
  });
  el.addEventListener('dragover', e => { e.preventDefault(); el.classList.add('drop'); });
  el.addEventListener('dragleave', () => el.classList.remove('drop'));
  el.addEventListener('drop', e => { e.preventDefault(); el.classList.remove('drop'); addFiles(Array.from(e.dataTransfer.files || [])); });
  fileInput.addEventListener('change', () => { addFiles(Array.from(fileInput.files)); fileInput.value = ''; });

  async function addFiles(files) {
    for (const f of files) {
      const item = { name: f.name, progress: 0, id: null, kind: f.type.startsWith('image/') ? 'image' : 'file' };
      atts.push(item);
      renderAtts();
      try {
        const a = await upload(f, conv() ? conv().id : null, p => { item.progress = p; renderAtts(); });
        Object.assign(item, a, { progress: 1 });
        if (a.kind === 'binary') toast(`${f.name}: no text could be read from this file`, 'warn');
        else if (a.kind === 'document' && !a.preview) toast(`${f.name}: no text layer found (a scanned document?)`, 'warn');
      } catch (e) {
        errorToast(e);
        atts = atts.filter(x => x !== item);
      }
      renderAtts();
    }
  }

  function renderAtts() {
    clear(attRow);
    attRow.hidden = !atts.length;
    for (const a of atts) {
      const chip = a.id ? attChip(a) : h('span', { class: 'att uploading' }, icon('clip', 15), h('span', { class: 'att-name' }, a.name), ` ${Math.round(a.progress * 100)}%`);
      attRow.appendChild(h('span', { class: 'att-wrap' }, chip,
        h('button', { class: 'att-remove', type: 'button', title: 'Remove', onclick: () => { atts = atts.filter(x => x !== a); renderAtts(); } }, icon('x', 12))));
    }
    autosize();
  }

  async function send() {
    const text = ta.value;
    if (!text.trim() && !atts.length) return;
    if (atts.some(a => !a.id)) { toast('Wait for the upload to finish'); return; }
    if (!S.models.length && !imageMode) {  // nothing could answer: say so here instead of failing the message
      toast(S.me.role === 'owner' ? 'No model is ready yet. Test and approve one in Settings > Models first.'
        : 'No model is ready yet. The owner needs to approve one in Settings > Models.', 'error', 7000);
      return;
    }
    const sending = atts.slice();
    ta.value = ''; atts = []; renderAtts(); autosize();
    const extra = imageMode ? { image: { shape: imageShape, edit: imageEdit } } : research ? { research: true } : {};
    if (imageMode) imageEdit = false;
    try { await opts.onSend(text, sending, extra); }
    catch (e) { errorToast(e); ta.value = text; atts = sending; renderAtts(); autosize(); }
  }

  async function stop() {
    if (!conv()) return;
    try { await post(`/api/conversations/${conv().id}/stop`, {}); } catch (e) { errorToast(e); }
  }

  sendBtn.addEventListener('click', () => (running && !ta.value.trim() && !atts.length) ? stop() : send());

  // controls ---------------------------------------------------------------------------------------------
  const plus = h('button', { class: 'icon-btn composer-btn', type: 'button', title: 'Add files or a folder' }, icon('plus', 18));
  plus.addEventListener('click', () => menu(plus, [
    { label: 'Upload files', icon: 'clip', onClick: () => fileInput.click() },
    { label: hasFolder() ? 'Change folder' : 'Work in a folder', icon: 'folder', onClick: () => pickFolder() },
    opts.home ? { label: draft.incognito ? 'Incognito: on' : 'Incognito chat', icon: 'ghost', checked: draft.incognito, onClick: () => { draft.incognito = !draft.incognito; renderControls(); } } : null,
  ].filter(Boolean), { above: !opts.home }));

  const modeBtn = h('button', { class: 'pill', type: 'button' });
  modeBtn.addEventListener('click', () => menu(modeBtn, S.modes.map(m => ({
    label: m.label, icon: MODE_ICON[m.id], hint: MODE_HINT[m.id], checked: currentMode() === m.id, onClick: () => setMode(m.id),
  })), { above: !opts.home }));
  const thinkBtn = h('button', { class: 'pill', type: 'button', title: 'Let the model think before answering (slower, often better)' });
  thinkBtn.addEventListener('click', () => setThink(!currentThink()));
  const researchBtn = h('button', { class: 'pill', type: 'button', title: 'Research: search the web, read several sources and write a report with citations' });
  researchBtn.addEventListener('click', () => {
    research = !research;
    if (research) imageMode = false;
    draft.research = research;
    draft.image = null;
    placeholder();
    renderControls();
    ta.focus();
  });
  // dictation: record here, transcribe on this computer's GPU
  const micBtn = h('button', { class: 'icon-btn composer-btn mic-btn', type: 'button', title: 'Dictate (speech to text on this computer)' }, icon('mic', 18));
  let rec = null, recTimer = null;
  micBtn.addEventListener('click', async () => {
    const R = await import('../recorder.js');
    if (rec) {
      const r = rec; rec = null; clearInterval(recTimer);
      micBtn.classList.remove('recording'); micBtn.title = 'Transcribing…'; micBtn.disabled = true;
      try {
        const wavBlob = r.stop();
        const res = await api('POST', `/api/transcribe?lang=${encodeURIComponent((navigator.language || '').slice(0, 2))}`, wavBlob, { contentType: 'audio/wav' });
        const at = ta.selectionStart ?? ta.value.length;
        const sep = at > 0 && !/\s$/.test(ta.value.slice(0, at)) ? ' ' : '';
        ta.value = ta.value.slice(0, at) + sep + res.text + ta.value.slice(at);
        autosize(); ta.focus();
      } catch (e) { errorToast(e); }
      finally { micBtn.disabled = false; micBtn.title = 'Dictate (speech to text on this computer)'; clear(micBtn).appendChild(icon('mic', 18)); }
      return;
    }
    if (!R.supported()) { toast('Dictation needs a secure (HTTPS) page and a browser with microphone access', 'warn'); return; }
    try {
      const t0 = Date.now();
      rec = await R.start(level => micBtn.style.setProperty('--level', Math.min(1, level * 3).toFixed(2)));
      micBtn.classList.add('recording');
      micBtn.title = 'Stop and transcribe';
      recTimer = setInterval(() => { clear(micBtn).append(icon('stop', 14), h('span', { class: 'rec-time' }, `${Math.round((Date.now() - t0) / 1000)}s`)); }, 500);
    } catch (e) { rec = null; errorToast(e.name === 'NotAllowedError' ? new Error('The browser did not allow the microphone') : e); }
  });
  ta.addEventListener('keydown', e => { if (e.key === 'Escape' && rec) { e.preventDefault(); rec.cancel(); rec = null; clearInterval(recTimer); micBtn.classList.remove('recording'); clear(micBtn).appendChild(icon('mic', 18)); } });

  const imageBtn = h('button', { class: 'pill', type: 'button', title: 'Make an image from your description (attach an image to change it)' });
  imageBtn.addEventListener('click', () => { setImageMode(!imageMode, imageShape, false); ta.focus(); });
  const shapeBtn = h('button', { class: 'pill', type: 'button', title: 'Image shape' });
  shapeBtn.addEventListener('click', () => menu(shapeBtn, [['square', 'Square'], ['portrait', 'Portrait'], ['landscape', 'Landscape'], ['wide', 'Wide']]
    .map(([v, l]) => ({ label: l, checked: imageShape === v, onClick: () => { imageShape = v; if (draft.image) draft.image.shape = v; renderControls(); } })), { above: !opts.home }));
  // With more than one image model: which one makes this person's images (their setting, else the default)
  const imageModelName = () => {
    const names = (S.imageModels || []).map(m => m.name);
    const mine = S.me && S.me.settings.image_model;
    return names.includes(mine) ? mine : (S.imageDefault || names[0] || null);
  };
  const imageModelBtn = h('button', { class: 'pill', type: 'button', title: 'Image model' });
  imageModelBtn.addEventListener('click', () => menu(imageModelBtn, (S.imageModels || []).map(m => ({
    label: m.name,
    hint: [(m.capabilities || []).includes('edit') ? 'edits images' : null,
      m.seconds_1024 ? `about ${Math.round(m.seconds_1024)} s an image` : m.seconds_512 ? `about ${Math.round(m.seconds_512)} s at 512 px` : null]
      .filter(Boolean).join(' · ') || null,
    checked: m.name === imageModelName(),
    onClick: async () => {
      try { const d = await patch('/api/me', { settings: { image_model: m.name } }); S.me = d.account; renderControls(); } catch (e) { errorToast(e); }
    },
  })), { above: !opts.home }));
  function setImageMode(on, shape, edit) {
    imageMode = on;
    imageShape = shape || imageShape;
    imageEdit = !!edit;
    if (on) research = false;
    draft.research = research;
    draft.image = on ? { shape: imageShape, edit: imageEdit } : null;
    placeholder();
    renderControls();
  }
  function placeholder() {
    ta.placeholder = imageMode ? (imageEdit ? 'Describe the change to the image…' : 'Describe the image…') : research ? 'What should baabaa research?' : opts.placeholder;
  }
  const netBtn = h('button', { class: 'pill', type: 'button', title: 'Network access for commands' });
  netBtn.addEventListener('click', () => setNetwork(!currentNetwork()));
  const folderChip = h('button', { class: 'pill', type: 'button', title: 'Working folder' });
  folderChip.addEventListener('click', () => pickFolder());
  const modelBtn = h('button', { class: 'model-btn', type: 'button', title: 'Model' });
  modelBtn.addEventListener('click', () => modelMenu(modelBtn));
  const ring = h('span', { class: 'ctx-ring', title: 'Context used' });
  ring.addEventListener('click', () => {
    if (!conv()) return;
    menu(ring, [{ label: 'Compact now', icon: 'compress', onClick: async () => { try { toast('Summarizing…'); await post(`/api/conversations/${conv().id}/compact`, {}); } catch (e) { errorToast(e); } } }], { above: true, alignRight: true });
  });

  function currentMode() { return opts.home ? (draft.mode || S.me.settings.default_mode || (draft.folder ? 'accept_edits' : 'manual')) : conv().mode; }
  function currentThink() {
    const v = opts.home ? (draft.think ?? S.me.settings.think) : (conv().settings || {}).think;
    return v === true || v === 'on' || v === 'low' || v === 'medium' || v === 'high';
  }
  function currentNetwork() { return !opts.home && !!(conv().settings || {}).network; }
  function currentModel() { return opts.home ? (draft.model || S.me.settings.default_model || S.defaultModel) : (conv().model || S.defaultModel); }
  function modelInfo(name) { return S.models.find(m => m.name === name); }

  async function setMode(mode) {
    if (opts.home) { draft.mode = mode; renderControls(); return; }
    try { const d = await patch(`/api/conversations/${conv().id}`, { mode }); S.conv = { ...S.conv, ...d.conversation }; renderControls(); } catch (e) { errorToast(e); }
  }
  function cycleMode() {
    const order = S.modes.map(m => m.id);
    const next = order[(order.indexOf(currentMode()) + 1) % order.length];
    setMode(next);
    toast(`Mode: ${S.modes.find(m => m.id === next).label}`, 'info', 1200);
  }
  async function setThink(on) {
    if (opts.home) { draft.think = on; renderControls(); return; }
    try { const d = await patch(`/api/conversations/${conv().id}`, { settings: { think: on } }); S.conv = { ...S.conv, ...d.conversation }; renderControls(); } catch (e) { errorToast(e); }
  }
  async function setNetwork(on) {
    if (on && !await confirmDialog('Allow network access?', 'Commands in this conversation will be able to reach the internet (web ports only). Local network devices, this computer’s other services and the model server stay unreachable.', 'Allow')) return;
    try { const d = await patch(`/api/conversations/${conv().id}`, { settings: { network: on } }); S.conv = { ...S.conv, ...d.conversation }; renderControls(); } catch (e) { errorToast(e); }
  }
  async function setModel(name) {
    if (opts.home) { draft.model = name; renderControls(); return; }
    try { const d = await patch(`/api/conversations/${conv().id}`, { model: name }); S.conv = { ...S.conv, ...d.conversation }; renderControls(); } catch (e) { errorToast(e); }
  }
  function modelMenu(anchor) {
    const items = S.models.map(m => ({
      label: m.name, checked: m.name === currentModel(),
      hint: [m.num_ctx ? `${ctxk(m.num_ctx)} context` : null, m.tokens_per_s ? `${Math.round(m.tokens_per_s)} tok/s` : null,
        (m.capabilities || []).includes('vision') ? 'images' : null, toolNote(m)].filter(Boolean).join(' · '),
      onClick: () => setModel(m.name),
    }));
    if (!items.length) items.push({ label: 'No approved models yet', disabled: true });
    if (S.me.role === 'owner') items.push('sep', { label: 'Manage models…', icon: 'cpu', onClick: () => { location.hash = '#/settings/models'; } });
    menu(anchor, items, { above: !opts.home, alignRight: true });
  }

  async function pickFolder() {
    const path = await folderPicker(opts.home ? draft.folder : conv() && conv().folder);
    if (!path) return;
    if (opts.home) { draft.folder = path; if (!draft.mode) draft.mode = null; renderControls(); return; }
    try { const d = await patch(`/api/conversations/${conv().id}`, { folder: path }); S.conv = { ...S.conv, ...d.conversation }; renderControls(); toast(`Working in ${path}`); } catch (e) { errorToast(e); }
  }

  function renderControls() {
    clear(left); clear(right);
    left.appendChild(plus);
    if (hasFolder()) {
      const folder = opts.home ? draft.folder : conv().folder;
      clear(folderChip).append(icon('folder', 14), h('span', null, folder.split('/').filter(Boolean).pop()));
      const mode = currentMode();
      clear(modeBtn).append(icon(MODE_ICON[mode], 14), h('span', null, (S.modes.find(m => m.id === mode) || {}).label || mode));
      modeBtn.className = `pill mode-${mode}`;
      modeBtn.title = `${MODE_HINT[mode]} (Shift+Tab to change)`;
      left.append(folderChip, modeBtn);
      if (!opts.home) {
        clear(netBtn).append(icon('globe', 14), h('span', null, currentNetwork() ? 'Network on' : 'Network off'));
        netBtn.classList.toggle('on', currentNetwork());
        left.appendChild(netBtn);
      }
    }
    const mi = modelInfo(currentModel());
    if (!mi || (mi.capabilities || []).includes('thinking')) {
      clear(thinkBtn).append(icon('brain', 14), h('span', null, 'Think'));
      thinkBtn.classList.toggle('on', currentThink());
      left.appendChild(thinkBtn);
    }
    clear(researchBtn).append(icon('globe', 14), h('span', null, 'Research'));
    researchBtn.classList.toggle('on', research);
    left.appendChild(researchBtn);
    if ((S.imageModels || []).length) {
      clear(imageBtn).append(icon('image', 14), h('span', null, 'Image'));
      imageBtn.classList.toggle('on', imageMode);
      left.appendChild(imageBtn);
      if (imageMode) {
        clear(shapeBtn).append(h('span', null, { square: 'Square', portrait: 'Portrait', landscape: 'Landscape', wide: 'Wide' }[imageShape]), icon('chevron', 12));
        left.appendChild(shapeBtn);
        if (S.imageModels.length > 1) {
          clear(imageModelBtn).append(h('span', null, imageModelName()), icon('chevron', 12));
          left.appendChild(imageModelBtn);
        }
      }
    }
    if (opts.home && draft.incognito) left.appendChild(h('span', { class: 'pill on', title: 'Not saved to your history' }, icon('ghost', 14), 'Incognito'));
    clear(modelBtn).append(h('span', null, currentModel() || 'No model yet'), icon('chevron', 14));
    if (S.sttModel) right.appendChild(micBtn);
    right.append(modelBtn);
    if (!opts.home) right.appendChild(ring);
    right.appendChild(sendBtn);
  }

  renderControls();
  autosize();
  const kept = takeKeptDraft();  // the message being written when an update reloaded the page
  if (kept) setTimeout(() => { ta.value = kept; autosize(); ta.dispatchEvent(new Event('input')); }, 0);
  return {
    el,
    focus: () => setTimeout(() => ta.focus({ preventScroll: true }), 0),
    setText: t => { ta.value = t; autosize(); ta.focus(); },
    setImageMode: (on, shape, edit) => setImageMode(on, shape, edit),
    pickFolder,
    update: () => renderControls(),
    getDraft: () => ({ text: ta.value, atts }),
    restoreDraft: d => { if (d) { ta.value = d.text; atts = d.atts || []; renderAtts(); autosize(); } },
    setRunning: r => {
      running = r;
      clear(sendBtn).appendChild(icon(r ? 'stop' : 'send', 18));
      sendBtn.classList.toggle('stop', r);
      sendBtn.title = r ? 'Stop (Esc) — or type to queue a message' : 'Send (Enter)';
      autosize();
    },
    setContext: c => {
      if (!c || !c.num_ctx) { ring.hidden = true; return; }
      ring.hidden = false;
      const used = c.used || 0, frac = Math.min(1, used / c.num_ctx);
      const R = 7, L = 2 * Math.PI * R;
      ring.innerHTML = `<svg width="20" height="20" viewBox="0 0 20 20"><circle cx="10" cy="10" r="${R}" fill="none" stroke="currentColor" stroke-opacity=".2" stroke-width="2.4"/><circle cx="10" cy="10" r="${R}" fill="none" stroke="currentColor" stroke-width="2.4" stroke-dasharray="${(frac * L).toFixed(1)} ${L.toFixed(1)}" transform="rotate(-90 10 10)" stroke-linecap="round"/></svg>`;
      ring.title = `${tokens(used)} of ${ctxk(c.num_ctx)} tokens of context used`;
      ring.classList.toggle('warn', frac > 0.8);
    },
  };
}

// Folder picker ---------------------------------------------------------------------------------------------
function folderPicker(start) {
  return new Promise(resolve => {
    let chosen = null;
    const m = modal('Choose a working folder', { wide: true, onClose: () => resolve(chosen) });
    const pathEl = h('input', { class: 'input mono', placeholder: '/path/to/folder' });
    const list = h('div', { class: 'folder-list' });
    const note = h('div', { class: 'muted small' });
    m.body.append(h('div', { class: 'folder-path' }, pathEl, h('button', { class: 'btn', type: 'button', onclick: () => load(pathEl.value) }, 'Go')), note, list,
      h('div', { class: 'dialog-actions' },
        h('button', { class: 'btn', type: 'button', onclick: () => m.close() }, 'Cancel'),
        h('button', { class: 'btn btn-primary', type: 'button', onclick: () => { chosen = pathEl.value.trim() || null; m.close(); } }, 'Use this folder')));
    pathEl.addEventListener('keydown', e => { if (e.key === 'Enter') load(pathEl.value); });
    async function load(path) {
      try {
        const d = await get(`/api/folders?path=${encodeURIComponent(path || '')}`);
        pathEl.value = d.path;
        clear(list);
        note.textContent = d.path ? (d.trusted ? 'Trusted folder' : '') : 'Folders you can use:';
        if (d.parent) list.appendChild(h('button', { class: 'folder-item', type: 'button', onclick: () => load(d.parent) }, icon('chevronLeft', 15), '..'));
        for (const f of d.folders) list.appendChild(h('button', { class: 'folder-item', type: 'button', onclick: () => load(f.path) }, icon('folder', 15), f.name));
        if (!d.folders.length) list.appendChild(h('div', { class: 'muted small' }, 'No subfolders.'));
      } catch (e) { errorToast(e); }
    }
    load(start || '');
  });
}

// Artifacts panel ----------------------------------------------------------------------------------------------
async function openArtifact(aid, version, auto = false) {
  const { showArtifact } = await import('./panel.js');
  showArtifact(aid, version, auto);
}
async function togglePanelList() {
  const { showArtifactList } = await import('./panel.js');
  showArtifactList();
}
function closePanel() { import('./panel.js').then(p => p.closePanel()); }

// Images --------------------------------------------------------------------------------------------------------
async function exportMessagePng(m) {
  const el = msgEl(m.id);
  if (!el) return;
  const { nodeToPng } = await import('../png.js');
  try { await nodeToPng(el, `${(S.conv.title || 'message').replace(/[^\w-]+/g, '-')}.png`); } catch (e) { errorToast(e); }
}
async function exportThreadPng() {
  if (!V) return;
  const { nodeToPng } = await import('../png.js');
  try { await nodeToPng(V.inner, `${(S.conv.title || 'conversation').replace(/[^\w-]+/g, '-')}.png`); } catch (e) { errorToast(e); }
}
