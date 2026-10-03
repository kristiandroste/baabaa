// Small UI primitives: toasts, menus, modals, confirm and prompt dialogs.
import { h, icon } from './dom.js';

let toastRoot = null;
export function toast(text, kind = 'info', ms = 3500) {
  if (!toastRoot) {
    toastRoot = h('div', { class: 'toasts', 'aria-live': 'polite' });
    document.body.appendChild(toastRoot);
  }
  const t = h('div', { class: `toast toast-${kind}` }, text);
  toastRoot.appendChild(t);
  requestAnimationFrame(() => t.classList.add('show'));
  setTimeout(() => { t.classList.remove('show'); setTimeout(() => t.remove(), 300); }, ms);
}

export function errorToast(e) { toast(e && e.message ? e.message : String(e), 'error', 6000); }

// The message being written when an update reloads the page: kept for the same place, put back once.
export function keepDraft() {
  try {
    const ta = document.querySelector('.composer-input');
    if (ta && ta.value.trim()) sessionStorage.setItem('baabaa.draft', JSON.stringify({ hash: location.hash, text: ta.value }));
  } catch { /* storage blocked: nothing kept */ }
}

export function takeKeptDraft() {
  try {
    const d = JSON.parse(sessionStorage.getItem('baabaa.draft') || 'null');
    if (!d || d.hash !== location.hash) return null;
    sessionStorage.removeItem('baabaa.draft');
    return d.text;
  } catch { return null; }
}

// A dropdown menu anchored to an element. items: [{label, icon, onClick, danger, checked, disabled, hint}] or 'sep'.
let openMenu = null;
export function menu(anchor, items, opts = {}) {
  closeMenu();
  const m = h('div', { class: 'menu', role: 'menu' });
  for (const it of items) {
    if (!it) continue;
    if (it === 'sep') { m.appendChild(h('div', { class: 'menu-sep' })); continue; }
    if (it.heading) { m.appendChild(h('div', { class: 'menu-heading' }, it.heading)); continue; }
    const b = h('button', {
      class: `menu-item${it.danger ? ' danger' : ''}${it.checked ? ' checked' : ''}`, role: 'menuitem', type: 'button',
      disabled: it.disabled || null,
      onclick: e => { e.stopPropagation(); closeMenu(); it.onClick && it.onClick(); },
    }, it.icon ? icon(it.icon, 16) : h('span', { class: 'icon-space' }), h('span', { class: 'menu-label' }, it.label,
      it.hint ? h('span', { class: 'menu-hint' }, it.hint) : null), it.checked ? icon('check', 16, 'menu-check') : null);
    m.appendChild(b);
  }
  document.body.appendChild(m);
  const r = anchor.getBoundingClientRect();
  const mw = m.offsetWidth, mh = m.offsetHeight;
  let left = opts.alignRight ? r.right - mw : r.left;
  let top = opts.above ? r.top - mh - 6 : r.bottom + 6;
  if (top + mh > window.innerHeight - 8) top = r.top - mh - 6;
  if (top < 8) top = 8;
  left = Math.max(8, Math.min(left, window.innerWidth - mw - 8));
  m.style.left = left + 'px';
  m.style.top = top + 'px';
  openMenu = m;
  setTimeout(() => document.addEventListener('click', closeMenu, { once: true }), 0);
  const first = m.querySelector('.menu-item:not([disabled])');
  if (first) first.focus({ preventScroll: true });
  m.addEventListener('keydown', e => {
    const all = Array.from(m.querySelectorAll('.menu-item:not([disabled])'));
    const i = all.indexOf(document.activeElement);
    if (e.key === 'ArrowDown') { e.preventDefault(); (all[i + 1] || all[0]).focus(); }
    if (e.key === 'ArrowUp') { e.preventDefault(); (all[i - 1] || all[all.length - 1]).focus(); }
    if (e.key === 'Escape') { closeMenu(); anchor.focus && anchor.focus(); }
  });
  return m;
}

export function closeMenu() {
  if (openMenu) { openMenu.remove(); openMenu = null; }
}

// Modal dialog. Returns {el, body, close}.
export function modal(title, opts = {}) {
  const body = h('div', { class: 'modal-body' });
  const close = () => { overlay.remove(); document.removeEventListener('keydown', onKey); opts.onClose && opts.onClose(); };
  const onKey = e => { if (e.key === 'Escape' && !opts.sticky) close(); };
  const box = h('div', { class: `modal ${opts.wide ? 'modal-wide' : ''} ${opts.class || ''}`, role: 'dialog', 'aria-modal': 'true' },
    title !== null ? h('div', { class: 'modal-head' }, h('h2', null, title),
      h('button', { class: 'icon-btn', type: 'button', title: 'Close', onclick: close }, icon('x'))) : null,
    body);
  const overlay = h('div', { class: 'overlay', onmousedown: e => { if (e.target === overlay && !opts.sticky) close(); } }, box);
  document.body.appendChild(overlay);
  document.addEventListener('keydown', onKey);
  return { el: box, body, close };
}

export function confirmDialog(title, text, okLabel = 'OK', danger = false) {
  return new Promise(resolve => {
    const m = modal(title, { onClose: () => resolve(false) });
    m.body.appendChild(h('p', { class: 'dialog-text' }, text));
    const ok = h('button', { class: `btn ${danger ? 'btn-danger' : 'btn-primary'}`, type: 'button', onclick: () => { resolve(true); m.close(); } }, okLabel);
    m.body.appendChild(h('div', { class: 'dialog-actions' },
      h('button', { class: 'btn', type: 'button', onclick: () => { resolve(false); m.close(); } }, 'Cancel'), ok));
    ok.focus();
  });
}

export function promptDialog(title, label, value = '', okLabel = 'Save', opts = {}) {
  return new Promise(resolve => {
    let done = false;
    const m = modal(title, { onClose: () => { if (!done) resolve(null); } });
    const input = opts.multiline
      ? h('textarea', { class: 'input', rows: opts.rows || 4, placeholder: opts.placeholder || '' }, value)
      : h('input', { class: 'input', type: opts.type || 'text', value, placeholder: opts.placeholder || '' });
    const submit = () => { done = true; resolve(input.value); m.close(); };
    input.addEventListener('keydown', e => { if (e.key === 'Enter' && !opts.multiline) { e.preventDefault(); submit(); } });
    m.body.appendChild(h('label', { class: 'field' }, h('span', null, label), input));
    m.body.appendChild(h('div', { class: 'dialog-actions' },
      h('button', { class: 'btn', type: 'button', onclick: () => m.close() }, 'Cancel'),
      h('button', { class: 'btn btn-primary', type: 'button', onclick: submit }, okLabel)));
    setTimeout(() => { input.focus(); input.select && input.select(); }, 0);
  });
}

export function switchEl(checked, onChange, label) {
  const input = h('input', { type: 'checkbox', checked: checked || null, onchange: e => onChange(e.target.checked) });
  return h('label', { class: 'switch' }, input, h('span', { class: 'switch-track' }, h('span', { class: 'switch-thumb' })),
    label ? h('span', { class: 'switch-label' }, label) : null);
}

export function segmented(options, value, onChange) {
  const el = h('div', { class: 'segmented', role: 'radiogroup' });
  for (const o of options) {
    el.appendChild(h('button', {
      type: 'button', class: o.value === value ? 'active' : '', role: 'radio', 'aria-checked': String(o.value === value),
      onclick: () => { for (const b of el.children) b.classList.remove('active'); el.children[options.indexOf(o)].classList.add('active'); onChange(o.value); },
    }, o.label));
  }
  return el;
}
