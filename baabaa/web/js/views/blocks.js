// Rendering messages and their blocks (text, thinking, tool calls, approvals, questions, plans).
import { S } from '../app.js';
import { post, downloadFrom } from '../api.js';
import { h, clear, icon, esc, duration, tokens, copyText, bytes, busyMark } from '../dom.js';
import { toast, errorToast } from '../ui.js';

// Markdown ----------------------------------------------------------------------------------------
export function mdHTML(text, streaming = false) {
  const M = globalThis.BaabaaMarkdown;
  const H = globalThis.BaabaaHighlight;
  if (!M) return esc(text).replace(/\n/g, '<br>');
  try {
    return M.render(text || '', { streaming, math: true, mermaid: true, highlight: H ? (code, lang) => H.highlight(code, lang) : null });
  } catch (e) {
    console.error('markdown', e);
    return esc(text).replace(/\n/g, '<br>');
  }
}

export function enhance(root) {
  // Mermaid diagrams, once a message is complete.
  const Mm = globalThis.BaabaaMermaid;
  if (!Mm) return;
  for (const div of root.querySelectorAll('.mermaid-block:not(.rendered)')) {
    div.classList.add('rendered');
    try {
      const svg = Mm.render(div.dataset.src || '', {});
      const box = h('div', { class: 'mermaid-svg', html: svg });
      const src = div.querySelector('.mermaid-src');
      const toggle = h('button', { class: 'chip-btn', type: 'button', onclick: () => { src.hidden = !src.hidden; toggle.textContent = src.hidden ? 'Show source' : 'Hide source'; } }, 'Show source');
      if (src) src.hidden = true;
      div.prepend(box, h('div', { class: 'mermaid-tools' }, toggle));
    } catch (e) {
      div.classList.add('mermaid-failed');
      div.prepend(h('div', { class: 'mermaid-error' }, `Diagram could not be drawn: ${e.message}`));
    }
  }
}

// Messages ------------------------------------------------------------------------------------------
export function renderMessage(m, ctx) {
  if (m.role === 'user') return renderUser(m, ctx);
  if (m.role === 'compaction') return renderCompaction(m);
  return renderAssistant(m, ctx);
}

function versionNav(m, ctx) {
  const sibs = m.siblings || [];
  if (sibs.length < 2) return null;
  const i = m.sibling_index || 0;
  return h('span', { class: 'versions' },
    h('button', { class: 'icon-btn tiny', type: 'button', title: 'Previous version', disabled: i === 0 || null, onclick: () => ctx.branch(sibs[i - 1]) }, icon('chevronLeft', 14)),
    h('span', { class: 'versions-label' }, `${i + 1} / ${sibs.length}`),
    h('button', { class: 'icon-btn tiny', type: 'button', title: 'Next version', disabled: i === sibs.length - 1 || null, onclick: () => ctx.branch(sibs[i + 1]) }, icon('chevronRight', 14)));
}

function renderUser(m, ctx) {
  const text = m.blocks.filter(b => b.type === 'text').map(b => b.text).join('\n\n');
  const atts = m.blocks.filter(b => b.type === 'attachment');
  const bubble = h('div', { class: 'user-bubble' });
  if (atts.length) bubble.appendChild(h('div', { class: 'att-row' }, atts.map(attChip)));
  if (text) bubble.appendChild(h('div', { class: 'user-text' }, text));
  const actions = h('div', { class: 'msg-actions' },
    versionNav(m, ctx),
    h('button', { class: 'icon-btn tiny', type: 'button', title: 'Copy', onclick: () => copyText(text).then(() => toast('Copied')) }, icon('copy', 15)),
    h('button', { class: 'icon-btn tiny', type: 'button', title: 'Edit (makes a new version)', onclick: () => editUser(el, m, text, ctx) }, icon('edit', 15)),
    ctx.hasFolder ? h('button', { class: 'icon-btn tiny', type: 'button', title: 'Rewind to here', onclick: () => ctx.rewind(m) }, icon('undo', 15)) : null);
  const el = h('div', { class: 'msg msg-user', id: `m-${m.id}`, dataset: { id: m.id } }, bubble, actions);
  return el;
}

function editUser(el, m, text, ctx) {
  const ta = h('textarea', { class: 'input edit-area', rows: Math.min(12, text.split('\n').length + 1) }, text);
  const box = h('div', { class: 'edit-box' }, ta, h('div', { class: 'dialog-actions' },
    h('button', { class: 'btn', type: 'button', onclick: () => { box.replaceWith(el.firstChild.cloneNode(true)); ctx.rerender(); } }, 'Cancel'),
    h('button', { class: 'btn btn-primary', type: 'button', onclick: () => ctx.edit(m, ta.value) }, 'Send')));
  el.firstChild.replaceWith(box);
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
}

export function attChip(a) {
  if (a.kind === 'image') {
    return h('a', { class: 'att att-image', href: `/api/attachments/${a.id}`, target: '_blank', title: a.name },
      h('img', { src: `/api/attachments/${a.id}`, alt: a.name, loading: 'lazy' }));
  }
  return h('span', { class: 'att', title: a.name }, icon('file', 15), h('span', { class: 'att-name' }, a.name));
}

function renderCompaction(m) {
  const text = m.blocks.map(b => b.text || '').join('\n');
  const details = h('details', { class: 'compaction' },
    h('summary', null, icon('compress', 15), ` Earlier messages were summarized${m.meta && m.meta.reason === 'auto' ? ' automatically' : ''}`),
    h('div', { class: 'md', html: mdHTML(text) }));
  return h('div', { class: 'msg msg-compaction', id: `m-${m.id}` }, details);
}

function renderAssistant(m, ctx) {
  const body = h('div', { class: 'assistant-body' });
  const el = h('div', { class: 'msg msg-assistant' + (m.status === 'streaming' ? ' streaming' : ''), id: `m-${m.id}`, dataset: { id: m.id } }, body);
  m.blocks.forEach((b, i) => { const be = renderBlock(b, m, ctx); if (be) { be.dataset.index = i; body.appendChild(be); } });
  if (m.status === 'streaming') el.appendChild(h('div', { class: 'working', title: 'baabaa is writing' }, busyMark(26)));
  el.appendChild(assistantFooter(m, ctx));
  return el;
}

export function assistantFooter(m, ctx) {
  const done = m.status !== 'streaming';
  const text = m.blocks.filter(b => b.type === 'text').map(b => b.text).join('\n\n');
  const meta = m.meta || {};
  const info = [];
  if (m.model) info.push(m.model);
  if (meta.output_tokens) info.push(`${tokens(meta.output_tokens)} tokens`);
  if (meta.duration_ms) info.push(duration(meta.duration_ms));
  if (m.status === 'stopped') info.push('stopped');
  return h('div', { class: 'msg-actions assistant-actions' + (done ? '' : ' hidden') },
    versionNav(m, ctx),
    h('button', { class: 'icon-btn tiny', type: 'button', title: 'Copy', onclick: () => copyText(text).then(() => toast('Copied')) }, icon('copy', 15)),
    h('button', { class: 'icon-btn tiny', type: 'button', title: 'Retry', onclick: () => ctx.retry(m) }, icon('retry', 15)),
    h('button', { class: 'icon-btn tiny', type: 'button', title: 'Save as image', onclick: () => ctx.png(m) }, icon('image', 15)),
    'speechSynthesis' in window && ctx.speak ? h('button', { class: 'icon-btn tiny', type: 'button', title: 'Read aloud (on-device voice)', onclick: e => ctx.speak(m, e.currentTarget) }, icon('speaker', 15)) : null,
    h('button', { class: 'icon-btn tiny' + (meta.feedback === 1 ? ' active' : ''), type: 'button', title: 'Good reply', onclick: e => ctx.feedback(m, meta.feedback === 1 ? 0 : 1, e.currentTarget) }, icon('up', 15)),
    h('button', { class: 'icon-btn tiny' + (meta.feedback === -1 ? ' active' : ''), type: 'button', title: 'Bad reply', onclick: e => ctx.feedback(m, meta.feedback === -1 ? 0 : -1, e.currentTarget) }, icon('down', 15)),
    h('span', { class: 'msg-info' }, info.join(' · ')));
}

export function renderBlock(b, m, ctx) {
  switch (b.type) {
    case 'text': {
      if (!(b.artifacts || []).length) return h('div', { class: 'md', html: mdHTML(b.text, m.status === 'streaming') });
      // pages the model wrote in its reply, shown as artifacts: a card in place of each page's code
      const parts = h('div', { class: 'md-parts' });
      let pos = 0;
      for (const a of [...b.artifacts].sort((x, y) => x.start - y.start)) {
        const before = b.text.slice(pos, a.start);
        if (before.trim()) parts.appendChild(h('div', { class: 'md', html: mdHTML(before) }));
        parts.appendChild(artifactButton(a, ctx));
        pos = a.end;
      }
      const rest = b.text.slice(pos);
      if (rest.trim()) parts.appendChild(h('div', { class: 'md', html: mdHTML(rest, m.status === 'streaming') }));
      return parts;
    }
    case 'thinking': return renderThinking(b, m);
    case 'tool': return renderTool(b, m, ctx);
    case 'retrieval': return renderTool({ ...b, name: 'search_project', auto: true, status: 'done', args: { query: b.query } }, m, ctx);
    case 'image': return imageBlock(b, m, ctx);
    case 'error': return h('div', { class: 'notice notice-error' }, icon('x', 15), h('span', null, b.text));
    case 'notice': return h('div', { class: 'notice' }, b.text);
    default: return null;
  }
}

function renderThinking(b, m) {
  const live = m.status === 'streaming';
  const d = h('details', { class: 'thinking', open: (live && S.me.settings.show_thinking !== false) || null },
    h('summary', null, icon('brain', 15), h('span', { class: 'thinking-label' }, live ? 'Thinking…' : 'Thoughts')),
    h('div', { class: 'thinking-text' }, b.text));
  return d;
}

// Tools --------------------------------------------------------------------------------------------
const TOOL_ICON = { read_file: 'file', list_files: 'folder', search: 'search', write_file: 'pencil', edit_file: 'pencil', bash: 'terminal',
  bash_output: 'terminal', kill_shell: 'stop', web_fetch: 'globe', web_search: 'globe', todo_write: 'list', ask_user: 'user',
  propose_plan: 'map', create_artifact: 'code', update_artifact: 'code', rewrite_artifact: 'code', read_artifact: 'code', task: 'sparkle',
  search_project: 'search', memory: 'brain', past_chats: 'list', create_file: 'file', research_plan: 'map', research_notes: 'pencil',
  generate_image: 'image' };

function short(s, n = 80) { s = String(s ?? ''); return s.length > n ? s.slice(0, n - 1) + '…' : s; }

export function toolTitle(b) {
  const a = b.args || {};
  switch (b.name) {
    case 'read_file': return ['Read', short(a.path)];
    case 'list_files': return ['Listed', short(a.pattern || a.path || '.')];
    case 'search': return ['Searched for', short(a.pattern)];
    case 'write_file': return ['Wrote', short(a.path)];
    case 'edit_file': return ['Edited', short(a.path)];
    case 'bash': return [a.background ? 'Started' : 'Ran', short(a.command, 120)];
    case 'bash_output': return ['Checked', a.id];
    case 'kill_shell': return ['Stopped', a.id];
    case 'web_fetch': return ['Fetched', short(a.url)];
    case 'web_search': return ['Searched the web for', short(a.query)];
    case 'todo_write': return ['Updated the task list', ''];
    case 'ask_user': return ['Asked', short(a.question)];
    case 'propose_plan': return ['Proposed a plan', ''];
    case 'create_artifact': return ['Created', short(a.title)];
    case 'update_artifact': case 'rewrite_artifact': return ['Updated artifact', ''];
    case 'read_artifact': return a.id ? ['Read the artifact', short((S.artifacts.find(x => x.id === a.id) || {}).title || a.id)] : ['Listed the artifacts', ''];
    case 'task': return ['Helper:', short(a.description)];
    case 'create_file': return ['Created', short(a.path)];
    case 'research_plan': return ['Planned the research', ''];
    case 'generate_image': return [a.edit ? 'Changed the image:' : 'Made an image:', short(a.prompt, 100)];
    case 'research_notes': return [`Took notes from source ${a.source}:`, short(a.title, 90)];
    case 'search_project': return [b.auto ? 'Searched the project’s documents' : 'Searched the project for', b.auto ? '' : short(a.query)];
    case 'memory': return [{ add: 'Saved to memory', update: 'Updated a memory', delete: 'Deleted a memory' }[a.action] || 'Memory', a.action === 'delete' ? '' : short(a.text, 100)];
    case 'past_chats': return [a.query ? 'Searched earlier chats for' : 'Looked at recent chats', short(a.query || '')];
    default: return [b.name, ''];
  }
}

const STATUS = { pending: 'Queued', checking: 'Checking…', waiting: 'Waiting for approval', running: 'Running…', done: '',
  error: 'Failed', denied: 'Not run', stopped: 'Stopped' };

function decisionText(b) {
  const d = b.decision || {};
  if (!d.action) return '';
  if (b.status === 'denied') return d.reason || 'Declined';
  if (d.layer === 'judge' && b.judge) return `Auto mode: ${b.judge.risk} risk${b.judge.allowed ? ', ran automatically' : ''}`;
  if (d.layer === 'rule') return d.reason;
  if (d.action === 'ask' && ['running', 'done', 'error'].includes(b.status)) return 'Approved by you';
  if (d.action === 'ask') return '';
  return '';
}

function renderTool(b, m, ctx) {
  if (['create_artifact', 'update_artifact', 'rewrite_artifact'].includes(b.name) && b.artifact_id) return artifactCard(b, ctx);
  if (b.name === 'todo_write') return todoCard(b);
  const [verb, obj] = toolTitle(b);
  const statusText = STATUS[b.status] ?? b.status;
  const cls = `tool tool-${b.status}${b.research ? ' research-step' : ''}`;
  const head = h('summary', { class: 'tool-head' },
    icon(TOOL_ICON[b.name] || 'bolt', 15, 'tool-icon'),
    h('span', { class: 'tool-verb' }, verb),
    obj ? h('code', { class: 'tool-obj' }, obj) : null,
    b.status === 'running' || b.status === 'checking' ? h('span', { class: 'spinner' }) : null,
    statusText ? h('span', { class: `tool-status st-${b.status}` }, statusText) : null,
    b.exit_code ? h('span', { class: 'tool-status st-error' }, `exit ${b.exit_code}`) : null,
    b.duration_ms && b.status === 'done' ? h('span', { class: 'tool-time' }, duration(b.duration_ms)) : null);
  const body = h('div', { class: 'tool-body' });
  const dtext = decisionText(b);
  if (dtext) body.appendChild(h('div', { class: 'tool-decision' }, icon(b.status === 'denied' ? 'shield' : 'check', 13), dtext));
  if (b.judge && b.judge.reason) body.appendChild(h('div', { class: 'tool-judge' }, `Judge: ${b.judge.reason}`));
  body.appendChild(toolDetails(b));
  const open = b.status === 'waiting' || b.status === 'running' && b.name === 'bash' || b.status === 'error' || b.name === 'task';
  const d = h('details', { class: cls, open: open || null, dataset: { block: b.id } }, head, body);
  if (b.status === 'waiting') {
    const p = S.pending.find(x => x.block_id === b.id);
    if (p) d.appendChild(pendingCard(p, ctx));
    else if (b.approval_id) d.appendChild(h('div', { class: 'pending-card muted' }, 'Waiting for approval…'));
  }
  if (b.files && b.files.length && m.conv_id) return h('div', { class: 'tool-wrap', dataset: { block: b.id } }, d, fileCards(b.files, m.conv_id));
  return d;
}

// Images the assistant made -----------------------------------------------------------------------------
export function imageBlock(b, m, ctx) {
  if (b.status === 'running' && m.status !== 'streaming') b = { ...b, status: 'stopped' };  // e.g. the server restarted
  if (b.status === 'running') {
    const p = b.progress || {};
    const text = p.status === 'queued' ? 'Waiting for the image model…' : p.status === 'generating' ? 'Painting…' : 'Loading the image model…';
    return h('div', { class: 'image-card pending', style: { aspectRatio: `${b.width} / ${b.height}` } },
      h('div', { class: 'image-wait' }, h('span', { class: 'spinner' }), h('span', null, text), p.seconds ? h('span', { class: 'muted small' }, `${Math.round(p.seconds)} s`) : null),
      h('div', { class: 'image-caption muted small' }, b.prompt));
  }
  if (b.status !== 'done' || !b.id) {
    return h('div', { class: 'notice' + (b.status === 'error' ? ' notice-error' : '') }, b.status === 'error' ? `The image failed: ${b.error || ''}` : 'The image was stopped.');
  }
  const src = `/api/attachments/${b.id}`;
  const img = h('img', { src, alt: b.prompt, loading: 'lazy', width: b.width, height: b.height, onclick: () => window.open(src, '_blank', 'noopener') });
  const actions = h('div', { class: 'image-actions' },
    h('button', { class: 'icon-btn tiny', type: 'button', title: 'Download', onclick: () => downloadFrom(src, `baabaa-${b.id.slice(-6)}.png`).catch(() => {}) }, icon('download', 15)),
    ctx && ctx.imageAgain ? h('button', { class: 'icon-btn tiny', type: 'button', title: 'Another version (new seed)', onclick: () => ctx.imageAgain(b) }, icon('retry', 15)) : null,
    ctx && ctx.imageEdit ? h('button', { class: 'icon-btn tiny', type: 'button', title: 'Change this image', onclick: () => ctx.imageEdit(b) }, icon('pencil', 15)) : null,
    h('button', { class: 'icon-btn tiny', type: 'button', title: 'Copy the prompt', onclick: () => copyText(b.prompt).then(() => toast('Copied')) }, icon('copy', 15)));
  return h('figure', { class: 'image-card' }, img,
    h('figcaption', { class: 'image-caption' }, h('span', { class: 'muted small' }, `${b.edit ? 'Edited' : 'Made'} with ${b.model} · ${b.width}×${b.height}${b.seconds ? ` · ${Math.round(b.seconds)} s` : ''}`), actions));
}

// Files the assistant made: download cards, and a preview for images.
const FILE_KIND = { docx: 'Word document', xlsx: 'Excel workbook', pptx: 'PowerPoint deck', pdf: 'PDF', csv: 'CSV table',
  md: 'Markdown', txt: 'Text', html: 'Web page', json: 'JSON', py: 'Python', png: 'Image', jpg: 'Image', jpeg: 'Image',
  svg: 'SVG image', gif: 'Image', webp: 'Image' };
export function fileCards(files, convId) {
  return h('div', { class: 'file-cards' }, files.map(f => {
    const ext = (f.name.includes('.') ? f.name.split('.').pop() : '').toLowerCase();
    const url = `/api/conversations/${convId}/files?path=${encodeURIComponent(f.path)}`;
    const card = h('button', { class: 'file-card', type: 'button', title: `Download ${f.path}`, onclick: () => downloadFrom(url, f.name).catch(() => {}) },
      h('span', { class: `file-card-icon ft-${ext}` }, (ext || 'file').slice(0, 4).toUpperCase()),
      h('span', { class: 'file-card-text' }, h('strong', null, f.name), h('span', { class: 'muted small' }, `${FILE_KIND[ext] || 'File'} · ${bytes(f.size)}`)),
      icon('download', 16));
    if (['png', 'jpg', 'jpeg', 'gif', 'webp'].includes(ext)) {
      return h('div', { class: 'file-preview' }, h('img', { src: `${url}&inline=1`, alt: f.name, loading: 'lazy' }), card);
    }
    return card;
  }));
}

function toolDetails(b) {
  const a = b.args || {};
  const box = h('div', { class: 'tool-details' });
  if (b.name === 'bash') {
    box.appendChild(h('pre', { class: 'tool-cmd' }, h('span', { class: 'prompt' }, '$ '), a.command || ''));
    if (a.network) box.appendChild(h('div', { class: 'tool-note' }, 'With network access'));
  } else if (b.name === 'task') {
    box.appendChild(h('div', { class: 'tool-note' }, a.prompt ? short(a.prompt, 400) : ''));
    if (b.steps && b.steps.length) {
      box.appendChild(h('ol', { class: 'task-steps' }, b.steps.map(s => h('li', { class: `st-${s.status}` }, h('code', null, s.name), ' ', s.args))));
    }
  } else if (b.name === 'search_project' || b.name === 'past_chats' || b.name === 'memory') {
    if (b.sources && b.sources.length) {
      box.appendChild(h('div', { class: 'sources' }, b.sources.map(src => h('span', { class: 'source-chip', title: src.file }, icon('file', 12), ` ${short(src.file, 40)} · part ${src.part}`))));
    }
  } else if (b.name === 'research_notes') {
    if (a.url && /^https?:/.test(a.url)) box.appendChild(h('a', { href: a.url, target: '_blank', rel: 'noopener noreferrer' }, a.url));
  } else if (b.name === 'web_fetch') {
    if (a.url && /^https?:/.test(a.url)) box.appendChild(h('a', { href: a.url, target: '_blank', rel: 'noopener noreferrer' }, a.url));
  } else if (!['write_file', 'edit_file', 'read_file', 'list_files', 'search', 'web_search', 'bash_output', 'kill_shell', 'read_artifact'].includes(b.name) && Object.keys(a).length) {
    box.appendChild(h('pre', { class: 'tool-args' }, JSON.stringify(a, null, 1)));
  }
  if (b.diff) box.appendChild(diffView(b.diff));
  else if (b.name === 'write_file' && a.content) box.appendChild(h('pre', { class: 'tool-out' }, short(a.content, 4000)));
  const out = b.output || '';
  if (out && !(b.diff && b.status === 'done')) {
    const pre = h('pre', { class: 'tool-out' + (b.name === 'task' ? ' md-out' : '') });
    if (b.name === 'task' && b.status === 'done') pre.innerHTML = mdHTML(out);
    else pre.textContent = out.length > 20000 ? out.slice(0, 20000) + '\n…' : out;
    box.appendChild(pre);
  }
  if (b.status === 'running' && b.name === 'bash') box.appendChild(h('pre', { class: 'tool-out live', dataset: { live: b.id } }, b._live || ''));
  return box;
}

export function diffView(diff) {
  const pre = h('pre', { class: 'diff' });
  for (const line of diff.split('\n').slice(0, 800)) {
    const c = line.startsWith('+++') || line.startsWith('---') ? 'd-file' : line.startsWith('@@') ? 'd-hunk'
      : line.startsWith('+') ? 'd-add' : line.startsWith('-') ? 'd-del' : 'd-ctx';
    pre.appendChild(h('span', { class: c }, line + '\n'));
  }
  return pre;
}

function artifactCard(b, ctx) {
  const a = b.args || {};
  const known = S.artifacts.find(x => x.id === b.artifact_id) || {};
  return artifactButton({ id: b.artifact_id, version: b.artifact_version, title: a.title || known.title, kind: a.kind || known.kind,
    failed: b.status === 'error' }, ctx);
}

function artifactButton(a, ctx) {
  const kind = a.kind || '';
  return h('button', { class: 'artifact-card', type: 'button', onclick: () => ctx.openArtifact(a.id, a.version) },
    h('span', { class: 'artifact-card-icon' }, icon(kind === 'html' ? 'globe' : kind === 'svg' || kind === 'mermaid' ? 'image' : 'code', 18)),
    h('span', { class: 'artifact-card-text' }, h('strong', null, a.title || 'Artifact'),
      h('span', { class: 'muted small' }, `${kindLabel(kind)} · version ${a.version || 1}${a.failed ? ' · failed' : ''}`)),
    icon('chevronRight', 16));
}

export function kindLabel(k) {
  return { html: 'Web page', svg: 'SVG image', markdown: 'Document', slides: 'Slides', code: 'Code', mermaid: 'Diagram' }[k] || 'Artifact';
}

function todoCard(b) {
  const todos = (b.args && b.args.todos) || [];
  return h('div', { class: 'todo-inline' }, h('div', { class: 'todo-head' }, icon('list', 15), ' Task list'),
    h('ul', { class: 'todos' }, todos.map(t => h('li', { class: `todo-${t.status}` }, h('span', { class: 'todo-box' }), t.content))));
}

// Pending: approvals, questions and plans ------------------------------------------------------------
export function pendingCard(p, ctx) {
  if (p.kind === 'question') return questionCard(p);
  if (p.kind === 'plan') return planCard(p);
  const mine = !p.for_owner || S.me.role === 'owner';
  const card = h('div', { class: 'pending-card', dataset: { pending: p.id } });
  const d = p.decision || {};
  card.appendChild(h('div', { class: 'pending-title' }, icon('shield', 16),
    p.for_owner && S.me.role === 'owner' && p.account_id !== S.me.id ? `${p.account_name} asks: allow this ${actionNoun(p.tool)}?` : `Allow this ${actionNoun(p.tool)}?`));
  if (d.reason) card.appendChild(h('div', { class: 'pending-reason' }, d.danger ? '⚠ ' : '', d.reason));
  if (!mine) {
    card.appendChild(h('div', { class: 'muted' }, 'Waiting for the owner to approve.'));
    return card;
  }
  const reason = h('input', { class: 'input small-input', placeholder: 'Tell baabaa what to do instead (optional)' });
  const answer = async (decision) => {
    try {
      await post(`/api/pending/${p.id}`, { decision, reason: reason.value || undefined, rule: decision === 'allow_always' ? ruleInput.value : undefined });
    } catch (e) { errorToast(e); }
  };
  const ruleInput = h('input', { class: 'input small-input mono', value: p.suggested_rule || '', title: 'The rule to add' });
  const actions = h('div', { class: 'pending-actions' },
    h('button', { class: 'btn btn-primary', type: 'button', onclick: () => answer('allow_once') }, 'Allow once'),
    p.suggested_rule ? h('button', { class: 'btn', type: 'button', onclick: () => answer('allow_always') }, 'Always allow') : null,
    h('button', { class: 'btn btn-danger-soft', type: 'button', onclick: () => answer('deny') }, 'Deny'));
  card.appendChild(actions);
  if (p.suggested_rule) card.appendChild(h('label', { class: 'pending-rule' }, h('span', { class: 'muted small' }, 'Always allow adds the rule'), ruleInput));
  card.appendChild(reason);
  return card;
}

function actionNoun(tool) {
  return { bash: 'command', write_file: 'file change', edit_file: 'file edit', read_file: 'read', web_fetch: 'fetch' }[tool] || 'action';
}

function questionCard(p) {
  const card = h('div', { class: 'pending-card question-card', dataset: { pending: p.id } },
    h('div', { class: 'pending-title' }, icon('user', 16), p.question));
  const send = async text => {
    if (!String(text).trim()) return;
    try { await post(`/api/pending/${p.id}`, { text }); } catch (e) { errorToast(e); }
  };
  if (p.options && p.options.length) {
    card.appendChild(h('div', { class: 'pending-options' }, p.options.map(o => h('button', { class: 'btn', type: 'button', onclick: () => send(o) }, o))));
  }
  const input = h('input', { class: 'input', placeholder: 'Your answer' });
  input.addEventListener('keydown', e => { if (e.key === 'Enter') send(input.value); });
  card.appendChild(h('div', { class: 'pending-answer' }, input, h('button', { class: 'btn btn-primary', type: 'button', onclick: () => send(input.value) }, 'Answer')));
  setTimeout(() => input.focus({ preventScroll: true }), 50);
  return card;
}

function planCard(p) {
  const card = h('div', { class: 'pending-card plan-card', dataset: { pending: p.id } },
    h('div', { class: 'pending-title' }, icon('map', 16), 'Plan ready for review'),
    h('div', { class: 'md plan-body', html: mdHTML(p.plan || '') }));
  const fb = h('input', { class: 'input', placeholder: 'What should change? (for “Keep planning”)' });
  const answer = async (decision, mode) => {
    try { await post(`/api/pending/${p.id}`, { decision, mode, text: fb.value || undefined }); } catch (e) { errorToast(e); }
  };
  card.append(h('div', { class: 'pending-actions' },
    h('button', { class: 'btn btn-primary', type: 'button', onclick: () => answer('approve', 'auto') }, 'Approve · Auto mode'),
    h('button', { class: 'btn', type: 'button', onclick: () => answer('approve', 'accept_edits') }, 'Approve · Accept edits'),
    h('button', { class: 'btn', type: 'button', onclick: () => answer('approve', 'manual') }, 'Approve · Manual')),
    h('div', { class: 'pending-answer' }, fb, h('button', { class: 'btn', type: 'button', onclick: () => answer('revise') }, 'Keep planning')));
  return card;
}

export function copyCodeHandler(e) {
  const btn = e.target.closest('.code-copy');
  if (!btn) return;
  const code = btn.closest('.code-block')?.querySelector('code');
  if (!code) return;
  copyText(code.textContent).then(ok => {
    btn.textContent = ok ? 'Copied' : 'Copy failed';
    setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
  });
}

export function clearEl(el) { return clear(el); }
