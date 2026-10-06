// DOM helpers, icons and formatting shared by every view.

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'html') el.innerHTML = v;
      else if (k === 'text') el.textContent = v;
      else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (v === true) el.setAttribute(k, '');
      else el.setAttribute(k, v);
    }
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    if (Array.isArray(c)) append(el, c);
    else if (c instanceof Node) el.appendChild(c);
    else el.appendChild(document.createTextNode(String(c)));
  }
}

export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// Icons: 24x24 strokes drawn for baabaa.
const P = {
  plus: '<path d="M12 5v14M5 12h14"/>',
  send: '<path d="M12 19V5M5.5 11.5 12 5l6.5 6.5"/>',
  'arrow-down': '<path d="M12 5v14M5.5 12.5 12 19l6.5-6.5"/>',
  stop: '<rect x="7" y="7" width="10" height="10" rx="1.5" fill="currentColor" stroke="none"/>',
  clip: '<path d="M20 11.5 12.2 19.3a5 5 0 0 1-7.1-7.1l8.5-8.5a3.4 3.4 0 0 1 4.8 4.8l-8.5 8.5a1.7 1.7 0 0 1-2.4-2.4l7.8-7.8"/>',
  folder: '<path d="M3.5 7.5a2 2 0 0 1 2-2h3.6l2 2h7.4a2 2 0 0 1 2 2v7.5a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>',
  chevron: '<path d="m7 10 5 5 5-5"/>',
  chevronRight: '<path d="m10 7 5 5-5 5"/>',
  chevronLeft: '<path d="m14 7-5 5 5 5"/>',
  copy: '<rect x="8.5" y="8.5" width="11" height="11" rx="2"/><path d="M15.5 8.5V6.5a2 2 0 0 0-2-2h-7a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h2"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  retry: '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3M19.5 4.5v4h-4"/>',
  edit: '<path d="M4.5 19.5h4l10-10a2.8 2.8 0 0 0-4-4l-10 10z"/>',
  trash: '<path d="M4.5 7h15M9.5 7V5h5v2M6.5 7l1 12.5h9l1-12.5"/>',
  pin: '<path d="M9 4.5h6M10 4.5v5.5l-3.5 3.5h11L14 10V4.5M12 13.5v6"/>',
  search: '<circle cx="11" cy="11" r="6"/><path d="m20 20-4.5-4.5"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M12 2.8v2.4M12 18.8v2.4M4.9 4.9l1.7 1.7M17.4 17.4l1.7 1.7M2.8 12h2.4M18.8 12h2.4M4.9 19.1l1.7-1.7M17.4 6.6l1.7-1.7"/>',
  sidebar: '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="M9.5 4.5v15"/>',
  more: '<circle cx="5.5" cy="12" r="1.3" fill="currentColor"/><circle cx="12" cy="12" r="1.3" fill="currentColor"/><circle cx="18.5" cy="12" r="1.3" fill="currentColor"/>',
  x: '<path d="m6 6 12 12M18 6 6 18"/>',
  download: '<path d="M12 4.5v11M7 11l5 5 5-5M5 19.5h14"/>',
  image: '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><circle cx="9" cy="10" r="1.8"/><path d="m20.5 16-5-5-8 8.5"/>',
  code: '<path d="m8.5 8-4 4 4 4M15.5 8l4 4-4 4M13.5 5.5l-3 13"/>',
  terminal: '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="m7.5 9.5 3 2.5-3 2.5M12.5 15h4"/>',
  globe: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.5 2.8 2.5 14.2 0 17M12 3.5c-2.5 2.8-2.5 14.2 0 17"/>',
  file: '<path d="M6.5 3.5h7l4 4v13h-11z"/><path d="M13.5 3.5v4h4"/>',
  list: '<path d="M9 7h11M9 12h11M9 17h11M4.5 7h.01M4.5 12h.01M4.5 17h.01"/>',
  brain: '<path d="M9 4.5a3 3 0 0 0-3 3 3 3 0 0 0-1.5 5.3A3 3 0 0 0 7.5 18a2.5 2.5 0 0 0 4.5 1V5.5A2.5 2.5 0 0 0 9 4.5zM15 4.5a3 3 0 0 1 3 3 3 3 0 0 1 1.5 5.3A3 3 0 0 1 16.5 18a2.5 2.5 0 0 1-4.5 1"/>',
  bolt: '<path d="M13 3.5 5.5 13.5h6l-1 7 7.5-10h-6z"/>',
  shield: '<path d="M12 3.5 5 6v5.5c0 4.2 2.9 7.6 7 9 4.1-1.4 7-4.8 7-9V6z"/>',
  pencil: '<path d="M4.5 19.5h4l10-10a2.8 2.8 0 0 0-4-4l-10 10zM13 7l4 4"/>',
  map: '<path d="M9 5 3.5 7v12L9 17l6 2 5.5-2V5L15 7zM9 5v12M15 7v12"/>',
  chart: '<path d="M4.5 19.5h15M7.5 16v-5M12 16V7.5M16.5 16v-3"/>',
  user: '<circle cx="12" cy="8.5" r="3.5"/><path d="M5 19.5a7 7 0 0 1 14 0"/>',
  users: '<circle cx="9" cy="9" r="3"/><path d="M3.5 19a5.5 5.5 0 0 1 11 0M15.5 6.5a3 3 0 0 1 0 5.5M17 14a5.5 5.5 0 0 1 3.5 5"/>',
  cpu: '<rect x="6.5" y="6.5" width="11" height="11" rx="1.5"/><path d="M9.5 3.5v3M14.5 3.5v3M9.5 17.5v3M14.5 17.5v3M3.5 9.5h3M3.5 14.5h3M17.5 9.5h3M17.5 14.5h3"/>',
  logout: '<path d="M14.5 8V5.5h-9v13h9V16M10.5 12H20M17 9l3 3-3 3"/>',
  branch: '<circle cx="7" cy="6" r="2"/><circle cx="7" cy="18" r="2"/><circle cx="17" cy="9" r="2"/><path d="M7 8v8M17 11c0 3-4 3-8.5 5.5"/>',
  undo: '<path d="M9 7.5 4.5 12 9 16.5M5 12h9.5a5 5 0 0 1 0 10H12"/>',
  sparkle: '<path d="M12 4v4M12 16v4M4 12h4M16 12h4M7 7l2 2M15 15l2 2M17 7l-2 2M9 15l-2 2"/>',
  ghost: '<path d="M6 19.5V10a6 6 0 0 1 12 0v9.5l-2-1.5-2 1.5-2-1.5-2 1.5-2-1.5z"/><circle cx="10" cy="10.5" r=".8" fill="currentColor"/><circle cx="14" cy="10.5" r=".8" fill="currentColor"/>',
  lock: '<rect x="5.5" y="10.5" width="13" height="9.5" rx="2"/><path d="M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5"/>',
  refresh: '<path d="M19.5 8.5A7.5 7.5 0 0 0 5 9M4.5 15.5A7.5 7.5 0 0 0 19 15M19.5 4.5v4h-4M4.5 19.5v-4h4"/>',
  mic: '<rect x="9" y="3.5" width="6" height="11" rx="3"/><path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v2.5"/>',
  speaker: '<path d="M4.5 9.5h3.5l4.5-4v13l-4.5-4H4.5zM16 9a4 4 0 0 1 0 6M18.5 6.5a7.5 7.5 0 0 1 0 11"/>',
  fork: '<circle cx="6" cy="5.5" r="2"/><circle cx="18" cy="5.5" r="2"/><circle cx="12" cy="18.5" r="2"/><path d="M6 7.5v1.5a3 3 0 0 0 3 3h6a3 3 0 0 0 3-3V7.5M12 12v4.5"/>',
  compress: '<path d="M9 3.5V9H3.5M15 20.5V15h5.5M20.5 9H15V3.5M3.5 15H9v5.5"/>',
  expand: '<path d="M14.5 4.5h5v5M9.5 19.5h-5v-5M19.5 4.5 13 11M4.5 19.5 11 13"/>',
  panel: '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="M14.5 4.5v15"/>',
  box: '<path d="M4 7.5 12 3.5l8 4v9l-8 4-8-4zM4 7.5l8 4 8-4M12 11.5v9"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  upload: '<path d="M12 19.5v-11M7 13l5-5 5 5M5 4.5h14"/>',
  up: '<path d="M7.5 10.5v9h-3v-9zM7.5 10.5l3.5-6.5a2 2 0 0 1 2 2.3l-.6 3.2h5.3a2 2 0 0 1 2 2.3l-1.1 6a2 2 0 0 1-2 1.7H7.5"/>',
  down: '<path d="M7.5 13.5v-9h-3v9zM7.5 13.5l3.5 6.5a2 2 0 0 0 2-2.3l-.6-3.2h5.3a2 2 0 0 0 2-2.3l-1.1-6a2 2 0 0 0-2-1.7H7.5"/>',
};

export function icon(name, size = 18, extra = '') {
  const span = document.createElement('span');
  span.className = `icon ${extra}`.trim();
  span.innerHTML = `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${P[name] || ''}</svg>`;
  return span;
}

export const SHEEP = `<svg viewBox="0 0 64 64" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M18 30c-5 0-8 4-7 8 1 5 6 6 9 5 1 5 6 8 11 7 4 4 11 4 15 0 5 1 9-3 9-8 3-2 4-7 1-10 1-5-3-9-8-8-2-5-8-6-12-3-4-3-10-2-12 3-3-1-6 1-6 6z"/><path d="M22 50v6M44 50v6"/><ellipse cx="17" cy="33" rx="6.5" ry="8" fill="var(--bg-elev, #fff)"/><circle cx="15" cy="31.5" r="1.3" fill="currentColor" stroke="none"/><path d="M11 28c-2-1-3-3-2-5M22 27c2-1 3-3 2-5"/></g></svg>`;

export function logo(size = 28) {
  const s = document.createElement('span');
  s.className = 'logo';
  s.style.width = s.style.height = size + 'px';
  s.innerHTML = SHEEP;
  return s;
}

// formatting ---------------------------------------------------------------------------------------
export function ago(ms) {
  const d = (Date.now() - ms) / 1000;
  if (d < 45) return 'just now';
  if (d < 3600) return `${Math.round(d / 60)} min ago`;
  if (d < 86400) return `${Math.round(d / 3600)} h ago`;
  if (d < 86400 * 7) return `${Math.round(d / 86400)} d ago`;
  return new Date(ms).toLocaleDateString();
}

export function bytes(n) {
  if (n == null) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (Math.abs(n) >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${i ? n.toFixed(n < 10 ? 1 : 0) : n} ${u[i]}`;
}

export function tokens(n) {
  if (n == null) return '—';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + 'k';
  return String(n);
}

export function ctxk(n) {
  // context windows are powers of two: 65536 -> "64k"
  if (!n) return '—';
  return n >= 1024 ? `${Math.round(n / 1024)}k` : String(n);
}

export function toolNote(m) {
  // the fit test's tool checks: a short warning when the model failed some (null when it passed all, or was not tested)
  const t = m.tool_use;
  if (!t || !t.of || t.passed >= t.of) return null;
  return `unreliable with tools (${t.passed}/${t.of} checks)`;
}

export function duration(ms) {
  if (ms == null) return '—';
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ${Math.round(s % 60)} s`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}

export function initials(name) {
  const parts = String(name || '?').trim().split(/\s+/);
  return (parts[0][0] + (parts[1] ? parts[1][0] : '')).toUpperCase();
}

export function avatar(account, size = 28) {
  return h('span', { class: 'avatar', style: { background: account.color || '#5b6470', width: size + 'px', height: size + 'px', fontSize: Math.round(size * 0.42) + 'px' } }, initials(account.display_name || account.name));
}

export function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = h('textarea', { style: { position: 'fixed', opacity: '0' } }, text);
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch { ok = false; }
    ta.remove();
    return ok;
  }
}

export function download(name, blob) {
  const url = URL.createObjectURL(blob);
  const a = h('a', { href: url, download: name });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}
