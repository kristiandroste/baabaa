// Settings: general preferences, models, permissions, usage, accounts and about.
import { S, applyTheme, changed } from '../app.js';
import { api, get, post, patch, del } from '../api.js';
import { h, clear, icon, avatar, bytes } from '../dom.js';
import { modal, toast, errorToast, confirmDialog, promptDialog, switchEl, segmented } from '../ui.js';

let M = null;
const TABS = [
  { id: 'general', label: 'General', icon: 'user' },
  { id: 'models', label: 'Models', icon: 'cpu' },
  { id: 'permissions', label: 'Permissions', icon: 'shield' },
  { id: 'memory', label: 'Memory', icon: 'brain' },
  { id: 'connectors', label: 'Connectors', icon: 'bolt' },
  { id: 'customize', label: 'Customize', icon: 'sparkle' },
  { id: 'usage', label: 'Usage', icon: 'chart' },
  { id: 'accounts', label: 'Accounts', icon: 'users', owner: true },
  { id: 'about', label: 'About', icon: 'sparkle' },
];

export function openSettings(tab, onClose) {
  if (!M) {
    const m = modal(null, { wide: true, class: 'settings-modal', onClose: () => { M = null; onClose && onClose(); } });
    const nav = h('nav', { class: 'settings-nav' });
    const pane = h('div', { class: 'settings-pane' });
    m.body.append(h('div', { class: 'settings' }, nav, pane),
      h('button', { class: 'icon-btn settings-close', type: 'button', title: 'Close', onclick: () => m.close() }, icon('x')));
    M = { m, nav, pane, tab: null };
  }
  clear(M.nav);
  M.nav.appendChild(h('h2', null, 'Settings'));
  for (const t of TABS) {
    if (t.owner && S.me.role !== 'owner') continue;
    M.nav.appendChild(h('a', { class: 'settings-tab' + (t.id === tab ? ' active' : ''), href: `#/settings/${t.id}` }, icon(t.icon, 16), t.label));
  }
  // The same tab drawn again (after a change in it) keeps its place: its old height is held until the new
  // content is in. Another tab starts at the top.
  const again = M.tab === tab;
  const keep = again ? M.pane.scrollTop : 0;
  const hold = again && M.pane.firstElementChild ? M.pane.firstElementChild.offsetHeight : 0;
  M.tab = tab;
  clear(M.pane);
  // each tab draws into its own container, so a tab's late async updates stop once another tab opens
  const content = h('div', { class: 'settings-content' });
  if (hold) content.style.minHeight = hold + 'px';
  M.pane.appendChild(content);
  M.pane.scrollTop = keep;
  const render = { general, models: modelsTab, permissions, memory: memoryTab, connectors, customize, usage: usageTab, accounts, about }[tab] || general;
  const pane = M.pane;
  Promise.resolve(render(content)).catch(() => {}).then(() => requestAnimationFrame(() => {
    content.style.minHeight = '';
    if (again && content.isConnected) pane.scrollTop = keep;
  }));
}

async function memoryTab(pane) {
  const { renderMemory } = await import('./memory.js');
  renderMemory(pane, { saveMe });
}

export function closeSettings() {
  if (M) M.m.close();
}

function section(title, ...children) {
  return h('section', { class: 'set-section' }, title ? h('h3', null, title) : null, ...children);
}

function row(label, control, hint) {
  return h('div', { class: 'set-row' }, h('div', { class: 'set-label' }, h('span', null, label), hint ? h('span', { class: 'muted small' }, hint) : null), h('div', { class: 'set-control' }, control));
}

async function saveMe(patchBody) {
  try {
    const d = await patch('/api/me', patchBody);
    S.me = d.account;
    applyTheme();
    changed('me');
  } catch (e) { errorToast(e); }
}

// The profile colour: baabaa's own green always first, then up to 15 colours the person chose, newest first.
// A colour from the colour tool is saved, and joins the swatches, when the tool closes.
const BRAND_COLOUR = '#2f7d6d';

function colourPicker() {
  const wrap = h('div', { class: 'swatches' });
  const draw = () => {
    clear(wrap);
    const current = (S.me.color || BRAND_COLOUR).toLowerCase();
    const saved = (S.me.settings.recent_colors || []).filter(c => c !== BRAND_COLOUR);
    // a colour the account was given at creation shows too until another is chosen
    const shown = saved.includes(current) || current === BRAND_COLOUR ? saved : [current, ...saved].slice(0, 15);
    const swatch = (c, title) => h('button', { class: 'swatch' + (c === current ? ' on' : ''), type: 'button', title, 'aria-label': title,
      'aria-pressed': c === current ? 'true' : 'false', style: { background: c }, onclick: () => { if (c !== current) saveMe({ color: c }).then(draw); } });
    const tool = h('input', { type: 'color', value: current, 'aria-label': 'Choose another colour' });
    const add = h('label', { class: 'swatch swatch-add', title: 'Choose another colour' }, icon('plus', 14), tool);
    tool.addEventListener('input', () => { add.style.background = tool.value; add.classList.add('picking'); });
    tool.addEventListener('change', () => {
      const c = tool.value.toLowerCase();
      const list = [c, ...shown.filter(x => x !== c)].filter(x => x !== BRAND_COLOUR).slice(0, 15);
      saveMe({ color: c, settings: { recent_colors: list } }).then(draw);
    });
    wrap.append(swatch(BRAND_COLOUR, 'baabaa green (the default)'), ...shown.map(c => swatch(c, c)), add);
  };
  draw();
  return wrap;
}

// General ----------------------------------------------------------------------------------------------------
function general(pane) {
  const s = S.me.settings;
  const name = h('input', { class: 'input', value: S.me.display_name });
  name.addEventListener('change', () => saveMe({ display_name: name.value }));
  const instr = h('textarea', { class: 'input', rows: 5, placeholder: 'For example: I am a neuroscientist; answer concisely; use metric units; I write Python.' }, s.instructions || '');
  instr.addEventListener('change', () => saveMe({ settings: { instructions: instr.value } }));
  const modelSel = h('select', { class: 'input' }, h('option', { value: '' }, `Default (${S.defaultModel || 'none'})`),
    S.models.map(m => h('option', { value: m.name, selected: s.default_model === m.name || null }, m.name)));
  modelSel.addEventListener('change', () => saveMe({ settings: { default_model: modelSel.value || null } }));
  const modeSel = h('select', { class: 'input' }, S.modes.map(m => h('option', { value: m.id, selected: (s.default_mode || 'accept_edits') === m.id || null }, m.label)));
  const styleSel = h('select', { class: 'input' }, h('option', { value: 'default' }, 'Default'));
  get('/api/extend').then(d => {
    for (const st of d.styles) if (st.name !== 'default') styleSel.appendChild(h('option', { value: st.name, selected: s.style === st.name || null }, st.label));
  }).catch(() => {});
  styleSel.addEventListener('change', () => saveMe({ settings: { style: styleSel.value } }));
  modeSel.addEventListener('change', () => saveMe({ settings: { default_mode: modeSel.value } }));
  pane.append(
    section('Profile',
      row('Name', name),
      row('Colour', colourPicker())),
    section('Preferences',
      row('What should baabaa know about you?', instr, 'Added to every conversation.'),
      row('Theme', segmented([{ value: 'auto', label: 'System' }, { value: 'light', label: 'Light' }, { value: 'dark', label: 'Dark' }], s.theme || 'auto', v => saveMe({ settings: { theme: v } }))),
      row('Reply font', segmented([{ value: 'serif', label: 'Serif' }, { value: 'sans', label: 'Sans' }, { value: 'mono', label: 'Mono' }], s.font || 'serif', v => saveMe({ settings: { font: v } }))),
      row('Send with', segmented([{ value: 'enter', label: 'Enter' }, { value: 'mod-enter', label: 'Ctrl+Enter' }], s.send_key || 'enter', v => saveMe({ settings: { send_key: v } }))),
      row('Show thinking while it streams', switchEl(s.show_thinking !== false, v => saveMe({ settings: { show_thinking: v } }))),
      row('Think by default', switchEl(!!s.think, v => saveMe({ settings: { think: v } })), 'For models that can think before answering.'),
      row('Code execution and file creation', switchEl(s.code_exec !== false, v => saveMe({ settings: { code_exec: v } })),
        'In chats without a folder, baabaa can run code in a private, sandboxed folder and make Word, Excel, PowerPoint and PDF files for you to download.'),
      row('Default model', modelSel),
      row('Reply style', styleSel, 'Custom styles are made in Customize.'),
      row('Default mode in folders', modeSel, 'Applies when baabaa works in one of your folders. Chats without a folder always have all their tools.')),
    voiceSection(s),
    section('Password',
      h('p', { class: 'muted small' }, S.me.has_password ? 'This account has a password.' : 'This account has no password: anyone on your network can open it.'),
      h('button', { class: 'btn', type: 'button', onclick: changePassword }, S.me.has_password ? 'Change or remove password' : 'Set a password')),
    keysSection());
}

function voiceSection(s) {
  const box = section('Voice and notifications');
  const voiceSel = h('select', { class: 'input' }, h('option', { value: '' }, 'Loading voices…'));
  const rate = h('select', { class: 'input small-input' }, [0.8, 0.9, 1, 1.1, 1.25, 1.5].map(r =>
    h('option', { value: r, selected: r === ((s.voice || {}).rate || 1) || null }, `${r}×`)));
  const save = () => saveMe({ settings: { voice: { uri: voiceSel.value || null, rate: Number(rate.value) } } });
  voiceSel.addEventListener('change', save);
  rate.addEventListener('change', save);
  import('../speech.js').then(async sp => {
    const voices = await sp.voicesReady();
    clear(voiceSel);
    if (!voices.length) { voiceSel.appendChild(h('option', { value: '' }, 'No on-device voices in this browser')); voiceSel.disabled = true; return; }
    voiceSel.appendChild(h('option', { value: '' }, 'Automatic (an on-device voice for your language)'));
    for (const v of voices) voiceSel.appendChild(h('option', { value: v.voiceURI, selected: v.voiceURI === (s.voice || {}).uri || null }, `${v.name} (${v.lang})`));
  });
  const canNotify = 'Notification' in window && window.isSecureContext;
  const notifySw = switchEl(!!s.notify && canNotify && Notification.permission === 'granted', async v => {
    if (v && Notification.permission !== 'granted') {
      const p = await Notification.requestPermission();
      if (p !== 'granted') { toast('Notifications are blocked for this site in the browser settings', 'warn'); saveMe({ settings: { notify: false } }); return; }
    }
    saveMe({ settings: { notify: v } });
  });
  box.append(
    row('Voice for reading aloud', voiceSel, 'Only voices that run on this device are offered; the text is never sent to a speech service.'),
    row('Reading speed', rate),
    row('Notify me', canNotify ? notifySw : h('span', { class: 'muted small' }, 'Needs HTTPS'), 'When a reply is ready or baabaa needs you while this tab is in the background.'));
  return box;
}

function keysSection() {
  const box = h('div', { class: 'rule-list' });
  const load = async () => {
    clear(box);
    try {
      const d = await get('/api/me/keys');
      for (const k of d.keys) {
        box.appendChild(h('div', { class: 'rule' }, h('span', null, h('strong', null, k.name), h('span', { class: 'muted small' },
          ` · created ${new Date(k.created_ms).toLocaleDateString()}${k.last_used_ms ? ` · last used ${new Date(k.last_used_ms).toLocaleString()}` : ' · never used'}`)),
          h('button', { class: 'btn small btn-danger-soft', type: 'button', onclick: async () => { try { await del(`/api/me/keys/${k.id}`); load(); } catch (e) { errorToast(e); } } }, 'Revoke')));
      }
      if (!d.keys.length) box.appendChild(h('div', { class: 'muted small' }, 'No keys.'));
    } catch (e) { errorToast(e); }
  };
  load();
  const create = async () => {
    const name = await promptDialog('New API key', 'What is it for?', 'script');
    if (name === null) return;
    try {
      const d = await post('/api/me/keys', { name });
      const m = modal('Your new API key', {});
      m.body.append(h('p', { class: 'dialog-text' }, 'Copy it now: it is shown only once. Send it as “Authorization: Bearer …”.'),
        h('input', { class: 'input mono', value: d.token, readonly: true, onclick: e => e.target.select() }),
        h('div', { class: 'dialog-actions' }, h('button', { class: 'btn btn-primary', type: 'button', onclick: () => m.close() }, 'Done')));
      load();
    } catch (e) { errorToast(e); }
  };
  return section('API keys', h('p', { class: 'muted small' }, 'For scripts and other programs on your network, with this account’s access. See docs/API.md.'),
    box, h('div', { class: 'rule-add' }, h('button', { class: 'btn small', type: 'button', onclick: create }, icon('plus', 14), ' New key')));
}

async function changePassword() {
  const m = modal('Password', {});
  const cur = h('input', { class: 'input', type: 'password', autocomplete: 'current-password' });
  const nw = h('input', { class: 'input', type: 'password', autocomplete: 'new-password', placeholder: 'Empty to remove the password' });
  m.body.append(S.me.has_password ? h('label', { class: 'field' }, h('span', null, 'Current password'), cur) : null,
    h('label', { class: 'field' }, h('span', null, 'New password'), nw),
    h('div', { class: 'dialog-actions' }, h('button', { class: 'btn', type: 'button', onclick: () => m.close() }, 'Cancel'),
      h('button', { class: 'btn btn-primary', type: 'button', onclick: async () => {
        try {
          const d = await post('/api/me/password', { current: cur.value, new: nw.value || null });
          S.me = d.account;
          const { setCsrf } = await import('../api.js');
          setCsrf(d.csrf);
          m.close();
          toast('Password updated');
        } catch (e) { errorToast(e); }
      } }, 'Save')));
}

// Models ----------------------------------------------------------------------------------------------------------
async function modelsTab(pane) {
  const { renderModels } = await import('./models.js');
  renderModels(pane);
}

// Permissions ------------------------------------------------------------------------------------------------------
async function permissions(pane) {
  let d;
  try { d = await get('/api/rules'); } catch (e) { errorToast(e); return; }
  const listFor = kind => {
    const box = h('div', { class: 'rule-list' });
    const rules = kind in { allow: 1, ask: 1, deny: 1 } ? d.rules.filter(r => r.kind === kind) : d.auto_rules.filter(r => r.kind === kind);
    for (const r of rules) {
      box.appendChild(h('div', { class: 'rule' }, h('span', { class: kind in { allow: 1, ask: 1, deny: 1 } ? 'mono' : '' }, r.pattern || r.text),
        h('button', { class: 'icon-btn tiny', type: 'button', title: 'Remove', onclick: async () => { try { await del(`/api/rules/${r.id}`); permissionsReload(); } catch (e) { errorToast(e); } } }, icon('trash', 14))));
    }
    if (!rules.length) box.appendChild(h('div', { class: 'muted small' }, 'None.'));
    const input = h('input', { class: 'input' + (kind in { allow: 1, ask: 1, deny: 1 } ? ' mono' : ''), placeholder: kind in { allow: 1, ask: 1, deny: 1 } ? 'Bash(npm test:*)' : 'A sentence, e.g. “Installing Python packages from PyPI is fine.”' });
    const add = async () => {
      const v = input.value.trim();
      if (!v) return;
      try {
        await post('/api/rules', kind in { allow: 1, ask: 1, deny: 1 } ? { kind, pattern: v } : { kind, text: v });
        permissionsReload();
      } catch (e) { errorToast(e); }
    };
    input.addEventListener('keydown', e => { if (e.key === 'Enter') add(); });
    box.appendChild(h('div', { class: 'rule-add' }, input, h('button', { class: 'btn', type: 'button', onclick: add }, 'Add')));
    return box;
  };
  pane.append(
    section('Modes',
      h('p', { class: 'muted' }, 'Each conversation with a folder has a mode: Manual asks before every edit and command; Accept edits applies edits inside the folder and asks for commands; Plan only reads and proposes a plan; Auto runs safe actions and asks for risky ones. Shift+Tab in the message box cycles the mode.'),
      h('p', { class: 'muted small' }, 'In Auto mode, three checks run in order: your rules below, a check of the shell command, and a judgement by the local model. Anything it is unsure of waits for you.')),
    section('Allow', h('p', { class: 'muted small' }, 'Actions matching these run without asking (except in Plan mode).'), listFor('allow')),
    section('Ask', h('p', { class: 'muted small' }, 'Actions matching these always ask.'), listFor('ask')),
    section('Deny', h('p', { class: 'muted small' }, 'Actions matching these never run.'), listFor('deny')),
    section('Rules for Auto mode, in plain words',
      h('h4', null, 'Trusted'), listFor('trust'),
      h('h4', null, 'Always hold for me'), listFor('block'),
      h('h4', null, 'Exceptions'), listFor('exception')),
    section('Rule syntax', h('pre', { class: 'help-pre' },
      'Bash                  every command\nBash(npm test)        exactly this command\nBash(git log:*)       commands starting with “git log”\n'
      + 'Edit(src/**)          edits under src/\nRead(**/*.env)        reading .env files\nWebFetch(domain:example.com)\nWebSearch')));
}

function permissionsReload() { if (M) openSettings('permissions'); }

// Customize: skills, commands, helper agents, styles, hooks -----------------------------------------------------------
const TEMPLATES = {
  skills: name => `---\nname: ${name}\ndescription: When to use this skill, in one sentence.\n---\n\n# ${name}\n\nStep-by-step instructions for the task.\n`,
  commands: name => `---\ndescription: What /${name} does\n---\n\nThe prompt to send. $ARGUMENTS is replaced by what follows /${name}.\n`,
  agents: name => `---\nname: ${name}\ndescription: When the main agent should hand work to this helper.\ntools: [read_file, list_files, search]\n---\n\nYou are a helper who …\n`,
  styles: name => `---\nlabel: ${name}\n---\n\nHow replies should be written in this style.\n`,
};
const KIND_INFO = {
  skills: ['Skills', 'Instructions for particular tasks. The model sees only each skill’s name and description, and loads the full text when a task matches.'],
  commands: ['Commands', 'Prompt templates you run by typing /name in the message box.'],
  agents: ['Helper agents', 'Specialists the model can hand work to, each with its own instructions and tools.'],
  styles: ['Reply styles', 'How replies are written. Choose one in General.'],
};

async function customize(pane) {
  let d;
  try { d = await get('/api/extend'); } catch (e) { errorToast(e); return; }
  const reload = () => { if (M) openSettings('customize'); };
  pane.appendChild(h('p', { class: 'muted small' }, 'Everything here belongs to your account. A trusted working folder can add its own in .baabaa/skills, .baabaa/commands, .baabaa/agents, .baabaa/styles and .baabaa/settings.json (hooks).'));
  for (const kind of ['skills', 'commands', 'agents', 'styles']) {
    const items = (d[kind] || []).filter(x => x.source !== 'built-in');
    const list = h('div', { class: 'rule-list' });
    for (const it of items) {
      list.appendChild(h('div', { class: 'rule' }, h('span', null, h('strong', { class: 'mono' }, kind === 'commands' ? '/' + it.name : it.name), ' ', h('span', { class: 'muted small' }, it.description || it.label || '')),
        h('span', null,
          h('button', { class: 'icon-btn tiny', type: 'button', title: 'Edit', onclick: () => editFile(kind, it.name, reload) }, icon('edit', 14)),
          h('button', { class: 'icon-btn tiny', type: 'button', title: 'Delete', onclick: async () => {
            if (await confirmDialog('Delete?', `${it.name} will be deleted.`, 'Delete', true)) { try { await del(`/api/extend/${kind}/${encodeURIComponent(it.name)}`); reload(); } catch (e) { errorToast(e); } }
          } }, icon('trash', 14)))));
    }
    if (!items.length) list.appendChild(h('div', { class: 'muted small' }, 'None yet.'));
    const add = h('button', { class: 'btn small', type: 'button', onclick: async () => {
      const name = await promptDialog(`New ${KIND_INFO[kind][0].toLowerCase().replace(/s$/, '')}`, 'Name (letters, digits, - or _)', '');
      if (name) editFile(kind, name.trim(), reload, TEMPLATES[kind](name.trim()));
    } }, icon('plus', 14), ' New');
    pane.appendChild(section(KIND_INFO[kind][0], h('p', { class: 'muted small' }, KIND_INFO[kind][1]), list, h('div', { class: 'rule-add' }, add)));
  }
  const counts = Object.entries(d.hooks || {}).filter(([, n]) => n).map(([k, n]) => `${k}: ${n}`).join(', ') || 'none';
  pane.appendChild(section('Hooks', h('p', { class: 'muted small' }, 'Commands that run (in the sandbox) at PreToolUse, PostToolUse, UserPromptSubmit and Stop. They receive the event as JSON on standard input; exit code 2 blocks the action (or, for Stop, sends the model back to work) and the error output tells the model why.'),
    h('p', { class: 'small' }, `Active: ${counts}`),
    h('button', { class: 'btn small', type: 'button', onclick: () => editFile('hooks', 'hooks', reload, '{\n  "hooks": {\n    "PreToolUse": [\n      {"matcher": "bash", "command": "grep -q rm-rf - && exit 2 || exit 0"}\n    ]\n  }\n}\n', true) }, icon('edit', 14), ' Edit hooks')));
}

async function editFile(kind, name, done, template, json) {
  let content = '';
  try { content = (await get(`/api/extend/${kind}/${encodeURIComponent(name)}`)).content; } catch (e) { errorToast(e); return; }
  const m = modal(`${kind === 'hooks' ? 'Hooks' : name}`, { wide: true });
  const ta = h('textarea', { class: 'input mono editor', rows: 22, spellcheck: 'false' }, content || template || '');
  m.body.append(ta, h('div', { class: 'dialog-actions' },
    h('button', { class: 'btn', type: 'button', onclick: () => m.close() }, 'Cancel'),
    h('button', { class: 'btn btn-primary', type: 'button', onclick: async () => {
      try { await api('PUT', `/api/extend/${kind}/${encodeURIComponent(name)}`, { content: ta.value }); m.close(); toast('Saved'); done && done(); } catch (e) { errorToast(e); }
    } }, 'Save')));
  setTimeout(() => ta.focus(), 0);
}

// Connectors (MCP servers) --------------------------------------------------------------------------------------------
async function connectors(pane) {
  let d;
  try { d = await get('/api/connectors'); } catch (e) { errorToast(e); return; }
  const reload = () => { if (M) openSettings('connectors'); };
  const list = h('div', { class: 'acct-list' });
  for (const c of d.connectors) {
    const status = h('span', { class: 'muted small' }, c.connected ? `connected · ${c.tools.length} tools` : c.enabled ? 'not connected yet' : 'disabled');
    list.appendChild(h('div', { class: 'acct' },
      h('div', { class: 'set-head' }, h('div', null, h('strong', null, c.name), h('div', { class: 'muted small mono' },
        c.transport === 'stdio' ? `${c.command} ${(c.args || []).join(' ')}` : c.url)), status),
      c.tools.length ? h('ul', { class: 'conn-tools' }, c.tools.map(tl => h('li', null, h('code', null, tl.name), tl.read_only ? h('span', { class: 'badge' }, 'read-only') : null, ' ', h('span', { class: 'muted small' }, tl.description)))) : null,
      h('div', { class: 'acct-actions' },
        switchEl(c.enabled, async v => { try { await patch(`/api/connectors/${c.id}`, { enabled: v }); reload(); } catch (e) { errorToast(e); } }, c.enabled ? 'Enabled' : 'Disabled'),
        h('button', { class: 'btn small', type: 'button', onclick: async () => {
          try { const r = await post(`/api/connectors/${c.id}/test`, {}); r.ok ? toast(`Connected: ${r.tools} tools`) : errorToast(new Error(r.error)); reload(); } catch (e) { errorToast(e); }
        } }, 'Connect and list tools'),
        h('button', { class: 'btn small btn-danger-soft', type: 'button', onclick: async () => {
          if (await confirmDialog('Remove connector?', `${c.name} will no longer be available.`, 'Remove', true)) { try { await del(`/api/connectors/${c.id}`); reload(); } catch (e) { errorToast(e); } }
        } }, 'Remove'))));
  }
  if (!d.connectors.length) list.appendChild(h('p', { class: 'muted' }, 'No connectors yet.'));
  const name = h('input', { class: 'input', placeholder: 'Name, e.g. github' });
  const kind = h('select', { class: 'input small-input' }, h('option', { value: 'http' }, 'Remote server (URL)'),
    d.can_add_programs ? h('option', { value: 'stdio' }, 'Program on this computer') : null);
  const target = h('input', { class: 'input mono', placeholder: 'https://example.com/mcp' });
  const extra = h('input', { class: 'input mono', placeholder: 'Headers: Authorization=Bearer … (optional)' });
  // a program on this computer runs outside the sandbox: the server asks for the owner's password again
  const password = h('input', { class: 'input', type: 'password', placeholder: 'Your password', autocomplete: 'current-password', hidden: true });
  kind.addEventListener('change', () => {
    password.hidden = kind.value !== 'stdio';
    target.placeholder = kind.value === 'stdio' ? 'Command and arguments, e.g. npx -y @modelcontextprotocol/server-memory' : 'https://example.com/mcp';
    extra.placeholder = kind.value === 'stdio' ? 'Environment: KEY=value KEY2=value (optional)' : 'Headers: Authorization=Bearer … (optional)';
  });
  const add = async () => {
    const pairs = Object.fromEntries(extra.value.split(/\s+(?=[\w-]+=)/).filter(Boolean).map(s => [s.split('=')[0], s.split('=').slice(1).join('=')]));
    const body = { name: name.value.trim(), transport: kind.value };
    if (kind.value === 'stdio') { const parts = target.value.trim().split(/\s+/); body.command = parts[0]; body.args = parts.slice(1); body.env = pairs; body.password = password.value; }
    else { body.url = target.value.trim(); body.headers = pairs; }
    try { await post('/api/connectors', body); toast('Added'); reload(); } catch (e) { errorToast(e); }
  };
  pane.append(section('Connectors',
    h('p', { class: 'muted small' }, 'Connectors are MCP servers that give baabaa more tools. When there are many, the model finds the ones it needs with a search, so they do not fill its context. Tools a server marks read-only run directly; others follow the conversation’s mode.'),
    d.can_add_programs ? h('p', { class: 'muted small' }, 'A program on this computer runs with your own permissions, outside the tool sandbox. Add only programs you trust.') : null,
    list),
    section('Add a connector', h('div', { class: 'form-grid' }, name, kind), target, extra, password, h('div', { class: 'dialog-actions' }, h('button', { class: 'btn btn-primary', type: 'button', onclick: add }, 'Add'))));
}

// Usage --------------------------------------------------------------------------------------------------------------
async function usageTab(pane) {
  const { renderUsage } = await import('./usage.js');
  renderUsage(pane);
}

// Accounts (owner) -----------------------------------------------------------------------------------------------------
async function accounts(pane) {
  let d;
  try { d = await get('/api/accounts'); } catch (e) { errorToast(e); return; }
  const list = h('div', { class: 'acct-list' });
  for (const a of d.accounts) list.appendChild(accountCard(a));
  const name = h('input', { class: 'input', placeholder: 'name (lowercase)' });
  const disp = h('input', { class: 'input', placeholder: 'Display name' });
  const pw = h('input', { class: 'input', type: 'password', placeholder: 'Password (optional)' });
  const role = h('select', { class: 'input' }, h('option', { value: 'user' }, 'Member'), h('option', { value: 'owner' }, 'Owner'));
  pane.append(section('Accounts', h('p', { class: 'muted small' }, 'Each account has its own conversations, files and settings. Members use only the folders you grant them.'), list),
    section('Add an account', h('div', { class: 'form-grid' }, disp, name, pw, role,
      h('button', { class: 'btn btn-primary', type: 'button', onclick: async () => {
        try { await post('/api/accounts', { name: name.value.trim(), display_name: disp.value.trim(), password: pw.value || null, role: role.value }); reloadAccounts(); } catch (e) { errorToast(e); }
      } }, 'Add'))));
}

function reloadAccounts() { if (M) openSettings('accounts'); }

function accountCard(a) {
  const upd = async body => { try { await patch(`/api/accounts/${a.id}`, body); reloadAccounts(); } catch (e) { errorToast(e); } };
  const grants = h('div', { class: 'grants' });
  for (const g of a.grants) {
    grants.appendChild(h('div', { class: 'rule' }, h('span', { class: 'mono' }, `${g.access === 'rw' ? 'read-write' : 'read-only'}  ${g.path}`),
      h('button', { class: 'icon-btn tiny', type: 'button', title: 'Revoke', onclick: async () => { try { await del(`/api/accounts/${a.id}/grants`, { path: g.path }); reloadAccounts(); } catch (e) { errorToast(e); } } }, icon('trash', 14))));
  }
  const gpath = h('input', { class: 'input mono', placeholder: '/path/to/folder' });
  const gaccess = h('select', { class: 'input' }, h('option', { value: 'rw' }, 'read-write'), h('option', { value: 'ro' }, 'read-only'));
  return h('div', { class: 'acct' },
    h('div', { class: 'acct-head' }, avatar(a, 36), h('div', null, h('strong', null, a.display_name), h('div', { class: 'muted small' }, `${a.name} · ${a.role === 'owner' ? 'Owner' : 'Member'} · ${a.has_password ? 'password' : 'no password'}${a.disabled ? ' · disabled' : ''}`))),
    a.role !== 'owner' ? row('Actions that ask need an owner’s approval', switchEl(a.require_owner_approval, v => upd({ require_owner_approval: v }))) : null,
    h('div', { class: 'acct-actions' },
      h('button', { class: 'btn small', type: 'button', onclick: () => upd({ role: a.role === 'owner' ? 'user' : 'owner' }) }, a.role === 'owner' ? 'Make member' : 'Make owner'),
      h('button', { class: 'btn small', type: 'button', onclick: async () => { const p = await promptDialog('Set password', 'New password (empty removes it)', '', 'Save', { type: 'password' }); if (p !== null) upd({ password: p || null }); } }, 'Password'),
      h('button', { class: 'btn small', type: 'button', onclick: () => upd({ disabled: !a.disabled }) }, a.disabled ? 'Enable' : 'Disable'),
      a.id !== S.me.id ? h('button', { class: 'btn small btn-danger-soft', type: 'button', onclick: async () => {
        if (await confirmDialog('Delete account?', `${a.display_name} will no longer be able to sign in. Their files stay on disk until you remove them.`, 'Delete', true)) {
          try { await del(`/api/accounts/${a.id}`); reloadAccounts(); } catch (e) { errorToast(e); }
        }
      } }, 'Delete') : null),
    a.role !== 'owner' ? h('div', { class: 'acct-grants' }, h('h4', null, 'Folders'), grants,
      h('div', { class: 'rule-add' }, gpath, gaccess, h('button', { class: 'btn', type: 'button', onclick: async () => {
        try { await post(`/api/accounts/${a.id}/grants`, { path: gpath.value.trim(), access: gaccess.value }); reloadAccounts(); } catch (e) { errorToast(e); }
      } }, 'Grant'))) : null);
}

// About ------------------------------------------------------------------------------------------------------------------
async function about(pane) {
  let st = {};
  try { st = await get('/api/status'); } catch (e) { errorToast(e); }
  const gpu = st.gpu || {};
  const updates = await import('./update.js');
  updates.onReopen(() => { if (M && M.tab === 'about') openSettings('about'); });
  const upd = await updates.updatesSection(section, row);
  const net = await updates.networkSection(section, row);
  pane.append(...[  // sections a viewer does not get are null: leave them out (append would write "null")
    section('baabaa', h('p', null, `Version ${st.version || '?'}. A local assistant and coding agent on your own GPU.`),
      h('ul', { class: 'about-list' },
        h('li', null, `Ollama: ${st.ollama ? `running (${st.ollama})` : 'not reachable'}`),
        h('li', null, `GPU: ${gpu.name || 'unknown'}${gpu.vram_used != null ? ` · ${bytes(gpu.vram_used)} of ${bytes(gpu.vram_total)} in use` : ''}${gpu.temp_c != null ? ` · ${gpu.temp_c} °C` : ''}${gpu.power_mw != null ? ` · ${(gpu.power_mw / 1000).toFixed(0)} W` : ''}`),
        h('li', null, `GPU queue: ${st.queue && st.queue.busy ? `busy (${(st.queue.running || {}).label || (st.queue.running || {}).kind || ''}), ${st.queue.waiting} waiting` : 'idle'}`),
        h('li', null, `Open windows for your account: ${st.windows ?? '?'}`))),
    upd,
    net,
    st.network === 'local' ? null : section('Phones and other computers', h('p', { class: 'muted' }, 'baabaa uses its own certificate authority for HTTPS on your network. Install it once on each device to avoid warnings and to allow the microphone.'),
      h('a', { class: 'btn', href: '/ca.crt' }, icon('download', 15), ' Download the certificate')),
    section('Keyboard', h('table', { class: 'keys' }, [
      ['Enter', 'Send (Shift+Enter for a new line)'], ['Esc', 'Stop the reply'], ['Shift+Tab', 'Change mode (in a folder)'],
      ['Ctrl+K', 'Search'], ['Ctrl+Shift+O', 'New chat'], ['Ctrl+.', 'Show or hide the sidebar'], ['Ctrl+,', 'Settings'],
    ].map(([k, v]) => h('tr', null, h('td', null, h('kbd', null, k)), h('td', null, v)))))].filter(Boolean));
}
