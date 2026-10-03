// The artifact panel beside the conversation: preview (sandboxed frame) or code, versions, copy, download.
import { S } from '../app.js';
import { get, downloadFrom } from '../api.js';
import { h, clear, icon, copyText, download, $ } from '../dom.js';
import { toast, errorToast, menu } from '../ui.js';
import { kindLabel } from './blocks.js';

let current = null; // {id, version, tab}

export function closePanel() {
  const p = $('#panel');
  if (p) { p.hidden = true; clear(p); }
  $('#layout')?.classList.remove('panel-open');
  current = null;
}

function openPanel() {
  const p = $('#panel');
  p.hidden = false;
  $('#layout').classList.add('panel-open');
  return p;
}

const EXT = { html: 'html', svg: 'svg', markdown: 'md', slides: 'md', mermaid: 'mmd' };
const EXPORTS = { markdown: [['docx', 'Word document'], ['pdf', 'PDF'], ['pptx', 'PowerPoint (a slide per heading)']],
  slides: [['pptx', 'PowerPoint'], ['pdf', 'PDF'], ['docx', 'Word document']] };
const LANG_EXT = { python: 'py', javascript: 'js', typescript: 'ts', bash: 'sh', shell: 'sh', rust: 'rs', go: 'go', java: 'java',
  c: 'c', cpp: 'cpp', css: 'css', json: 'json', yaml: 'yml', sql: 'sql', ruby: 'rb', php: 'php', kotlin: 'kt', swift: 'swift' };

export async function showArtifact(aid, version, auto = false) {
  if (auto && current && current.id !== aid && current.pinned) return;
  let a;
  try { a = (await get(`/api/artifacts/${aid}${version ? `?version=${version}` : ''}`)).artifact; }
  catch (e) { errorToast(e); return; }
  const p = openPanel();
  const tab = (current && current.id === aid && current.tab) || (a.kind === 'code' ? 'code' : 'preview');
  current = { id: aid, version: a.shown_version, tab, pinned: current && current.pinned };
  clear(p);
  const versions = h('button', { class: 'pill', type: 'button', title: 'Versions', onclick: () => {
    const items = [];
    for (let v = a.version; v >= 1; v--) items.push({ label: `Version ${v}${v === a.version ? ' (latest)' : ''}`, checked: v === a.shown_version, onClick: () => showArtifact(aid, v) });
    menu(versions, items, { alignRight: true });
  } }, `v${a.shown_version}`, icon('chevron', 12));
  const tabs = h('div', { class: 'segmented small' },
    h('button', { type: 'button', class: tab === 'preview' ? 'active' : '', onclick: () => { current.tab = 'preview'; showArtifact(aid, a.shown_version); } }, 'Preview'),
    h('button', { type: 'button', class: tab === 'code' ? 'active' : '', onclick: () => { current.tab = 'code'; showArtifact(aid, a.shown_version); } }, 'Code'));
  const ext = a.kind === 'code' ? (LANG_EXT[(a.language || '').toLowerCase()] || 'txt') : EXT[a.kind] || 'txt';
  const fname = `${(a.title || 'artifact').replace(/[^\w.-]+/g, '-').replace(/^-|-$/g, '') || 'artifact'}.${ext}`;
  const head = h('div', { class: 'panel-head' },
    h('div', { class: 'panel-title' }, h('strong', null, a.title), h('span', { class: 'muted small' }, kindLabel(a.kind) + (a.language ? ` · ${a.language}` : ''))),
    h('div', { class: 'panel-tools' }, a.kind !== 'code' ? tabs : null, versions,
      h('button', { class: 'icon-btn', type: 'button', title: 'Copy', onclick: () => copyText(a.content).then(() => toast('Copied')) }, icon('copy')),
      h('button', { class: 'icon-btn', type: 'button', title: 'Download', onclick: e => {
        const plain = () => download(fname, new Blob([a.content], { type: 'text/plain' }));
        if (!EXPORTS[a.kind]) return plain();
        menu(e.currentTarget, [{ label: `As ${ext === 'md' ? 'Markdown' : ext}`, icon: 'download', onClick: plain },
          ...EXPORTS[a.kind].map(([f, label]) => ({ label: `As ${label}`, icon: 'file', onClick: () =>
            downloadFrom(`/api/artifacts/${aid}/export?format=${f}&version=${a.shown_version}`, fname.replace(/\.md$/, '.' + f)).catch(errorToast) }))],
        { alignRight: true });
      } }, icon('download')),
      a.kind !== 'code' ? h('button', { class: 'icon-btn', type: 'button', title: 'Open in a new tab', onclick: () => window.open(`/artifact-frame/${aid}?v=${a.shown_version}`, '_blank', 'noopener') }, icon('expand')) : null,
      h('button', { class: 'icon-btn', type: 'button', title: 'Close', onclick: closePanel }, icon('x'))));
  let body;
  if (tab === 'preview' && a.kind !== 'code') {
    body = h('iframe', { class: 'artifact-frame', sandbox: 'allow-scripts allow-modals allow-downloads', src: `/artifact-frame/${aid}?v=${a.shown_version}`, title: a.title, referrerpolicy: 'no-referrer' });
  } else {
    const H = globalThis.BaabaaHighlight;
    const lang = a.kind === 'code' ? a.language : a.kind === 'markdown' ? 'markdown' : a.kind === 'svg' ? 'html' : a.kind;
    const code = h('code', { html: H ? H.highlight(a.content, lang || '') : undefined });
    if (!H) code.textContent = a.content;
    body = h('pre', { class: 'artifact-code' }, code);
  }
  p.append(head, h('div', { class: 'panel-body' }, body));
}

export function showArtifactList() {
  const p = $('#panel');
  if (p && !p.hidden && !current) { closePanel(); return; }
  const panel = openPanel();
  current = null;
  clear(panel);
  panel.append(h('div', { class: 'panel-head' }, h('div', { class: 'panel-title' }, h('strong', null, 'Artifacts in this conversation')),
    h('button', { class: 'icon-btn', type: 'button', title: 'Close', onclick: closePanel }, icon('x'))));
  const list = h('div', { class: 'panel-list' });
  for (const a of S.artifacts) {
    list.appendChild(h('button', { class: 'artifact-card', type: 'button', onclick: () => showArtifact(a.id) },
      h('span', { class: 'artifact-card-text' }, h('strong', null, a.title), h('span', { class: 'muted small' }, `${kindLabel(a.kind)} · version ${a.version}`)),
      icon('chevronRight', 16)));
  }
  if (!S.artifacts.length) list.appendChild(h('div', { class: 'muted' }, 'No artifacts yet.'));
  panel.appendChild(list);
}
