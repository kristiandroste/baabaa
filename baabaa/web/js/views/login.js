// Sign-in: a profile picker (passwordless accounts open directly), and first-run setup of the owner.
import { get, post } from '../api.js';
import { h, clear, logo, avatar } from '../dom.js';
import { errorToast } from '../ui.js';

export async function renderLogin(root, setupToken, done) {
  clear(root);
  const card = h('div', { class: 'login-card' });
  root.appendChild(h('div', { class: 'login' }, card));
  let data;
  try { data = await get('/api/profiles'); } catch (e) { errorToast(e); return; }
  if (data.setup) return renderSetup(card, setupToken, done);
  card.append(logo(56), h('h1', null, 'Who is using baabaa?'));
  const grid = h('div', { class: 'profiles' });
  for (const p of data.profiles) {
    grid.appendChild(h('button', { class: 'profile', type: 'button', onclick: () => pick(p) },
      avatar(p, 64), h('span', { class: 'profile-name' }, p.display_name), p.has_password ? h('span', { class: 'profile-lock' }, 'password') : null));
  }
  card.appendChild(grid);
  const form = h('form', { class: 'login-form', hidden: true });
  card.appendChild(form);

  async function pick(p) {
    if (!p.has_password) return login(p, null);
    clear(form).hidden = false;
    grid.hidden = true;
    const pw = h('input', { class: 'input', type: 'password', autocomplete: 'current-password', placeholder: 'Password' });
    form.append(h('div', { class: 'login-who' }, avatar(p, 40), h('strong', null, p.display_name)), pw,
      h('div', { class: 'dialog-actions' },
        h('button', { class: 'btn', type: 'button', onclick: () => { form.hidden = true; grid.hidden = false; } }, 'Back'),
        h('button', { class: 'btn btn-primary', type: 'submit' }, 'Sign in')));
    form.onsubmit = e => { e.preventDefault(); login(p, pw.value); };
    pw.focus();
  }

  async function login(p, password) {
    try {
      await post('/api/login', { account_id: p.id, password });
      done();
    } catch (e) { errorToast(e); }
  }
}

function renderSetup(card, token, done) {
  const name = h('input', { class: 'input', value: 'owner', autocomplete: 'username', pattern: '[a-z0-9][a-z0-9_.-]{0,31}' });
  const display = h('input', { class: 'input', placeholder: 'Your name', autocomplete: 'name' });
  const pw = h('input', { class: 'input', type: 'password', autocomplete: 'new-password', placeholder: 'Optional' });
  const form = h('form', { class: 'login-form' },
    h('label', { class: 'field' }, h('span', null, 'Your name'), display),
    h('label', { class: 'field' }, h('span', null, 'Account name (lowercase)'), name),
    h('label', { class: 'field' }, h('span', null, 'Password (optional)'), pw),
    h('p', { class: 'muted small' }, 'This account owns baabaa: it approves models, manages accounts and sees usage for everyone. '
      + 'Without a password, anyone on your network can open it.'),
    h('div', { class: 'dialog-actions' }, h('button', { class: 'btn btn-primary', type: 'submit' }, 'Create owner account')));
  form.onsubmit = async e => {
    e.preventDefault();
    try {
      await post('/api/setup', { name: name.value.trim(), display_name: display.value.trim(), password: pw.value || null, token });
      history.replaceState(null, '', '#/');
      done();
    } catch (err) { errorToast(err); }
  };
  card.append(logo(56), h('h1', null, 'Welcome to baabaa'), h('p', { class: 'muted' }, 'Set up the owner account for this computer.'), form);
  display.focus();
}
