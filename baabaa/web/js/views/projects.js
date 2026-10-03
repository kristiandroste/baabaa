// Projects: a list, and a project's page with its instructions, documents and conversations.
import { S, onState, loadConversations, toggleSidebar } from '../app.js';
import { get, post, patch, del, on, uploadTo } from '../api.js';
import { h, clear, icon, bytes, ago } from '../dom.js';
import { menu, toast, errorToast, modal, confirmDialog, promptDialog } from '../ui.js';

let unsub = [];
function teardown() { for (const u of unsub) u(); unsub = []; }

function topbar(...left) {
  return h('header', { class: 'topbar' },
    h('div', { class: 'tb-left' }, h('button', { class: 'icon-btn tb-sidebar', type: 'button', title: 'Sidebar (Ctrl+.)', onclick: () => toggleSidebar() }, icon('sidebar')), ...left),
    h('div', { class: 'tb-right' }));
}

// The list ----------------------------------------------------------------------------------------------
export async function renderProjects(main) {
  teardown();
  S.conv = null;
  clear(main);
  const list = h('div', { class: 'project-grid' });
  const search = h('input', { class: 'input', type: 'search', placeholder: 'Search projects' });
  const page = h('div', { class: 'page' },
    h('div', { class: 'page-head' }, h('h1', null, 'Projects'),
      h('button', { class: 'btn btn-primary', type: 'button', onclick: newProject }, icon('plus', 16), ' New project')),
    h('p', { class: 'muted' }, 'A project keeps conversations together with shared instructions and documents. baabaa reads the documents in every conversation of the project, whole when they fit, otherwise the passages that match each message.'),
    search, list);
  main.append(topbar(h('span', { class: 'tb-crumb' }, 'Projects')), h('div', { class: 'page-wrap' }, page));
  let projects = [];
  const draw = () => {
    clear(list);
    const q = search.value.trim().toLowerCase();
    const shown = projects.filter(p => !q || p.name.toLowerCase().includes(q) || (p.description || '').toLowerCase().includes(q));
    if (!shown.length) list.appendChild(h('div', { class: 'empty' }, projects.length ? 'No matching projects.' : 'No projects yet. Create one to give a set of conversations shared documents and instructions.'));
    for (const p of shown) {
      list.appendChild(h('a', { class: 'project-card', href: `#/p/${p.id}` },
        h('div', { class: 'project-card-title' }, icon('box', 16), h('strong', null, p.name)),
        p.description ? h('div', { class: 'project-card-desc' }, p.description) : null,
        h('div', { class: 'muted small' }, [`${p.conversations} conversation${p.conversations === 1 ? '' : 's'}`, `${p.files} document${p.files === 1 ? '' : 's'}`,
          `updated ${ago(p.last_ms || p.updated_ms)}`].join(' · '))));
    }
  };
  search.addEventListener('input', draw);
  const load = async () => { try { projects = (await get('/api/projects')).projects; draw(); } catch (e) { errorToast(e); } };
  unsub.push(on('project', load));
  await load();
}

async function newProject() {
  const m = modal('New project');
  const name = h('input', { class: 'input', placeholder: 'Name', maxlength: 120 });
  const desc = h('textarea', { class: 'input', rows: 2, placeholder: 'What is it about? (optional)' });
  m.body.append(h('div', { class: 'form' }, name, desc),
    h('div', { class: 'dialog-actions' },
      h('button', { class: 'btn', type: 'button', onclick: () => m.close() }, 'Cancel'),
      h('button', { class: 'btn btn-primary', type: 'button', onclick: async () => {
        if (!name.value.trim()) { name.focus(); return; }
        try { const d = await post('/api/projects', { name: name.value.trim(), description: desc.value.trim() }); m.close(); location.hash = `#/p/${d.project.id}`; }
        catch (e) { errorToast(e); }
      } }, 'Create project')));
  setTimeout(() => name.focus(), 0);
}

// A project ----------------------------------------------------------------------------------------------
export async function renderProject(main, pid) {
  teardown();
  S.conv = null;
  let d;
  try { d = await get(`/api/projects/${pid}`); } catch (e) { errorToast(e); location.hash = '#/projects'; return; }
  clear(main);
  const P = { ...d };
  const titleEl = h('h1', null, P.project.name);
  const descEl = h('p', { class: 'muted project-desc' }, P.project.description || '');
  const more = h('button', { class: 'icon-btn', type: 'button', title: 'More', onclick: () => projectMenu(more, P) }, icon('more'));
  const convList = h('div', { class: 'project-convs' });
  const filesEl = h('div', { class: 'file-list' });
  const instrEl = h('div', { class: 'instr-preview' });
  const modeEl = h('div', { class: 'muted small' });

  const { createComposer } = await import('./chat.js');
  const composer = createComposer({
    home: true, draft: {}, placeholder: `Start a conversation in ${P.project.name}…`,
    onSend: async (text, atts, extra) => {
      const body = { project_id: pid };
      const { conversation } = await post('/api/conversations', body);
      await post(`/api/conversations/${conversation.id}/messages`, { text, attachments: atts.map(a => a.id), ...(extra || {}) });
      loadConversations();
      location.hash = `#/c/${conversation.id}`;
    },
  });

  const left = h('div', { class: 'project-main' },
    h('a', { class: 'back-link', href: '#/projects' }, icon('chevronLeft', 15), 'All projects'),
    h('div', { class: 'page-head' }, titleEl, more), descEl, composer.el, convList);
  const fileInput = h('input', { type: 'file', multiple: true, hidden: true });
  fileInput.addEventListener('change', () => { addFiles(Array.from(fileInput.files)); fileInput.value = ''; });
  const right = h('aside', { class: 'project-side' },
    h('section', { class: 'card' },
      h('div', { class: 'card-head' }, h('h3', null, 'Instructions'), h('button', { class: 'btn small', type: 'button', onclick: editInstructions }, P.project.instructions ? 'Edit' : 'Add')),
      instrEl),
    h('section', { class: 'card', ondragover: e => { e.preventDefault(); }, ondrop: e => { e.preventDefault(); addFiles(Array.from(e.dataTransfer.files || [])); } },
      h('div', { class: 'card-head' }, h('h3', null, 'Documents'),
        h('button', { class: 'btn small', type: 'button', onclick: e => menu(e.currentTarget, [
          { label: 'Upload files', icon: 'upload', onClick: () => fileInput.click() },
          { label: 'Add text', icon: 'file', onClick: addText },
        ], { alignRight: true }) }, icon('plus', 14), ' Add')),
      modeEl, filesEl, fileInput));
  main.append(topbar(h('a', { class: 'tb-crumb', href: '#/projects' }, 'Projects'), h('span', { class: 'tb-sep' }, '/'), h('span', { class: 'tb-crumb' }, P.project.name)),
    h('div', { class: 'page-wrap' }, h('div', { class: 'project-page' }, left, right)));

  function drawInstructions() {
    clear(instrEl);
    instrEl.appendChild(P.project.instructions
      ? h('div', { class: 'instr-text' }, P.project.instructions.length > 600 ? P.project.instructions.slice(0, 600) + '…' : P.project.instructions)
      : h('p', { class: 'muted small' }, 'Tell baabaa how to work in this project: its role, tone, what to focus on.'));
  }
  function drawFiles() {
    clear(filesEl);
    const modeText = { full: 'All documents are read whole in every conversation.', search: 'The documents are too large to read whole: each message searches them for matching passages.' +
      (P.embed_model ? '' : ' No embedding model is approved, so the search matches words only.'), none: '' }[P.knowledge_mode] || '';
    modeEl.textContent = modeText;
    if (!P.files.length) filesEl.appendChild(h('p', { class: 'muted small' }, 'Add text, code, PDFs or office documents. Drop files here.'));
    for (const f of P.files) {
      const waiting = S.queue && S.queue.paused ? 'waiting for the GPU' : 'indexing…';
      const status = f.status === 'ready' ? (f.error ? h('span', { class: 'st-waiting', title: f.error }, ' · words only') : null)
        : h('span', null, f.total && f.done ? ` · indexing ${Math.round(100 * f.done / f.total)}%` : ` · ${waiting}`);
      filesEl.appendChild(h('div', { class: 'file-row' },
        icon('file', 15),
        h('div', { class: 'file-info' },
          h('button', { class: 'file-name link', type: 'button', title: f.name, onclick: () => viewFile(f) }, f.name),
          h('span', { class: 'muted small' }, `${bytes(f.size)}${f.chars ? ` · ${Math.round(f.chars / 3.3 / 1000 * 10) / 10}k tokens` : ''}`, status)),
        h('button', { class: 'icon-btn tiny', type: 'button', title: 'Remove', onclick: async () => {
          if (!await confirmDialog('Remove document?', `${f.name} will be removed from the project.`, 'Remove', true)) return;
          try { await del(`/api/projects/${pid}/files/${f.id}`); await reload(); } catch (e) { errorToast(e); }
        } }, icon('trash', 14))));
    }
  }
  function drawConvs() {
    clear(convList);
    convList.appendChild(h('h3', null, 'Conversations'));
    if (!P.conversations.length) { convList.appendChild(h('p', { class: 'muted small' }, 'No conversations yet.')); return; }
    for (const c of P.conversations) {
      convList.appendChild(h('a', { class: 'project-conv', href: `#/c/${c.id}` },
        h('span', { class: 'project-conv-title' }, c.title || 'Untitled'), h('span', { class: 'muted small' }, ago(c.updated_ms))));
    }
  }
  async function reload() {
    try { Object.assign(P, await get(`/api/projects/${pid}`)); titleEl.textContent = P.project.name; descEl.textContent = P.project.description || ''; drawInstructions(); drawFiles(); drawConvs(); }
    catch (e) { errorToast(e); }
  }
  async function addFiles(files) {
    for (const f of files) {
      toast(`Adding ${f.name}…`);
      try { await uploadTo(`/api/projects/${pid}/files?name=${encodeURIComponent(f.name)}`, f); } catch (e) { errorToast(e); }
    }
    await reload();
  }
  async function addText() {
    const m = modal('Add text', { wide: true });
    const name = h('input', { class: 'input', placeholder: 'Title', value: 'Notes' });
    const text = h('textarea', { class: 'input', rows: 14, placeholder: 'Paste or write text' });
    m.body.append(h('div', { class: 'form' }, name, text), h('div', { class: 'dialog-actions' },
      h('button', { class: 'btn', type: 'button', onclick: () => m.close() }, 'Cancel'),
      h('button', { class: 'btn btn-primary', type: 'button', onclick: async () => {
        try { await post(`/api/projects/${pid}/text`, { name: name.value, text: text.value }); m.close(); await reload(); } catch (e) { errorToast(e); }
      } }, 'Add')));
    setTimeout(() => text.focus(), 0);
  }
  async function editInstructions() {
    const m = modal('Project instructions', { wide: true });
    const ta = h('textarea', { class: 'input', rows: 14, placeholder: 'For example: You are helping me run a sheep farm in a cold climate. Answer practically, in metric units.' }, P.project.instructions || '');
    m.body.append(h('p', { class: 'muted small' }, 'Every conversation in this project starts with these instructions.'), ta,
      h('div', { class: 'dialog-actions' },
        h('button', { class: 'btn', type: 'button', onclick: () => m.close() }, 'Cancel'),
        h('button', { class: 'btn btn-primary', type: 'button', onclick: async () => {
          try { P.project = (await patch(`/api/projects/${pid}`, { instructions: ta.value })).project; m.close(); drawInstructions(); } catch (e) { errorToast(e); }
        } }, 'Save')));
    setTimeout(() => ta.focus(), 0);
  }
  async function viewFile(f) {
    try {
      const d = await get(`/api/projects/${pid}/files/${f.id}`);
      const m = modal(f.name, { wide: true });
      m.body.append(h('pre', { class: 'file-view' }, d.file.text || ''));
    } catch (e) { errorToast(e); }
  }

  drawInstructions(); drawFiles(); drawConvs();
  unsub.push(on('project.file', ev => {
    if (ev.project_id !== pid) return;
    if (ev.file) {
      const i = P.files.findIndex(x => x.id === ev.file.id);
      if (i >= 0) P.files[i] = { ...P.files[i], ...ev.file }; else P.files.push(ev.file);
      drawFiles();
      if (ev.file.status === 'ready' && !ev.file.total) reload();
    }
  }));
  unsub.push(on('project', ev => { if (ev.deleted === pid) location.hash = '#/projects'; else if (ev.project && ev.project.id === pid) reload(); }));
  unsub.push(onState(w => { if (w === 'convs') get(`/api/conversations?project=${pid}&limit=200`).then(r => { P.conversations = r.conversations; drawConvs(); }).catch(() => {}); }));
  composer.focus();
}

function projectMenu(anchor, P) {
  const pid = P.project.id;
  menu(anchor, [
    { label: 'Rename', icon: 'edit', onClick: async () => {
      const t = await promptDialog('Rename project', 'Name', P.project.name);
      if (t) try { await patch(`/api/projects/${pid}`, { name: t }); } catch (e) { errorToast(e); }
    } },
    { label: 'Edit description', icon: 'pencil', onClick: async () => {
      const t = await promptDialog('Project description', 'Description', P.project.description || '');
      if (t !== null) try { await patch(`/api/projects/${pid}`, { description: t }); } catch (e) { errorToast(e); }
    } },
    'sep',
    { label: 'Delete project', icon: 'trash', danger: true, onClick: async () => {
      if (!await confirmDialog('Delete project?', `“${P.project.name}”, its documents and its memory will be deleted. Its conversations stay in your chat list.`, 'Delete', true)) return;
      try { await del(`/api/projects/${pid}`); loadConversations(); location.hash = '#/projects'; } catch (e) { errorToast(e); }
    } },
  ], { alignRight: true });
}
