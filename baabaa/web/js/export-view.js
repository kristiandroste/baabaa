// Renders an exported conversation (the HTML export inlines this script with the renderers).
(function () {
  var data = JSON.parse(document.getElementById('data').textContent);
  var root = document.getElementById('export');
  var M = globalThis.BaabaaMarkdown, H = globalThis.BaabaaHighlight, Mm = globalThis.BaabaaMermaid;
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function md(t) { return M ? M.render(t || '', { highlight: H ? function (c, l) { return H.highlight(c, l); } : null }) : esc(t).replace(/\n/g, '<br>'); }
  var html = '<h1 class="export-title">' + esc(data.conversation.title || 'Conversation') + '</h1>';
  data.messages.forEach(function (m) {
    if (m.role === 'user') {
      var text = m.blocks.filter(function (b) { return b.type === 'text'; }).map(function (b) { return b.text; }).join('\n\n');
      var atts = m.blocks.filter(function (b) { return b.type === 'attachment'; }).map(function (b) { return '<span class="att">' + esc(b.name) + '</span>'; }).join(' ');
      html += '<div class="msg msg-user"><div class="user-bubble">' + (atts ? '<div class="att-row">' + atts + '</div>' : '') + '<div class="user-text">' + esc(text) + '</div></div></div>';
    } else if (m.role === 'assistant') {
      html += '<div class="msg msg-assistant"><div class="assistant-body">';
      m.blocks.forEach(function (b) {
        if (b.type === 'text') html += '<div class="md">' + md(b.text) + '</div>';
        else if (b.type === 'thinking' && b.text) html += '<details class="thinking"><summary>Thoughts</summary><div class="thinking-text">' + esc(b.text) + '</div></details>';
        else if (b.type === 'tool') {
          var a = b.args || {};
          var what = a.command || a.path || a.query || a.url || a.title || a.pattern || '';
          html += '<details class="tool"><summary class="tool-head"><span class="tool-verb">' + esc(b.name) + '</span> <code class="tool-obj">' + esc(what) + '</code></summary>'
            + '<div class="tool-body"><pre class="tool-out">' + esc((b.output || '').slice(0, 20000)) + '</pre></div></details>';
        } else if (b.type === 'error' || b.type === 'notice') html += '<div class="notice">' + esc(b.text) + '</div>';
      });
      html += '<div class="msg-info">' + esc(m.model || '') + '</div></div></div>';
    } else if (m.role === 'compaction') {
      html += '<details class="compaction"><summary>Earlier messages were summarized</summary><div class="md">' + md(m.blocks.map(function (b) { return b.text || ''; }).join('\n')) + '</div></details>';
    }
  });
  root.innerHTML = html;
  if (Mm) {
    Array.prototype.forEach.call(root.querySelectorAll('.mermaid-block'), function (d) {
      try { d.innerHTML = Mm.render(d.getAttribute('data-src') || '', {}); } catch (e) { /* keep the source */ }
    });
  }
  if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) document.documentElement.setAttribute('data-theme', 'dark');
})();
