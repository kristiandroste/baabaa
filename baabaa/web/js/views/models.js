// Settings > Models: installed models and their GPU fit, approval, installs with live progress, and
// finding models: newer versions of the installed ones (all, or one), or a search by need or category.
import { S, onState, loadModels } from '../app.js';
import { get, post, patch } from '../api.js';
import { h, clear, icon, bytes, tokens, ctxk, toolNote, ago } from '../dom.js';
import { toast, errorToast, confirmDialog, switchEl } from '../ui.js';

let pane = null;
let off = null;
let lastQuery = '';
let installName = '';
let reveal = false;  // scroll the results into view once a search the owner started shows up
const HIDDEN_FAILURES = 'baabaa.hiddenFailures';
const hiddenFailures = new Set((() => { try { return JSON.parse(localStorage.getItem(HIDDEN_FAILURES) || '[]'); } catch { return []; } })());

export function renderModels(el) {
  pane = el;
  if (off) off();
  off = onState(w => { if ((w === 'jobs' || w === 'models') && pane && pane.isConnected) draw(); else if (!pane || !pane.isConnected) { off && off(); off = null; } });
  draw();
  if (S.me.role === 'owner') loadModels();
  if (S.findModels && S.me.role === 'owner') {  // the home screen's "Find a model"
    const want = S.findModels;
    S.findModels = null;
    find(want);
  }
}

function draw() {
  if (!pane) return;
  const scroller = pane.closest('.settings-pane') || pane;  // what scrolls is the settings page around this tab
  const scroll = scroller.scrollTop;
  // live job updates redraw the page: keep the text box being typed in
  const active = document.activeElement;
  const focusKey = active && pane.contains(active) ? active.dataset.key : null;
  const caret = focusKey ? active.selectionStart : null;
  clear(pane);
  if (S.me.role !== 'owner') return drawMember();
  const d = S.modelsFull || {};
  const jobs = [...S.jobs.values()].sort((a, b) => b.created_ms - a.created_ms);
  const running = jobs.filter(j => j.status === 'running');
  const scoutState = d.scout || {};
  const lastScout = jobs.find(j => j.kind === 'scout');
  const vram = d.gpu && d.gpu.vram_total;

  pane.appendChild(h('section', { class: 'set-section' },
    h('div', { class: 'set-head' }, h('h3', null, 'Models'),
      h('div', { class: 'set-head-actions' },
        h('button', { class: 'btn', type: 'button', onclick: async () => { try { await post('/api/models/sync', {}); await loadModels(); toast('Model list refreshed'); } catch (e) { errorToast(e); } } }, icon('refresh', 15), ' Refresh'))),
    h('p', { class: 'muted small' }, `Every model must run entirely in GPU memory${vram ? ` (${bytes(vram)} on ${d.gpu.name})` : ''}. `
      + 'A fit test loads each model at growing context sizes and keeps the largest that fits fully. Only models that pass and that you approve appear in the model picker.')));

  pane.appendChild(gpuSection(d.gpu || {}));
  const others = running.filter(j => j.kind !== 'scout');  // a search shows its progress with its results
  if (others.length) {
    pane.appendChild(h('section', { class: 'set-section' }, h('h3', null, 'In progress'), others.map(jobRow)));
  }
  const failed = jobs.filter(j => j.status === 'error' && (j.kind === 'pull' || j.kind === 'fit') && !hiddenFailures.has(j.id)
    && Date.now() - (j.updated_ms || 0) < 864e5);
  if (failed.length) {
    pane.appendChild(h('section', { class: 'set-section' }, h('h3', null, 'Didn’t finish'), failed.map(failRow)));
  }
  pane.appendChild(findSection(scoutState));
  const results = lastScout && lastScout.id !== scoutState.cleared ? scoutSection(lastScout, scoutState) : null;
  if (results) pane.appendChild(results);

  const installed = (d.installed || []).slice().sort((a, b) => (b.approved - a.approved) || ((b.fit || {}).fits === true) - ((a.fit || {}).fits === true) || a.name.localeCompare(b.name));
  const table = h('div', { class: 'model-list' });
  for (const m of installed) table.appendChild(modelRow(m, vram));
  pane.appendChild(h('section', { class: 'set-section' }, h('h3', null, 'Installed models'), table));

  const name = h('input', { class: 'input mono', placeholder: 'e.g. qwen3.5:4b', value: installName, 'data-key': 'install' });
  name.addEventListener('input', () => { installName = name.value; });
  pane.appendChild(h('section', { class: 'set-section' }, h('h3', null, 'Install by name'),
    h('div', { class: 'rule-add' }, name, h('button', { class: 'btn', type: 'button', onclick: () => install(name.value.trim()) }, icon('download', 15), ' Install')),
    h('p', { class: 'muted small' }, 'After the download, the fit test runs by itself. Models too large for the GPU are never offered.')));

  pane.appendChild(externalSection());
  pane.appendChild(settingsSection(d.settings || {}));
  if (focusKey) {
    const el = pane.querySelector(`[data-key="${focusKey}"]`);
    if (el) { el.focus({ preventScroll: true }); try { el.setSelectionRange(caret, caret); } catch { /* not a text box */ } }
  }
  scroller.scrollTop = scroll;  // after the focus: putting the caret back scrolls to the text box
  if (reveal && results) {  // follow the search the owner started until its results are in
    const finished = lastScout.status !== 'running';
    results.scrollIntoView({ block: finished ? 'start' : 'nearest' });
    if (finished) reveal = false;
  }
}

// Finding models: a free-text search, categories, and newer versions of what is installed ------------------
function findSection(scoutState) {
  const q = h('input', { class: 'input', value: lastQuery, 'data-key': 'find',
    placeholder: 'What are you looking for? e.g. “best model for coding” or “reads screenshots”' });
  q.addEventListener('input', () => { lastQuery = q.value; });
  const go = () => { const v = q.value.trim(); if (v) find({ query: v }); };
  q.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); go(); } });
  return h('section', { class: 'set-section' }, h('h3', null, 'Find models'),
    h('div', { class: 'rule-add' }, q, h('button', { class: 'btn btn-primary', type: 'button', onclick: go }, icon('search', 15), ' Search')),
    h('div', { class: 'find-cats' },
      (scoutState.categories || []).map(c => h('button', { class: 'chip', type: 'button', onclick: () => find({ category: c.id }) }, c.label)),
      h('button', { class: 'chip', type: 'button', title: 'Look for newer versions of every model you have', onclick: () => find({}) }, 'Newer versions of mine')),
    h('p', { class: 'muted small' }, 'Searches the Ollama library and suggests only models that fit this GPU, chosen by your default model. '
      + 'For newer versions of one model you have, use Newer? on its row below.'));
}

function scoutTitle(p) {
  const cat = (((S.modelsFull || {}).scout || {}).categories || []).find(c => c.id === p.category);
  if (p.query) return `Models for “${p.query}”`;
  if (p.category) return `${cat ? cat.label : p.category} models`;
  if (p.model) return `Newer than ${p.model}`;
  return 'Newer versions of your models';
}

const recKey = rec => rec.kind === 'rebuild' ? `${rec.model}@${rec.digest || ''}` : rec.model;
const SCOUT_PHASE = { installed: 'Reading installed models…', library: 'Reading the Ollama library…', thinking: 'Your default model is choosing…', verifying: 'Checking sizes against the GPU…' };

// The GPU: what holds its memory, and the owner's pause switch -------------------------------------------
function gpuSection(g) {
  const others = g.others || [];
  const paused = g.paused;
  const lines = [];
  if (g.vram_total) lines.push(`${bytes(g.vram_used || 0)} of ${bytes(g.vram_total)} in use.`);
  if (others.length) lines.push('Other programs hold GPU memory: ' + others.map(o => `${o.name} (${bytes(o.used || 0)})`).join(', ') + '. Models may not fit until they finish.');
  if (g.llamacpp) lines.push(`llama.cpp server running: ${g.llamacpp.name} at ${ctxk(g.llamacpp.num_ctx)} context.`);
  const reason = h('input', { class: 'input', placeholder: 'Reason (optional), e.g. “another project tonight”', value: paused ? paused.reason || '' : '' });
  const sw = switchEl(!!paused, async v => {
    try { await post('/api/gpu/pause', { paused: v, reason: reason.value.trim() }); await loadModels(); toast(v ? 'GPU work paused' : 'GPU work resumed'); } catch (e) { errorToast(e); }
  }, paused ? 'Paused' : 'Pause GPU work');
  // Ollama passes these on to the llama.cpp server it runs each model with; without them that server may
  // hold up to 8 GiB of old conversations in system RAM, plus up to 32 snapshots each.
  const missing = g.ollama_ram_caps || [];
  const ctxWarn = missing.length ? h('div', { class: 'notice ollama-ram' },
    h('div', null, h('strong', null, 'Cap Ollama’s memory use. '),
      'Its model server may keep up to 8 GiB of old conversations in system RAM. These lines cap that at about 2 GB. Add them to Ollama’s service (',
      h('code', null, 'sudo systemctl edit ollama'), '), then restart it (', h('code', null, 'sudo systemctl restart ollama'), '):',
      h('pre', null, '[Service]\n' + missing.map(l => `Environment="${l}"`).join('\n')))) : null;
  return h('section', { class: 'set-section' + (paused ? ' paused' : '') }, h('h3', null, 'GPU'),
    h('p', { class: 'muted small' }, lines.join(' ') || 'No GPU readings available.'), ctxWarn,
    h('div', { class: 'set-row' }, h('div', { class: 'set-label' }, h('span', null, 'Pause baabaa’s GPU work'),
      h('span', { class: 'muted small' }, paused ? `Paused by ${paused.by}${paused.reason ? `: ${paused.reason}` : ''}. Messages wait in the queue until you resume.`
        : 'For when another program needs the GPU: baabaa unloads its models and starts nothing new until you resume.')),
      h('div', { class: 'set-controls' }, paused ? null : reason, sw)));
}

// Models run by other programs: llama.cpp (chat) and stable-diffusion.cpp (images) ---------------------------
function externalSection() {
  const input = (ph, type) => h('input', { class: 'input' + (type ? '' : ' mono'), placeholder: ph, type: type || 'text' });
  const f = {
    runtime: h('select', { class: 'input' }, h('option', { value: 'llamacpp' }, 'Chat model, served by llama.cpp'),
      h('option', { value: 'sdcpp' }, 'Image model, served by stable-diffusion.cpp')),
    name: input('bonsai-2-27b'), server: input('/path/to/llama-server or sd-server'),
    model: input('/path/to/model.gguf'), mmproj: input('Vision projector .gguf (optional)'),
    diffusion_model: input('/path/to/diffusion-model.gguf'), llm: input('/path/to/text-encoder.gguf'),
    vae: input('/path/to/vae.safetensors'), llm_vision: input('Text encoder’s vision projector, for editing (optional)'),
    steps: input('e.g. 8'), cfg: input('e.g. 1.0'),
    libs: input('Library folders, comma-separated (optional)'), args: input('Extra server arguments (optional)'),
    password: input('Your password', 'password'),
  };
  const edit = h('input', { type: 'checkbox' });
  const staged = h('input', { type: 'checkbox' });
  const label = { name: 'Name', server: 'Server program', model: 'Model file', mmproj: 'Vision', diffusion_model: 'Diffusion model',
    llm: 'Text encoder', vae: 'VAE', llm_vision: 'Vision (edits)', steps: 'Steps', cfg: 'Guidance', libs: 'Libraries', args: 'Arguments', password: 'Password' };
  const fieldsBox = h('div', { class: 'ext-fields' });
  const draw = () => {
    clear(fieldsBox);
    const image = f.runtime.value === 'sdcpp';
    const keys = image ? ['name', 'server', 'diffusion_model', 'llm', 'vae', 'llm_vision', 'steps', 'cfg', 'libs', 'args'] : ['name', 'server', 'model', 'mmproj', 'libs', 'args'];
    for (const k of keys) fieldsBox.appendChild(h('label', { class: 'ext-field' }, h('span', { class: 'small' }, label[k]), f[k]));
    if (image) {
      fieldsBox.appendChild(h('label', { class: 'ext-check' }, edit, h('span', null, 'It can change images (editing with reference images)')));
      fieldsBox.appendChild(h('label', { class: 'ext-check' }, staged, h('span', null,
        h('strong', null, 'Staged'), ' — for a model too large to keep in VRAM whole: each part (text encoder, image model, decoder) is read from its file into VRAM for its step and released after (',
        h('code', null, '--params-backend disk'), '). All computing stays on the GPU and system memory stays small; images take longer because the parts are reloaded for each one.')));
    }
    fieldsBox.appendChild(h('label', { class: 'ext-field' }, h('span', { class: 'small' }, label.password), f.password));
  };
  f.runtime.addEventListener('change', draw);
  draw();
  const form = h('div', { class: 'ext-form', hidden: true },
    h('label', { class: 'ext-field' }, h('span', { class: 'small' }, 'Kind'), f.runtime), fieldsBox,
    h('p', { class: 'muted small' }, 'This runs a program on this computer, so it needs your password. baabaa starts the server itself, bound to this computer only, and refuses the model unless it is on the GPU as the rules require. The fit test runs next.'),
    h('div', { class: 'dialog-actions' }, h('button', { class: 'btn btn-primary', type: 'button', onclick: async () => {
      const v = k => f[k].value.trim();
      const body = { runtime: f.runtime.value, name: v('name'), server: v('server'), lib_dirs: v('libs').split(',').map(x => x.trim()).filter(Boolean),
        args: v('args') ? v('args').split(/\s+/) : [], password: f.password.value };
      if (body.runtime === 'sdcpp') Object.assign(body, { diffusion_model: v('diffusion_model'), llm: v('llm'), vae: v('vae'), llm_vision: v('llm_vision'),
        steps: v('steps') ? Number(v('steps')) : null, cfg: v('cfg') ? Number(v('cfg')) : null, edit: edit.checked, staged: staged.checked });
      else Object.assign(body, { model: v('model'), mmproj: v('mmproj') || null });
      try { await post('/api/models/external', body); f.password.value = ''; form.hidden = true; await loadModels(); toast(`Registered ${body.name}; testing it on the GPU…`); }
      catch (e) { errorToast(e); }
    } }, 'Add and test')));
  return h('section', { class: 'set-section' },
    h('div', { class: 'set-head' }, h('h3', null, 'Models from other programs'),
      h('button', { class: 'btn', type: 'button', onclick: () => { form.hidden = !form.hidden; } }, icon('plus', 15), ' Add')),
    h('p', { class: 'muted small' }, 'Chat models that need a special build of llama.cpp (for example 1-bit and ternary models), and image models served by stable-diffusion.cpp. baabaa runs them through the same GPU queue and checks, one program at a time. From a terminal: ',
      h('code', null, 'baabaa models add-llamacpp …'), ' or ', h('code', null, 'add-sdcpp …')),
    form);
}

function drawMember() {
  pane.appendChild(h('section', { class: 'set-section' }, h('h3', null, 'Available models'),
    h('p', { class: 'muted small' }, 'The owner chooses which models are available. All of them run fully on this computer’s GPU.'),
    h('div', { class: 'model-list' }, S.models.map(m => h('div', { class: 'model-row' },
      h('div', { class: 'model-main' }, h('strong', null, m.name), h('span', { class: 'muted small' }, capsText(m))),
      h('div', { class: 'model-fit' }, [`${ctxk(m.num_ctx)} context`, m.tokens_per_s ? `${Math.round(m.tokens_per_s)} tokens/s` : null, toolNote(m)].filter(Boolean).join(' · ')))))));
}

function capsText(m) {
  const caps = (m.capabilities || []).filter(c => c !== 'completion');
  return [m.parameter_size, m.quantization, bytes(m.size), caps.join(', ')].filter(Boolean).join(' · ');
}

function fitText(m) {
  const f = m.fit || {};
  if (m.role === 'image' && f.fits === true) return `Fits${f.staged ? ' (staged)' : ''} · ${f.seconds_1024 ? `${Math.round(f.seconds_1024)} s per 1024² image`
    : f.seconds_512 ? `${f.seconds_512} s per 512² image` : 'tested'}`;
  if (f.fits === true) return `Fits up to ${ctxk(f.max_ctx)} context${f.tokens_per_s ? ` · ${f.tokens_per_s} tokens/s` : ''}${toolsText(f.tools)}`;
  if (f.fits === false) return f.reason || 'Does not fit in GPU memory';
  return 'Not tested yet';
}

function toolsText(t) {
  // the fit test's tool checks: calls a tool when it must, answers without one, writes a long argument
  if (!t || !t.of) return '';
  return t.passed >= t.of ? ` · tool use ${t.passed}/${t.of}` : ` · unreliable with tools (${t.passed}/${t.of} checks)`;
}

function modelRow(m, vram) {
  const f = m.fit || {};
  const tooBig = vram && m.size && m.size > vram;
  const job = [...S.jobs.values()].find(j => j.status === 'running' && j.params && j.params.model === m.name);
  const ctxOptions = (f.steps || []).filter(s => s.fits).map(s => s.ctx);
  const ctxSel = h('select', { class: 'input small-input', disabled: !m.approved || null, title: 'Context size used for this model' },
    ctxOptions.map(c => h('option', { value: c, selected: c === m.num_ctx || null }, `${ctxk(c)} context`)));
  ctxSel.addEventListener('change', () => approve(m.name, true, Number(ctxSel.value)));
  const actions = h('div', { class: 'model-actions' });
  if (m.role === 'chat' || m.role === 'embed' || m.role === 'image') {
    actions.append(...[
      f.fits === true ? switchEl(m.approved, v => approve(m.name, v), m.approved ? 'Approved' : 'Approve') : null,
      m.approved && ctxOptions.length ? ctxSel : null,
      !tooBig && !job ? h('button', { class: 'btn small', type: 'button', onclick: () => fitTest(m.name) }, f.fits === undefined ? 'Test fit' : 'Test again') : null,
      (!m.runtime || m.runtime === 'ollama') && !m.name.includes('/') && !job
        ? h('button', { class: 'btn small', type: 'button', title: 'Look for newer versions of this model', onclick: () => find({ model: m.name }) }, 'Newer?') : null,
      h('button', { class: 'icon-btn tiny', type: 'button', title: m.runtime === 'ollama' ? 'Remove from Ollama' : 'Unregister (the files stay)', onclick: () => remove(m) }, icon('trash', 15)),
    ].filter(Boolean));
  }
  const runtime = m.runtime && m.runtime !== 'ollama' ? h('span', { class: 'badge', title: (m.source || {}).model || '' }, m.runtime === 'llamacpp' ? 'llama.cpp' : m.runtime === 'sdcpp' ? 'stable-diffusion.cpp' : m.runtime) : null;
  return h('div', { class: 'model-row' + (m.approved ? ' approved' : '') + (f.fits === false || tooBig ? ' nofit' : '') },
    h('div', { class: 'model-main' }, h('strong', null, m.name, runtime ? ' ' : null, runtime), h('span', { class: 'muted small' }, capsText(m) + (m.role === 'embed' ? ' · embeddings' : m.role === 'image' ? ' · images' : ''))),
    h('div', { class: 'model-fit' }, job ? jobInline(job) : tooBig ? 'Larger than GPU memory: not allowed' : fitText(m)),
    actions);
}

function failRow(j) {
  const p = j.params || {};
  return h('div', { class: 'job-row failed' },
    h('div', { class: 'job-text' }, h('strong', null, `${j.kind === 'pull' ? 'Installing' : 'GPU fit test of'} ${p.model}`),
      h('span', { class: 'small error-text' }, j.error || 'It stopped without a reason.'), h('span', { class: 'muted small' }, ago(j.updated_ms))),
    j.kind === 'pull' ? h('button', { class: 'btn small', type: 'button', onclick: () => install(p.model) }, 'Try again') : null,
    h('button', { class: 'icon-btn tiny', type: 'button', title: 'Dismiss', onclick: () => {
      hiddenFailures.add(j.id);
      try { localStorage.setItem(HIDDEN_FAILURES, JSON.stringify([...hiddenFailures].slice(-200))); } catch { /* private window */ }
      draw();
    } }, icon('x', 15)));
}

function jobInline(j) {
  const p = j.progress || {};
  if (j.kind === 'scout') return 'Looking for newer versions…';
  if (j.kind === 'fit') return `Fit test: ${p.phase === 'speed' ? 'measuring speed' : p.phase === 'tools' ? 'checking tool use' : `loading at ${ctxk(p.ctx)} context`}…`;
  if (j.kind === 'pull') return `Installing… ${p.total ? Math.round(100 * p.completed / p.total) : 0}%`;
  return 'Working…';
}

function jobRow(j) {
  const p = j.progress || {};
  let label, pct = null, detail = '';
  if (j.kind === 'pull') {
    label = `Installing ${j.params.model}`;
    if (p.total) { pct = 100 * p.completed / p.total; detail = `${bytes(p.completed)} of ${bytes(p.total)}${p.rate ? ` · ${bytes(p.rate)}/s` : ''}`; }
    else detail = p.status || 'starting';
  } else if (j.kind === 'fit') {
    label = `GPU fit test: ${j.params.model}`;
    const steps = p.steps || [];
    pct = p.of ? 100 * steps.length / (p.of + 1) : null;
    detail = p.phase === 'speed' ? 'Measuring speed' : p.phase === 'tools' ? 'Checking how well it uses tools'
      : `Loading at ${ctxk(p.ctx)} context · ${steps.filter(s => s.fits).length} sizes fit so far`;
  } else {
    label = scoutTitle(j.params || {});
    detail = SCOUT_PHASE[p.phase] || 'Starting…';
  }
  return h('div', { class: 'job-row' },
    h('div', { class: 'job-text' }, h('strong', null, label), h('span', { class: 'muted small' }, detail)),
    h('span', { class: 'bar wide' }, h('span', { class: 'bar-fill' + (pct === null ? ' indeterminate' : ''), style: { width: (pct ?? 35) + '%' } })),
    h('button', { class: 'btn small', type: 'button', onclick: async () => { try { await post(`/api/jobs/${j.id}/cancel`, {}); } catch (e) { errorToast(e); } } }, 'Cancel'));
}

function scoutSection(j, scoutState) {
  const p = j.params || {};
  const done = j.status !== 'running';
  const box = h('section', { class: 'set-section scout' },
    h('div', { class: 'set-head' }, h('h3', null, scoutTitle(p)),
      h('div', { class: 'set-head-actions' },
        done ? h('button', { class: 'btn small', type: 'button', onclick: () => find(p) }, icon('refresh', 15), ' Check again') : null,
        h('button', { class: 'icon-btn tiny', type: 'button', title: done ? 'Clear these results' : 'Stop', onclick: () => done ? clearResults(j.id) : cancel(j.id) }, icon('x', 15)))));
  if (!done) {
    box.appendChild(h('div', { class: 'job-row' }, h('span', { class: 'muted' }, SCOUT_PHASE[(j.progress || {}).phase] || 'Starting…'),
      h('span', { class: 'bar wide' }, h('span', { class: 'bar-fill indeterminate', style: { width: '35%' } }))));
    return box;
  }
  if (j.status === 'error') { box.appendChild(h('p', { class: 'notice notice-error' }, `The search failed: ${j.error}`)); return box; }
  if (j.status === 'cancelled') { box.appendChild(h('p', { class: 'muted' }, 'This search was stopped.')); return box; }
  const r = j.result || {};
  const dismissed = new Set(scoutState.dismissed || []);
  const all = r.recommendations || [];
  const recs = all.filter(rec => !dismissed.has(recKey(rec)));
  const old = Date.now() - (j.updated_ms || 0) > 7 * 864e5;
  const why = (/\((.*)\)$/.exec(r.method || '') || [])[1];
  box.appendChild(h('p', { class: 'muted small' + (old ? ' stale' : '') }, `Checked ${ago(j.updated_ms)} in the Ollama library`
    + (r.judge ? `, chosen by ${r.judge}.` : `, ranked by name, date and popularity${why ? ` (${why})` : ''}.`)
    + (old ? ' The library changes often, so check again for current results.' : '')));
  if (r.note) box.appendChild(h('p', null, r.note));
  else if (!recs.length) {
    box.appendChild(h('p', null, all.length ? 'You have hidden every suggestion from this search.'
      : p.query || p.category ? 'Nothing in the library matches that and fits this GPU.'
        : p.model ? `No newer version of ${p.model} was found${p.other_families ? '' : ' in its family'}.` : `No newer versions of your models were found${p.other_families ? '' : ' in their families'}.`));
  }
  const installedNames = new Set((S.modelsFull.installed || []).map(m => m.name));
  for (const rec of recs) {
    const installing = [...S.jobs.values()].find(x => x.status === 'running' && x.kind === 'pull' && x.params && x.params.model === rec.model);
    const have = installedNames.has(rec.model) && rec.kind !== 'rebuild';
    box.appendChild(h('div', { class: 'rec' },
      h('div', { class: 'rec-main' },
        h('div', { class: 'rec-title' }, h('strong', { class: 'mono' }, rec.model),
          rec.kind === 'rebuild' ? h('span', { class: 'badge' }, 'newer build') : rec.replaces ? h('span', { class: 'muted small' }, `instead of ${rec.replaces}`) : null),
        h('div', { class: 'small' }, rec.reason),
        rec.requires ? h('div', { class: 'small error-text' }, `Needs Ollama ${rec.requires} or newer; this computer has ${rec.ollama || 'an older one'}. Update Ollama to install it.`) : null,
        h('div', { class: 'muted small' }, [rec.download_bytes ? `${bytes(rec.download_bytes)} download` : null, rec.context ? `${ctxk(rec.context)} context (native)` : null,
          rec.inputs ? `input: ${rec.inputs}` : null, (rec.capabilities || []).join(', '), rec.updated ? `updated ${ago(Date.parse(rec.updated))}` : null,
          rec.pulls ? `${rec.pulls} downloads` : null].filter(Boolean).join(' · '))),
      h('div', { class: 'rec-actions' },
        have ? h('span', { class: 'muted small' }, 'Installed')
          : installing ? jobInline(installing)
            : h('button', { class: 'btn btn-primary', type: 'button', disabled: rec.requires ? true : null, title: rec.requires ? 'Needs a newer Ollama' : null,
              onclick: () => install(rec.model) }, icon('download', 15), ' Install'),
        h('button', { class: 'icon-btn tiny', type: 'button', title: 'Not interested: never suggest this again', onclick: () => dismiss(recKey(rec)) }, icon('x', 15)))));
  }
  if (!p.query && !p.category && !p.other_families && !r.note) {
    box.appendChild(h('p', { class: 'muted small' }, 'Only the same family (for example qwen to a newer qwen) is suggested as an update. ',
      h('button', { class: 'chip', type: 'button', onclick: () => find({ ...p, other_families: true }) }, 'Also look at other makers')));
  }
  if (recs.some(rec => !installedNames.has(rec.model) || rec.kind === 'rebuild')) {
    box.appendChild(h('p', { class: 'muted small' }, 'Installing downloads the model into Ollama in the background; you can keep chatting. The fit test then runs automatically, and you approve the model here once it passes.'));
  }
  return box;
}

function settingsSection(s) {
  const names = S.models.map(m => m.name);
  const def = h('select', { class: 'input' }, names.map(n => h('option', { value: n, selected: n === (s.default_model || S.defaultModel) || null }, n)));
  def.addEventListener('change', () => saveMachine({ default_model: def.value }));
  const judge = h('select', { class: 'input' }, h('option', { value: '' }, 'The conversation’s own model'),
    names.map(n => h('option', { value: n, selected: n === s.judge_model || null }, n)));
  judge.addEventListener('change', () => saveMachine({ judge_model: judge.value || null }));
  const keep = h('input', { class: 'input small-input', value: s.keep_alive || '30m', title: 'e.g. 30m, 2h, -1 (forever)' });
  keep.addEventListener('change', () => saveMachine({ keep_alive: keep.value.trim() }));
  const maxctx = h('select', { class: 'input small-input' }, [16384, 32768, 65536, 131072, 262144].map(c => h('option', { value: c, selected: c === Number(s.max_default_ctx || 65536) || null }, ctxk(c))));
  maxctx.addEventListener('change', () => saveMachine({ max_default_ctx: Number(maxctx.value) }));
  return h('section', { class: 'set-section' }, h('h3', null, 'For everyone'),
    h('div', { class: 'set-row' }, h('div', { class: 'set-label' }, 'Default model'), def),
    h('div', { class: 'set-row' }, h('div', { class: 'set-label' }, h('span', null, 'Auto-mode judge'), h('span', { class: 'muted small' }, 'Using the conversation’s model avoids swapping models on the GPU.')), judge),
    h('div', { class: 'set-row' }, h('div', { class: 'set-label' }, h('span', null, 'Keep models loaded for'), h('span', { class: 'muted small' }, 'How long an idle model stays in GPU memory.')), keep),
    h('div', { class: 'set-row' }, h('div', { class: 'set-label' }, h('span', null, 'Largest default context'), h('span', { class: 'muted small' }, 'The fit test picks the largest size up to this that fits.')), maxctx));
}

async function saveMachine(body) {
  try { await patch('/api/settings', body); toast('Saved'); loadModels(); } catch (e) { errorToast(e); }
}

async function find(p) {
  const body = { model: p.model || null, query: p.query || null, category: p.category || null, other_families: !!p.other_families };
  try { await post('/api/models/scout', body); reveal = true; } catch (e) { errorToast(e); }
}

async function dismiss(key) {
  try { S.modelsFull.scout = await post('/api/models/scout/dismiss', { key }); draw(); } catch (e) { errorToast(e); }
}

async function clearResults(id) {
  try { S.modelsFull.scout = await post('/api/models/scout/dismiss', { clear: id }); draw(); } catch (e) { errorToast(e); }
}

async function cancel(id) {
  try { await post(`/api/jobs/${id}/cancel`, {}); } catch (e) { errorToast(e); }
}

async function install(name) {
  if (!name) return;
  try { await post('/api/models/pull', { model: name }); toast(`Installing ${name}…`); } catch (e) { errorToast(e); }
}

async function fitTest(name) {
  try { await post('/api/models/test', { model: name }); toast(`Testing ${name} on the GPU…`); } catch (e) { errorToast(e); }
}

async function approve(name, approved, num_ctx) {
  try { await post('/api/models/approve', { model: name, approved, num_ctx }); await loadModels(); } catch (e) { errorToast(e); }
}

async function remove(m) {
  const name = m.name;
  const ollama = !m.runtime || m.runtime === 'ollama';
  const text = ollama ? `${name} will be deleted from Ollama. Other programs on this computer that use it will lose it too.`
    : `${name} will no longer be offered. Its program and model files stay where they are.`;
  if (!await confirmDialog(ollama ? 'Remove model?' : 'Unregister model?', text, ollama ? 'Remove' : 'Unregister', true)) return;
  try { await post('/api/models/remove', { model: name }); await loadModels(); toast(`Removed ${name}`); } catch (e) { errorToast(e); }
}
