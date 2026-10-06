// The strand at the foot of a reply being written (thread.js draws it). There is one per reply, kept while
// the reply is re-rendered, so the strand never jumps; it stops by itself once the reply is finished or
// has left the page.
import { S } from '../app.js';
import { h } from '../dom.js';

const live = new Map();      // message id -> { thread, row, label, shown, gone }
let ticking = false, last = 0;

// The row for the reply `m`: the strand and the words beside it.
export function workingRow(m) {
  const T = globalThis.BaabaaThread;
  let w = live.get(m.id);
  if (!w) {
    const host = h('span', { class: 'th' }), label = h('span', { class: 'working-label' });
    const row = h('div', { class: 'working', title: 'baabaa is working' }, host, label);
    if (!T) return row;      // the renderer did not load: the place stays empty
    const still = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    w = { thread: new T.Thread(host, { still }), row, label, shown: '', gone: 0 };
    live.set(m.id, w);
    show(w, m);
    w.thread.step(0);
    w.thread.draw();
  }
  if (!ticking) { ticking = true; last = performance.now(); requestAnimationFrame(tick); }
  return w.row;
}

// Text arrived for a reply.
export function pulse(id, text) {
  const w = live.get(id);
  if (w) w.thread.pulse(text);
}

function show(w, m) {
  const T = globalThis.BaabaaThread, mode = T.liveMode(m, S.turn);
  if (mode !== w.thread.mode) w.thread.set(mode);
  if (mode === 'think') {    // a window opened part-way through a thought still gets a ball of the right size
    const b = m.blocks[m.blocks.length - 1];
    w.thread.tokens = Math.max(w.thread.tokens, (b.text || '').length / 4);
  }
  const text = T.liveLabel(m, S.turn, w.thread);
  if (text !== w.shown) { w.shown = text; w.label.textContent = text; }
}

function tick(ms) {
  const dt = Math.max(0, Math.min((ms - last) / 1000, 0.05));
  last = ms;
  for (const [id, w] of live) {
    const m = S.thread.find(x => x.id === id);
    w.gone = w.row.isConnected ? 0 : w.gone + 1;
    if (!m || m.status !== 'streaming' || w.gone > 120) { live.delete(id); continue; }
    show(w, m);
    w.thread.step(dt);
    if (!w.gone) w.thread.draw();
  }
  if (live.size) requestAnimationFrame(tick); else ticking = false;
}
