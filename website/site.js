// baabaa's website: the strand on the landing page, the install command's Copy button, and the document reader.
// No request leaves the site: the documents are files beside this one.
(function () {
  'use strict';

  const dark = window.matchMedia('(prefers-color-scheme: dark)');
  const theme = () => { document.documentElement.dataset.theme = dark.matches ? 'dark' : 'light'; };
  dark.addEventListener('change', theme);
  theme();

  // ---- landing page: the thread, fed by a stand-in for a model -------------------------------------------
  const strand = document.getElementById('strand');
  if (strand && window.BaabaaThread) {
    const still = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const thread = new window.BaabaaThread.Thread(strand, { width: 300, still });
    const label = document.getElementById('strandLabel');
    const words = ('The script stops at the download step, and only on a Mac. So the cause is probably the shell and not the network.\n\n'
      + 'What is on that line? A message with the version number in it. Let me read the line before guessing.\n\n').match(/\S+\s*/g);
    // what the strand goes through, over and over: [state, seconds, the words beside it]
    const acts = [['wait', 2.2, 'Waiting for the GPU (1 ahead)'], ['think', 9, 'Thinking…'], ['tool', 3, 'Running a command…'],
      ['think', 5, 'Thinking…'], ['write', 7, ''], ['wait', 1.2, '']];
    let act = -1, left = 0, at = 0, owed = 0, last = 0, shown = null;
    const next = () => { act = (act + 1) % acts.length; left = acts[act][1]; thread.set(acts[act][0]); };
    next();
    const tick = ms => {
      const dt = Math.max(0, Math.min((ms - last) / 1000, 0.05));
      last = ms;
      if (!document.hidden) {
        left -= dt;
        if (left <= 0) next();
        const mode = acts[act][0];
        if (mode === 'think' || mode === 'write') {          // about 28 tokens a second
          owed += 28 * 4 * dt;
          while (owed >= words[at].length) { owed -= words[at].length; thread.pulse(words[at]); at = (at + 1) % words.length; }
        }
        const text = mode === 'think' ? `Thinking… ${Math.max(1, Math.round(thread.seconds))} s` : acts[act][2];
        if (text !== shown) { shown = text; label.textContent = text; }
        thread.step(dt);
        thread.draw();
      }
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(ms => { last = ms; requestAnimationFrame(tick); });
  }

  const copy = document.getElementById('copy');
  if (copy) {
    copy.addEventListener('click', async () => {
      const code = document.getElementById('command');
      try { await navigator.clipboard.writeText(code.textContent); copy.textContent = 'Copied'; }
      catch { window.getSelection().selectAllChildren(code); copy.textContent = 'Selected: press Ctrl+C'; }
      setTimeout(() => { copy.textContent = 'Copy'; }, 2500);
    });
  }

  // ---- the document reader ------------------------------------------------------------------------------------
  const doc = document.getElementById('doc'), toc = document.getElementById('toc');
  if (!doc || !toc) return;
  const TITLES = { 'docs/TUTORIAL.md': 'Tutorial', 'README.md': 'Reference', 'docs/DEVELOPING.md': 'Developer guide', 'docs/API.md': 'HTTP API',
    'docs/AUTO_MODE.md': 'Auto mode', 'docs/RUNTIMES.md': 'Other model programs', 'docs/STATISTICS.md': 'Statistics',
    'docs/RELEASING.md': 'Making a release', 'CHANGELOG.md': 'Changelog', 'SECURITY.md': 'Security', 'CONTRIBUTING.md': 'Contributing',
    'CLA.md': 'Contributor agreement', 'LICENSE': 'License' };
  const ORDER = Object.keys(TITLES);
  const esc = s => s.replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  // Where a link inside a document leads: another document here, or the repository for anything else in it.
  function resolve(from, href) {
    const parts = from.split('/').slice(0, -1);
    for (const p of href.split('#')[0].split('/')) { if (p === '..') parts.pop(); else if (p && p !== '.') parts.push(p); }
    return parts.join('/');
  }

  async function show(names) {
    const wanted = decodeURIComponent(location.search.slice(1)), name = names.includes(wanted) ? wanted : names[0];
    toc.textContent = '';
    names.forEach((n, i) => {
      if (i && ORDER.indexOf(n) === ORDER.indexOf('CHANGELOG.md')) toc.appendChild(Object.assign(document.createElement('span'), { className: 'sep' }));
      const a = Object.assign(document.createElement('a'), { textContent: TITLES[n] || n, className: n === name ? 'on' : '' });
      a.setAttribute('href', '?' + n);
      toc.appendChild(a);
    });
    document.title = `${TITLES[name] || name} · baabaa`;
    let text;
    try {
      const res = await fetch('src/' + name);
      if (!res.ok) throw new Error(String(res.status));
      text = await res.text();
    } catch (e) {
      doc.innerHTML = '<p class="notice">That document could not be loaded.</p>';
      return;
    }
    const M = window.BaabaaMarkdown, H = window.BaabaaHighlight;
    if (!name.endsWith('.md') || !M) { doc.innerHTML = '<pre class="plain">' + esc(text) + '</pre>'; return; }
    // The renderer makes only full web addresses clickable, so a link to another file becomes one first:
    // to this reader when the file is a document here, to the repository otherwise.
    const here = location.href.split('?')[0];
    text = text.replace(/\]\((?![a-z]+:|#)([^)\s]+)\)/gi, (whole, href) => {
      const to = resolve(name, href);
      return '](' + (names.includes(to) ? here + '?' + to : 'https://github.com/kristiandroste/baabaa/blob/main/' + to) + ')';
    });
    doc.innerHTML = M.render(text, { breaks: false, highlight: H ? (code, lang) => H.highlight(code, lang) : null });
    for (const a of doc.querySelectorAll('a[href]')) {
      if (a.getAttribute('href').startsWith(here + '?')) a.removeAttribute('target');   // stay in this window
    }
    window.scrollTo(0, 0);
  }

  fetch('src/index.json').then(r => r.json()).then(names => {
    names.sort((a, b) => (ORDER.indexOf(a) + 1 || 99) - (ORDER.indexOf(b) + 1 || 99));
    show(names);
  }).catch(() => { doc.innerHTML = '<p class="notice">The documents could not be loaded.</p>'; });
})();
