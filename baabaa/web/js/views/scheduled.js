// Scheduled tasks: prompts that run by themselves and arrive as conversations.
import { S, toggleSidebar } from '../app.js';
import { get, post, patch, del, on } from '../api.js';
import { h, clear, icon, ago } from '../dom.js';
import { toast, errorToast, modal, confirmDialog, switchEl } from '../ui.js';

let unsub = [];
const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

export async function renderScheduled(main) {
  for (const u of unsub) u();
  unsub = [];
  S.conv = null;
  clear(main);
  const list = h('div', { class: 'sched-list' });
  const note = h('p', { class: 'muted small' });
  const page = h('div', { class: 'page' },
    h('div', { class: 'page-head' }, h('h1', null, 'Scheduled'),
      h('button', { class: 'btn btn-primary', type: 'button', onclick: () => editor(null, load) }, icon('plus', 16), ' New task')),
    h('p', { class: 'muted' }, 'A scheduled task sends a prompt by itself — once, every day, on chosen days or every few hours — and the reply arrives as a conversation. Turn on notifications in Settings to hear about it. It runs in the queue like any other message, so it waits while the GPU is busy or paused.'),
    note, list);
  main.append(h('header', { class: 'topbar' }, h('div', { class: 'tb-left' },
    h('button', { class: 'icon-btn tb-sidebar', type: 'button', title: 'Sidebar (Ctrl+.)', onclick: () => toggleSidebar() }, icon('sidebar')),
    h('span', { class: 'tb-crumb' }, 'Scheduled'))), h('div', { class: 'page-wrap' }, page));

  async function load() {
    let d;
    try { d = await get('/api/schedules'); } catch (e) { errorToast(e); return; }
    note.textContent = `Times are this computer's local time${d.timezone ? ` (${d.timezone})` : ''}.`;
    clear(list);
    if (!d.schedules.length) { list.appendChild(h('div', { class: 'empty' }, 'No scheduled tasks yet.')); return; }
    for (const s of d.schedules) {
      const next = s.enabled && s.next_ms ? new Date(s.next_ms).toLocaleString(undefined, { weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : null;
      list.appendChild(h('div', { class: 'sched' + (s.enabled ? '' : ' off') },
        h('div', { class: 'sched-main' },
          h('div', { class: 'sched-title' }, icon('clock', 15), h('strong', null, s.title),
            s.settings.research ? h('span', { class: 'badge' }, 'research') : null),
          h('div', { class: 'small' }, s.when, next ? ` · next ${next}` : s.enabled ? '' : ' · off'),
          h('div', { class: 'muted small sched-prompt' }, s.prompt),
          s.last_ms ? h('div', { class: 'muted small' }, `Last run ${ago(s.last_ms)} · `, s.last_conv ? h('a', { href: `#/c/${s.last_conv}` }, 'open') : null,
            s.last_status && s.last_status !== 'started' ? ` · ${s.last_status}` : '') : null),
        h('div', { class: 'sched-actions' },
          switchEl(s.enabled, async v => { try { await patch(`/api/schedules/${s.id}`, { enabled: v }); load(); } catch (e) { errorToast(e); load(); } }),
          h('button', { class: 'btn small', type: 'button', onclick: async () => {
            try { const r = await post(`/api/schedules/${s.id}/run`, {}); toast('Started'); location.hash = `#/c/${r.schedule.last_conv}`; } catch (e) { errorToast(e); }
          } }, 'Run now'),
          h('button', { class: 'icon-btn tiny', type: 'button', title: 'Edit', onclick: () => editor(s, load) }, icon('edit', 15)),
          h('button', { class: 'icon-btn tiny', type: 'button', title: 'Delete', onclick: async () => {
            if (!await confirmDialog('Delete scheduled task?', s.title, 'Delete', true)) return;
            try { await del(`/api/schedules/${s.id}`); load(); } catch (e) { errorToast(e); }
          } }, icon('trash', 15)))));
    }
  }
  unsub.push(on('schedule', load));
  await load();
}

async function editor(s, done) {
  const m = modal(s ? 'Edit scheduled task' : 'New scheduled task', { wide: true });
  const spec = s ? s.spec : { kind: 'daily', at: '08:00' };
  const set = s ? s.settings : {};
  const title = h('input', { class: 'input', placeholder: 'Title, e.g. Morning briefing', value: s ? s.title : '' });
  const prompt = h('textarea', { class: 'input', rows: 5, placeholder: 'What should baabaa do? e.g. Summarize today’s news about sheep farming in three bullet points.' }, s ? s.prompt : '');
  const kind = h('select', { class: 'input' }, [['daily', 'Every day'], ['weekdays', 'Weekdays'], ['weekly', 'On chosen days'], ['once', 'Once'], ['interval', 'Every few hours']]
    .map(([v, l]) => h('option', { value: v, selected: spec.kind === v || null }, l)));
  const at = h('input', { class: 'input small-input', type: 'time', value: spec.at || '08:00' });
  const date = h('input', { class: 'input small-input', type: 'date', value: spec.date || new Date(Date.now() + 86400000).toISOString().slice(0, 10) });
  const every = h('select', { class: 'input small-input' }, [30, 60, 120, 180, 240, 360, 720].map(v => h('option', { value: v, selected: (spec.every_min || 60) === v || null }, v < 60 ? `${v} min` : `${v / 60} h`)));
  const days = new Set(spec.days || [0]);
  const dayRow = h('div', { class: 'day-picks' }, DAYS.map((d, i) => {
    const b = h('button', { class: 'chip' + (days.has(i) ? ' on' : ''), type: 'button', onclick: () => { days.has(i) ? days.delete(i) : days.add(i); b.classList.toggle('on', days.has(i)); } }, d);
    return b;
  }));
  const whenRow = h('div', { class: 'when-row' });
  const drawWhen = () => {
    clear(whenRow);
    const k = kind.value;
    whenRow.append(kind);
    if (k === 'once') whenRow.append(date);
    if (k !== 'interval') whenRow.append(at); else whenRow.append(every);
    if (k === 'weekly') whenRow.append(dayRow);
  };
  kind.addEventListener('change', drawWhen);
  drawWhen();
  const research = switchEl(!!set.research, () => {}, 'Research (search the web and cite sources)');
  const same = switchEl(!!set.same_conversation, () => {}, 'Keep every run in one conversation');
  const projSel = h('select', { class: 'input' }, h('option', { value: '' }, 'No project'));
  get('/api/projects').then(d => { for (const p of d.projects) projSel.appendChild(h('option', { value: p.id, selected: p.id === set.project_id || null }, p.name)); }).catch(() => {});
  const modelSel = h('select', { class: 'input' }, h('option', { value: '' }, `Default model (${S.defaultModel || 'none'})`),
    S.models.map(x => h('option', { value: x.name, selected: x.name === set.model || null }, x.name)));
  m.body.append(h('div', { class: 'form' }, title, prompt, h('label', { class: 'small muted' }, 'When'), whenRow,
    h('div', { class: 'form-grid' }, projSel, modelSel), research, same),
    h('div', { class: 'dialog-actions' },
      h('button', { class: 'btn', type: 'button', onclick: () => m.close() }, 'Cancel'),
      h('button', { class: 'btn btn-primary', type: 'button', onclick: async () => {
        const k = kind.value;
        const body = {
          title: title.value.trim(), prompt: prompt.value.trim(),
          spec: { kind: k, at: at.value, date: date.value, days: [...days], every_min: Number(every.value) },
          settings: { research: research.querySelector('input').checked, same_conversation: same.querySelector('input').checked,
            project_id: projSel.value || null, model: modelSel.value || null },
        };
        try {
          if (s) await patch(`/api/schedules/${s.id}`, body); else await post('/api/schedules', body);
          m.close(); done();
        } catch (e) { errorToast(e); }
      } }, s ? 'Save' : 'Create')));
  setTimeout(() => title.focus(), 0);
}
