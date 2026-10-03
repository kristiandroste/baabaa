// Every image made on this account, newest first; each opens its conversation.
import { S, toggleSidebar } from '../app.js';
import { get } from '../api.js';
import { h, clear, icon, ago } from '../dom.js';
import { errorToast } from '../ui.js';

export async function renderImages(main) {
  S.conv = null;
  clear(main);
  const grid = h('div', { class: 'image-grid' });
  const hint = h('p', { class: 'muted' });
  main.append(h('header', { class: 'topbar' }, h('div', { class: 'tb-left' },
    h('button', { class: 'icon-btn tb-sidebar', type: 'button', title: 'Sidebar (Ctrl+.)', onclick: () => toggleSidebar() }, icon('sidebar')),
    h('span', { class: 'tb-crumb' }, 'Images'))),
    h('div', { class: 'page-wrap' }, h('div', { class: 'page' }, h('div', { class: 'page-head' }, h('h1', null, 'Images')), hint, grid)));
  hint.textContent = (S.imageModels || []).length
    ? 'Turn on Image in the message box and describe what you want, or ask for a picture in any conversation. Attach an image to change it.'
    : 'No image model is approved on this computer yet. The owner adds one in Settings → Models.';
  let d;
  try { d = await get('/api/images'); } catch (e) { errorToast(e); return; }
  if (!d.images.length) { grid.appendChild(h('div', { class: 'empty' }, 'No images yet.')); return; }
  for (const im of d.images) {
    const m = im.meta || {};
    grid.appendChild(h('a', { class: 'image-tile', href: im.conv_id ? `#/c/${im.conv_id}` : `/api/attachments/${im.id}`, title: m.prompt || im.name },
      h('img', { src: `/api/attachments/${im.id}`, alt: m.prompt || im.name, loading: 'lazy' }),
      h('span', { class: 'image-tile-cap' }, h('span', null, m.prompt || im.name), h('span', { class: 'muted small' }, `${m.model || ''} · ${ago(im.created_ms)}`))));
  }
}
