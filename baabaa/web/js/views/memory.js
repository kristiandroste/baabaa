// Settings > Memory: what baabaa remembers across conversations, and whether it may search earlier chats.
import { S } from '../app.js';
import { get, post, patch, del, on } from '../api.js';
import { h, clear, icon, ago } from '../dom.js';
import { errorToast, switchEl, promptDialog, confirmDialog } from '../ui.js';

export function renderMemory(pane, { saveMe }) {
  const s = S.me.settings;
  const list = h('div', { class: 'memory-list' });
  const input = h('input', { class: 'input', placeholder: 'Something baabaa should remember, e.g. “I work in a neuroscience lab.”', maxlength: 500 });
  const add = async () => {
    const text = input.value.trim();
    if (!text) return;
    try { await post('/api/memory', { text }); input.value = ''; load(); } catch (e) { errorToast(e); }
  };
  input.addEventListener('keydown', e => { if (e.key === 'Enter') add(); });
  pane.append(
    h('section', { class: 'set-section' }, h('h3', null, 'Memory'),
      h('div', { class: 'set-row' }, h('div', { class: 'set-label' }, h('span', null, 'Remember things across conversations'),
        h('span', { class: 'muted small' }, 'Saved memories go into every new conversation, and baabaa can save new ones when you ask it to remember something. Projects keep their own memories.')),
      switchEl(s.memory !== false, v => saveMe({ settings: { memory: v } }))),
      h('div', { class: 'set-row' }, h('div', { class: 'set-label' }, h('span', null, 'Search earlier conversations'),
        h('span', { class: 'muted small' }, 'baabaa may look through your earlier chats when you refer to them. Incognito chats are never searched and never use memory.')),
      switchEl(s.chat_search !== false, v => saveMe({ settings: { chat_search: v } })))),
    h('section', { class: 'set-section' }, h('h3', null, 'Saved memories'),
      h('div', { class: 'rule-add' }, input, h('button', { class: 'btn', type: 'button', onclick: add }, icon('plus', 15), ' Add')),
      list));

  async function load() {
    let d;
    try { d = await get('/api/memory'); } catch (e) { errorToast(e); return; }
    clear(list);
    if (!d.memories.length) { list.appendChild(h('p', { class: 'muted small' }, 'Nothing saved yet.')); return; }
    const groups = new Map();
    for (const m of d.memories) {
      const key = m.project_name ? `Project: ${m.project_name}` : 'Everywhere';
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(m);
    }
    for (const [title, items] of groups) {
      list.appendChild(h('h4', null, title));
      for (const m of items) {
        list.appendChild(h('div', { class: 'memory-row' },
          h('div', { class: 'memory-text' }, m.text,
            h('span', { class: 'muted small' }, ` · ${m.source === 'model' ? 'saved by baabaa' : 'added by you'} ${ago(m.created_ms)}`)),
          h('button', { class: 'icon-btn tiny', type: 'button', title: 'Edit', onclick: async () => {
            const t = await promptDialog('Edit memory', 'Memory', m.text);
            if (t) try { await patch(`/api/memory/${m.id}`, { text: t }); load(); } catch (e) { errorToast(e); }
          } }, icon('edit', 14)),
          h('button', { class: 'icon-btn tiny', type: 'button', title: 'Forget', onclick: async () => {
            if (!await confirmDialog('Forget this?', m.text, 'Forget', true)) return;
            try { await del(`/api/memory/${m.id}`); load(); } catch (e) { errorToast(e); }
          } }, icon('trash', 14))));
      }
    }
  }
  load();
  const off = on('memory', () => { if (pane.isConnected) load(); else off(); });
}
