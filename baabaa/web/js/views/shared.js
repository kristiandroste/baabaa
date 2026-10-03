// Sharing between the accounts on this baabaa: a read-only copy of a conversation, which the other person
// can read and continue in their own history.
import { S, toggleSidebar, loadConversations } from '../app.js';
import { get, post, del } from '../api.js';
import { h, clear, icon, ago, avatar } from '../dom.js';
import { toast, errorToast, modal, confirmDialog } from '../ui.js';
import { renderMessage, enhance, copyCodeHandler } from './blocks.js';

function topbar(...left) {
  return h('header', { class: 'topbar' }, h('div', { class: 'tb-left' },
    h('button', { class: 'icon-btn tb-sidebar', type: 'button', title: 'Sidebar (Ctrl+.)', onclick: () => toggleSidebar() }, icon('sidebar')), ...left));
}

export async function shareDialog(conv) {
  let people = [];
  try { people = (await get('/api/profiles')).profiles.filter(p => p.id !== S.me.id); } catch (e) { errorToast(e); return; }
  const m = modal('Share with…');
  const chosen = new Set();
  const everyone = h('input', { type: 'checkbox' });
  const list = h('div', { class: 'share-people' }, people.map(p => {
    const cb = h('input', { type: 'checkbox', onchange: e => { e.target.checked ? chosen.add(p.id) : chosen.delete(p.id); } });
    return h('label', { class: 'share-person' }, cb, avatar(p, 24), p.display_name);
  }));
  everyone.addEventListener('change', () => { list.classList.toggle('disabled', everyone.checked); });
  m.body.append(
    h('p', { class: 'muted small' }, `“${conv.title || 'Untitled'}” as it is now: its messages and artifacts, read-only. Later messages are not shared. Attachments are shared by name only.`),
    people.length ? list : h('p', { class: 'muted' }, 'There are no other accounts on this baabaa yet.'),
    h('label', { class: 'share-person' }, everyone, icon('users', 18), 'Everyone on this baabaa'),
    h('div', { class: 'dialog-actions' },
      h('button', { class: 'btn', type: 'button', onclick: () => m.close() }, 'Cancel'),
      h('button', { class: 'btn btn-primary', type: 'button', onclick: async () => {
        const to = everyone.checked ? '*' : [...chosen];
        if (to !== '*' && !to.length) { toast('Choose someone to share with'); return; }
        try { await post(`/api/conversations/${conv.id}/share`, { to }); m.close(); toast('Shared'); } catch (e) { errorToast(e); }
      } }, 'Share')));
}

export async function renderShared(main) {
  S.conv = null;
  clear(main);
  const withMe = h('div', { class: 'share-list' });
  const byMe = h('div', { class: 'share-list' });
  main.append(topbar(h('span', { class: 'tb-crumb' }, 'Shared')), h('div', { class: 'page-wrap' }, h('div', { class: 'page' },
    h('div', { class: 'page-head' }, h('h1', null, 'Shared')),
    h('p', { class: 'muted' }, 'Conversations people on this baabaa shared with you, and the ones you shared. Open one to read it, or continue it as your own copy.'),
    h('h3', null, 'Shared with you'), withMe, h('h3', null, 'Shared by you'), byMe)));
  let d;
  try { d = await get('/api/shares'); } catch (e) { errorToast(e); return; }
  const row = (s, mine) => h('div', { class: 'share-row' },
    h('a', { class: 'share-title', href: `#/s/${s.id}` }, icon('users', 15), h('strong', null, s.title)),
    h('span', { class: 'muted small' }, mine ? `with ${s.to.join(', ')} · ${ago(s.created_ms)}` : `from ${s.from} · ${ago(s.created_ms)}`),
    mine ? h('button', { class: 'btn small btn-danger-soft', type: 'button', onclick: async () => {
      if (!await confirmDialog('Stop sharing?', `${s.title} will no longer be visible to ${s.to.join(', ')}. Copies they made stay theirs.`, 'Stop sharing', true)) return;
      try { await del(`/api/shares/${s.id}`); renderShared(main); } catch (e) { errorToast(e); }
    } }, 'Stop sharing') : null);
  if (!d.shared_with_me.length) withMe.appendChild(h('p', { class: 'muted small' }, 'Nothing yet.'));
  for (const s of d.shared_with_me) withMe.appendChild(row(s, false));
  if (!d.shared_by_me.length) byMe.appendChild(h('p', { class: 'muted small' }, 'Nothing yet. Share a conversation from its ⋯ menu.'));
  for (const s of d.shared_by_me) byMe.appendChild(row(s, true));
}

export async function renderShare(main, id) {
  S.conv = null;
  let d;
  try { d = (await get(`/api/shares/${id}`)).share; } catch (e) { errorToast(e); location.hash = '#/shared'; return; }
  clear(main);
  const inner = h('div', { class: 'thread-inner' });
  const thread = h('div', { class: 'thread' }, inner);
  thread.addEventListener('click', copyCodeHandler);
  const ctx = { readonly: true, branch() {}, retry() {}, edit() {}, rerender() {}, rewind() {}, png() {}, feedback() {}, openArtifact() {} };
  for (const m of d.snapshot.thread || []) {
    const el = renderMessage({ ...m, siblings: [], conv_id: null }, ctx);
    el.querySelectorAll('.msg-actions').forEach(x => x.remove());
    inner.appendChild(el);
    enhance(el);
  }
  const copy = h('button', { class: 'btn btn-primary', type: 'button', onclick: async () => {
    try { const r = await post(`/api/shares/${id}/copy`, {}); loadConversations(); location.hash = `#/c/${r.conversation.id}`; } catch (e) { errorToast(e); }
  } }, icon('fork', 15), ' Continue in my chats');
  main.append(topbar(h('a', { class: 'tb-crumb', href: '#/shared' }, 'Shared'), h('span', { class: 'tb-sep' }, '/'), h('span', { class: 'tb-crumb' }, d.title)),
    h('div', { class: 'share-banner' }, h('span', null, d.mine ? `You shared this with ${d.to.join(', ')}.` : `Shared by ${d.from}. Read-only.`), copy),
    thread);
}
