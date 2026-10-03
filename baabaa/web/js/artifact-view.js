// Renders a Markdown, code or diagram artifact inside its sandboxed frame.
(function () {
  var data = JSON.parse(document.getElementById('data').textContent);
  var root = document.getElementById('root');
  var M = globalThis.BaabaaMarkdown, H = globalThis.BaabaaHighlight, Mm = globalThis.BaabaaMermaid;
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  if (data.kind === 'markdown') {
    root.className = 'doc';
    root.innerHTML = M ? M.render(data.content, { highlight: H ? function (c, l) { return H.highlight(c, l); } : null }) : '<pre>' + esc(data.content) + '</pre>';
    if (Mm) Array.prototype.forEach.call(root.querySelectorAll('.mermaid-block'), function (d) {
      try { d.innerHTML = Mm.render(d.getAttribute('data-src') || '', {}); } catch (e) { /* keep the source */ }
    });
  } else if (data.kind === 'slides') {
    // one slide per # or ## heading (or ---), shown one at a time: arrow keys, space or a click to move
    var parts = [], cur = [];
    data.content.split('\n').forEach(function (line) {
      if ((/^#{1,2}\s/.test(line) && cur.some(function (l) { return l.trim(); })) || /^\s*(---+|<!--\s*pagebreak\s*-->)\s*$/.test(line)) {
        parts.push(cur.join('\n')); cur = [];
        if (/^\s*(---+|<!--)/.test(line)) return;
      }
      cur.push(line);
    });
    parts.push(cur.join('\n'));
    parts = parts.filter(function (p) { return p.trim(); });
    root.className = 'slides';
    var i = 0;
    var stage = document.createElement('div'); stage.className = 'slide';
    var nav = document.createElement('div'); nav.className = 'slide-nav';
    root.appendChild(stage); root.appendChild(nav);
    function show(n) {
      i = Math.max(0, Math.min(parts.length - 1, n));
      stage.innerHTML = M ? M.render(parts[i], { highlight: H ? function (c, l) { return H.highlight(c, l); } : null }) : '<pre>' + esc(parts[i]) + '</pre>';
      nav.textContent = (i + 1) + ' / ' + parts.length;
    }
    document.addEventListener('keydown', function (e) {
      if (['ArrowRight', 'PageDown', ' ', 'Enter'].indexOf(e.key) >= 0) { e.preventDefault(); show(i + 1); }
      if (['ArrowLeft', 'PageUp', 'Backspace'].indexOf(e.key) >= 0) { e.preventDefault(); show(i - 1); }
      if (e.key === 'Home') show(0);
      if (e.key === 'End') show(parts.length - 1);
    });
    stage.addEventListener('click', function (e) { show(e.clientX > window.innerWidth / 3 ? i + 1 : i - 1); });
    show(0);
  } else if (data.kind === 'mermaid') {
    root.className = 'diagram';
    try { root.innerHTML = Mm ? Mm.render(data.content, {}) : '<pre>' + esc(data.content) + '</pre>'; }
    catch (e) { root.innerHTML = '<p class="error">' + esc(e.message) + '</p><pre>' + esc(data.content) + '</pre>'; }
  } else {
    root.className = 'code';
    root.innerHTML = '<pre><code>' + (H ? H.highlight(data.content, data.language || '') : esc(data.content)) + '</code></pre>';
  }
  if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) document.documentElement.className = 'dark';
})();
