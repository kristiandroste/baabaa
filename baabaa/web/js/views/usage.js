// Settings > Usage: statistics by day, model, account and tool; GPU readings; exports for analysis.
import { S } from '../app.js';
import { get, downloadFrom } from '../api.js';
import { h, clear, icon, tokens, duration, bytes, esc } from '../dom.js';
import { errorToast, segmented } from '../ui.js';

const RANGES = [{ value: 1, label: '24 h' }, { value: 7, label: '7 days' }, { value: 30, label: '30 days' }, { value: 90, label: '90 days' }, { value: 3650, label: 'All' }];
let state = { days: 7, group: 'day', account: 'all' };

export function renderUsage(pane) {
  clear(pane);
  const owner = S.me.role === 'owner';
  const controls = h('div', { class: 'usage-controls' },
    segmented(RANGES, state.days, v => { state.days = v; load(); }),
    owner ? accountSelect() : null);
  const out = h('div', { class: 'usage-out' });
  pane.append(h('section', { class: 'set-section' }, h('div', { class: 'set-head' }, h('h3', null, 'Usage'), controls),
    h('p', { class: 'muted small' }, owner ? 'Everything baabaa does is recorded as metadata (never the content of messages), for every account. Members see only their own usage.'
      : 'Your usage on this computer. Only metadata is recorded, never the content of messages.')), out, exportSection(owner));
  async function load() {
    out.style.minHeight = out.offsetHeight + 'px';  // new figures replace the old ones in place: the page keeps its place
    clear(out).appendChild(h('div', { class: 'muted' }, 'Loading…'));
    const to = Date.now() + 60000, from = Date.now() - state.days * 86400000;
    const tz = -new Date().getTimezoneOffset();
    const q = `from=${from}&to=${to}&tz=${tz}${owner ? `&account=${state.account}` : ''}`;
    try {
      const [byDay, byModel, byKind, gpu, byAccount] = await Promise.all([
        get(`/api/stats/summary?${q}&group=day`), get(`/api/stats/summary?${q}&group=model`), get(`/api/stats/summary?${q}&group=kind`),
        get(`/api/stats/gpu?from=${Math.max(from, Date.now() - 86400000)}&to=${to}`),
        owner && state.account === 'all' ? get(`/api/stats/summary?${q}&group=account_id`) : Promise.resolve(null)]);
      clear(out);
      out.style.minHeight = '';
      out.append(...[cards(byDay.total), chartSection(byDay.groups), tableSection('By model', byModel.groups, 'Model'),
        byAccount ? tableSection('By account', byAccount.groups, 'Account', true) : null,
        tableSection('By kind of request', byKind.groups, 'Kind'), toolsSection(byDay.tools), approvalsSection(byDay.approvals),
        gpuSection(gpu.samples)].filter(Boolean));
    } catch (e) { clear(out); out.style.minHeight = ''; errorToast(e); }
  }
  load();

  function accountSelect() {
    const sel = h('select', { class: 'input small-input' }, h('option', { value: 'all' }, 'All accounts'));
    get('/api/accounts').then(d => { for (const a of d.accounts) sel.appendChild(h('option', { value: a.id, selected: state.account === a.id || null }, a.display_name)); }).catch(() => {});
    sel.addEventListener('change', () => { state.account = sel.value; load(); });
    return sel;
  }
}

function cards(t) {
  const wh = t.energy_mj ? t.energy_mj / 3.6e6 : 0;
  const card = (label, value, hint) => h('div', { class: 'stat-card' }, h('div', { class: 'stat-value' }, value), h('div', { class: 'stat-label' }, label), hint ? h('div', { class: 'muted small' }, hint) : null);
  return h('div', { class: 'stat-cards' },
    card('Model requests', (t.requests || 0).toLocaleString(), `${t.conversations || 0} conversations`),
    card('Tokens in / out', `${tokens(t.prompt_tokens)} / ${tokens(t.output_tokens)}`),
    card('GPU time', duration(t.gpu_ms)),
    card('GPU energy', wh ? `${wh < 10 ? wh.toFixed(2) : wh.toFixed(0)} Wh` : '—', wh ? `≈ ${(wh / 1000).toFixed(3)} kWh` : null),
    card('Speed (median)', t.tokens_per_s_median ? `${t.tokens_per_s_median} tok/s` : '—'),
    card('First token (median)', t.ttft_ms_median != null ? duration(t.ttft_ms_median) : '—', t.queue_wait_ms_median ? `queue wait ${duration(t.queue_wait_ms_median)}` : null));
}

function chartSection(groups) {
  if (!groups.length) return h('section', { class: 'set-section' }, h('p', { class: 'muted' }, 'No requests in this period yet.'));
  const W = 640, H = 180, pad = 28;
  const max = Math.max(...groups.map(g => (g.prompt_tokens || 0) + (g.output_tokens || 0)), 1);
  const bw = Math.max(4, Math.min(36, (W - pad * 2) / groups.length - 4));
  let bars = '';
  groups.forEach((g, i) => {
    const x = pad + i * ((W - pad * 2) / groups.length) + 2;
    const hin = (H - pad * 1.5) * (g.prompt_tokens || 0) / max;
    const hout = (H - pad * 1.5) * (g.output_tokens || 0) / max;
    const y0 = H - pad;
    bars += `<rect x="${x}" y="${y0 - hin}" width="${bw}" height="${hin}" class="bar-in"><title>${esc(g.k)}: ${g.prompt_tokens} tokens in</title></rect>`;
    bars += `<rect x="${x}" y="${y0 - hin - hout}" width="${bw}" height="${hout}" class="bar-out"><title>${esc(g.k)}: ${g.output_tokens} tokens out</title></rect>`;
    if (groups.length <= 14 || i % Math.ceil(groups.length / 10) === 0) bars += `<text x="${x + bw / 2}" y="${H - 8}" text-anchor="middle" class="axis">${esc(String(g.k).slice(5))}</text>`;
  });
  const svg = `<svg viewBox="0 0 ${W} ${H}" class="chart" role="img" aria-label="Tokens per day"><line x1="${pad}" y1="${H - pad}" x2="${W - pad}" y2="${H - pad}" class="axis-line"/>${bars}<text x="${pad}" y="14" class="axis">${tokens(max)} tokens</text></svg>`;
  return h('section', { class: 'set-section' }, h('h3', null, 'Tokens per day'), h('div', { html: svg }),
    h('div', { class: 'legend' }, h('span', { class: 'lg lg-in' }), ' prompt ', h('span', { class: 'lg lg-out' }), ' output'));
}

function tableSection(title, groups, keyLabel, isAccount) {
  if (!groups.length) return null;
  return h('section', { class: 'set-section' }, h('h3', null, title), h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
    h('thead', null, h('tr', null, [keyLabel, 'Requests', 'Tokens in', 'Tokens out', 'GPU time', 'Energy', 'Stopped', 'Errors'].map(c => h('th', null, c)))),
    h('tbody', null, groups.map(g => h('tr', null, h('td', null, isAccount ? (g.label || g.k) : g.k || '—'), h('td', null, g.requests),
      h('td', null, tokens(g.prompt_tokens)), h('td', null, tokens(g.output_tokens)), h('td', null, duration(g.gpu_ms)),
      h('td', null, g.energy_mj ? `${(g.energy_mj / 3.6e6).toFixed(2)} Wh` : '—'), h('td', null, g.stopped || 0), h('td', null, g.errors || 0)))))));
}

function toolsSection(tools) {
  if (!tools || !tools.length) return null;
  return h('section', { class: 'set-section' }, h('h3', null, 'Tools'), h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
    h('thead', null, h('tr', null, ['Tool', 'Calls', 'Time', 'Failed'].map(c => h('th', null, c)))),
    h('tbody', null, tools.map(t => h('tr', null, h('td', { class: 'mono' }, t.tool), h('td', null, t.calls), h('td', null, duration(t.ms)), h('td', null, t.failed)))))));
}

function approvalsSection(rows) {
  if (!rows || !rows.length) return null;
  const label = { allow_once: 'allowed once', allow_always: 'always allowed', deny: 'denied' };
  return h('section', { class: 'set-section' }, h('h3', null, 'Approvals'),
    h('p', { class: 'muted small' }, 'How held actions were decided. Comparing the judge’s view with your answers shows how well Auto mode matches you.'),
    h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
      h('thead', null, h('tr', null, ['Held by', 'Judge said', 'You answered', 'Count'].map(c => h('th', null, c)))),
      h('tbody', null, rows.map(r => h('tr', null, h('td', null, r.layer || '—'), h('td', null, r.judge_decision || '—'), h('td', null, label[r.final] || r.final || 'no answer'), h('td', null, r.n)))))));
}

function gpuSection(samples) {
  if (!samples || samples.length < 2) return h('section', { class: 'set-section' }, h('h3', null, 'GPU (last 24 hours)'), h('p', { class: 'muted small' }, 'GPU readings appear here while models run.'));
  const W = 640, H = 160, pad = 28;
  const t0 = samples[0].ts_ms, t1 = samples[samples.length - 1].ts_ms || t0 + 1;
  const maxP = Math.max(...samples.map(s => s.power_mw || 0), 1);
  const maxV = Math.max(...samples.map(s => s.vram_used || 0), 1);
  const x = t => pad + (W - 2 * pad) * (t - t0) / Math.max(1, t1 - t0);
  const yP = p => H - pad - (H - 2 * pad) * p / maxP;
  const yV = v => H - pad - (H - 2 * pad) * v / maxV;
  const line = (f, key) => samples.map((s, i) => `${i ? 'L' : 'M'}${x(s.ts_ms).toFixed(1)},${f(s[key] || 0).toFixed(1)}`).join('');
  const svg = `<svg viewBox="0 0 ${W} ${H}" class="chart" role="img" aria-label="GPU power and memory"><line x1="${pad}" y1="${H - pad}" x2="${W - pad}" y2="${H - pad}" class="axis-line"/>`
    + `<path d="${line(yV, 'vram_used')}" class="line-vram"/><path d="${line(yP, 'power_mw')}" class="line-power"/>`
    + `<text x="${pad}" y="14" class="axis">${(maxP / 1000).toFixed(0)} W · ${bytes(maxV)}</text>`
    + `<text x="${pad}" y="${H - 8}" class="axis">${new Date(t0).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</text>`
    + `<text x="${W - pad}" y="${H - 8}" text-anchor="end" class="axis">${new Date(t1).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</text></svg>`;
  return h('section', { class: 'set-section' }, h('h3', null, 'GPU (last 24 hours, while models ran)'), h('div', { html: svg }),
    h('div', { class: 'legend' }, h('span', { class: 'lg lg-power' }), ' power ', h('span', { class: 'lg lg-vram' }), ' memory in use'));
}

function exportSection(owner) {
  const table = h('select', { class: 'input small-input' }, [
    ['model_requests', 'Model requests'], ['tool_calls', 'Tool calls'], ['approvals', 'Approvals'], ['events', 'Events'],
    ['jobs', 'Model jobs'], ['gpu_samples', 'GPU samples']].map(([v, l]) => h('option', { value: v }, l)));
  const fmt = h('select', { class: 'input small-input' }, h('option', { value: 'csv' }, 'CSV'), h('option', { value: 'jsonl' }, 'JSON Lines'));
  const range = () => `from=${Date.now() - state.days * 86400000}&to=${Date.now() + 60000}${owner ? `&account=${state.account}` : ''}`;
  return h('section', { class: 'set-section' }, h('h3', null, 'Export for analysis'),
    h('p', { class: 'muted small' }, 'Every row is one event with a UTC timestamp in milliseconds. Durations are milliseconds; energy is millijoules. The schema is in docs/STATISTICS.md.'),
    h('div', { class: 'rule-add' }, table, fmt,
      h('button', { class: 'btn', type: 'button', onclick: () => downloadFrom(`/api/stats/export?table=${table.value}&format=${fmt.value}&${range()}`, `baabaa-${table.value}.${fmt.value}`).catch(errorToast) }, icon('download', 15), ' Download'),
      h('button', { class: 'btn', type: 'button', title: 'The whole statistics database (SQLite)', onclick: () => downloadFrom(`/api/stats/snapshot${owner ? `?account=${state.account}` : ''}`, 'baabaa-stats.sqlite').catch(errorToast) }, icon('download', 15), ' Database')));
}
