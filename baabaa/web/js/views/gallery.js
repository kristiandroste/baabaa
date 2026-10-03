// All artifacts across conversations.
import { S, toggleSidebar } from '../app.js';
import { get } from '../api.js';
import { h, clear, icon, ago } from '../dom.js';
import { errorToast } from '../ui.js';
import { kindLabel } from './blocks.js';

export async function renderGallery(main) {
  S.conv = null;
  clear(main);
  const grid = h('div', { class: 'gallery' });
  main.append(h('header', { class: 'topbar' }, h('div', { class: 'tb-left' },
    h('button', { class: 'icon-btn tb-sidebar', type: 'button', title: 'Sidebar (Ctrl+.)', onclick: () => toggleSidebar() }, icon('sidebar')),
    h('span', { class: 'tb-crumb' }, 'Artifacts'))),
    h('div', { class: 'page-wrap' }, h('div', { class: 'page' }, grid)));
  try {
    const d = await get('/api/artifacts');
    if (!d.artifacts.length) grid.appendChild(h('p', { class: 'muted' }, 'Artifacts appear here when baabaa makes a web page, document, diagram or code file for you.'));
    for (const a of d.artifacts) {
      grid.appendChild(h('a', { class: 'gallery-card', href: `#/c/${a.conv_id}`, onclick: () => setTimeout(() => import('./panel.js').then(p => p.showArtifact(a.id)), 400) },
        h('div', { class: 'gallery-kind' }, icon(a.kind === 'html' ? 'globe' : a.kind === 'code' ? 'code' : a.kind === 'slides' ? 'panel' : 'file', 20)),
        h('strong', null, a.title), h('span', { class: 'muted small' }, `${kindLabel(a.kind)} · v${a.version} · ${ago(a.updated_ms)}`)));
    }
  } catch (e) { errorToast(e); }
}
