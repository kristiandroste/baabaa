/* mermaid-lite: renders a subset of Mermaid diagram syntax to standalone SVG markup.
   Classic script; exposes globalThis.BaabaaMermaid = { render, detect }. No DOM needed. */
(function () {
  'use strict';

  const LIMIT = { src: 200000, nodes: 1500, edges: 4000, layout: 60000, items: 4000 };
  const LOAD = Math.floor(Math.random() * 1679616).toString(36);
  let SEQ = 0;

  function fail(msg) {
    const e = new Error(msg);
    e.mmUser = true;
    throw e;
  }
  function clip(s, n) {
    s = String(s);
    n = n || 40;
    return s.length > n ? s.slice(0, n - 1) + '\u2026' : s;
  }

  // ------------------------------------------------------------------ text metrics
  // Helvetica/Arial advance widths (1/1000 em) for ASCII 32..126.
  const AW = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584];

  function charW(c) {
    if (c < 127) return c >= 32 ? AW[c - 32] / 1000 : 0;
    if (c < 0x300) return 0.6;
    if (c <= 0x36f) return 0;
    if (c < 0x1100) return 0.62;
    if (c <= 0x115f) return 1;
    if (c >= 0x200b && c <= 0x200f) return 0;
    if (c >= 0x2010 && c <= 0x2027) return c === 0x2014 || c === 0x2026 ? 1 : 0.5;
    if (c >= 0x2190 && c <= 0x21ff) return 0.9;
    if (c >= 0x2e80 && c <= 0xa4cf) return 1;
    if (c >= 0xac00 && c <= 0xd7a3) return 1;
    if (c >= 0xf900 && c <= 0xfaff) return 1;
    if (c >= 0xfe00 && c <= 0xfe0f) return 0;
    if (c >= 0xfe30 && c <= 0xfe4f) return 1;
    if (c >= 0xff00 && c <= 0xff60) return 1;
    if (c >= 0xffe0 && c <= 0xffe6) return 1;
    if (c >= 0x2600 && c <= 0x27bf) return 1.2;
    if (c >= 0x2b00 && c <= 0x2bff) return 1.2;
    if (c >= 0x1f000 && c <= 0x1faff) return 1.25;
    if (c >= 0x20000) return 1;
    return 0.62;
  }

  // Approximate rendered width; scaled up a little so common UI fonts do not overflow.
  function textW(s, fs, bold) {
    let w = 0;
    for (let i = 0; i < s.length; i++) {
      let c = s.charCodeAt(i);
      if (c >= 0xd800 && c <= 0xdbff && i + 1 < s.length) {
        const d = s.charCodeAt(i + 1);
        if (d >= 0xdc00 && d <= 0xdfff) {
          c = ((c - 0xd800) << 10) + (d - 0xdc00) + 0x10000;
          i++;
        }
      }
      w += charW(c);
    }
    return w * fs * (bold ? 1.15 : 1.08);
  }
  function maxW(lines, fs, bold) {
    let m = 0;
    for (const l of lines) m = Math.max(m, textW(l, fs, bold));
    return m;
  }

  // ------------------------------------------------------------------ escaping, numbers
  const NEEDS_ESC = /[&<>"'\u0000-\u001f\ud800-\udfff\ufffe\uffff]/;
  function esc(v) {
    const s = String(v);
    if (!NEEDS_ESC.test(s)) return s;
    let o = '';
    for (let i = 0; i < s.length; i++) {
      const c = s.charCodeAt(i);
      if (c === 38) o += '&amp;';
      else if (c === 60) o += '&lt;';
      else if (c === 62) o += '&gt;';
      else if (c === 34) o += '&quot;';
      else if (c === 39) o += '&#39;';
      else if (c < 32) o += c === 9 ? ' ' : '';
      else if (c >= 0xd800 && c <= 0xdbff) {
        const d = s.charCodeAt(i + 1);
        if (d >= 0xdc00 && d <= 0xdfff) {
          o += s[i] + s[i + 1];
          i++;
        }
      } else if ((c >= 0xdc00 && c <= 0xdfff) || c === 0xfffe || c === 0xffff) {
        // invalid in XML: drop
      } else o += s[i];
    }
    return o;
  }
  function f(n) {
    return Number.isFinite(n) ? String(Math.round(n * 100) / 100) : '0';
  }
  function pts(arr) {
    let s = '';
    for (let i = 0; i < arr.length; i += 2) s += (i ? ' ' : '') + f(arr[i]) + ',' + f(arr[i + 1]);
    return s;
  }

  // ------------------------------------------------------------------ labels
  const ENT = {
    quot: '"', amp: '&', lt: '<', gt: '>', apos: "'", nbsp: ' ', ndash: '\u2013', mdash: '\u2014',
    hellip: '\u2026', copy: '\u00a9', reg: '\u00ae', trade: '\u2122', larr: '\u2190', rarr: '\u2192',
    uarr: '\u2191', darr: '\u2193', harr: '\u2194', rArr: '\u21d2', lArr: '\u21d0', times: '\u00d7',
    divide: '\u00f7', le: '\u2264', ge: '\u2265', ne: '\u2260', plusmn: '\u00b1', deg: '\u00b0',
    middot: '\u00b7', bull: '\u2022', laquo: '\u00ab', raquo: '\u00bb', check: '\u2713', semi: ';',
    colon: ':', num: '#', lpar: '(', rpar: ')', lsqb: '[', rsqb: ']', lcub: '{', rcub: '}',
    vert: '|', excl: '!', infin: '\u221e', alpha: '\u03b1', beta: '\u03b2', mu: '\u03bc', pi: '\u03c0'
  };
  function cp(n) {
    return n >= 32 && n <= 0x10ffff && !(n >= 0xd800 && n <= 0xdfff) && !(n >= 127 && n < 160)
      ? String.fromCodePoint(n) : '';
  }
  function entity(name) {
    if (name[0] === '#') {
      const n = name[1] === 'x' || name[1] === 'X' ? parseInt(name.slice(2), 16) : parseInt(name.slice(1), 10);
      const r = cp(n);
      return r || null;
    }
    return Object.prototype.hasOwnProperty.call(ENT, name) ? ENT[name] : null;
  }
  function decodeEntities(s) {
    return s
      .replace(/&(#[xX][0-9a-fA-F]{1,6}|#\d{1,7}|[a-zA-Z]{2,8});/g, (m, n) => {
        const r = entity(n);
        return r === null ? m : r;
      })
      .replace(/#(\d{1,7}|[a-zA-Z]{2,8});/g, (m, n) => {
        const r = /^\d/.test(n) ? cp(parseInt(n, 10)) || null : entity(n);
        return r === null ? m : r;
      });
  }
  const FMT_TAGS = /<\/?(?:b|strong|i|em|u|s|strike|small|big|sub|sup|code|kbd|span|font|p|div|mark|del|ins|tt|center|h[1-6])(?:\s[^<>]*)?\/?>/gi;

  // Label text -> display lines (mermaid-style <br>, entities, light markdown removed).
  function labelLines(raw) {
    let s = String(raw == null ? '' : raw);
    s = s.replace(/<br\s*\/?>/gi, '\n').replace(FMT_TAGS, '');
    s = decodeEntities(s);
    s = s.replace(/\\n/g, '\n');
    s = s.replace(/\*\*(?=\S)(.+?)\*\*/g, '$1').replace(/__(?=\S)(.+?)__/g, '$1');
    s = s.replace(/(^|\s)fa[bsrl]?:fa-[\w-]+/g, '$1');
    const lines = s.split('\n').map((l) => l.replace(/[\t ]+/g, ' ').trim());
    while (lines.length > 1 && !lines[lines.length - 1]) lines.pop();
    while (lines.length > 1 && !lines[0]) lines.shift();
    return lines;
  }
  function unquote(s) {
    s = String(s).trim();
    if (s.length >= 2 && ((s[0] === '"' && s[s.length - 1] === '"') || (s[0] === "'" && s[s.length - 1] === "'"))) s = s.slice(1, -1);
    if (s.length >= 2 && s[0] === '`' && s[s.length - 1] === '`') s = s.slice(1, -1);
    return s;
  }

  // Greedy word wrap; words wider than the limit are split by character.
  function wrap(lines, limit, fs, bold) {
    const out = [];
    for (const line of lines) {
      if (!line || textW(line, fs, bold) <= limit) {
        out.push(line);
        continue;
      }
      let cur = '';
      for (let word of line.split(' ')) {
        while (textW(word, fs, bold) > limit * 1.1) {
          const chars = Array.from(word);
          let k = 1, w = 0;
          const room = cur ? limit - textW(cur + ' ', fs, bold) : limit;
          for (; k < chars.length; k++) {
            w = textW(chars.slice(0, k + 1).join(''), fs, bold);
            if (w > Math.max(room, limit * 0.3)) break;
          }
          const head = chars.slice(0, k).join('');
          out.push(cur ? cur + ' ' + head : head);
          cur = '';
          word = chars.slice(k).join('');
        }
        const cand = cur ? cur + ' ' + word : word;
        if (!cur || textW(cand, fs, bold) <= limit) cur = cand;
        else {
          out.push(cur);
          cur = word;
        }
      }
      if (cur) out.push(cur);
    }
    return out.length ? out : [''];
  }

  // <text> block whose lines are centred vertically on cy.
  function textBlock(lines, x, cy, ctx, cls, anchor, size, extra) {
    const fs = size || ctx.fs;
    const lh = fs * 1.3;
    const n = lines.length;
    const y0 = cy - (n * lh) / 2 + lh / 2 + fs * 0.35;
    let s = '<text class="' + cls + '" x="' + f(x) + '" y="' + f(y0) + '"';
    if (anchor !== 'start') s += ' text-anchor="' + (anchor || 'middle') + '"';
    if (size) s += ' font-size="' + f(size) + '"';
    s += (extra || '') + '>';
    if (n === 1) return s + esc(lines[0]) + '</text>';
    for (let i = 0; i < n; i++) s += '<tspan x="' + f(x) + '" y="' + f(y0 + i * lh) + '">' + esc(lines[i]) + '</tspan>';
    return s + '</text>';
  }

  // ------------------------------------------------------------------ user styles
  function safeColor(v) {
    v = String(v).trim();
    return /^#[0-9a-fA-F]{3,8}$/.test(v) || /^[a-zA-Z]{3,24}$/.test(v) ||
      /^(?:rgb|hsl)a?\(\s*[\d.%\s,/]+\)$/i.test(v) ? v : null;
  }
  function parseStyle(str) {
    const out = {};
    for (const part of String(str).split(/[,;](?![^(]*\))/)) {
      const m = /^\s*([\w-]+)\s*:\s*(.+?)\s*$/.exec(part);
      if (!m) continue;
      const k = m[1].toLowerCase();
      const v = m[2].replace(/\s*!important$/i, '');
      if (k === 'fill' || k === 'stroke' || k === 'color') {
        const c = safeColor(v);
        if (c) out[k] = c;
      } else if (k === 'stroke-width') {
        const n = parseFloat(v);
        if (n >= 0 && n <= 20) out.sw = n;
      } else if (k === 'stroke-dasharray') {
        if (/^[\d.\s,]+$/.test(v)) out.dash = v.trim().replace(/\s+/g, ' ');
      } else if (k === 'font-weight') {
        if (/^(?:bold|bolder|[6-9]00)$/i.test(v)) out.bold = true;
      }
    }
    return out;
  }
  function hexLum(c) {
    let m = /^#([0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$/i.exec(c || '');
    if (!m) return -1;
    let h = m[1];
    if (h.length <= 4) h = h.split('').map((x) => x + x).join('');
    const ch = [0, 2, 4].map((i) => {
      const v = parseInt(h.slice(i, i + 2), 16) / 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2];
  }
  function shapeStyle(st) {
    if (!st) return '';
    let s = '';
    if (st.fill) s += 'fill:' + st.fill + ';';
    if (st.stroke) s += 'stroke:' + st.stroke + ';';
    if (st.sw != null) s += 'stroke-width:' + st.sw + 'px;';
    if (st.dash) s += 'stroke-dasharray:' + st.dash + ';';
    return s ? ' style="' + esc(s) + '"' : '';
  }
  function textStyle(st) {
    if (!st) return '';
    let c = st.color;
    if (!c && st.fill) {
      const l = hexLum(st.fill);
      if (l >= 0) c = l > 0.4 ? '#1f1e1c' : '#ffffff';
    }
    return c ? ' style="' + esc('fill:' + c) + '"' : '';
  }

  // ------------------------------------------------------------------ stylesheet
  const PALETTE = ['#2f7d6d', '#eb6834', '#4a3aa7', '#eda100', '#2a78d6', '#e34948', '#008300', '#e87ba4'];
  const CSS = [
    '.mm-node{fill:var(--mm-node-bg,#f4f3ee);stroke:var(--mm-node-border,#8a8578);stroke-width:1.3px}',
    '.mm-text{fill:var(--mm-text,#1f1e1c)}',
    '.mm-muted{fill:var(--mm-text-muted,#6b665c)}',
    '.mm-bold{font-weight:600}',
    '.mm-italic{font-style:italic}',
    '.mm-under{text-decoration:underline}',
    '.mm-edge{fill:none;stroke:var(--mm-edge,#6b665c);stroke-width:1.5px}',
    '.mm-dotted{stroke-dasharray:3 4}',
    '.mm-dashed{stroke-dasharray:6 4}',
    '.mm-thick{stroke-width:3px}',
    '.mm-head{fill:var(--mm-edge,#6b665c);stroke:none}',
    '.mm-head-open{fill:none;stroke:var(--mm-edge,#6b665c);stroke-width:1.5px;stroke-linejoin:round;stroke-linecap:round}',
    '.mm-head-hollow{fill:var(--mm-bg,#ffffff);stroke:var(--mm-edge,#6b665c);stroke-width:1.3px;stroke-linejoin:round}',
    '.mm-edge-label-bg{fill:var(--mm-bg,#ffffff)}',
    '.mm-cluster{fill:var(--mm-cluster-bg,rgba(0,0,0,.03));stroke:var(--mm-cluster-border,#b3ad9f);stroke-width:1px}',
    '.mm-note{fill:var(--mm-note-bg,#fbf1c7);stroke:var(--mm-note-border,#c4ae5e);stroke-width:1px}',
    '.mm-lifeline{stroke:var(--mm-edge,#6b665c);stroke-width:1px;stroke-dasharray:4 4;opacity:.6}',
    '.mm-frame{fill:none;stroke:var(--mm-frame,#9a9588);stroke-width:1px}',
    '.mm-frame-tab{fill:var(--mm-node-bg,#f4f3ee);stroke:var(--mm-frame,#9a9588);stroke-width:1px}',
    '.mm-frame-sep{stroke:var(--mm-frame,#9a9588);stroke-width:1px;stroke-dasharray:5 4}',
    '.mm-block-bg{fill:var(--mm-cluster-bg,rgba(0,0,0,.03))}',
    '.mm-dot{fill:var(--mm-text,#1f1e1c)}',
    '.mm-ring{fill:none;stroke:var(--mm-text,#1f1e1c);stroke-width:1.5px}',
    '.mm-divider{stroke:var(--mm-node-border,#8a8578);stroke-width:1px}',
    '.mm-num{fill:var(--mm-bg,#ffffff);font-weight:600}',
    '.mm-slice{stroke:var(--mm-bg,#ffffff);stroke-width:2px}',
    '.mm-leader{fill:none;stroke:var(--mm-text-muted,#6b665c);stroke-width:1px}',
    '.mm-grid{stroke:var(--mm-grid,rgba(0,0,0,.1));stroke-width:1px}',
    '.mm-band{fill:var(--mm-cluster-bg,rgba(0,0,0,.03))}',
    '.mm-on-accent{fill:var(--mm-on-accent,#ffffff)}',
    '.mm-actor-fig{fill:var(--mm-node-bg,#f4f3ee);stroke:var(--mm-node-border,#8a8578);stroke-width:1.5px}',
    '.mm-bar-done{opacity:.45}',
    '.mm-bar-active{fill-opacity:.55}',
    '.mm-today{stroke:var(--mm-c6,#e34948);stroke-width:1.5px}'
  ].concat(PALETTE.map((c, i) => '.mm-c' + (i + 1) + '{fill:var(--mm-c' + (i + 1) + ',' + c + ')}'))
    .concat(PALETTE.map((c, i) => '.mm-k' + (i + 1) + '{stroke:var(--mm-c' + (i + 1) + ',' + c + ')}'))
    .join('');

  // ------------------------------------------------------------------ markers
  // Geometry in (d, y): d = distance back from the node boundary along the edge.
  // trim = how far the path end is pulled back so the marker reaches the boundary.
  const MARK = {
    arrow: { trim: 9, ext: 10, h: 6, cls: 'mm-head', g: [['poly', 0, 0, 10, -4.5, 10, 4.5]] },
    open: { trim: 0, ext: 10, h: 6, cls: 'mm-head-open', g: [['line', 9, -5, 0, 0, 9, 5]] },
    circle: { trim: 8, ext: 9, h: 5, cls: 'mm-head', g: [['circ', 4.5, 0, 4]] },
    cross: { trim: 5, ext: 10, h: 5, cls: 'mm-head-open', g: [['line', 1.5, -3.5, 8.5, 3.5], ['line', 1.5, 3.5, 8.5, -3.5]] },
    tri: { trim: 13, ext: 14, h: 8, cls: 'mm-head-hollow', g: [['poly', 0, 0, 14, -7, 14, 7]] },
    diamond: { trim: 16, ext: 17, h: 6, cls: 'mm-head', g: [['poly', 0, 0, 8, -5.5, 16, 0, 8, 5.5]] },
    odiamond: { trim: 16, ext: 17, h: 6, cls: 'mm-head-hollow', g: [['poly', 0, 0, 8, -5.5, 16, 0, 8, 5.5]] },
    one: { trim: 18, ext: 18, h: 8, cls: 'mm-head-hollow', g: [['line', 0, 0, 18, 0], ['line', 7, -7, 7, 7], ['line', 12, -7, 12, 7]] },
    zeroone: { trim: 22, ext: 22, h: 8, cls: 'mm-head-hollow', g: [['line', 0, 0, 22, 0], ['line', 7, -7, 7, 7], ['circ', 16, 0, 4.5]] },
    many: { trim: 18, ext: 18, h: 8, cls: 'mm-head-hollow', g: [['line', 0, 0, 18, 0], ['line', 0, -7, 11, 0, 0, 7], ['line', 15, -7, 15, 7]] },
    zeromany: { trim: 22, ext: 22, h: 8, cls: 'mm-head-hollow', g: [['line', 0, 0, 22, 0], ['line', 0, -7, 11, 0, 0, 7], ['circ', 16.5, 0, 4.5]] }
  };
  function markerRef(ctx, kind, atStart) {
    if (!kind || !MARK[kind]) return '';
    const id = ctx.uid + '-' + kind + (atStart ? '-s' : '-e');
    ctx.markers.add(kind + (atStart ? '-s' : '-e'));
    return ' marker-' + (atStart ? 'start' : 'end') + '="url(#' + id + ')"';
  }
  function markerDefs(ctx) {
    let out = '';
    for (const key of ctx.markers) {
      const kind = key.slice(0, -2), start = key.endsWith('-s');
      const m = MARK[kind];
      // end marker: x = trim - d ; start marker: x = d - trim (marker x axis follows the path)
      const X = (d) => (start ? d - m.trim : m.trim - d);
      const minx = start ? -m.trim : m.trim - m.ext;
      let g = '';
      for (const it of m.g) {
        if (it[0] === 'circ') g += '<circle cx="' + f(X(it[1])) + '" cy="' + f(it[2]) + '" r="' + f(it[3]) + '"/>';
        else {
          const p = [];
          for (let i = 1; i < it.length; i += 2) p.push(X(it[i]), it[i + 1]);
          g += it[0] === 'poly' ? '<polygon points="' + pts(p) + '"/>' : '<polyline points="' + pts(p) + '"/>';
        }
      }
      out += '<marker id="' + ctx.uid + '-' + key + '" viewBox="' + f(minx - 2) + ' ' + f(-m.h - 2) + ' ' + f(m.ext + 4) + ' ' + f(2 * m.h + 4) +
        '" refX="0" refY="0" markerWidth="' + f(m.ext + 4) + '" markerHeight="' + f(2 * m.h + 4) +
        '" markerUnits="userSpaceOnUse" orient="auto" overflow="visible"><g class="' + m.cls + '">' + g + '</g></marker>';
    }
    return out;
  }
  function trimOf(kind) {
    return kind && MARK[kind] ? MARK[kind].trim : 0;
  }

  // ------------------------------------------------------------------ source handling
  function prep(src) {
    if (typeof src !== 'string') fail('Diagram source must be text');
    if (src.length > LIMIT.src) fail('Diagram source is too large');
    let s = src.replace(/\r\n?/g, '\n').replace(/^\ufeff/, '');
    const fence = /^\s*(`{3,}|~{3,})[ \t]*mermaid\b[^\n]*\n([\s\S]*?)\n?[ \t]*\1[ \t]*\s*$/i.exec(s);
    if (fence) s = fence[2];
    const raw = s.split('\n');
    let start = 0;
    while (start < raw.length && !raw[start].trim()) start++;
    let title = '';
    if (start < raw.length && raw[start].trim() === '---') {
      let j = start + 1;
      while (j < raw.length && raw[j].trim() !== '---') {
        const m = /^\s*title\s*:\s*(.*?)\s*$/.exec(raw[j]);
        if (m) title = unquote(m[1]);
        j++;
      }
      if (j < raw.length) start = j + 1;
    }
    const lines = [];
    let accTitle = '', accDescr = '';
    for (let i = start; i < raw.length; i++) {
      const t = raw[i].trim();
      if (!t || t.startsWith('%%')) continue;
      let m;
      if ((m = /^accTitle\s*:\s*(.*)$/.exec(t))) {
        accTitle = m[1];
        continue;
      }
      if ((m = /^accDescr\s*:\s*(.*)$/.exec(t))) {
        accDescr = m[1];
        continue;
      }
      if ((m = /^accDescr\s*\{\s*(.*)$/.exec(t))) {
        const parts = [];
        let rest = m[1];
        while (true) {
          const k = rest.indexOf('}');
          if (k >= 0) {
            parts.push(rest.slice(0, k));
            break;
          }
          parts.push(rest);
          if (++i >= raw.length) break;
          rest = raw[i].trim();
        }
        accDescr = parts.join(' ').trim();
        continue;
      }
      lines.push({ s: raw[i].replace(/\s+$/, ''), t, n: i + 1 });
    }
    return { lines, title, accTitle, accDescr };
  }

  function typeOf(line) {
    const m = /^([A-Za-z][\w-]*)/.exec(line);
    if (!m) return null;
    switch (m[1].toLowerCase()) {
      case 'graph': case 'flowchart': case 'flowchart-elk': case 'flowchart-v2': return 'flowchart';
      case 'sequencediagram': return 'sequence';
      case 'pie': return 'pie';
      case 'statediagram': case 'statediagram-v2': return 'state';
      case 'classdiagram': case 'classdiagram-v2': return 'class';
      case 'erdiagram': return 'er';
      case 'gantt': return 'gantt';
      case 'mindmap': return 'mindmap';
    }
    return null;
  }

  function detect(src) {
    try {
      const p = prep(src);
      return p.lines.length ? typeOf(p.lines[0].t) : null;
    } catch (e) {
      return null;
    }
  }

  function finish(ctx, w, h, body) {
    const W = Math.max(1, Math.ceil(w)), H = Math.max(1, Math.ceil(h));
    let s = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ' + W + ' ' + H + '" width="' + W + '" height="' + H + '" class="mm-svg" role="img">';
    if (ctx.accTitle) s += '<title>' + esc(ctx.accTitle) + '</title>';
    if (ctx.accDescr) s += '<desc>' + esc(ctx.accDescr) + '</desc>';
    s += '<style>' + CSS + '</style>';
    const defs = markerDefs(ctx);
    if (defs) s += '<defs>' + defs + '</defs>';
    return s + '<g font-family="' + esc(ctx.ff) + '" font-size="' + f(ctx.fs) + '">' + body + '</g></svg>';
  }

  // Adds a centred bold title above a finished body.
  function withTitle(ctx, title, res) {
    if (!title) return res;
    const fs = ctx.fs * 1.15;
    const lines = wrap(labelLines(title), Math.max(res.w, 240), fs, true);
    const th = lines.length * fs * 1.3 + 10;
    const tw = maxW(lines, fs, true) + 16;
    const w = Math.max(res.w, tw);
    const dx = (w - res.w) / 2;
    const body = textBlock(lines, w / 2, 8 + th / 2 - 4, ctx, 'mm-text mm-bold', 'middle', fs) +
      '<g transform="translate(' + f(dx) + ',' + f(th) + ')">' + res.body + '</g>';
    return { body, w, h: res.h + th };
  }

  // ------------------------------------------------------------------ geometry helpers
  function unit(x, y) {
    const l = Math.hypot(x, y);
    return l > 1e-9 ? [x / l, y / l] : [0, 1];
  }
  function dist(a, b) {
    return Math.hypot(b[0] - a[0], b[1] - a[1]);
  }
  // First boundary hit of a ray (ox,oy)+t(dx,dy) against a closed polygon (flat, centre-relative).
  function rayHit(poly, ox, oy, dx, dy) {
    let best = Infinity;
    const n = poly.length;
    for (let i = 0; i < n; i += 2) {
      const px = poly[i], py = poly[i + 1];
      const qx = poly[(i + 2) % n], qy = poly[(i + 3) % n];
      const ex = qx - px, ey = qy - py;
      const den = dx * ey - dy * ex;
      if (Math.abs(den) < 1e-12) continue;
      const t = ((px - ox) * ey - (py - oy) * ex) / den;
      const s = ((px - ox) * dy - (py - oy) * dx) / den;
      if (t > 1e-7 && s >= -1e-9 && s <= 1 + 1e-9 && t < best) best = t;
    }
    return best === Infinity ? null : best;
  }
  function inBox(p, b) {
    return p[0] > b.x && p[0] < b.x + b.w && p[1] > b.y && p[1] < b.y + b.h;
  }
  // Point where segment a (inside) -> b (outside) leaves box b.
  function exitBox(a, b, box) {
    let t = 1;
    const dx = b[0] - a[0], dy = b[1] - a[1];
    if (dx > 1e-9) t = Math.min(t, (box.x + box.w - a[0]) / dx);
    if (dx < -1e-9) t = Math.min(t, (box.x - a[0]) / dx);
    if (dy > 1e-9) t = Math.min(t, (box.y + box.h - a[1]) / dy);
    if (dy < -1e-9) t = Math.min(t, (box.y - a[1]) / dy);
    t = Math.max(0, t);
    return [a[0] + dx * t, a[1] + dy * t];
  }
  function circlePoly(r, n) {
    const out = [];
    for (let i = 0; i < n; i++) {
      const a = (i / n) * Math.PI * 2;
      out.push(Math.cos(a) * r, Math.sin(a) * r);
    }
    return out;
  }

  // Smooth path through P with given end tangents (Hermite segments; interior tangents follow the chord).
  function curvePath(P, T0, Tn, trim0, trimN, ax) {
    const n = P.length;
    const Q = P.map((p) => p.slice());
    const T = new Array(n);
    T[0] = T0;
    T[n - 1] = Tn;
    for (let i = 1; i < n - 1; i++) {
      const dx = Q[i + 1][0] - Q[i - 1][0], dy = Q[i + 1][1] - Q[i - 1][1];
      const sg = ax ? Math.sign(dx * ax[0] + dy * ax[1]) : 0;
      T[i] = sg ? [ax[0] * sg, ax[1] * sg] : unit(dx, dy);
    }
    if (trim0) {
      const t = Math.min(trim0, dist(Q[0], Q[1]) * 0.7);
      Q[0] = [Q[0][0] + T[0][0] * t, Q[0][1] + T[0][1] * t];
    }
    if (trimN) {
      const t = Math.min(trimN, dist(Q[n - 2], Q[n - 1]) * 0.7);
      Q[n - 1] = [Q[n - 1][0] - T[n - 1][0] * t, Q[n - 1][1] - T[n - 1][1] * t];
    }
    let d = 'M' + f(Q[0][0]) + ',' + f(Q[0][1]);
    for (let i = 0; i < n - 1; i++) {
      const a = Q[i], b = Q[i + 1];
      const L = dist(a, b);
      if (L < 1e-6) continue;
      const h = (t) => {
        const proj = Math.abs((b[0] - a[0]) * t[0] + (b[1] - a[1]) * t[1]);
        return Math.max(Math.min(proj * 0.5, L * 0.45), L * 0.12);
      };
      const h1 = h(T[i]), h2 = h(T[i + 1]);
      d += 'C' + f(a[0] + T[i][0] * h1) + ',' + f(a[1] + T[i][1] * h1) + ' ' +
        f(b[0] - T[i + 1][0] * h2) + ',' + f(b[1] - T[i + 1][1] * h2) + ' ' + f(b[0]) + ',' + f(b[1]);
    }
    return d;
  }
  function polyMid(P) {
    let total = 0;
    for (let i = 1; i < P.length; i++) total += dist(P[i - 1], P[i]);
    let acc = 0;
    for (let i = 1; i < P.length; i++) {
      const l = dist(P[i - 1], P[i]);
      if (acc + l >= total / 2 && l > 0) {
        const t = (total / 2 - acc) / l;
        return [P[i - 1][0] + (P[i][0] - P[i - 1][0]) * t, P[i - 1][1] + (P[i][1] - P[i - 1][1]) * t];
      }
      acc += l;
    }
    return P[0].slice();
  }

  // ------------------------------------------------------------------ layered layout
  // Sugiyama-style: DFS cycle breaking, longest-path layering refined by local moves,
  // dummy chains for long edges (label dummies reserve room for edge labels),
  // barycentre ordering that keeps clusters contiguous, PAVA coordinate sweeps and a
  // final constraint pass that keeps cluster boxes free of foreign nodes.
  const REAL = 0, DUMMY = 1, LABEL = 2, PH = 3;

  function layoutGraph(inp) {
    const dir = inp.dir;
    const horiz = dir === 'LR' || dir === 'RL';
    const anyLabel = inp.edges.some((e) => e && e.label);
    const rankSep = anyLabel ? inp.rankSep / 2 : inp.rankSep;
    const nodeSep = inp.nodeSep, edgeSep = inp.edgeSep, pad = inp.pad, gap = inp.gap;

    // clusters
    const CL = inp.clusters;
    const C = CL.length;
    const cIndex = new Map();
    CL.forEach((c, i) => cIndex.set(c.id, i));
    const cParent = CL.map((c) => (c.parent != null && cIndex.has(c.parent) ? cIndex.get(c.parent) : -1));
    const cChain = [];
    for (let i = 0; i < C; i++) {
      const ch = [];
      let k = i, guard = 0;
      while (k >= 0 && guard++ <= C) {
        ch.push(k);
        k = cParent[k];
      }
      ch.reverse();
      cChain.push(ch);
    }
    const NOCHAIN = [];
    const titleTop = (c) => (dir === 'TB' ? CL[c].th : 0);
    const titleBot = (c) => (dir === 'BT' ? CL[c].th : 0);
    const titleLeft = (c) => (horiz ? CL[c].th : 0);

    // nodes
    const N = [];
    const nIndex = new Map();
    const addNode = (o) => {
      o.i = N.length;
      o.up = [];
      o.down = [];
      N.push(o);
      return o;
    };
    for (const nd of inp.nodes) {
      const cl = nd.cluster != null && cIndex.has(nd.cluster) ? cIndex.get(nd.cluster) : -1;
      const w = horiz ? nd.h : nd.w, h = horiz ? nd.w : nd.h;
      const n = addNode({ id: nd.id, kind: REAL, w, h, lw: w / 2, rw: w / 2 + (nd.loop || 0), cl, rank: 0 });
      nIndex.set(nd.id, n.i);
    }
    const cHas = new Uint8Array(C);
    for (const n of N) if (n.cl >= 0) for (const c of cChain[n.cl]) cHas[c] = 1;
    for (let c = C - 1; c >= 0; c--) {
      if (cHas[c]) continue;
      const w0 = Math.max(CL[c].tw, 40), h0 = 12;
      const w = horiz ? h0 : w0, h = horiz ? w0 : h0;
      addNode({ id: null, hidden: true, kind: REAL, w, h, lw: w / 2, rw: w / 2, cl: c, rank: 0 });
      for (const k of cChain[c]) cHas[k] = 1;
    }
    const n0 = N.length;
    const chainOf = (v) => (N[v].cl >= 0 ? cChain[N[v].cl] : NOCHAIN);
    const inCl = (v, c) => N[v].cl >= 0 && cChain[N[v].cl].indexOf(c) >= 0;
    const cMembers = Array.from({ length: C }, () => []);
    for (let v = 0; v < n0; v++) for (const c of chainOf(v)) cMembers[c].push(v);

    // edges -> layering edges (LE); cluster endpoints use internal sinks/sources
    const nodeOut = N.map(() => []), nodeIn = N.map(() => []);
    for (const e of inp.edges) {
      if (!e) continue;
      const u = nIndex.get(e.from), v = nIndex.get(e.to);
      if (u !== undefined && v !== undefined && u !== v) {
        nodeOut[u].push(v);
        nodeIn[v].push(u);
      }
    }
    const clusterEnds = (c, sinks) => {
      const mem = cMembers[c];
      const res = mem.filter((n) => !(sinks ? nodeOut[n] : nodeIn[n]).some((m) => inCl(m, c)));
      return res.length ? res : [sinks ? mem[mem.length - 1] : mem[0]];
    };
    const LE = [];
    const result = new Array(inp.edges.length).fill(null);
    inp.edges.forEach((e, ei) => {
      if (!e) return;
      const fu = nIndex.get(e.from), tv = nIndex.get(e.to);
      const fc = fu === undefined ? cIndex.get(e.from) : undefined;
      const tc = tv === undefined ? cIndex.get(e.to) : undefined;
      if ((fu === undefined && fc === undefined) || (tv === undefined && tc === undefined)) return;
      const minlen = Math.max(1, Math.min(8, Math.round(e.minlen || 1))) * (anyLabel ? 2 : 1);
      if (fu !== undefined && tv !== undefined) {
        if (fu === tv) {
          result[ei] = { loop: true };
          return;
        }
        LE.push({ u: fu, v: tv, minlen, w: 1, ei });
        return;
      }
      if (fc !== undefined && tc !== undefined && (cChain[fc].indexOf(tc) >= 0 || cChain[tc].indexOf(fc) >= 0)) return;
      if (fc !== undefined && tv !== undefined && inCl(tv, fc)) return;
      if (tc !== undefined && fu !== undefined && inCl(fu, tc)) return;
      const S = fc !== undefined ? clusterEnds(fc, true) : [fu];
      const T = tc !== undefined ? clusterEnds(tc, false) : [tv];
      const rs = S[S.length - 1], rt = T[0];
      LE.push({ u: rs, v: rt, minlen, w: 1, ei, fc, tc });
      let budget = 64;
      for (const s of S) for (const t of T) if ((s !== rs || t !== rt) && s !== t && budget-- > 0) LE.push({ u: s, v: t, minlen, w: 0.5, ei: -1 });
    });

    // cycle removal: reverse DFS back edges
    {
      const outA = Array.from({ length: n0 }, () => []);
      const inCnt = new Int32Array(n0);
      LE.forEach((e, k) => {
        outA[e.u].push(k);
        inCnt[e.v]++;
      });
      const st = new Uint8Array(n0);
      const starts = [];
      for (let i = 0; i < n0; i++) if (!inCnt[i]) starts.push(i);
      for (let i = 0; i < n0; i++) if (inCnt[i]) starts.push(i);
      for (const s0 of starts) {
        if (st[s0]) continue;
        st[s0] = 1;
        const stack = [s0], it = [0];
        while (stack.length) {
          const top = stack.length - 1, u = stack[top];
          if (it[top] < outA[u].length) {
            const k = outA[u][it[top]++];
            const v = LE[k].v;
            if (st[v] === 1) LE[k].rev = true;
            else if (!st[v]) {
              st[v] = 1;
              stack.push(v);
              it.push(0);
            }
          } else {
            st[u] = 2;
            stack.pop();
            it.pop();
          }
        }
      }
      for (const e of LE) {
        if (e.rev) {
          const t = e.u;
          e.u = e.v;
          e.v = t;
        }
      }
    }

    // layering
    {
      const inL = Array.from({ length: n0 }, () => []), outL = Array.from({ length: n0 }, () => []);
      const indeg = new Int32Array(n0);
      LE.forEach((e, k) => {
        outL[e.u].push(k);
        inL[e.v].push(k);
        indeg[e.v]++;
      });
      const topo = [];
      for (let i = 0; i < n0; i++) if (!indeg[i]) topo.push(i);
      for (let h = 0; h < topo.length; h++) {
        for (const k of outL[topo[h]]) if (--indeg[LE[k].v] === 0) topo.push(LE[k].v);
      }
      if (topo.length < n0) for (let i = 0; i < n0; i++) if (indeg[i] > 0) topo.push(i);
      const rank = new Float64Array(n0);
      for (const u of topo) {
        for (const k of outL[u]) {
          const e = LE[k];
          if (rank[e.v] < rank[u] + e.minlen) rank[e.v] = rank[u] + e.minlen;
        }
      }
      for (let pass = 0; pass < 16; pass++) {
        let changed = false;
        for (let t = 0; t < topo.length; t++) {
          const v = pass % 2 ? topo[topo.length - 1 - t] : topo[t];
          let lo = -Infinity, hi = Infinity, wi = 0, wo = 0;
          for (const k of inL[v]) {
            const e = LE[k];
            lo = Math.max(lo, rank[e.u] + e.minlen);
            wi += e.w;
          }
          for (const k of outL[v]) {
            const e = LE[k];
            hi = Math.min(hi, rank[e.v] - e.minlen);
            wo += e.w;
          }
          if (lo === -Infinity && hi === Infinity) continue;
          let r = rank[v];
          if (lo === -Infinity) r = hi;
          else if (hi === Infinity) r = lo;
          else if (lo > hi) continue;
          else if (wi > wo) r = lo;
          else if (wo > wi) r = hi;
          else r = Math.min(Math.max(r, lo), hi);
          if (r !== rank[v]) {
            rank[v] = r;
            changed = true;
          }
        }
        if (!changed) break;
      }
      let mn = Infinity;
      for (let i = 0; i < n0; i++) mn = Math.min(mn, rank[i]);
      for (let i = 0; i < n0; i++) N[i].rank = rank[i] - (mn === Infinity ? 0 : mn);
    }

    // cluster rank spans
    const cMin = new Array(C).fill(Infinity), cMax = new Array(C).fill(-Infinity);
    for (let c = 0; c < C; c++) {
      for (const v of cMembers[c]) {
        cMin[c] = Math.min(cMin[c], N[v].rank);
        cMax[c] = Math.max(cMax[c], N[v].rank);
      }
    }

    // dummy chains
    const lca = (a, b) => {
      if (a < 0 || b < 0) return -1;
      const A = cChain[a], B = cChain[b];
      let k = 0;
      while (k < A.length && k < B.length && A[k] === B[k]) k++;
      return k ? A[k - 1] : -1;
    };
    const link = (u, v) => {
      const ru = N[u].kind === REAL, rv = N[v].kind === REAL;
      const w = ru && rv ? 1 : ru || rv ? 2 : 8;
      N[u].down.push(v, w);
      N[v].up.push(u, w);
    };
    const chains = new Array(inp.edges.length).fill(null);
    for (const e of LE) {
      if (e.ei < 0) continue;
      const lab = inp.edges[e.ei].label;
      const r0 = N[e.u].rank, r1 = N[e.v].rank;
      const cl = lca(N[e.u].cl, N[e.v].cl);
      const labR = lab && r1 - r0 >= 2 ? r0 + Math.floor((r1 - r0) / 2) : -1;
      const chain = [e.u];
      let prev = e.u;
      for (let r = r0 + 1; r < r1; r++) {
        const isL = r === labR;
        const w = isL ? (horiz ? lab.h : lab.w) : 0, h = isL ? (horiz ? lab.w : lab.h) : 0;
        const d = addNode({ id: null, kind: isL ? LABEL : DUMMY, w, h, lw: w / 2, rw: w / 2, cl, rank: r });
        if (N.length > LIMIT.layout) fail('Diagram is too large to lay out');
        link(prev, d.i);
        chain.push(d.i);
        prev = d.i;
      }
      link(prev, e.v);
      chain.push(e.v);
      chains[e.ei] = { chain, rev: !!e.rev, lab: labR >= 0 ? chain[labR - r0] : -1, fc: e.fc, tc: e.tc };
    }

    let R = 0;
    for (const n of N) R = Math.max(R, n.rank + 1);
    // placeholders keep every cluster present on each rank of its span
    if (C) {
      const present = Array.from({ length: R }, () => new Set());
      for (const n of N) if (n.cl >= 0) for (const c of cChain[n.cl]) present[n.rank].add(c);
      for (let c = 0; c < C; c++) {
        for (let r = cMin[c]; r <= cMax[c]; r++) {
          if (present[r].has(c)) continue;
          addNode({ id: null, kind: PH, w: 0, h: 0, lw: 0, rw: 0, cl: c, rank: r });
          for (const a of cChain[c]) present[r].add(a);
        }
      }
    }
    const T = N.length;

    // ---- ordering
    let layers = Array.from({ length: R }, () => []);
    const pos = new Float64Array(T);
    const setPos = (r) => {
      const L = layers[r];
      for (let i = 0; i < L.length; i++) pos[L[i]] = i;
    };
    for (let r = 0; r < R; r++) setPos(r);

    let bias = false;
    const key = new Float64Array(T);
    const has = new Uint8Array(T);
    const arrange = (nodes, depth) => {
      const items = [];
      const groups = new Map();
      for (let idx = 0; idx < nodes.length; idx++) {
        const v = nodes[idx];
        const ch = chainOf(v);
        if (ch.length <= depth) items.push({ k: key[v], t: idx, list: [v] });
        else {
          let g = groups.get(ch[depth]);
          if (!g) {
            g = { t: idx, nodes: [], k: 0, list: null };
            groups.set(ch[depth], g);
            items.push(g);
          }
          g.nodes.push(v);
        }
      }
      for (const it of items) {
        if (!it.nodes) continue;
        it.list = arrange(it.nodes, depth + 1);
        let s = 0, n = 0, s2 = 0;
        for (const v of it.nodes) {
          if (has[v]) {
            s += key[v];
            n++;
          }
          s2 += key[v];
        }
        it.k = n ? s / n : s2 / it.nodes.length;
      }
      items.sort((a, b) => a.k - b.k || (bias ? b.t - a.t : a.t - b.t));
      const out = [];
      for (const it of items) for (const v of it.list) out.push(v);
      return out;
    };
    const sortLayer = (r, useUp) => {
      const L = layers[r];
      const adj = layers[useUp ? r - 1 : r + 1];
      const m = adj ? adj.length : 1;
      for (let i = 0; i < L.length; i++) {
        const v = L[i];
        const nb = useUp ? N[v].up : N[v].down;
        if (nb.length) {
          let s = 0, ws = 0;
          for (let k = 0; k < nb.length; k += 2) {
            s += nb[k + 1] * (pos[nb[k]] + 0.5) / m;
            ws += nb[k + 1];
          }
          key[v] = s / ws;
          has[v] = 1;
        } else {
          key[v] = (i + 0.5) / L.length;
          has[v] = 0;
        }
      }
      layers[r] = arrange(L, 0);
      setPos(r);
    };
    const crossings = () => {
      let total = 0;
      for (let r = 0; r + 1 < R; r++) {
        const m = layers[r + 1].length;
        const tree = new Float64Array(m + 1);
        let seen = 0;
        for (const u of layers[r]) {
          const d = N[u].down;
          const ps = [];
          for (let k = 0; k < d.length; k += 2) ps.push(pos[d[k]]);
          ps.sort((a, b) => a - b);
          for (const p of ps) {
            let c = 0;
            for (let i = p + 1; i > 0; i -= i & -i) c += tree[i];
            total += seen - c;
            for (let i = p + 1; i <= m; i += i & -i) tree[i]++;
            seen++;
          }
        }
      }
      return total;
    };
    // DFS start order (children forward or reversed), then barycentre sweeps; best of both kept
    const initOrder = (rev) => {
      layers = Array.from({ length: R }, () => []);
      const seen = new Uint8Array(T);
      const startOrder = [];
      for (let i = 0; i < n0; i++) startOrder.push(i);
      startOrder.sort((a, b) => N[a].rank - N[b].rank || (rev ? b - a : a - b));
      for (const s0 of startOrder) {
        if (seen[s0]) continue;
        const stack = [s0];
        while (stack.length) {
          const u = stack.pop();
          if (seen[u]) continue;
          seen[u] = 1;
          layers[N[u].rank].push(u);
          const d = N[u].down;
          if (rev) {
            for (let k = 0; k < d.length; k += 2) if (!seen[d[k]]) stack.push(d[k]);
          } else {
            for (let k = d.length - 2; k >= 0; k -= 2) if (!seen[d[k]]) stack.push(d[k]);
          }
        }
      }
      for (let i = 0; i < T; i++) if (!seen[i]) layers[N[i].rank].push(i);
      for (let r = 0; r < R; r++) {
        const L = layers[r];
        for (let i = 0; i < L.length; i++) {
          key[L[i]] = i;
          has[L[i]] = 1;
        }
        bias = false;
        layers[r] = arrange(L, 0);
        setPos(r);
      }
    };
    const sweeps = () => {
      let best = layers.map((l) => l.slice());
      let bestC = crossings(), stale = 0;
      const maxIt = T > 4000 ? 4 : T > 1500 ? 10 : 24;
      for (let it = 0; it < maxIt && bestC > 0; it++) {
        bias = it % 4 >= 2;
        if (it % 2 === 0) for (let r = 1; r < R; r++) sortLayer(r, true);
        else for (let r = R - 2; r >= 0; r--) sortLayer(r, false);
        const c = crossings();
        if (c < bestC) {
          bestC = c;
          best = layers.map((l) => l.slice());
          stale = 0;
        } else if (++stale >= 6) break;
      }
      return { best, bestC };
    };
    {
      initOrder(false);
      let res = sweeps();
      if (res.bestC > 0 && T <= 3000) {
        initOrder(true);
        const res2 = sweeps();
        if (res2.bestC < res.bestC) res = res2;
      }
      layers = res.best;
      for (let r = 0; r < R; r++) setPos(r);
    }
    // sibling clusters take one consistent left-to-right order on every rank
    if (C > 1) {
      const gk = new Float64Array(C), gn = new Float64Array(C);
      for (let r = 0; r < R; r++) {
        const L = layers[r];
        for (let i = 0; i < L.length; i++) for (const c of chainOf(L[i])) {
          gk[c] += (i + 0.5) / L.length;
          gn[c]++;
        }
      }
      for (let c = 0; c < C; c++) gk[c] = gn[c] ? gk[c] / gn[c] : 0;
      const fix = (nodes, depth) => {
        const items = [];
        const groups = new Map();
        for (const v of nodes) {
          const ch = chainOf(v);
          if (ch.length <= depth) items.push({ list: [v] });
          else {
            let g = groups.get(ch[depth]);
            if (!g) {
              g = { c: ch[depth], nodes: [] };
              groups.set(ch[depth], g);
              items.push(g);
            }
            g.nodes.push(v);
          }
        }
        const slots = [], cl = [];
        items.forEach((it, i) => {
          if (it.nodes) {
            it.list = fix(it.nodes, depth + 1);
            slots.push(i);
            cl.push(it);
          }
        });
        cl.sort((a, b) => gk[a.c] - gk[b.c] || a.c - b.c);
        slots.forEach((s, i) => (items[s] = cl[i]));
        const out = [];
        for (const it of items) for (const v of it.list) out.push(v);
        return out;
      };
      for (let r = 0; r < R; r++) {
        layers[r] = fix(layers[r], 0);
        setPos(r);
      }
    }

    // ---- x coordinates (TB space)
    const bInfo = [];
    const seqs = [];
    for (let r = 0; r < R; r++) {
      const s = [];
      const open = [];
      for (const v of layers[r]) {
        const ch = chainOf(v);
        let k = 0;
        while (k < open.length && k < ch.length && open[k] === ch[k]) k++;
        while (open.length > k) {
          s.push(T + bInfo.length);
          bInfo.push({ c: open.pop(), side: 1, r });
        }
        for (let j = k; j < ch.length; j++) {
          open.push(ch[j]);
          s.push(T + bInfo.length);
          bInfo.push({ c: ch[j], side: 0, r });
        }
        s.push(v);
      }
      while (open.length) {
        s.push(T + bInfo.length);
        bInfo.push({ c: open.pop(), side: 1, r });
      }
      seqs.push(s);
    }
    const VV = T + bInfo.length;
    const isB = (v) => v >= T;
    const sep = (a, b) => {
      if (!isB(a) && !isB(b)) {
        const ka = N[a].kind, kb = N[b].kind;
        let g;
        if (ka === REAL && kb === REAL) g = nodeSep;
        else if (ka === PH || kb === PH) g = edgeSep;
        else if ((ka === REAL && kb === LABEL) || (ka === LABEL && kb === REAL)) g = nodeSep * 0.6;
        else if (ka === DUMMY && kb === DUMMY) g = edgeSep;
        else g = edgeSep * 1.5;
        return N[a].rw + g + N[b].lw;
      }
      if (!isB(a)) return N[a].rw + (bInfo[b - T].side === 0 ? gap : pad);
      if (!isB(b)) {
        const A = bInfo[a - T];
        return (A.side === 0 ? pad + titleLeft(A.c) : gap) + N[b].lw;
      }
      const A = bInfo[a - T], B = bInfo[b - T];
      if (A.side === 0 && B.side === 0) return pad + titleLeft(A.c);
      if (A.side === 1 && B.side === 1) return pad;
      if (A.side === 1 && B.side === 0) return gap;
      return 2 * pad;
    };
    const sepR = seqs.map((s) => {
      const a = new Float64Array(Math.max(0, s.length - 1));
      for (let i = 0; i + 1 < s.length; i++) a[i] = sep(s[i], s[i + 1]);
      return a;
    });
    const bAt = Array.from({ length: C * 2 }, () => new Int32Array(R).fill(-1));
    bInfo.forEach((b, k) => (bAt[b.c * 2 + b.side][b.r] = T + k));

    const x = new Float64Array(VV);
    for (let r = 0; r < R; r++) {
      const s = seqs[r];
      let cx = 0;
      for (let i = 0; i < s.length; i++) {
        if (i) cx += sepR[r][i - 1];
        x[s[i]] = cx;
      }
      for (let i = 0; i < s.length; i++) x[s[i]] -= cx / 2;
    }
    const des = new Float64Array(VV);
    const wts = new Float64Array(VV);
    for (let v = 0; v < VV; v++) wts[v] = v >= T ? 0.5 : N[v].kind === PH ? 0.2 : 1;
    const bv = [], bw = [], bn = [];
    const project = (r) => {
      const s = seqs[r], n = s.length;
      if (!n) return;
      bv.length = bw.length = bn.length = 0;
      let off = 0;
      for (let i = 0; i < n; i++) {
        if (i) off += sepR[r][i - 1];
        let v = des[s[i]] - off, w = wts[s[i]], c = 1;
        while (bv.length && bv[bv.length - 1] > v) {
          const w2 = bw.pop();
          v = (bv.pop() * w2 + v * w) / (w2 + w);
          w += w2;
          c += bn.pop();
        }
        bv.push(v);
        bw.push(w);
        bn.push(c);
      }
      off = 0;
      let i = 0;
      for (let b = 0; b < bv.length; b++) {
        for (let c = 0; c < bn[b]; c++, i++) {
          if (i) off += sepR[r][i - 1];
          x[s[i]] = bv[b] + off;
        }
      }
    };
    const sweep = (r, useUp, useDown) => {
      const s = seqs[r];
      for (const v of s) {
        if (v >= T) continue;
        let sum = 0, ws = 0;
        if (useUp) {
          const a = N[v].up;
          for (let k = 0; k < a.length; k += 2) {
            sum += x[a[k]] * a[k + 1];
            ws += a[k + 1];
          }
        }
        if (useDown) {
          const a = N[v].down;
          for (let k = 0; k < a.length; k += 2) {
            sum += x[a[k]] * a[k + 1];
            ws += a[k + 1];
          }
        }
        des[v] = ws ? sum / ws : x[v];
      }
      const nbr = (b) => {
        const info = bInfo[b - T];
        const row = bAt[info.c * 2 + info.side];
        let sum = 0, n = 0;
        if (useUp && r > 0 && row[r - 1] >= 0) {
          sum += x[row[r - 1]];
          n++;
        }
        if (useDown && r + 1 < R && row[r + 1] >= 0) {
          sum += x[row[r + 1]];
          n++;
        }
        return n ? sum / n : null;
      };
      for (let i = s.length - 1; i >= 0; i--) {
        const v = s[i];
        if (v < T || bInfo[v - T].side !== 0) continue;
        const tight = i + 1 < s.length ? des[s[i + 1]] - sepR[r][i] : x[v];
        const nb = nbr(v);
        des[v] = nb === null ? tight : (tight + nb) / 2;
      }
      for (let i = 0; i < s.length; i++) {
        const v = s[i];
        if (v < T || bInfo[v - T].side !== 1) continue;
        const tight = i > 0 ? des[s[i - 1]] + sepR[r][i - 1] : x[v];
        const nb = nbr(v);
        des[v] = nb === null ? tight : (tight + nb) / 2;
      }
      project(r);
    };
    const iters = T > 4000 ? 2 : T > 1500 ? 4 : 8;
    for (let it = 0; it < iters; it++) {
      for (let r = 1; r < R; r++) sweep(r, true, false);
      for (let r = R - 2; r >= 0; r--) sweep(r, false, true);
    }
    for (let it = 0; it < 2; it++) for (let r = 0; r < R; r++) sweep(r, true, true);

    // unify per-rank cluster borders (boxes are rectangles); foreign items get pushed aside
    if (C) {
      const lo = new Float64Array(C), hi = new Float64Array(C);
      for (let k = 0; k < 6; k++) {
        lo.fill(Infinity);
        hi.fill(-Infinity);
        bInfo.forEach((bi, j) => {
          if (bi.side) hi[bi.c] = Math.max(hi[bi.c], x[T + j]);
          else lo[bi.c] = Math.min(lo[bi.c], x[T + j]);
        });
        for (let v = 0; v < T; v++) des[v] = x[v];
        bInfo.forEach((bi, j) => {
          des[T + j] = bi.side ? hi[bi.c] : lo[bi.c];
          wts[T + j] = 30;
        });
        for (let r = 0; r < R; r++) project(r);
      }
    }

    // final pass: merged cluster borders, difference constraints, symmetric push
    const MV = T + 2 * C;
    const X = new Float64Array(MV);
    for (let v = 0; v < T; v++) X[v] = x[v];
    for (let c = 0; c < C; c++) {
      X[T + 2 * c] = Infinity;
      X[T + 2 * c + 1] = -Infinity;
    }
    bInfo.forEach((b, k) => {
      const id = T + 2 * b.c + b.side;
      X[id] = b.side ? Math.max(X[id], x[T + k]) : Math.min(X[id], x[T + k]);
    });
    const mv = (v) => (v < T ? v : T + 2 * bInfo[v - T].c + bInfo[v - T].side);
    if (C) {
      const succ = Array.from({ length: MV }, () => []);
      const pred = Array.from({ length: MV }, () => []);
      const indeg = new Int32Array(MV);
      const addC = (a, b, d) => {
        succ[a].push(b, d);
        pred[b].push(a, d);
        indeg[b]++;
      };
      for (let r = 0; r < R; r++) {
        const s = seqs[r];
        for (let i = 0; i + 1 < s.length; i++) addC(mv(s[i]), mv(s[i + 1]), sepR[r][i]);
      }
      for (let c = 0; c < C; c++) {
        if (!Number.isFinite(X[T + 2 * c])) continue;
        addC(T + 2 * c, T + 2 * c + 1, horiz ? CL[c].th + 2 * pad : CL[c].tw + 2 * pad);
      }
      const topo = [];
      for (let v = 0; v < MV; v++) if (!indeg[v]) topo.push(v);
      for (let h = 0; h < topo.length; h++) {
        const a = succ[topo[h]];
        for (let k = 0; k < a.length; k += 2) if (--indeg[a[k]] === 0) topo.push(a[k]);
      }
      if (topo.length === MV) {
        for (let v = 0; v < MV; v++) if (!Number.isFinite(X[v])) X[v] = 0;
        const xr = new Float64Array(MV), xl = new Float64Array(MV);
        for (const v of topo) {
          let m = X[v];
          const p = pred[v];
          for (let k = 0; k < p.length; k += 2) m = Math.max(m, xr[p[k]] + p[k + 1]);
          xr[v] = m;
        }
        for (let h = topo.length - 1; h >= 0; h--) {
          const v = topo[h];
          let m = X[v];
          const s = succ[v];
          for (let k = 0; k < s.length; k += 2) m = Math.min(m, xl[s[k]] - s[k + 1]);
          xl[v] = m;
        }
        for (let v = 0; v < MV; v++) X[v] = (xr[v] + xl[v]) / 2;
      }
    }

    // straighten 1:1 chains (single down-link meeting a single up-link) within their slack
    {
      const seqIdx = new Int32Array(VV);
      for (let r = 0; r < R; r++) seqs[r].forEach((v, i) => (seqIdx[v] = i));
      const upOne = (v) => (N[v].up.length === 2 ? N[v].up[0] : -1);
      const downOne = (v) => (N[v].down.length === 2 ? N[v].down[0] : -1);
      const done = new Uint8Array(T);
      const place = (seg, lo, hi) => {
        if (seg.length < 2) return;
        let sum = 0, ws = 0;
        for (const c of seg) {
          const w = N[c].kind === REAL ? 1 : 0.3;
          sum += X[c] * w;
          ws += w;
        }
        const t = Math.min(Math.max(sum / ws, lo), hi);
        for (const c of seg) X[c] = t;
      };
      for (let r = 0; r < R; r++) {
        for (const v of layers[r]) {
          if (done[v] || N[v].kind === PH) continue;
          const u = upOne(v);
          if (u >= 0 && downOne(u) === v) continue;
          const chain = [v];
          done[v] = 1;
          for (let cur = v; ;) {
            const d = downOne(cur);
            if (d < 0 || upOne(d) !== cur) break;
            chain.push(d);
            done[d] = 1;
            cur = d;
          }
          if (chain.length < 2) continue;
          let seg = [], lo = -Infinity, hi = Infinity;
          for (const c of chain) {
            const rr = N[c].rank, s = seqs[rr], i = seqIdx[c];
            const l = i > 0 ? X[mv(s[i - 1])] + sepR[rr][i - 1] : -Infinity;
            const h = i + 1 < s.length ? X[mv(s[i + 1])] - sepR[rr][i] : Infinity;
            if (Math.max(lo, l) > Math.min(hi, h) + 1e-6) {
              place(seg, lo, hi);
              seg = [];
              lo = -Infinity;
              hi = Infinity;
            }
            seg.push(c);
            lo = Math.max(lo, l);
            hi = Math.min(hi, h);
          }
          place(seg, lo, hi);
        }
      }
    }

    // ---- y coordinates (TB space)
    const rankH = new Float64Array(R);
    for (const n of N) rankH[n.rank] = Math.max(rankH[n.rank], n.h);
    const startPad = new Float64Array(R), endPad = new Float64Array(R);
    for (let c = 0; c < C; c++) {
      if (cMin[c] === Infinity) continue;
      let sp = 0, ep = 0;
      for (const a of cChain[c]) {
        if (cMin[a] === cMin[c]) sp += pad + titleTop(a);
        if (cMax[a] === cMax[c]) ep += pad + titleBot(a);
      }
      startPad[cMin[c]] = Math.max(startPad[cMin[c]], sp);
      endPad[cMax[c]] = Math.max(endPad[cMax[c]], ep);
    }
    const extraB = new Float64Array(R + 1), extraA = new Float64Array(R + 1);
    const Y = new Float64Array(R);
    const computeY = () => {
      let y = startPad[0] + extraB[0] + rankH[0] / 2;
      Y[0] = y;
      for (let r = 1; r < R; r++) {
        y += rankH[r - 1] / 2 + rankSep + endPad[r - 1] + extraA[r - 1] + startPad[r] + extraB[r] + rankH[r] / 2;
        Y[r] = y;
      }
    };
    const cTop = new Float64Array(C), cBot = new Float64Array(C);
    const cOrder = [];
    for (let c = 0; c < C; c++) cOrder.push(c);
    cOrder.sort((a, b) => cChain[b].length - cChain[a].length);
    const cKids = Array.from({ length: C }, () => []);
    for (let c = 0; c < C; c++) if (cParent[c] >= 0) cKids[cParent[c]].push(c);
    const cDirect = Array.from({ length: C }, () => []);
    for (const n of N) if (n.cl >= 0 && (n.kind === REAL || n.kind === LABEL)) cDirect[n.cl].push(n.i);
    const clusterY = (expand) => {
      const deficit = new Float64Array(C);
      for (const c of cOrder) {
        let t = Infinity, b = -Infinity;
        for (const v of cDirect[c]) {
          t = Math.min(t, Y[N[v].rank] - N[v].h / 2);
          b = Math.max(b, Y[N[v].rank] + N[v].h / 2);
        }
        for (const k of cKids[c]) {
          t = Math.min(t, cTop[k]);
          b = Math.max(b, cBot[k]);
        }
        if (t === Infinity) {
          t = Y[Math.max(0, Math.min(R - 1, cMin[c] === Infinity ? 0 : cMin[c]))];
          b = t;
        }
        t -= pad + titleTop(c);
        b += pad + titleBot(c);
        if (horiz) {
          const need = CL[c].tw + 2 * pad;
          if (b - t < need) {
            deficit[c] = need - (b - t);
            if (expand) {
              t -= deficit[c] / 2;
              b += deficit[c] / 2;
            }
          }
        }
        cTop[c] = t;
        cBot[c] = b;
      }
      return deficit;
    };
    computeY();
    if (C) {
      const d = clusterY(false);
      let any = false;
      for (let c = 0; c < C; c++) {
        if (d[c] > 0 && cMin[c] !== Infinity) {
          extraB[cMin[c]] += d[c] / 2;
          extraA[cMax[c]] += d[c] / 2;
          any = true;
        }
      }
      if (any) computeY();
      clusterY(true);
    }

    // ---- to screen space
    const toS = (xt, yt) => (dir === 'TB' ? [xt, yt] : dir === 'BT' ? [xt, -yt] : dir === 'LR' ? [yt, xt] : [-yt, xt]);
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    const grow = (p) => {
      minX = Math.min(minX, p[0]);
      maxX = Math.max(maxX, p[0]);
      minY = Math.min(minY, p[1]);
      maxY = Math.max(maxY, p[1]);
    };
    const nodePos = new Map();
    for (let v = 0; v < n0; v++) {
      const n = N[v];
      const xt = X[v], yt = Y[n.rank];
      grow(toS(xt - n.lw, yt - n.h / 2));
      grow(toS(xt + n.rw, yt + n.h / 2));
      const c = toS(xt, yt);
      if (n.id != null) nodePos.set(n.id, { x: c[0], y: c[1], w: horiz ? n.h : n.w, h: horiz ? n.w : n.h });
    }
    const clusterBox = new Map();
    for (let c = 0; c < C; c++) {
      if (cMin[c] === Infinity) continue;
      const xl = X[T + 2 * c], xr = X[T + 2 * c + 1];
      if (!Number.isFinite(xl) || !Number.isFinite(xr)) continue;
      const a = toS(xl, cTop[c]), b = toS(xr, cBot[c]);
      const box = { x: Math.min(a[0], b[0]), y: Math.min(a[1], b[1]), w: Math.abs(b[0] - a[0]), h: Math.abs(b[1] - a[1]) };
      grow([box.x, box.y]);
      grow([box.x + box.w, box.y + box.h]);
      clusterBox.set(CL[c].id, box);
    }
    for (let v = n0; v < T; v++) {
      const n = N[v];
      if (n.kind !== LABEL) continue;
      grow(toS(X[v] - n.lw, Y[n.rank] - n.h / 2));
      grow(toS(X[v] + n.rw, Y[n.rank] + n.h / 2));
    }
    if (minX === Infinity) minX = minY = maxX = maxY = 0;
    const M = inp.margin == null ? 8 : inp.margin;
    const dx = M - minX, dy = M - minY;
    const sh = (p) => [p[0] + dx, p[1] + dy];
    for (const p of nodePos.values()) {
      p.x += dx;
      p.y += dy;
    }
    for (const b of clusterBox.values()) {
      b.x += dx;
      b.y += dy;
    }
    inp.edges.forEach((e, ei) => {
      if (!e || result[ei]) return;
      const ch = chains[ei];
      if (!ch) return;
      // node centres, band entry/exit points (straight stubs through each rank band), dummies
      let P = [];
      const last = ch.chain.length - 1;
      ch.chain.forEach((v, idx) => {
        const y = Y[N[v].rank], hh = rankH[N[v].rank] / 2;
        if (idx === 0) P.push(toS(X[v], y), toS(X[v], y + hh));
        else if (idx === last) P.push(toS(X[v], y - hh), toS(X[v], y));
        else if (hh > 0.5) P.push(toS(X[v], y - hh), toS(X[v], y + hh));
        else P.push(toS(X[v], y));
      });
      P = P.map(sh);
      if (ch.rev) P = P.reverse();
      result[ei] = {
        pts: P,
        label: ch.lab >= 0 ? sh(toS(X[ch.lab], Y[N[ch.lab].rank])) : null,
        fromCluster: nIndex.has(e.from) ? null : e.from,
        toCluster: nIndex.has(e.to) ? null : e.to
      };
    });
    const axis = dir === 'TB' ? [0, 1] : dir === 'BT' ? [0, -1] : dir === 'LR' ? [1, 0] : [-1, 0];
    const perp = horiz ? [0, 1] : [1, 0];
    return { nodes: nodePos, edges: result, clusters: clusterBox, width: maxX - minX + 2 * M, height: maxY - minY + 2 * M, axis, perp };
  }

  // ------------------------------------------------------------------ graph drawing (shared)
  // model: { dir, nodes:[{id,w,h,cluster,outline,draw(cx,cy)}],
  //          edges:[{from,to,minlen,label:lines|null,cls,style,start,end,hidden,startText,endText}],
  //          clusters:[{id,parent,title:lines,tw,th,style}] }
  function drawGraph(model, ctx, opt) {
    opt = opt || {};
    const fs = ctx.fs;
    const efs = fs * 0.92;
    const horiz = model.dir === 'LR' || model.dir === 'RL';
    const nodeMap = new Map(model.nodes.map((n) => [n.id, n]));
    const loopN = new Map();
    const edgesIn = model.edges.map((e) => {
      e._lab = null;
      if (e.label && e.label.some((l) => l)) e._lab = { w: maxW(e.label, efs) + 12, h: e.label.length * efs * 1.3 + 6 };
      e._loop = 0;
      if (e.from === e.to && nodeMap.has(e.from)) {
        e._loop = (loopN.get(e.from) || 0) + 1;
        loopN.set(e.from, e._loop);
      }
      return { from: e.from, to: e.to, minlen: e.minlen || 1, label: e._lab };
    });
    const loopExtra = new Map();
    for (const e of model.edges) {
      if (!e._loop) continue;
      const ext = 16 + 12 * e._loop + (e._lab ? (horiz ? e._lab.h : e._lab.w) + 6 : 0);
      loopExtra.set(e.from, Math.max(loopExtra.get(e.from) || 0, ext));
    }
    const lay = layoutGraph({
      dir: model.dir,
      nodes: model.nodes.map((n) => ({ id: n.id, w: n.w, h: n.h, cluster: n.cluster, loop: loopExtra.get(n.id) || 0 })),
      edges: edgesIn,
      clusters: model.clusters.map((c) => ({ id: c.id, parent: c.parent, tw: c.tw, th: c.th })),
      nodeSep: opt.nodeSep || fs * 2.9,
      rankSep: opt.rankSep || fs * 3.3,
      edgeSep: fs * 1.1,
      pad: fs * 0.9,
      gap: fs * 1.3
    });
    const ax = lay.axis, pp = lay.perp;
    const dot = (a, b) => a[0] * b[0] + a[1] * b[1];

    // ports: spread edge ends along the rank-facing sides, ray-cast onto the outline
    const routes = model.edges.map((e, i) => {
      const le = lay.edges[i];
      if (!le || le.loop) return null;
      return { P: le.pts.map((p) => p.slice()), le, T0: null, Tn: null };
    });
    const ends = new Map();
    const addEnd = (id, q) => {
      let a = ends.get(id);
      if (!a) ends.set(id, (a = []));
      a.push(q);
    };
    routes.forEach((r, i) => {
      if (!r) return;
      const P = r.P, n = P.length, e = model.edges[i];
      if (!r.le.fromCluster) addEnd(e.from, { r, at: 0, side: Math.sign(dot([P[1][0] - P[0][0], P[1][1] - P[0][1]], ax)) || 1, key: dot(P[2], pp) });
      if (!r.le.toCluster) addEnd(e.to, { r, at: 1, side: Math.sign(dot([P[n - 2][0] - P[n - 1][0], P[n - 2][1] - P[n - 1][1]], ax)) || -1, key: dot(P[n - 3], pp) });
    });
    for (const [id, list] of ends) {
      const nd = lay.nodes.get(id), mn = nodeMap.get(id);
      if (!nd || !mn) continue;
      for (const side of [-1, 1]) {
        const L = list.filter((q) => q.side === side).sort((a, b) => a.key - b.key);
        const k = L.length;
        if (!k) continue;
        const ext = horiz ? nd.h : nd.w;
        const spread = k > 1 ? Math.min(ext * (mn.spread || 0.6), (k - 1) * fs * 1.2) : 0;
        L.forEach((q, j) => {
          const off = k > 1 ? -spread / 2 + (spread * j) / (k - 1) : 0;
          const ox = pp[0] * off, oy = pp[1] * off;
          const dx = ax[0] * side, dy = ax[1] * side;
          const t = rayHit(mn.outline, ox, oy, dx, dy);
          const pt = t === null ? [nd.x + ox, nd.y + oy] : [nd.x + ox + dx * t, nd.y + oy + dy * t];
          if (q.at === 0) {
            q.r.P[0] = pt;
            q.r.T0 = [dx, dy];
          } else {
            q.r.P[q.r.P.length - 1] = pt;
            q.r.Tn = [-dx, -dy];
          }
        });
      }
    }
    // band stubs start at the port's perpendicular position; drop them when too short
    routes.forEach((r, i) => {
      if (!r) return;
      const e = model.edges[i];
      if (!r.le.fromCluster && r.T0 && r.P.length > 2) {
        const p0 = r.P[0], b = r.P[1], t = r.T0;
        const along = (b[0] - p0[0]) * t[0] + (b[1] - p0[1]) * t[1];
        if (along > trimOf(e.start) + 6) r.P[1] = [p0[0] + t[0] * along, p0[1] + t[1] * along];
        else r.P.splice(1, 1);
      }
      const n = r.P.length;
      if (!r.le.toCluster && r.Tn && n > 2) {
        const pn = r.P[n - 1], b = r.P[n - 2], t = r.Tn;
        const along = (pn[0] - b[0]) * t[0] + (pn[1] - b[1]) * t[1];
        if (along > trimOf(e.end) + 6) r.P[n - 2] = [pn[0] - t[0] * along, pn[1] - t[1] * along];
        else r.P.splice(n - 2, 1);
      }
    });
    // cluster endpoints: clip the polyline at the cluster box
    const boxTangent = (q, box, from, to) => {
      const onH = Math.abs(q[1] - box.y) < 0.5 || Math.abs(q[1] - box.y - box.h) < 0.5;
      const onV = Math.abs(q[0] - box.x) < 0.5 || Math.abs(q[0] - box.x - box.w) < 0.5;
      const d = unit(to[0] - from[0], to[1] - from[1]);
      if (ax[0] === 0 && onH) return [0, Math.sign(d[1]) || ax[1]];
      if (ax[1] === 0 && onV) return [Math.sign(d[0]) || ax[0], 0];
      return d;
    };
    routes.forEach((r) => {
      if (!r) return;
      if (r.le.fromCluster) {
        const box = lay.clusters.get(r.le.fromCluster);
        if (box) {
          let k = 1;
          while (k < r.P.length - 1 && inBox(r.P[k], box)) k++;
          if (!inBox(r.P[k], box)) {
            const q = exitBox(r.P[k - 1], r.P[k], box);
            r.P = [q].concat(r.P.slice(k));
            r.T0 = boxTangent(q, box, q, r.P[1]);
          }
        }
      }
      if (r.le.toCluster) {
        const box = lay.clusters.get(r.le.toCluster);
        if (box) {
          let k = r.P.length - 2;
          while (k > 0 && inBox(r.P[k], box)) k--;
          if (!inBox(r.P[k], box)) {
            const q = exitBox(r.P[k + 1], r.P[k], box);
            r.P = r.P.slice(0, k + 1).concat([q]);
            r.Tn = boxTangent(q, box, r.P[r.P.length - 2], q);
          }
        }
      }
      const P = r.P, n = P.length;
      if (!r.T0) r.T0 = unit(P[1][0] - P[0][0], P[1][1] - P[0][1]);
      if (!r.Tn) r.Tn = unit(P[n - 1][0] - P[n - 2][0], P[n - 1][1] - P[n - 2][1]);
    });

    let W = lay.width, H = lay.height;
    const grow = (x, y) => {
      W = Math.max(W, x + 8);
      H = Math.max(H, y + 8);
    };
    let out = '';
    // clusters, outermost first
    const byId = new Map(model.clusters.map((k) => [k.id, k]));
    const depth = (c) => {
      let d = 0, p = c.parent;
      while (p != null && byId.has(p) && d < 64) {
        d++;
        p = byId.get(p).parent;
      }
      return d;
    };
    const cl = model.clusters.map((c) => ({ c, d: depth(c) })).sort((a, b) => a.d - b.d);
    for (const { c } of cl) {
      const b = lay.clusters.get(c.id);
      if (!b) continue;
      out += '<rect class="mm-cluster" x="' + f(b.x) + '" y="' + f(b.y) + '" width="' + f(b.w) + '" height="' + f(b.h) + '" rx="6"' + shapeStyle(c.style) + '/>';
      if (c.title && c.title.length && c.title.some((l) => l)) {
        out += textBlock(c.title, b.x + b.w / 2, b.y + 6 + (c.title.length * ctx.lh) / 2, ctx, 'mm-text mm-cluster-label', 'middle', 0,
          textStyle(c.style && c.style.color ? { color: c.style.color } : null));
      }
    }
    // edges
    routes.forEach((r, i) => {
      if (!r) return;
      const e = model.edges[i];
      if (e.hidden) return;
      out += '<path class="' + (e.cls || 'mm-edge') + '" d="' + curvePath(r.P, r.T0, r.Tn, trimOf(e.start), trimOf(e.end), ax) + '"' +
        markerRef(ctx, e.start, true) + markerRef(ctx, e.end, false) + (e.style || '') + '/>';
    });
    const edgeLabel = (lines, cx, cy, L) => '<rect class="mm-edge-label-bg" x="' + f(cx - L.w / 2) + '" y="' + f(cy - L.h / 2) + '" width="' + f(L.w) + '" height="' + f(L.h) + '" rx="3"/>' +
      textBlock(lines, cx, cy, ctx, 'mm-text mm-edge-label', 'middle', efs);
    // self loops on the +perp side
    let loopLabels = '';
    model.edges.forEach((e) => {
      if (!e._loop || e.hidden) return;
      const nd = lay.nodes.get(e.from), mn = nodeMap.get(e.from);
      if (!nd || !mn) return;
      const along = horiz ? nd.w : nd.h;
      const off = Math.min(along * 0.28, fs);
      const hit = (s) => {
        const ox = ax[0] * s, oy = ax[1] * s;
        const t = rayHit(mn.outline, ox, oy, pp[0], pp[1]);
        const tt = t === null ? 0 : t;
        return [nd.x + ox + pp[0] * tt, nd.y + oy + pp[1] * tt];
      };
      const a = hit(-off), b = hit(off);
      const ext = 12 + 12 * e._loop;
      const tr = trimOf(e.end);
      const b2 = [b[0] + pp[0] * tr, b[1] + pp[1] * tr];
      out += '<path class="' + (e.cls || 'mm-edge') + '" d="M' + f(a[0]) + ',' + f(a[1]) +
        'C' + f(a[0] + pp[0] * ext * 1.6 - ax[0] * off) + ',' + f(a[1] + pp[1] * ext * 1.6 - ax[1] * off) + ' ' +
        f(b[0] + pp[0] * ext * 1.6 + ax[0] * off) + ',' + f(b[1] + pp[1] * ext * 1.6 + ax[1] * off) + ' ' + f(b2[0]) + ',' + f(b2[1]) + '"' +
        markerRef(ctx, e.end, false) + (e.style || '') + '/>';
      if (e._lab) {
        const L = e._lab;
        const reach = ext * 1.25 + 4 + (horiz ? L.h : L.w) / 2;
        const cx = (a[0] + b[0]) / 2 + pp[0] * reach, cy = (a[1] + b[1]) / 2 + pp[1] * reach;
        loopLabels += edgeLabel(e.label, cx, cy, L);
        grow(cx + L.w / 2, cy + L.h / 2);
      }
      grow(Math.max(a[0], b[0]) + pp[0] * ext * 1.3, Math.max(a[1], b[1]) + pp[1] * ext * 1.3);
    });
    // labels and end texts
    routes.forEach((r, i) => {
      if (!r) return;
      const e = model.edges[i];
      if (e.hidden) return;
      if (e._lab) {
        const p = r.le.label || polyMid(r.P);
        out += edgeLabel(e.label, p[0], p[1], e._lab);
      }
      const endText = (txt, P0, T, flip) => {
        if (!txt) return '';
        const nrm = [-T[1], T[0]];
        const cx = P0[0] + T[0] * (fs * 1.3) + nrm[0] * fs * 0.9 * flip;
        const cy = P0[1] + T[1] * (fs * 1.3) + nrm[1] * fs * 0.9 * flip;
        grow(cx + textW(txt, efs) / 2, cy + efs);
        return textBlock([txt], cx, cy, ctx, 'mm-text mm-end-label', 'middle', efs);
      };
      out += endText(e.startText, r.P[0], r.T0, 1);
      out += endText(e.endText, r.P[r.P.length - 1], [-r.Tn[0], -r.Tn[1]], -1);
    });
    out += loopLabels;
    // nodes
    for (const n of model.nodes) {
      const p = lay.nodes.get(n.id);
      if (p) out += n.draw(p.x, p.y);
    }
    return { body: out, w: W, h: H };
  }

  // ------------------------------------------------------------------ node shapes
  // Returns size, centre-relative outline polygon (for ports) and an SVG drawer.
  function shapeFor(kind, tw, th, ctx, dir) {
    const fs = ctx.fs;
    const px = fs * 1.05, py = fs * 0.6;
    let w = tw + 2 * px, h = th + 2 * py, tdx = 0, tdy = 0;
    let outline, svg;
    const rectO = (hw, hh) => [-hw, -hh, hw, -hh, hw, hh, -hw, hh];
    const rect = (cx, cy, a, rx) => '<rect' + a + ' x="' + f(cx - w / 2) + '" y="' + f(cy - h / 2) + '" width="' + f(w) + '" height="' + f(h) + '"' + (rx ? ' rx="' + f(rx) + '"' : '') + '/>';
    const poly = (o) => (cx, cy, a) => {
      const q = [];
      for (let i = 0; i < o.length; i += 2) q.push(cx + o[i], cy + o[i + 1]);
      return '<polygon' + a + ' points="' + pts(q) + '"/>';
    };
    switch (kind) {
      case 'round':
      case 'state': {
        w = Math.max(w, fs * 3.4);
        const rx = Math.min(kind === 'state' ? 10 : 11, h / 2.5);
        outline = rectO(w / 2, h / 2);
        svg = (cx, cy, a) => rect(cx, cy, a, rx);
        break;
      }
      case 'stadium': {
        w = tw + px + h;
        const r = h / 2;
        outline = [];
        for (let i = 0; i <= 8; i++) {
          const t = -Math.PI / 2 + (i / 8) * Math.PI;
          outline.push(w / 2 - r + Math.cos(t) * r, Math.sin(t) * r);
        }
        for (let i = 0; i <= 8; i++) {
          const t = Math.PI / 2 + (i / 8) * Math.PI;
          outline.push(-w / 2 + r + Math.cos(t) * r, Math.sin(t) * r);
        }
        svg = (cx, cy, a) => rect(cx, cy, a, r);
        break;
      }
      case 'subroutine': {
        w += 16;
        outline = rectO(w / 2, h / 2);
        svg = (cx, cy, a) => rect(cx, cy, a, 0) + '<path class="mm-divider" d="M' + f(cx - w / 2 + 8) + ',' + f(cy - h / 2) + 'v' + f(h) +
          'M' + f(cx + w / 2 - 8) + ',' + f(cy - h / 2) + 'v' + f(h) + '"/>';
        break;
      }
      case 'cylinder': {
        const ry = Math.max(5, Math.min(11, w * 0.08));
        h += 2 * ry;
        tdy = ry * 0.5;
        outline = rectO(w / 2, h / 2);
        svg = (cx, cy, a) => {
          const x0 = cx - w / 2, top = cy - h / 2, rx = w / 2, body = h - 2 * ry;
          const arc = 'a' + f(rx) + ',' + f(ry) + ' 0 0 0 ';
          return '<path' + a + ' d="M' + f(x0) + ',' + f(top + ry) + arc + f(w) + ',0' + arc + f(-w) + ',0v' + f(body) + arc + f(w) + ',0v' + f(-body) + '"/>';
        };
        break;
      }
      case 'circle':
      case 'dblcircle': {
        let d = Math.max(Math.hypot(tw, th) + fs * 1.1, fs * 2.8);
        if (kind === 'dblcircle') d += 10;
        w = h = d;
        outline = circlePoly(d / 2, 32);
        svg = (cx, cy, a) => '<circle' + a + ' cx="' + f(cx) + '" cy="' + f(cy) + '" r="' + f(d / 2) + '"/>' +
          (kind === 'dblcircle' ? '<circle' + a + ' cx="' + f(cx) + '" cy="' + f(cy) + '" r="' + f(d / 2 - 5) + '"/>' : '');
        break;
      }
      case 'rhombus': {
        const A = tw / 2 + px * 0.5, B = th / 2 + py * 0.5, r = 1.8;
        const a2 = A + B * r, b2 = a2 / r;
        w = 2 * a2;
        h = 2 * b2;
        outline = [0, -b2, a2, 0, 0, b2, -a2, 0];
        svg = poly(outline);
        break;
      }
      case 'hexagon': {
        const s = h * 0.3;
        w += s;
        outline = [-w / 2 + s, -h / 2, w / 2 - s, -h / 2, w / 2, 0, w / 2 - s, h / 2, -w / 2 + s, h / 2, -w / 2, 0];
        svg = poly(outline);
        break;
      }
      case 'lean_r':
      case 'lean_l':
      case 'trap_b':
      case 'trap_t': {
        const s = h * 0.35;
        w += s;
        const hw = w / 2, hh = h / 2;
        outline = kind === 'lean_r' ? [-hw + s, -hh, hw, -hh, hw - s, hh, -hw, hh]
          : kind === 'lean_l' ? [-hw, -hh, hw - s, -hh, hw, hh, -hw + s, hh]
            : kind === 'trap_b' ? [-hw + s, -hh, hw - s, -hh, hw, hh, -hw, hh]
              : [-hw, -hh, hw, -hh, hw - s, hh, -hw + s, hh];
        svg = poly(outline);
        break;
      }
      case 'odd': {
        const s = h * 0.3;
        w += s;
        tdx = s / 2;
        outline = [-w / 2, -h / 2, w / 2, -h / 2, w / 2, h / 2, -w / 2, h / 2, -w / 2 + s, 0];
        svg = poly(outline);
        break;
      }
      case 'text': {
        w = tw + 8;
        h = th + 6;
        outline = rectO(w / 2, h / 2);
        svg = () => '';
        break;
      }
      case 'note': {
        outline = rectO(w / 2, h / 2);
        svg = (cx, cy) => '<rect class="mm-note" x="' + f(cx - w / 2) + '" y="' + f(cy - h / 2) + '" width="' + f(w) + '" height="' + f(h) + '" rx="2"/>';
        break;
      }
      case 'start': {
        w = h = 16;
        outline = circlePoly(8, 16);
        svg = (cx, cy) => '<circle class="mm-dot" cx="' + f(cx) + '" cy="' + f(cy) + '" r="8"/>';
        break;
      }
      case 'end': {
        w = h = 20;
        outline = circlePoly(10, 16);
        svg = (cx, cy) => '<circle class="mm-ring" cx="' + f(cx) + '" cy="' + f(cy) + '" r="9"/><circle class="mm-dot" cx="' + f(cx) + '" cy="' + f(cy) + '" r="5"/>';
        break;
      }
      case 'fork': {
        const hz = dir === 'LR' || dir === 'RL';
        w = hz ? 8 : 72;
        h = hz ? 72 : 8;
        outline = rectO(w / 2, h / 2);
        svg = (cx, cy) => '<rect class="mm-dot" x="' + f(cx - w / 2) + '" y="' + f(cy - h / 2) + '" width="' + f(w) + '" height="' + f(h) + '" rx="2"/>';
        break;
      }
      case 'choice': {
        w = h = 26;
        outline = [0, -13, 13, 0, 0, 13, -13, 0];
        svg = poly(outline);
        break;
      }
      default: {
        w = Math.max(w, fs * 3.4);
        outline = rectO(w / 2, h / 2);
        svg = (cx, cy, a) => rect(cx, cy, a, 2);
      }
    }
    return { w, h, outline, svg, tdx, tdy };
  }

  // ------------------------------------------------------------------ flowchart
  const FLOW_SHAPES = [
    ['(((', [')))'], 'dblcircle'],
    ['((', ['))'], 'circle'],
    ['([', ['])'], 'stadium'],
    ['[[', [']]'], 'subroutine'],
    ['[(', [')]'], 'cylinder'],
    ['[/', ['/]', '\\]'], 'lean_r|trap_b'],
    ['[\\', ['\\]', '/]'], 'lean_l|trap_t'],
    ['{{', ['}}'], 'hexagon'],
    ['[', [']'], 'rect'],
    ['(', [')'], 'round'],
    ['{', ['}'], 'rhombus'],
    ['>', [']'], 'odd']
  ];
  const NEW_SHAPES = {
    rect: 'rect', rectangle: 'rect', proc: 'rect', process: 'rect', rounded: 'round', event: 'round',
    stadium: 'stadium', pill: 'stadium', terminal: 'stadium', 'fr-rect': 'subroutine', subproc: 'subroutine',
    subprocess: 'subroutine', subroutine: 'subroutine', 'framed-rectangle': 'subroutine', cyl: 'cylinder',
    cylinder: 'cylinder', database: 'cylinder', db: 'cylinder', circle: 'circle', circ: 'circle',
    'dbl-circ': 'dblcircle', 'double-circle': 'dblcircle', diam: 'rhombus', diamond: 'rhombus',
    decision: 'rhombus', question: 'rhombus', hex: 'hexagon', hexagon: 'hexagon', prepare: 'hexagon',
    'lean-r': 'lean_r', 'lean-right': 'lean_r', 'in-out': 'lean_r', 'lean-l': 'lean_l', 'lean-left': 'lean_l',
    'out-in': 'lean_l', 'trap-b': 'trap_b', 'trapezoid-bottom': 'trap_b', priority: 'trap_b', trapezoid: 'trap_b',
    'trap-t': 'trap_t', 'trapezoid-top': 'trap_t', manual: 'trap_t', 'inv-trapezoid': 'trap_t', text: 'text',
    odd: 'odd', flag: 'odd'
  };
  const ID_CHAR = /[\p{L}\p{N}_!#$%'*+?\\/`^]/u;
  const DIRS = { TB: 'TB', TD: 'TB', BT: 'BT', LR: 'LR', RL: 'RL', '>': 'LR', '<': 'RL', '^': 'BT', V: 'TB' };

  function skipWs(st) {
    while (st.i < st.s.length && (st.s[st.i] === ' ' || st.s[st.i] === '\t')) st.i++;
  }
  function readId(st) {
    const s = st.s, start = st.i;
    while (st.i < s.length) {
      const c = s[st.i];
      if (ID_CHAR.test(c)) {
        st.i++;
        continue;
      }
      const nx = s[st.i + 1];
      if ((c === '-' || c === '.') && st.i > start && nx !== undefined && ID_CHAR.test(nx)) {
        st.i++;
        continue;
      }
      if (c === '=' && st.i > start && nx !== undefined && ID_CHAR.test(nx)) {
        st.i++;
        continue;
      }
      break;
    }
    return s.slice(start, st.i);
  }
  function shapeText(st, open, closers) {
    const s = st.s;
    const t0 = st.i;
    let q = st.i;
    while (s[q] === ' ') q++;
    if (s[q] === '"') {
      const e = s.indexOf('"', q + 1);
      if (e >= 0) {
        let k = e + 1;
        while (s[k] === ' ') k++;
        for (const cl of closers) {
          if (s.startsWith(cl, k)) {
            st.i = k + cl.length;
            return { text: unquote(s.slice(q, e + 1)), closer: cl };
          }
        }
      }
    }
    const nest = open === '[' || open === '(' || open === '{' ? open : null;
    const nclose = nest ? closers[0] : null;
    let depth = 0;
    for (let p = t0; p < s.length; p++) {
      const ch = s[p];
      if (nest && ch === nest) {
        depth++;
        continue;
      }
      if (nest && depth > 0 && ch === nclose) {
        depth--;
        continue;
      }
      if (ch === '"') {
        const e = s.indexOf('"', p + 1);
        if (e > 0) {
          p = e;
          continue;
        }
      }
      if (depth === 0) {
        for (const cl of closers) {
          if (s.startsWith(cl, p)) {
            st.i = p + cl.length;
            return { text: s.slice(t0, p), closer: cl };
          }
        }
      }
    }
    fail('Missing "' + closers[0] + '" to close a node shape');
  }
  function flowNode(st) {
    const s = st.s;
    skipWs(st);
    const id = readId(st);
    if (!id) return null;
    const nd = { id, shape: null, label: null, cls: [] };
    let k = st.i;
    while (s[k] === ' ' || s[k] === '\t') k++;
    const c = s[k];
    if (s.startsWith('@{', st.i)) {
      const e = s.indexOf('}', st.i);
      if (e < 0) fail('Missing "}" after "' + clip(id, 20) + '@{"');
      const body = s.slice(st.i + 2, e);
      st.i = e + 1;
      const re = /([\w-]+)\s*:\s*(?:"([^"]*)"|'([^']*)'|([^,]+))/g;
      let m;
      while ((m = re.exec(body))) {
        const key = m[1].toLowerCase(), val = (m[2] ?? m[3] ?? m[4] ?? '').trim();
        if (key === 'shape') nd.shape = NEW_SHAPES[val.toLowerCase()] || 'rect';
        else if (key === 'label') nd.label = val;
      }
      if (!nd.shape && nd.label == null) nd.edgeProps = true;
    } else if (c === '[' || c === '(' || c === '{' || (c === '>' && k === st.i)) {
      st.i = k;
      for (const [open, closers, shp] of FLOW_SHAPES) {
        if (!s.startsWith(open, st.i)) continue;
        st.i += open.length;
        const r = shapeText(st, open, closers);
        nd.label = r.text;
        nd.shape = shp.indexOf('|') < 0 ? shp : shp.split('|')[r.closer === closers[0] ? 0 : 1];
        break;
      }
    }
    while (s.startsWith(':::', st.i)) {
      st.i += 3;
      const m = /^[\w-]+/.exec(s.slice(st.i));
      if (!m) break;
      nd.cls.push(m[0]);
      st.i += m[0].length;
    }
    return nd;
  }
  function headAt(s, k) {
    const c = s[k];
    if (c === '>') return { head: 'arrow', end: k + 1, weak: false };
    if (c === 'x' || c === 'o') {
      const nx = s[k + 1];
      return { head: c === 'x' ? 'cross' : 'circle', end: k + 1, weak: nx !== undefined && ID_CHAR.test(nx) };
    }
    return null;
  }
  function findCloser(s, from, fam) {
    const re = fam === 'dotted' ? /(\.+)-(?:(>)|([xo])(?![\p{L}\p{N}_]))?/gu
      : fam === 'thick' ? /(={2,})(?:(>)|([xo])(?![\p{L}\p{N}_]))?/gu
        : /(-{2,})(?:(>)|([xo])(?![\p{L}\p{N}_]))?/gu;
    re.lastIndex = from;
    let m;
    while ((m = re.exec(s))) {
      if (m.index === from) continue;
      const head = m[2] ? 'arrow' : m[3] ? (m[3] === 'x' ? 'cross' : 'circle') : null;
      if (fam === 'dotted') return { at: m.index, end: m.index + m[0].length, head, len: m[1].length };
      const n = m[1].length;
      if (head) return { at: m.index, end: m.index + m[0].length, head, len: Math.max(1, n - 1) };
      if (n >= 3) return { at: m.index, end: m.index + m[0].length, head: null, len: Math.max(1, n - 2) };
    }
    return null;
  }
  function flowLink(st) {
    const s = st.s;
    skipWs(st);
    let i = st.i;
    const mId = /^[A-Za-z_]\w*@(?=[-=.<ox~])/.exec(s.slice(i, i + 64));
    if (mId) i += mId[0].length;
    let startHead = null;
    const c0 = s[i];
    if ((c0 === '<' || c0 === 'x' || c0 === 'o') && (s[i + 1] === '-' || s[i + 1] === '=')) {
      startHead = c0 === '<' ? 'arrow' : c0 === 'x' ? 'cross' : 'circle';
      i++;
    }
    let style, endHead = null, len = 1, label = null;
    if (s.startsWith('~~~', i)) {
      let k = i;
      while (s[k] === '~') k++;
      style = 'invisible';
      len = Math.max(1, k - i - 2);
      i = k;
    } else if (s[i] === '-' && s[i + 1] === '.') {
      style = 'dotted';
      let k = i + 1;
      while (s[k] === '.') k++;
      const dots = k - i - 1;
      if (s[k] === '-') {
        k++;
        const h = headAt(s, k);
        if (h) {
          endHead = h.head;
          k = h.end;
        }
        len = dots;
        i = k;
      } else {
        const cl = findCloser(s, k, 'dotted');
        if (!cl) return null;
        label = s.slice(k, cl.at).trim();
        endHead = cl.head;
        len = cl.len;
        i = cl.end;
      }
    } else if ((s[i] === '-' && s[i + 1] === '-') || (s[i] === '=' && s[i + 1] === '=')) {
      const ch = s[i];
      style = ch === '=' ? 'thick' : 'solid';
      let k = i;
      while (s[k] === ch) k++;
      const n = k - i;
      const h = headAt(s, k);
      const fam = ch === '=' ? 'thick' : 'solid';
      if (h && !h.weak) {
        endHead = h.head;
        len = n - 1;
        i = h.end;
      } else if (h && h.weak && (n >= 3 || !findCloser(s, k, fam))) {
        endHead = h.head;
        len = Math.max(1, n - 1);
        i = h.end;
      } else if (n >= 3) {
        len = n - 2;
        i = k;
      } else {
        const cl = findCloser(s, k, fam);
        if (!cl) return null;
        label = s.slice(k, cl.at).trim();
        endHead = cl.head;
        len = cl.len;
        i = cl.end;
      }
    } else return null;
    let k = i;
    while (s[k] === ' ' || s[k] === '\t') k++;
    if (s[k] === '|') {
      let q = k + 1;
      while (s[q] === ' ') q++;
      let end = -1, txt = '';
      if (s[q] === '"') {
        const e2 = s.indexOf('"', q + 1);
        if (e2 > 0) {
          let e3 = e2 + 1;
          while (s[e3] === ' ') e3++;
          if (s[e3] === '|') {
            txt = s.slice(q, e2 + 1);
            end = e3;
          }
        }
      }
      if (end < 0) {
        end = s.indexOf('|', k + 1);
        if (end < 0) fail('Missing closing "|" in an edge label');
        txt = s.slice(k + 1, end);
      }
      label = txt.trim();
      i = end + 1;
    }
    st.i = i;
    if (label) label = unquote(label);
    return { style, startHead, endHead, len: Math.max(1, Math.min(8, len)), label: label || null };
  }
  function splitStatements(line) {
    const out = [];
    let depth = 0, q = false, start = 0;
    for (let i = 0; i < line.length; i++) {
      const c = line[i];
      if (c === '"') q = !q;
      else if (!q) {
        if (c === '[' || c === '(' || c === '{') depth++;
        else if (c === ']' || c === ')' || c === '}') depth = Math.max(0, depth - 1);
        else if (c === ';' && depth === 0) {
          out.push(line.slice(start, i));
          start = i + 1;
        }
      }
    }
    out.push(line.slice(start));
    return out.map((x) => x.trim()).filter(Boolean);
  }

  function parseFlow(p) {
    const head = /^(?:graph|flowchart(?:-elk|-v2)?)\b[ \t]*(?:(TB|TD|BT|LR|RL|[<>^]|v)(?![\w-]))?\s*;?\s*(.*)$/i.exec(p.lines[0].t);
    if (!head) fail('Invalid flowchart header');
    const g = {
      dir: head[1] ? DIRS[head[1].toUpperCase()] || 'TB' : 'TB',
      nodes: new Map(), edges: [], clusters: [], memberOf: new Map(), title: '',
      classDefs: new Map(), nodeCls: new Map(), nodeStyle: new Map(), linkStyle: new Map(), linkDefault: null
    };
    const stack = [], closed = [], clusterIds = new Set();
    let auto = 0;
    const touch = (nd) => {
      let n = g.nodes.get(nd.id);
      if (!n) {
        if (g.nodes.size >= LIMIT.nodes) fail('Too many nodes');
        n = { id: nd.id, label: null, shape: null };
        g.nodes.set(nd.id, n);
      }
      if (nd.label != null) n.label = nd.label;
      if (nd.shape) n.shape = nd.shape;
      if (nd.cls.length) g.nodeCls.set(nd.id, (g.nodeCls.get(nd.id) || []).concat(nd.cls));
      if (stack.length) stack[stack.length - 1].mentioned.push(nd.id);
    };
    const line = (s) => {
      let m;
      if ((m = /^subgraph\b\s*(.*)$/i.exec(s))) {
        const rest = m[1].trim();
        let id, title, mm;
        if ((mm = /^([^\s[\]"]+)\s*\[\s*(?:"([^"]*)"|([^\]]*))\s*\]$/.exec(rest))) {
          id = mm[1];
          title = mm[2] ?? mm[3];
        } else if ((mm = /^"([^"]*)"$/.exec(rest))) {
          id = '\u0000sg' + auto++;
          title = mm[1];
        } else if (rest) {
          id = rest;
          title = rest;
        } else {
          id = '\u0000sg' + auto++;
          title = '';
        }
        if (clusterIds.has(id)) id += '\u0000' + auto++;
        clusterIds.add(id);
        if (clusterIds.size > 300) fail('Too many subgraphs');
        stack.push({ id, title: unquote(title.trim()), parent: stack.length ? stack[stack.length - 1].id : null, mentioned: [] });
        return;
      }
      if (/^end$/i.test(s)) {
        if (!stack.length) fail('Unexpected "end" without a matching "subgraph"');
        closed.push(stack.pop());
        return;
      }
      if ((m = /^direction\s+(TB|TD|BT|LR|RL)$/i.exec(s))) {
        if (!stack.length) g.dir = DIRS[m[1].toUpperCase()];
        return;
      }
      if ((m = /^classDef\s+(\S+)\s+(.*)$/i.exec(s))) {
        for (const name of m[1].split(',')) g.classDefs.set(name, parseStyle(m[2]));
        return;
      }
      if ((m = /^class\s+(.+?)\s+([\w-]+)\s*$/i.exec(s))) {
        for (const id of m[1].split(/\s*,\s*/)) g.nodeCls.set(id, (g.nodeCls.get(id) || []).concat([m[2]]));
        return;
      }
      if ((m = /^style\s+(\S+)\s+(.*)$/i.exec(s))) {
        g.nodeStyle.set(m[1], Object.assign(g.nodeStyle.get(m[1]) || {}, parseStyle(m[2])));
        return;
      }
      if ((m = /^linkStyle\s+(default|[\d,\s]+?)\s+(.*)$/i.exec(s))) {
        const st = parseStyle(m[2]);
        if (/^default$/i.test(m[1])) g.linkDefault = st;
        else for (const k of m[1].split(/[\s,]+/)) if (k) g.linkStyle.set(parseInt(k, 10), st);
        return;
      }
      if (/^(?:click|callback|href)\b/i.test(s)) return;
      if ((m = /^title\s+(.+)$/i.exec(s))) {
        g.title = m[1];
        return;
      }
      const st = { s, i: 0 };
      const groups = [], links = [];
      for (;;) {
        const grp = [];
        for (;;) {
          const nd = flowNode(st);
          if (!nd) {
            fail(groups.length || grp.length ? 'Expected a node near "' + clip(s.slice(st.i).trim() || s, 24) + '"' : 'Cannot parse "' + clip(s, 40) + '"');
          }
          grp.push(nd);
          skipWs(st);
          if (s[st.i] === '&') {
            st.i++;
            continue;
          }
          break;
        }
        groups.push(grp);
        skipWs(st);
        if (st.i >= s.length) break;
        const lk = flowLink(st);
        if (!lk) fail('Unexpected "' + clip(s.slice(st.i), 24) + '"');
        links.push(lk);
      }
      if (groups.length === 1 && groups[0].length === 1 && groups[0][0].edgeProps) return;
      for (const grp of groups) for (const nd of grp) touch(nd);
      links.forEach((L, li) => {
        for (const a of groups[li]) {
          for (const b of groups[li + 1]) {
            if (g.edges.length >= LIMIT.edges) fail('Too many edges');
            g.edges.push({ from: a.id, to: b.id, style: L.style, startHead: L.startHead, endHead: L.endHead, len: L.len, label: L.label });
          }
        }
      });
    };
    const stmts = [];
    if (head[2]) for (const s of splitStatements(head[2])) stmts.push([s, p.lines[0].n]);
    for (let i = 1; i < p.lines.length; i++) for (const s of splitStatements(p.lines[i].t)) stmts.push([s, p.lines[i].n]);
    for (const [s, n] of stmts) {
      try {
        line(s);
      } catch (e) {
        if (e.mmUser) e.message = 'Line ' + n + ': ' + e.message;
        throw e;
      }
    }
    while (stack.length) closed.push(stack.pop());
    for (const sg of closed) {
      for (const id of sg.mentioned) if (!clusterIds.has(id) && !g.memberOf.has(id)) g.memberOf.set(id, sg.id);
    }
    for (const id of clusterIds) g.nodes.delete(id);
    g.clusters = closed.slice().reverse();
    return g;
  }

  function mergeStyles(list) {
    const out = {};
    for (const s of list) if (s) Object.assign(out, s);
    return out;
  }

  function renderFlowchart(p, ctx) {
    const g = parseFlow(p);
    if (!g.nodes.size && !g.clusters.length) fail('The flowchart has no nodes');
    const fs = ctx.fs;
    const nodes = [];
    const dflt = g.classDefs.get('default');
    for (const n of g.nodes.values()) {
      const shape = n.shape || 'rect';
      const lines = wrap(labelLines(n.label != null ? n.label : n.id), (shape === 'rhombus' ? 11 : 15) * fs, fs);
      const st = mergeStyles([dflt].concat((g.nodeCls.get(n.id) || []).map((c) => g.classDefs.get(c)), [g.nodeStyle.get(n.id)]));
      const geom = shapeFor(shape, maxW(lines, fs, st.bold), lines.length * ctx.lh, ctx);
      nodes.push({
        id: n.id, w: geom.w, h: geom.h, cluster: g.memberOf.get(n.id) ?? null, outline: geom.outline,
        draw: (cx, cy) => '<g class="mm-node-g">' + geom.svg(cx, cy, ' class="mm-node mm-shape-' + shape + '"' + shapeStyle(st)) +
          textBlock(lines, cx + geom.tdx, cy + geom.tdy, ctx, 'mm-text' + (st.bold ? ' mm-bold' : ''), 'middle', 0, textStyle(st)) + '</g>'
      });
    }
    const edges = g.edges.map((e, i) => {
      const ls = g.linkStyle.get(i) || g.linkDefault;
      let style = '';
      if (ls) {
        let s = '';
        if (ls.stroke) s += 'stroke:' + ls.stroke + ';';
        if (ls.sw != null) s += 'stroke-width:' + ls.sw + 'px;';
        if (ls.dash) s += 'stroke-dasharray:' + ls.dash + ';';
        if (s) style = ' style="' + esc(s) + '"';
      }
      return {
        from: e.from, to: e.to, minlen: e.len,
        label: e.label ? wrap(labelLines(e.label), 15 * fs, fs * 0.92) : null,
        cls: 'mm-edge' + (e.style === 'dotted' ? ' mm-dotted' : e.style === 'thick' ? ' mm-thick' : ''),
        hidden: e.style === 'invisible', start: e.startHead, end: e.endHead, style
      };
    });
    const clusters = g.clusters.map((c) => {
      const lines = c.title ? wrap(labelLines(c.title), 18 * fs, fs) : [];
      const has = lines.some((l) => l);
      return { id: c.id, parent: c.parent, title: has ? lines : [], tw: has ? maxW(lines, fs) : 0, th: has ? lines.length * ctx.lh + 4 : 0, style: g.nodeStyle.get(c.id) };
    });
    const res = drawGraph({ dir: g.dir, nodes, edges, clusters }, ctx);
    return withTitle(ctx, g.title || p.title, res);
  }


  // ------------------------------------------------------------------ sequence diagram
  const SEQ_MSG = /^(.+?)\s*(<<-->>|<<->>|-->>|->>|--[xX]|-[xX]|--\)|-\)|-->|->)\s*([+-]?)\s*([^:]+?)\s*(?::(.*))?$/;
  const BOX_COLORS = /^(?:rgba?\([^)]*\)|hsla?\([^)]*\)|#[0-9a-fA-F]{3,8}|transparent|aqua|black|blue|brown|cyan|gold|gray|green|grey|lavender|lightblue|lightgreen|lightgray|lightgrey|lightyellow|lime|magenta|maroon|navy|olive|orange|pink|purple|red|silver|teal|violet|wheat|white|yellow)(?:\s+|$)/i;

  function parseSequence(p) {
    const parts = [], pmap = new Map(), ev = [], boxes = [];
    let title = p.title, box = null;
    const blocks = [];
    const part = (raw, label, kind, explicit) => {
      let id = String(raw).trim();
      if (id.length >= 2 && id[0] === '"' && id[id.length - 1] === '"') id = id.slice(1, -1).trim();
      if (!id) fail('Missing participant name');
      let q = pmap.get(id);
      if (!q) {
        if (parts.length >= 100) fail('Too many participants');
        q = { id, label: label || id, kind: kind || 'participant', idx: parts.length };
        parts.push(q);
        pmap.set(id, q);
        if (box) box.members.push(q);
      } else if (explicit) {
        if (label) q.label = label;
        if (kind) q.kind = kind;
      }
      return q;
    };
    for (let i = 1; i < p.lines.length; i++) {
      const t = p.lines[i].t;
      let m;
      try {
        if ((m = /^(?:create\s+)?(participant|actor)\s+(.+?)(?:\s+as\s+(.+?))?\s*$/i.exec(t))) {
          part(m[2].replace(/@\{.*\}\s*$/, ''), m[3] ? m[3].trim() : null, m[1].toLowerCase(), true);
        } else if (/^destroy\s+/i.test(t)) {
          // ignored
        } else if ((m = /^box\b\s*(.*)$/i.exec(t))) {
          if (box) fail('Boxes cannot be nested');
          const rest = m[1].trim();
          const c = BOX_COLORS.exec(rest);
          box = { label: c ? rest.slice(c[0].length).trim() : rest, members: [] };
          boxes.push(box);
        } else if (/^end$/i.test(t)) {
          if (blocks.length) {
            blocks.pop();
            ev.push({ t: 'end' });
          } else if (box) box = null;
          else fail('Unexpected "end"');
        } else if ((m = /^(loop|alt|opt|par_over|par|critical|break|rect)\b\s*(.*)$/i.exec(t))) {
          const kind = m[1].toLowerCase() === 'par_over' ? 'par' : m[1].toLowerCase();
          blocks.push(kind);
          if (blocks.length > 24) fail('Blocks are nested too deeply');
          ev.push({ t: 'block', kind, text: kind === 'rect' ? '' : m[2].trim() });
        } else if ((m = /^(else|and|option)\b\s*(.*)$/i.exec(t))) {
          if (!blocks.length) fail('"' + m[1] + '" outside of a block');
          ev.push({ t: 'else', text: m[2].trim() });
        } else if ((m = /^note\s+(left\s+of|right\s+of|over)\s+([^:]+?)\s*:\s*(.*)$/i.exec(t))) {
          const ps = m[2].split(',').map((x) => part(x));
          ev.push({ t: 'note', pos: m[1].toLowerCase().split(/\s+/)[0], ps, text: m[3] });
        } else if ((m = /^autonumber\b\s*(.*)$/i.exec(t))) {
          if (/^off$/i.test(m[1].trim())) ev.push({ t: 'num', on: false });
          else {
            const nums = m[1].trim() ? m[1].trim().split(/\s+/).map(Number) : [];
            ev.push({ t: 'num', on: true, start: nums[0] >= 0 ? nums[0] : 1, step: nums[1] > 0 ? nums[1] : 1 });
          }
        } else if ((m = /^(activate|deactivate)\s+(.+)$/i.exec(t))) {
          ev.push({ t: m[1].toLowerCase(), p: part(m[2]) });
        } else if ((m = /^title\s*:?\s*(.*)$/i.exec(t))) {
          title = m[1];
        } else if (/^(?:links?|properties|details)\b/i.test(t)) {
          // menus are not rendered
        } else if ((m = SEQ_MSG.exec(t))) {
          const from = part(m[1]), to = part(m[4]);
          ev.push({ t: 'msg', from, to, arrow: m[2].toLowerCase(), act: m[3] === '+', deact: m[3] === '-', text: m[5] == null ? '' : m[5].trim() });
        } else fail('Cannot parse "' + clip(t, 40) + '"');
      } catch (e) {
        if (e.mmUser) e.message = 'Line ' + p.lines[i].n + ': ' + e.message;
        throw e;
      }
      if (ev.length > LIMIT.items) fail('Too many sequence diagram items');
    }
    if (!parts.length) fail('The sequence diagram has no participants');
    while (blocks.length) {
      blocks.pop();
      ev.push({ t: 'end' });
    }
    return { parts, ev, boxes, title };
  }

  function seqArrow(a) {
    const dotted = a.indexOf('--') >= 0;
    const both = a.startsWith('<<');
    const tail = a.replace(/^<<|-/g, '');
    const end = tail === '>>' || both ? 'arrow' : tail === 'x' ? 'cross' : tail === ')' ? 'open' : null;
    return { dotted, end, start: both ? 'arrow' : null };
  }

  function renderSequence(p, ctx) {
    const S = parseSequence(p);
    const fs = ctx.fs, lh = ctx.lh;
    const P = S.parts, n = P.length;
    const M = 10;
    for (const q of P) {
      q.lines = wrap(labelLines(q.label), 12 * fs, fs);
      q.w = Math.max(maxW(q.lines, fs) + 2 * fs, 5.5 * fs);
      q.bh = q.kind === 'actor' ? 46 + q.lines.length * lh : q.lines.length * lh + fs * 1.3;
    }
    const boxH = Math.max(...P.map((q) => q.bh));

    // horizontal placement: minimum centre distances from labels, notes and self messages
    const gaps = new Float64Array(n);
    for (let i = 1; i < n; i++) gaps[i] = (P[i - 1].w + P[i].w) / 2 + fs * 2;
    const cons = [];
    let extraL = 0, extraR = 0, numOn = false;
    for (const e of S.ev) {
      if (e.t === 'num') numOn = e.on;
      else if (e.t === 'msg') {
        e.lines = e.text ? wrap(labelLines(e.text), 28 * fs, fs) : [];
        const tw = e.lines.length ? maxW(e.lines, fs) : 0;
        const a = e.from.idx, b = e.to.idx;
        if (a === b) {
          const need = Math.max(tw + fs * 1.6, fs * 3.2) + fs;
          if (a + 1 < n) cons.push([a, a + 1, need + P[a + 1].w / 2 * 0.2]);
          else extraR = Math.max(extraR, need);
        } else cons.push([Math.min(a, b), Math.max(a, b), tw + fs * 2.4 + (numOn ? fs * 1.6 : 0)]);
      } else if (e.t === 'note') {
        e.lines = wrap(labelLines(e.text), 16 * fs, fs);
        e.w = Math.max(maxW(e.lines, fs) + fs * 1.4, 4 * fs);
        const idx = e.ps.map((q) => q.idx);
        const a = Math.min(...idx), b = Math.max(...idx);
        if (e.pos === 'right') {
          if (a + 1 < n) cons.push([a, a + 1, e.w + fs * 1.8]);
          else extraR = Math.max(extraR, e.w + fs * 1.2);
        } else if (e.pos === 'left') {
          if (a > 0) cons.push([a - 1, a, e.w + fs * 1.8]);
          else extraL = Math.max(extraL, e.w + fs * 1.2);
        } else if (a === b) {
          if (a > 0) cons.push([a - 1, a, e.w / 2 + fs]);
          else extraL = Math.max(extraL, e.w / 2);
          if (a + 1 < n) cons.push([a, a + 1, e.w / 2 + fs]);
          else extraR = Math.max(extraR, e.w / 2);
        } else cons.push([a, b, e.w - fs * 2.4]);
      }
    }
    cons.sort((x, y) => x[1] - x[0] - (y[1] - y[0]));
    for (const [a, b, d] of cons) {
      let cur = 0;
      for (let k = a + 1; k <= b; k++) cur += gaps[k];
      if (cur < d) {
        const add = (d - cur) / (b - a);
        for (let k = a + 1; k <= b; k++) gaps[k] += add;
      }
    }
    const X = new Float64Array(n);
    X[0] = M + Math.max(P[0].w / 2, extraL);
    for (let i = 1; i < n; i++) X[i] = X[i - 1] + gaps[i];

    let minX = M, maxX = X[n - 1] + Math.max(P[n - 1].w / 2, extraR);
    const titleLines = S.title ? wrap(labelLines(S.title), Math.max(maxX, 300), fs * 1.15, true) : [];
    const titleH = titleLines.length ? titleLines.length * fs * 1.5 + 8 : 0;
    const boxLabelH = S.boxes.some((b) => b.label) ? lh + 6 : S.boxes.length ? 6 : 0;
    const y0 = M + titleH + boxLabelH;
    let y = y0 + boxH + fs * 1.4;
    let lastY = y;

    const frames = [], done = [];
    const ext = (x1, x2) => {
      minX = Math.min(minX, x1);
      maxX = Math.max(maxX, x2);
      if (frames.length) {
        const fr = frames[frames.length - 1];
        fr.x1 = Math.min(fr.x1, x1);
        fr.x2 = Math.max(fr.x2, x2);
      }
    };
    const act = P.map(() => []);
    const bars = [];
    const edgeX = (i, dir) => {
      const d = act[i].length;
      return d ? X[i] + (d - 1) * 5 + (dir > 0 ? 5 : -5) : X[i];
    };
    const closeBar = (i, yEnd) => {
      const y1 = act[i].pop();
      bars.push({ x: X[i] - 5 + act[i].length * 5, y1, y2: Math.max(yEnd, y1 + fs * 0.8) });
    };
    let msgs = '', notes = '', num = 1, step = 1;
    numOn = false;
    for (const e of S.ev) {
      switch (e.t) {
        case 'num':
          numOn = e.on;
          if (e.on) {
            num = e.start;
            step = e.step;
          }
          break;
        case 'msg': {
          const a = e.from.idx, b = e.to.idx;
          const textH = e.lines.length * lh;
          const k = seqArrow(e.arrow);
          const cls = 'mm-edge mm-msg' + (k.dotted ? ' mm-dotted' : '');
          if (a !== b) {
            y += textH + (textH ? 4 : 0);
            const my = y;
            if (e.act) act[b].push(my);
            const dir = b > a ? 1 : -1;
            const x1 = edgeX(a, dir), x2 = edgeX(b, -dir);
            if (e.lines.length) msgs += textBlock(e.lines, (x1 + x2) / 2, my - 5 - textH / 2, ctx, 'mm-text', 'middle');
            msgs += '<path class="' + cls + '" d="M' + f(x1 + dir * trimOf(k.start)) + ',' + f(my) + 'H' + f(x2 - dir * trimOf(k.end)) + '"' +
              markerRef(ctx, k.start, true) + markerRef(ctx, k.end, false) + '/>';
            if (numOn) {
              msgs += '<circle class="mm-head" cx="' + f(x1) + '" cy="' + f(my) + '" r="' + f(fs * 0.62) + '"/>' +
                textBlock([String(num)], x1, my, ctx, 'mm-num', 'middle', fs * 0.68);
              num += step;
            }
            ext(Math.min(x1, x2), Math.max(x1, x2));
            if (e.deact && act[a].length) closeBar(a, my);
            lastY = my;
            y += fs * 1.3;
          } else {
            const x1 = edgeX(a, 1);
            y += textH + (textH ? 2 : 0);
            const y1 = y, y2 = y + fs * 1.5;
            if (e.act) act[a].push(y2);
            const x2 = edgeX(a, 1);
            const lw = fs * 2.6;
            if (e.lines.length) msgs += textBlock(e.lines, x1 + 8, y1 - 4 - textH / 2, ctx, 'mm-text', 'start');
            const tr = trimOf(k.end);
            msgs += '<path class="' + cls + '" d="M' + f(x1) + ',' + f(y1) + 'C' + f(x1 + lw) + ',' + f(y1) + ' ' + f(x1 + lw) + ',' + f(y2) + ' ' + f(x2 + tr) + ',' + f(y2) + '"' +
              markerRef(ctx, k.end, false) + '/>';
            if (numOn) {
              msgs += '<circle class="mm-head" cx="' + f(x1) + '" cy="' + f(y1) + '" r="' + f(fs * 0.62) + '"/>' +
                textBlock([String(num)], x1, y1, ctx, 'mm-num', 'middle', fs * 0.68);
              num += step;
            }
            ext(x1, x1 + Math.max(lw, (e.lines.length ? maxW(e.lines, fs) : 0) + 12));
            if (e.deact && act[a].length) closeBar(a, y2);
            lastY = y2;
            y = y2 + fs * 1.3;
          }
          break;
        }
        case 'note': {
          const idx = e.ps.map((q) => q.idx);
          const a = Math.min(...idx), b = Math.max(...idx);
          const nh = e.lines.length * lh + fs * 0.9;
          y += fs * 0.3;
          let x, w = e.w;
          if (e.pos === 'right') x = edgeX(a, 1) + fs * 0.6;
          else if (e.pos === 'left') x = edgeX(a, -1) - fs * 0.6 - w;
          else if (a === b) x = X[a] - w / 2;
          else {
            const l = X[a] - fs * 1.2, r = X[b] + fs * 1.2;
            w = Math.max(w, r - l);
            x = (l + r) / 2 - w / 2;
          }
          notes += '<rect class="mm-note" x="' + f(x) + '" y="' + f(y) + '" width="' + f(w) + '" height="' + f(nh) + '" rx="2"/>' +
            textBlock(e.lines, x + w / 2, y + nh / 2, ctx, 'mm-text', 'middle');
          ext(x, x + w);
          y += nh + fs * 0.9;
          break;
        }
        case 'block': {
          y += fs * 0.5;
          const fr = { kind: e.kind, top: y, x1: Infinity, x2: -Infinity, seps: [], lines: [] };
          frames.push(fr);
          if (e.kind === 'rect') y += fs * 0.6;
          else {
            fr.lines = e.text ? wrap(labelLines('[' + e.text + ']'), 24 * fs, fs) : [];
            y += Math.max(fs * 1.5, fr.lines.length * lh + 4) + fs * 0.9;
          }
          break;
        }
        case 'else': {
          const fr = frames[frames.length - 1];
          y += fs * 0.3;
          const lines = e.text ? wrap(labelLines('[' + e.text + ']'), 24 * fs, fs) : [];
          fr.seps.push({ y, lines });
          y += lines.length * lh + fs * 1.1;
          break;
        }
        case 'end': {
          const fr = frames.pop();
          y += fs * 0.3;
          fr.bottom = y;
          if (fr.x1 === Infinity) {
            fr.x1 = X[0];
            fr.x2 = X[n - 1];
          }
          fr.x1 -= fs * 1.1;
          fr.x2 += fs * 1.1;
          fr.tabW = fr.kind === 'rect' ? 0 : textW(fr.kind, fs * 0.9, true) + fs * 1.3;
          let need = fr.tabW + (fr.lines.length ? maxW(fr.lines, fs) + fs * 1.2 : 0);
          for (const sp of fr.seps) need = Math.max(need, (sp.lines.length ? maxW(sp.lines, fs) : 0) + fs * 2);
          if (fr.x2 - fr.x1 < need) fr.x2 = fr.x1 + need;
          done.push(fr);
          ext(fr.x1, fr.x2);
          y += fs * 0.7;
          break;
        }
        case 'activate':
          act[e.p.idx].push(Math.min(lastY, y));
          break;
        case 'deactivate':
          if (act[e.p.idx].length) closeBar(e.p.idx, lastY);
          break;
      }
    }
    for (let i = 0; i < n; i++) while (act[i].length) closeBar(i, y);
    y += fs * 0.4;
    const yb = y;
    let bg = '', life = '', fr = '', top = '';
    const drawPart = (q, ty) => {
      const cx = X[q.idx];
      if (q.kind === 'actor') {
        const hy = ty + 9;
        return '<g class="mm-actor-fig"><circle cx="' + f(cx) + '" cy="' + f(hy) + '" r="7"/><path fill="none" d="M' + f(cx) + ',' + f(hy + 7) + 'v14M' + f(cx - 11) + ',' + f(hy + 12) + 'h22M' +
          f(cx) + ',' + f(hy + 21) + 'l-9,11M' + f(cx) + ',' + f(hy + 21) + 'l9,11"/></g>' +
          textBlock(q.lines, cx, ty + 45 + (q.lines.length * lh) / 2, ctx, 'mm-text', 'middle');
      }
      return '<rect class="mm-node mm-participant" x="' + f(cx - q.w / 2) + '" y="' + f(ty) + '" width="' + f(q.w) + '" height="' + f(boxH) + '" rx="3"/>' +
        textBlock(q.lines, cx, ty + boxH / 2, ctx, 'mm-text', 'middle');
    };
    for (const q of P) {
      life += '<path class="mm-lifeline" d="M' + f(X[q.idx]) + ',' + f(y0 + boxH) + 'V' + f(yb) + '"/>';
      top += drawPart(q, y0) + drawPart(q, yb);
      minX = Math.min(minX, X[q.idx] - q.w / 2);
      maxX = Math.max(maxX, X[q.idx] + q.w / 2);
    }
    for (const b of S.boxes) {
      if (!b.members.length) continue;
      const x1 = Math.min(...b.members.map((q) => X[q.idx] - q.w / 2)) - fs * 0.6;
      const x2 = Math.max(...b.members.map((q) => X[q.idx] + q.w / 2)) + fs * 0.6;
      const lines = b.label ? wrap(labelLines(b.label), Math.max(x2 - x1, 80), fs, true) : [];
      bg += '<rect class="mm-cluster" x="' + f(x1) + '" y="' + f(y0 - boxLabelH) + '" width="' + f(x2 - x1) + '" height="' + f(yb + boxH - y0 + boxLabelH + 6) + '" rx="4"/>';
      if (lines.length) bg += textBlock(lines.slice(0, 1), (x1 + x2) / 2, y0 - boxLabelH / 2 - 1, ctx, 'mm-text mm-bold', 'middle');
      minX = Math.min(minX, x1);
      maxX = Math.max(maxX, x2);
    }
    for (const b of done) {
      const h = b.bottom - b.top;
      if (b.kind === 'rect') {
        bg += '<rect class="mm-block-bg" x="' + f(b.x1) + '" y="' + f(b.top) + '" width="' + f(b.x2 - b.x1) + '" height="' + f(h) + '" rx="3"/>';
        continue;
      }
      const th = fs * 1.5;
      fr += '<rect class="mm-frame" x="' + f(b.x1) + '" y="' + f(b.top) + '" width="' + f(b.x2 - b.x1) + '" height="' + f(h) + '" rx="2"/>' +
        '<polygon class="mm-frame-tab" points="' + pts([b.x1, b.top, b.x1 + b.tabW, b.top, b.x1 + b.tabW, b.top + th - 6, b.x1 + b.tabW - 6, b.top + th, b.x1, b.top + th]) + '"/>' +
        textBlock([b.kind], b.x1 + fs * 0.55, b.top + th / 2, ctx, 'mm-text mm-bold', 'start', fs * 0.9);
      if (b.lines.length) fr += textBlock(b.lines, b.x1 + b.tabW + fs * 0.6, b.top + (b.lines.length * lh) / 2 + 2, ctx, 'mm-text mm-muted', 'start');
      for (const sp of b.seps) {
        fr += '<path class="mm-frame-sep" d="M' + f(b.x1) + ',' + f(sp.y) + 'H' + f(b.x2) + '"/>';
        if (sp.lines.length) fr += textBlock(sp.lines, (b.x1 + b.x2) / 2, sp.y + 3 + (sp.lines.length * lh) / 2, ctx, 'mm-text mm-muted', 'middle');
      }
    }
    let barsSvg = '';
    for (const b of bars) barsSvg += '<rect class="mm-node mm-activation" x="' + f(b.x) + '" y="' + f(b.y1) + '" width="10" height="' + f(b.y2 - b.y1) + '"/>';
    const W = maxX - minX + 2 * M, H = yb + boxH + M + 6;
    const dx = M - minX;
    let body = '';
    if (titleLines.length) body += textBlock(titleLines, W / 2, M + titleH / 2 - 2, ctx, 'mm-text mm-bold', 'middle', fs * 1.15);
    body += '<g transform="translate(' + f(dx) + ',0)">' + bg + life + fr + barsSvg + msgs + notes + top + '</g>';
    return { body, w: W, h: H };
  }

  // ------------------------------------------------------------------ pie chart
  function renderPie(p, ctx) {
    const head = p.lines[0].t;
    let showData = /\bshowData\b/i.test(head);
    let title = p.title;
    const mt = /\btitle\s+(.*)$/i.exec(head);
    if (mt) title = mt[1].trim();
    const items = [];
    for (let i = 1; i < p.lines.length; i++) {
      const t = p.lines[i].t;
      let m;
      if ((m = /^title\s+(.*)$/i.exec(t))) {
        title = m[1].trim();
        continue;
      }
      if (/^showData$/i.test(t)) {
        showData = true;
        continue;
      }
      m = /^(?:"([^"]*)"|'([^']*)'|([^:]+?))\s*:\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\s*;?$/.exec(t);
      if (!m) fail('Line ' + p.lines[i].n + ': cannot parse "' + clip(t, 40) + '"');
      const v = parseFloat(m[4]);
      if (!Number.isFinite(v) || v < 0) fail('Line ' + p.lines[i].n + ': pie values must be zero or positive');
      items.push({ label: m[1] ?? m[2] ?? m[3].trim(), v });
      if (items.length > 200) fail('Too many pie slices');
    }
    if (!items.length) fail('The pie chart has no data');
    const total = items.reduce((s, x) => s + x.v, 0);
    if (!(total > 0) || !Number.isFinite(total)) fail('Pie values must add up to more than zero');
    const fs = ctx.fs, lh = ctx.lh;
    const M = 10, R = fs * 9;
    const fmtPct = (x) => {
      const r = Math.round(x * 10) / 10;
      return (Number.isInteger(r) ? String(r) : r.toFixed(1)) + '%';
    };
    const fmtNum = (v) => (Math.abs(v) >= 1e15 ? v.toExponential(3) : String(Math.round(v * 1e4) / 1e4));
    let a = 0;
    const sl = items.map((it, i) => {
      const frac = it.v / total;
      const s = { label: it.label, v: it.v, i, a0: a, a1: a + frac * Math.PI * 2, pct: frac * 100 };
      a = s.a1;
      return s;
    });
    // outside labels with leader lines, de-collided per side
    const labs = sl.filter((s) => s.pct >= 0.5).map((s) => {
      const mid = (s.a0 + s.a1) / 2;
      return { s, mid, side: Math.sin(mid) >= 0 ? 1 : -1, text: fmtPct(s.pct), y: -Math.cos(mid) * (R + fs * 0.9) };
    });
    const gapY = fs * 1.3, lim = R + fs * 1.6;
    for (const side of [1, -1]) {
      const L = labs.filter((l) => l.side === side).sort((x, y) => x.y - y.y);
      for (let i = 1; i < L.length; i++) L[i].y = Math.max(L[i].y, L[i - 1].y + gapY);
      if (L.length && L[L.length - 1].y > lim) {
        L[L.length - 1].y = lim;
        for (let i = L.length - 2; i >= 0; i--) L[i].y = Math.min(L[i].y, L[i + 1].y - gapY);
      }
    }
    const lw = labs.filter((l) => l.side < 0).reduce((m, l) => Math.max(m, textW(l.text, fs)), 0);
    const rw = labs.filter((l) => l.side > 0).reduce((m, l) => Math.max(m, textW(l.text, fs)), 0);
    let top = M;
    const titleLines = title ? wrap(labelLines(title), 36 * fs, fs * 1.15, true) : [];
    const titleH = titleLines.length ? titleLines.length * fs * 1.5 + fs * 0.8 : 0;
    top += titleH;
    const labTop = Math.min(-R, ...labs.map((l) => l.y - gapY / 2));
    const labBot = Math.max(R, ...labs.map((l) => l.y + gapY / 2));
    const cx = M + lw + R + fs * 2.2;
    const cy = top - labTop + fs * 0.4;
    // legend
    const legX = cx + R + fs * 2.2 + rw + fs * 2;
    const rows = sl.map((s) => s.label + (showData ? ' [' + fmtNum(s.v) + ']' : ''));
    const rowH = lh * 1.2;
    const legH = rows.length * rowH;
    const legW = rows.reduce((m, r) => Math.max(m, textW(r, fs)), 0) + fs * 1.6;
    const legY = cy - legH / 2;
    let body = '';
    const P = (ang, r) => [cx + Math.sin(ang) * r, cy - Math.cos(ang) * r];
    for (const s of sl) {
      const c = 'mm-slice mm-c' + ((s.i % 8) + 1);
      const tip = '<title>' + esc(s.label + ': ' + fmtNum(s.v) + ' (' + fmtPct(s.pct) + ')') + '</title>';
      if (s.pct >= 99.9999) body += '<circle class="' + c + '" cx="' + f(cx) + '" cy="' + f(cy) + '" r="' + f(R) + '">' + tip + '</circle>';
      else if (s.a1 - s.a0 > 1e-6) {
        const p0 = P(s.a0, R), p1 = P(s.a1, R);
        body += '<path class="' + c + '" d="M' + f(cx) + ',' + f(cy) + 'L' + f(p0[0]) + ',' + f(p0[1]) + 'A' + f(R) + ',' + f(R) + ' 0 ' +
          (s.a1 - s.a0 > Math.PI ? 1 : 0) + ' 1 ' + f(p1[0]) + ',' + f(p1[1]) + 'Z">' + tip + '</path>';
      }
    }
    for (const l of labs) {
      const q0 = P(l.mid, R + 2), q1 = P(l.mid, R + fs * 0.7);
      const ex = cx + l.side * (R + fs * 1.6);
      body += '<polyline class="mm-leader" points="' + pts([q0[0], q0[1], q1[0], q1[1], ex, cy + l.y]) + '"/>' +
        textBlock([l.text], ex + l.side * 4, cy + l.y, ctx, 'mm-text', l.side > 0 ? 'start' : 'end');
    }
    sl.forEach((s, i) => {
      const yy = legY + i * rowH;
      body += '<rect class="mm-c' + ((s.i % 8) + 1) + '" x="' + f(legX) + '" y="' + f(yy + rowH / 2 - 6) + '" width="12" height="12" rx="2"/>' +
        textBlock([rows[i]], legX + fs * 1.4, yy + rowH / 2, ctx, 'mm-text', 'start');
    });
    let W = legX + legW + M;
    const H = Math.max(cy + labBot + fs * 0.6, legY + legH, cy + R) + M;
    const tw = titleLines.length ? maxW(titleLines, fs * 1.15, true) + 2 * M : 0;
    let shift = 0;
    if (tw > W) {
      shift = (tw - W) / 2;
      W = tw;
    }
    let out = '';
    if (titleLines.length) out += textBlock(titleLines, W / 2, M + titleH / 2 - fs * 0.3, ctx, 'mm-text mm-bold', 'middle', fs * 1.15);
    out += shift ? '<g transform="translate(' + f(shift) + ',0)">' + body + '</g>' : body;
    return { body: out, w: W, h: H };
  }

  // ------------------------------------------------------------------ helpers for graph-like diagrams
  // Claims members for scopes in closing order (inner scopes first); first claim wins.
  function claimMembers(closed, isCluster) {
    const memberOf = new Map();
    for (const sc of closed) for (const id of sc.mentioned) if (!isCluster(id) && !memberOf.has(id)) memberOf.set(id, sc.id);
    return memberOf;
  }
  function clusterTitle(ctx, text) {
    const lines = text ? wrap(labelLines(text), 18 * ctx.fs, ctx.fs) : [];
    const has = lines.some((l) => l);
    return { title: has ? lines : [], tw: has ? maxW(lines, ctx.fs) : 0, th: has ? lines.length * ctx.lh + 4 : 0 };
  }
  // Box with a bold title and optional left-aligned compartments separated by dividers.
  function boxNode(ctx, head, sections, opt) {
    const fs = ctx.fs, lh = ctx.lh;
    opt = opt || {};
    const px = fs * 0.8, py = fs * 0.45;
    const sub = head.sub || [];
    const headW = Math.max(maxW(head.lines, fs, true), sub.length ? maxW(sub, fs * 0.85) : 0);
    let bodyW = 0;
    for (const sec of sections) for (const r of sec) bodyW = Math.max(bodyW, textW(r.text, fs, r.bold));
    const w = Math.max(headW + 2 * px, bodyW + 2 * px, opt.minW || fs * 6);
    const headH = py * 2 + head.lines.length * lh + sub.length * lh * 0.9;
    const secH = sections.map((sec) => (sec.length ? sec.length * lh + py * 2 : opt.emptyH == null ? py * 1.6 : opt.emptyH));
    const h = headH + secH.reduce((a, b) => a + b, 0);
    const outline = [-w / 2, -h / 2, w / 2, -h / 2, w / 2, h / 2, -w / 2, h / 2];
    const draw = (cx, cy, cls) => {
      const x0 = cx - w / 2, y0 = cy - h / 2;
      let s = '<g class="mm-node-g"><rect class="mm-node' + (cls ? ' ' + cls : '') + '" x="' + f(x0) + '" y="' + f(y0) + '" width="' + f(w) + '" height="' + f(h) + '" rx="' + (opt.rx || 3) + '"/>';
      let y = y0 + py;
      if (sub.length) {
        s += textBlock(sub, cx, y + (sub.length * lh * 0.9) / 2, ctx, 'mm-text mm-italic mm-muted', 'middle', fs * 0.85);
        y += sub.length * lh * 0.9;
      }
      s += textBlock(head.lines, cx, y + (head.lines.length * lh) / 2, ctx, 'mm-text mm-bold', 'middle');
      y = y0 + headH;
      sections.forEach((sec, i) => {
        s += '<path class="mm-divider" d="M' + f(x0) + ',' + f(y) + 'H' + f(x0 + w) + '"/>';
        let ry = y + py;
        for (const r of sec) {
          s += textBlock([r.text], x0 + px, ry + lh / 2, ctx, 'mm-text' + (r.cls ? ' ' + r.cls : ''), 'start');
          ry += lh;
        }
        y += secH[i];
      });
      return s + '</g>';
    };
    return { w, h, outline, draw };
  }

  // ------------------------------------------------------------------ state diagram
  function renderState(p, ctx) {
    const fs = ctx.fs, lh = ctx.lh;
    let dir = 'TB';
    const states = new Map(), comps = new Map(), edges = [];
    const scopes = [{ id: null, mentioned: [] }], closed = [];
    let noteN = 0;
    const cur = () => scopes[scopes.length - 1];
    const getState = (id) => {
      let s = states.get(id);
      if (!s) {
        if (states.size >= LIMIT.nodes) fail('Too many states');
        s = { id, label: null, desc: [], kind: 'state' };
        states.set(id, s);
      }
      cur().mentioned.push(id);
      return s;
    };
    const pseudo = (which) => {
      const sc = cur();
      const id = '\u0000' + which + ':' + (sc.id == null ? '' : sc.id);
      if (!states.has(id)) states.set(id, { id, label: '', desc: [], kind: which });
      sc.mentioned.push(id);
      return id;
    };
    const clean = (raw) => unquote(String(raw).trim().replace(/:::[\w-]+$/, '').trim());
    const ref = (raw, side) => {
      const t = clean(raw);
      if (t === '[*]') return pseudo(side ? 'end' : 'start');
      if (!t) fail('Missing state name');
      getState(t);
      return t;
    };
    const declare = (id, label, open, stereo) => {
      id = clean(id);
      if (!id) fail('Missing state name');
      if (open) {
        if (comps.size >= 300) fail('Too many composite states');
        let c = comps.get(id);
        if (!c) {
          c = { id, label: label || id, parent: cur().id, mentioned: [] };
          comps.set(id, c);
        } else if (label) c.label = label;
        cur().mentioned.push(id);
        scopes.push(c);
        return;
      }
      const s = getState(id);
      if (label) s.label = label;
      if (stereo) {
        const k = stereo.toLowerCase();
        if (k === 'fork' || k === 'join') s.kind = 'fork';
        else if (k === 'choice') s.kind = 'choice';
      }
    };
    const addNote = (target, text) => {
      const t = clean(target);
      getState(t);
      const id = '\u0000note' + noteN++;
      states.set(id, { id, label: text, desc: [], kind: 'note' });
      cur().mentioned.push(id);
      edges.push({ from: t, to: id, note: true });
    };
    const L = p.lines;
    for (let i = 1; i < L.length; i++) {
      const t = L[i].t;
      let m;
      try {
        if ((m = /^direction\s+(TB|TD|BT|LR|RL)$/i.exec(t))) {
          if (scopes.length === 1) dir = DIRS[m[1].toUpperCase()];
        } else if (t === '}') {
          if (scopes.length === 1) fail('Unexpected "}"');
          closed.push(scopes.pop());
        } else if ((m = /^state\s+"([^"]*)"\s+as\s+([^\s{]+)\s*(\{)?\s*$/i.exec(t))) {
          declare(m[2], m[1], m[3]);
        } else if ((m = /^state\s+([^\s{<:"]+)\s+as\s+"([^"]*)"\s*(\{)?\s*$/i.exec(t))) {
          declare(m[1], m[2], m[3]);
        } else if ((m = /^state\s+([^\s{<:"]+)\s*(?:<<\s*(\w+)\s*>>)?\s*(\{)?\s*$/i.exec(t))) {
          declare(m[1], null, m[3], m[2]);
        } else if ((m = /^state\s+([^\s{<:"]+)\s*:\s*(.*)$/i.exec(t))) {
          getState(clean(m[1])).desc.push(m[2]);
        } else if ((m = /^note\s+(?:left|right)\s+of\s+([^\s:]+)\s*:\s*(.*)$/i.exec(t))) {
          addNote(m[1], m[2]);
        } else if ((m = /^note\s+(?:left|right)\s+of\s+([^\s:]+)\s*$/i.exec(t))) {
          const buf = [];
          let j = i + 1;
          while (j < L.length && !/^end\s*note$/i.test(L[j].t)) buf.push(L[j++].t);
          if (j >= L.length) fail('Missing "end note"');
          i = j;
          addNote(m[1], buf.join('\n'));
        } else if ((m = /^(.+?)\s*-->\s*(.+?)\s*(?::\s*(.*))?$/.exec(t))) {
          const a = ref(m[1], 0), b = ref(m[2], 1);
          if (edges.length >= LIMIT.edges) fail('Too many transitions');
          edges.push({ from: a, to: b, label: m[3] ? m[3].trim() : null });
        } else if (t === '--' || /^(?:classDef|class|style|hide|scale|click|linkStyle|note\s+"|end\s+note)\b/i.test(t)) {
          // styling and concurrency separators are not rendered
        } else if ((m = /^([^\s:]+)\s*:\s*(.*)$/.exec(t))) {
          getState(clean(m[1])).desc.push(m[2]);
        } else if ((m = /^([^\s:{}]+)$/.exec(t))) {
          getState(clean(m[1]));
        } else fail('Cannot parse "' + clip(t, 40) + '"');
      } catch (e) {
        if (e.mmUser) e.message = 'Line ' + L[i].n + ': ' + e.message;
        throw e;
      }
    }
    while (scopes.length > 1) closed.push(scopes.pop());
    for (const id of comps.keys()) states.delete(id);
    if (!states.size && !comps.size) fail('The state diagram has no states');
    const memberOf = claimMembers(closed, (id) => comps.has(id));
    const nodes = [];
    for (const s of states.values()) {
      let geom, draw;
      if (s.kind === 'state' && s.desc.length) {
        const title = wrap(labelLines(s.label || s.id), 15 * fs, fs, true);
        const rows = [];
        for (const d of s.desc) for (const l of wrap(labelLines(d), 18 * fs, fs)) rows.push({ text: l });
        const b = boxNode(ctx, { lines: title }, [rows], { rx: 9 });
        geom = { w: b.w, h: b.h, outline: b.outline };
        draw = (cx, cy) => b.draw(cx, cy, 'mm-state');
      } else {
        const kind = s.kind === 'state' ? 'state' : s.kind;
        const lines = kind === 'state' || kind === 'note' ? wrap(labelLines(kind === 'state' ? s.label || s.id : s.label), 15 * fs, fs) : [];
        const g = shapeFor(kind, lines.length ? maxW(lines, fs) : 0, lines.length * lh, ctx, dir);
        geom = g;
        draw = (cx, cy) => '<g class="mm-node-g">' + g.svg(cx, cy, ' class="mm-node mm-shape-' + kind + '"') +
          (lines.length ? textBlock(lines, cx, cy, ctx, 'mm-text', 'middle') : '') + '</g>';
      }
      nodes.push({ id: s.id, w: geom.w, h: geom.h, outline: geom.outline, cluster: memberOf.get(s.id) ?? null, draw });
    }
    const gEdges = edges.map((e) => ({
      from: e.from, to: e.to, minlen: 1,
      label: e.label ? wrap(labelLines(e.label), 15 * fs, fs * 0.92) : null,
      cls: e.note ? 'mm-edge mm-dotted' : 'mm-edge', end: e.note ? null : 'arrow'
    }));
    const clusters = [...comps.values()].map((c) => Object.assign({ id: c.id, parent: c.parent }, clusterTitle(ctx, c.label)));
    const res = drawGraph({ dir, nodes, edges: gEdges, clusters }, ctx);
    return withTitle(ctx, p.title, res);
  }

  // ------------------------------------------------------------------ class diagram
  const CN = '(`[^`]+`|[\\p{L}\\p{N}_.$]+(?:~[^~]*~)?)';
  const CLS_REL = new RegExp('^' + CN + '\\s*(?:"([^"]*)"\\s*)?(<\\||\\*|o|<)?(--|\\.\\.)(\\|>|\\*|o|>)?\\s*(?:"([^"]*)"\\s*)?' + CN + '\\s*(?::\\s*(.*))?$', 'u');
  const CLS_DECL = new RegExp('^class\\s+' + CN + '\\s*(?:\\[\\s*"?([^\\]"]*)"?\\s*\\])?\\s*(?::::[\\w-]+)?\\s*(\\{)?\\s*(.*?)\\s*$', 'u');
  const CLS_MEMBER = new RegExp('^' + CN + '\\s*:\\s*(.+)$', 'u');
  const CLS_ALONE = new RegExp('^' + CN + '(?::::[\\w-]+)?$', 'u');
  const REL_MARK = { '<|': 'tri', '|>': 'tri', '*': 'diamond', o: 'odiamond', '<': 'open', '>': 'open' };

  function renderClass(p, ctx) {
    const fs = ctx.fs;
    let dir = 'TB';
    const classes = new Map(), rels = [], spaces = new Map();
    const nsStack = [];
    let block = null;
    const generic = (s) => s.replace(/~([^~]*)~/g, '<$1>');
    const cls = (raw) => {
      let name = String(raw).trim(), gen = '';
      const g = /^(.*?)~([^~]*)~$/.exec(name);
      if (g) {
        name = g[1];
        gen = g[2];
      }
      name = name.replace(/^`|`$/g, '').trim();
      if (!name) fail('Missing class name');
      let c = classes.get(name);
      if (!c) {
        if (classes.size >= LIMIT.nodes) fail('Too many classes');
        c = { id: name, label: null, gen, ann: [], attrs: [], methods: [], ns: nsStack.length ? nsStack[nsStack.length - 1] : null };
        classes.set(name, c);
      }
      if (gen && !c.gen) c.gen = gen;
      return c;
    };
    const member = (c, raw) => {
      let t = String(raw).trim();
      if (!t || t === '{' || t === '}') return;
      const an = /^<<\s*(.*?)\s*>>$/.exec(t);
      if (an) {
        c.ann.push(an[1]);
        return;
      }
      let deco = '';
      if (t.indexOf('(') >= 0) {
        const m = /^(.*\))\s*([$*])?\s*(.*?)\s*([$*])?$/.exec(t);
        if (m) {
          deco = m[2] || m[4] || '';
          t = m[1] + (m[3] ? ' : ' + m[3] : '');
        }
        c.methods.push({ text: generic(t), cls: deco === '$' ? 'mm-under' : deco === '*' ? 'mm-italic' : '' });
      } else {
        const m = /^(.*?)\s*([$*])$/.exec(t);
        if (m) {
          deco = m[2];
          t = m[1];
        }
        c.attrs.push({ text: generic(t), cls: deco === '$' ? 'mm-under' : deco === '*' ? 'mm-italic' : '' });
      }
      if (c.attrs.length + c.methods.length > 200) fail('Too many class members');
    };
    const L = p.lines;
    for (let i = 1; i < L.length; i++) {
      const t = L[i].t;
      let m;
      try {
        if (block) {
          if (t === '}') block = null;
          else if (t.endsWith('}')) {
            member(block, t.slice(0, -1));
            block = null;
          } else member(block, t);
          continue;
        }
        if ((m = /^direction\s+(TB|TD|BT|LR|RL)$/i.exec(t))) dir = DIRS[m[1].toUpperCase()];
        else if ((m = /^namespace\s+([\p{L}\p{N}_.$-]+)\s*\{\s*$/iu.exec(t))) {
          nsStack.push(m[1]);
          if (!spaces.has(m[1])) spaces.set(m[1], { id: m[1], parent: nsStack.length > 1 ? nsStack[nsStack.length - 2] : null });
        } else if (t === '}') {
          if (!nsStack.length) fail('Unexpected "}"');
          nsStack.pop();
        } else if ((m = CLS_DECL.exec(t))) {
          const c = cls(m[1]);
          if (m[2]) c.label = m[2];
          if (m[3]) {
            let rest = m[4] || '';
            if (rest.endsWith('}')) {
              for (const part of rest.slice(0, -1).split(/;|\s{2,}/)) member(c, part);
            } else {
              if (rest) member(c, rest);
              block = c;
            }
          }
        } else if ((m = /^<<\s*([^>]+?)\s*>>\s*(\S+)$/.exec(t))) cls(m[2]).ann.push(m[1]);
        else if ((m = CLS_REL.exec(t))) {
          if (rels.length >= LIMIT.edges) fail('Too many relationships');
          const a = cls(m[1]), b = cls(m[7]);
          rels.push({ from: a.id, to: b.id, start: REL_MARK[m[3]] || null, end: REL_MARK[m[5]] || null, dashed: m[4] === '..', c1: m[2], c2: m[6], label: m[8] ? m[8].trim() : null });
        } else if ((m = CLS_MEMBER.exec(t))) member(cls(m[1]), m[2]);
        else if (/^(?:note|classDef|cssClass|style|click|link|callback)\b/i.test(t)) {
          // not rendered
        } else if ((m = CLS_ALONE.exec(t))) cls(m[1]);
        else fail('Cannot parse "' + clip(t, 40) + '"');
      } catch (e) {
        if (e.mmUser) e.message = 'Line ' + L[i].n + ': ' + e.message;
        throw e;
      }
    }
    if (!classes.size) fail('The class diagram has no classes');
    const nodes = [];
    for (const c of classes.values()) {
      const title = wrap(labelLines((c.label || c.id) + (c.gen ? '<' + c.gen + '>' : '')), 16 * fs, fs, true);
      const sub = c.ann.map((a) => '\u00ab' + a + '\u00bb');
      const b = boxNode(ctx, { lines: title, sub }, [c.attrs, c.methods], { minW: fs * 6.5 });
      nodes.push({ id: c.id, w: b.w, h: b.h, outline: b.outline, cluster: c.ns, spread: 0.8, draw: (cx, cy) => b.draw(cx, cy, 'mm-class') });
    }
    const edges = rels.map((r) => ({
      from: r.from, to: r.to, minlen: 1,
      label: r.label ? wrap(labelLines(r.label), 14 * fs, fs * 0.92) : null,
      cls: 'mm-edge' + (r.dashed ? ' mm-dashed' : ''), start: r.start, end: r.end, startText: r.c1 || null, endText: r.c2 || null
    }));
    const clusters = [...spaces.values()].map((s) => Object.assign({ id: s.id, parent: s.parent }, clusterTitle(ctx, s.id)));
    const res = drawGraph({ dir, nodes, edges, clusters }, ctx);
    return withTitle(ctx, p.title, res);
  }

  // ------------------------------------------------------------------ entity relationship diagram
  const ER_NAME = '("[^"]+"|[\\p{L}\\p{N}_-]+)(?:\\s*\\[\\s*"?([^\\]"]*)"?\\s*\\])?';
  const ER_REL = new RegExp('^' + ER_NAME + '\\s*(\\|o|\\|\\||\\}o|\\}\\||o\\||o\\{|\\|\\{)(--|\\.\\.)(o\\||\\|\\||o\\{|\\|\\{|\\|o|\\}o|\\}\\|)\\s*' + ER_NAME + '\\s*(?::\\s*(.*))?$', 'u');
  const ER_WORD = '(only one|one or zero|zero or one|one or more|one or many|many\\(1\\)|1\\+|zero or more|zero or many|many\\(0\\)|0\\+|1)';
  const ER_REL_W = new RegExp('^' + ER_NAME + '\\s+' + ER_WORD + '\\s+(to|optionally to)\\s+' + ER_WORD + '\\s+' + ER_NAME + '\\s*(?::\\s*(.*))?$', 'iu');
  const ER_CARD = { '|o': 'zeroone', 'o|': 'zeroone', '||': 'one', '}o': 'zeromany', 'o{': 'zeromany', '}|': 'many', '|{': 'many' };
  const ER_WCARD = {
    'only one': 'one', 1: 'one', 'one or zero': 'zeroone', 'zero or one': 'zeroone', 'one or more': 'many', 'one or many': 'many',
    'many(1)': 'many', '1+': 'many', 'zero or more': 'zeromany', 'zero or many': 'zeromany', 'many(0)': 'zeromany', '0+': 'zeromany'
  };
  const ER_BLOCK = new RegExp('^' + ER_NAME + '\\s*\\{\\s*(.*)$', 'u');
  const ER_ALONE = new RegExp('^' + ER_NAME + '$', 'u');

  function renderER(p, ctx) {
    const fs = ctx.fs, lh = ctx.lh;
    let dir = 'TB';
    const ents = new Map(), rels = [];
    let block = null;
    const ent = (raw, alias) => {
      const id = unquote(String(raw).trim());
      if (!id) fail('Missing entity name');
      let e = ents.get(id);
      if (!e) {
        if (ents.size >= LIMIT.nodes) fail('Too many entities');
        e = { id, label: null, attrs: [] };
        ents.set(id, e);
      }
      if (alias) e.label = alias;
      return e;
    };
    const attr = (e, raw) => {
      const t = raw.trim();
      if (!t) return;
      const m = /^([^\s"]+)\s+([^\s"]+)((?:\s*,?\s*\b(?:PK|FK|UK)\b)*)\s*(?:"([^"]*)")?\s*$/i.exec(t);
      if (m) e.attrs.push({ type: m[1], name: m[2], keys: m[3].toUpperCase().match(/PK|FK|UK/g)?.join(', ') || '', comment: m[4] || '' });
      else e.attrs.push({ type: '', name: t.replace(/^"|"$/g, ''), keys: '', comment: '' });
      if (e.attrs.length > 200) fail('Too many attributes');
    };
    const L = p.lines;
    for (let i = 1; i < L.length; i++) {
      const t = L[i].t;
      let m;
      try {
        if (block) {
          if (t === '}') block = null;
          else if (t.endsWith('}')) {
            attr(block, t.slice(0, -1));
            block = null;
          } else attr(block, t);
          continue;
        }
        if ((m = /^direction\s+(TB|TD|BT|LR|RL)$/i.exec(t))) dir = DIRS[m[1].toUpperCase()];
        else if ((m = ER_REL.exec(t))) {
          if (rels.length >= LIMIT.edges) fail('Too many relationships');
          const a = ent(m[1], m[2]), b = ent(m[6], m[7]);
          rels.push({ from: a.id, to: b.id, start: ER_CARD[m[3]], end: ER_CARD[m[5]], dashed: m[4] === '..', label: m[8] ? unquote(m[8]) : '' });
        } else if ((m = ER_REL_W.exec(t))) {
          const a = ent(m[1], m[2]), b = ent(m[6], m[7]);
          rels.push({ from: a.id, to: b.id, start: ER_WCARD[m[3].toLowerCase()], end: ER_WCARD[m[5].toLowerCase()], dashed: /^optionally/i.test(m[4]), label: m[8] ? unquote(m[8]) : '' });
        } else if ((m = ER_BLOCK.exec(t))) {
          const e = ent(m[1], m[2]);
          const rest = m[3].trim();
          if (rest.endsWith('}')) rest.slice(0, -1).split(/;/).forEach((x) => attr(e, x));
          else {
            if (rest) attr(e, rest);
            block = e;
          }
        } else if ((m = ER_ALONE.exec(t))) ent(m[1], m[2]);
        else if (/^(?:classDef|class|style)\b/i.test(t)) {
          // not rendered
        } else fail('Cannot parse "' + clip(t, 40) + '"');
      } catch (e) {
        if (e.mmUser) e.message = 'Line ' + L[i].n + ': ' + e.message;
        throw e;
      }
    }
    if (!ents.size) fail('The ER diagram has no entities');
    const nodes = [];
    for (const e of ents.values()) {
      const title = wrap(labelLines(e.label || e.id), 16 * fs, fs, true);
      const cols = [0, 0, 0, 0];
      const rows = e.attrs.map((a) => [a.type, a.name, a.keys, a.comment]);
      for (const r of rows) r.forEach((c, k) => (cols[k] = Math.max(cols[k], c ? textW(c, k === 2 ? fs * 0.85 : fs, k === 2) : 0)));
      const px = fs * 0.7, gap = fs * 0.9;
      let tableW = px * 2;
      cols.forEach((c) => (tableW += c ? c + gap : 0));
      const headH = title.length * lh + fs * 0.9;
      const rowH = lh + fs * 0.3;
      const w = Math.max(maxW(title, fs, true) + fs * 2, tableW, fs * 7);
      const h = headH + rows.length * rowH + (rows.length ? fs * 0.2 : 0);
      const outline = [-w / 2, -h / 2, w / 2, -h / 2, w / 2, h / 2, -w / 2, h / 2];
      const draw = (cx, cy) => {
        const x0 = cx - w / 2, y0 = cy - h / 2;
        let s = '<g class="mm-node-g"><rect class="mm-node mm-entity" x="' + f(x0) + '" y="' + f(y0) + '" width="' + f(w) + '" height="' + f(h) + '" rx="3"/>' +
          textBlock(title, cx, y0 + headH / 2, ctx, 'mm-text mm-bold', 'middle');
        if (rows.length) s += '<path class="mm-divider" d="M' + f(x0) + ',' + f(y0 + headH) + 'H' + f(x0 + w) + '"/>';
        rows.forEach((r, ri) => {
          const ry = y0 + headH + fs * 0.1 + ri * rowH;
          if (ri % 2 === 1) s += '<rect class="mm-band" x="' + f(x0 + 1) + '" y="' + f(ry) + '" width="' + f(w - 2) + '" height="' + f(rowH) + '"/>';
          let x = x0 + px;
          r.forEach((c, k) => {
            if (!cols[k]) return;
            if (c) s += textBlock([c], x, ry + rowH / 2, ctx, k === 1 ? 'mm-text' : k === 2 ? 'mm-text mm-bold' : 'mm-text mm-muted' + (k === 3 ? ' mm-italic' : ''), 'start', k === 2 ? fs * 0.85 : 0);
            x += cols[k] + gap;
          });
        });
        return s + '</g>';
      };
      nodes.push({ id: e.id, w, h, outline, cluster: null, spread: 0.7, draw });
    }
    const edges = rels.map((r) => ({
      from: r.from, to: r.to, minlen: 1,
      label: r.label ? wrap(labelLines(r.label), 14 * fs, fs * 0.92) : null,
      cls: 'mm-edge' + (r.dashed ? ' mm-dashed' : ''), start: r.start, end: r.end
    }));
    const res = drawGraph({ dir, nodes, edges, clusters: [] }, ctx);
    return withTitle(ctx, p.title, res);
  }

  // ------------------------------------------------------------------ gantt chart
  const DAY = 864e5;
  const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const MONTH = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
  const WDAY = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const WEEKDAY = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
  const pad2 = (n) => (n < 10 ? '0' : '') + n;

  function dateParser(fmt) {
    const tok = /YYYY|YY|MMMM|MMM|MM|M|DD|Do|D|HH|H|hh|h|mm|m|ss|s|SSS|A|a|X|x/g;
    let re = '^', last = 0, m;
    const keys = [];
    const escRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    while ((m = tok.exec(fmt))) {
      re += escRe(fmt.slice(last, m.index));
      last = m.index + m[0].length;
      const k = m[0];
      keys.push(k);
      re += k === 'YYYY' ? '(\\d{4})' : k === 'MMMM' || k === 'MMM' ? '([A-Za-z]+)' : k === 'SSS' ? '(\\d{1,3})' : k === 'A' || k === 'a' ? '([AaPp][Mm])' :
        k === 'X' || k === 'x' ? '(-?\\d+)' : k === 'Do' ? '(\\d{1,2})(?:st|nd|rd|th)' : '(\\d{1,2})';
    }
    re += escRe(fmt.slice(last)) + '$';
    const rx = new RegExp(re);
    return (s) => {
      const r = rx.exec(s.trim());
      if (!r) return null;
      let y = 1970, mo = 0, d = 1, h = 0, mi = 0, se = 0, ms = 0, pm = null;
      for (let i = 0; i < keys.length; i++) {
        const v = r[i + 1], k = keys[i];
        const n = parseInt(v, 10);
        if (k === 'X') return n * 1000;
        if (k === 'x') return n;
        if (k === 'YYYY') y = n;
        else if (k === 'YY') y = 2000 + n;
        else if (k === 'MMMM' || k === 'MMM') {
          mo = MON.findIndex((x) => x.toLowerCase() === v.slice(0, 3).toLowerCase());
          if (mo < 0) return null;
        } else if (k === 'MM' || k === 'M') mo = n - 1;
        else if (k === 'DD' || k === 'D' || k === 'Do') d = n;
        else if (k === 'HH' || k === 'H' || k === 'hh' || k === 'h') h = n;
        else if (k === 'mm' || k === 'm') mi = n;
        else if (k === 'ss' || k === 's') se = n;
        else if (k === 'SSS') ms = n;
        else pm = /p/i.test(v);
      }
      if (pm !== null && h < 12 && pm) h += 12;
      if (pm === false && h === 12) h = 0;
      if (mo < 0 || mo > 11 || d < 1 || d > 31 || h > 23 || mi > 59 || se > 59) return null;
      return Date.UTC(y, mo, d, h, mi, se, ms);
    };
  }
  function fmtDate(t, fmt) {
    const d = new Date(t);
    return fmt.replace(/%-?([a-zA-Z%])/g, (m, c) => {
      switch (c) {
        case 'Y': return String(d.getUTCFullYear());
        case 'y': return pad2(d.getUTCFullYear() % 100);
        case 'm': return pad2(d.getUTCMonth() + 1);
        case 'd': return pad2(d.getUTCDate());
        case 'e': return String(d.getUTCDate());
        case 'b': return MON[d.getUTCMonth()];
        case 'B': return MONTH[d.getUTCMonth()];
        case 'a': return WDAY[d.getUTCDay()];
        case 'A': return WEEKDAY[d.getUTCDay()];
        case 'H': return pad2(d.getUTCHours());
        case 'I': return pad2(((d.getUTCHours() + 11) % 12) + 1);
        case 'p': return d.getUTCHours() < 12 ? 'AM' : 'PM';
        case 'M': return pad2(d.getUTCMinutes());
        case 'S': return pad2(d.getUTCSeconds());
        case 'j': return String(Math.floor((t - Date.UTC(d.getUTCFullYear(), 0, 1)) / DAY) + 1);
        case '%': return '%';
        default: return m;
      }
    });
  }
  const DUR = /^(\d+(?:\.\d+)?)\s*(ms|s|m|h|d|w|M|y)$/;
  const DUR_MS = { ms: 1, s: 1e3, m: 6e4, h: 36e5, d: DAY, w: 7 * DAY, M: 30 * DAY, y: 365 * DAY };

  function renderGantt(p, ctx) {
    const fs = ctx.fs, lh = ctx.lh;
    let title = p.title, dateFmt = 'YYYY-MM-DD', axisFmt = null;
    const tasks = [], sections = [];
    let sec = null;
    for (let i = 1; i < p.lines.length; i++) {
      const t = p.lines[i].t;
      let m;
      if ((m = /^title\s+(.*)$/i.exec(t))) title = m[1].trim();
      else if ((m = /^dateFormat\s+(.+)$/i.exec(t))) dateFmt = m[1].trim();
      else if ((m = /^axisFormat\s+(.+)$/i.exec(t))) axisFmt = m[1].trim();
      else if (/^(?:excludes|includes|todayMarker|weekday|inclusiveEndDates|topAxis|displayMode|tickInterval|click)\b/i.test(t)) {
        // not supported: ignored
      } else if ((m = /^section\s+(.*)$/i.exec(t))) {
        sec = { name: m[1].trim(), tasks: [] };
        sections.push(sec);
      } else if ((m = /^([^:]+?)\s*:\s*(.*)$/.exec(t))) {
        if (tasks.length >= 1000) fail('Too many tasks');
        const parts = m[2].split(',').map((s) => s.trim()).filter(Boolean);
        const tags = new Set();
        while (parts.length && /^(?:done|active|crit|milestone)$/i.test(parts[0])) tags.add(parts.shift().toLowerCase());
        const task = { name: m[1].trim(), tags, id: null, startSpec: null, endSpec: null, n: p.lines[i].n, sec };
        if (parts.length >= 3) {
          task.id = parts[0];
          task.startSpec = parts[1];
          task.endSpec = parts[2];
        } else if (parts.length === 2) {
          task.startSpec = parts[0];
          task.endSpec = parts[1];
        } else if (parts.length === 1) task.endSpec = parts[0];
        else fail('Line ' + task.n + ': task "' + clip(task.name, 30) + '" needs a date or duration');
        if (!sec) {
          sec = { name: '', tasks: [] };
          sections.push(sec);
        }
        sec.tasks.push(task);
        tasks.push(task);
      } else fail('Line ' + p.lines[i].n + ': cannot parse "' + clip(t, 40) + '"');
    }
    if (!tasks.length) fail('The gantt chart has no tasks');
    const parse = dateParser(dateFmt);
    const toDate = (s) => {
      let v = parse(s);
      if (v === null) {
        const iso = /^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?$/.exec(s.trim());
        if (iso) v = Date.UTC(+iso[1], +iso[2] - 1, +iso[3], +(iso[4] || 0), +(iso[5] || 0), +(iso[6] || 0));
      }
      return v;
    };
    const byId = new Map();
    for (const t of tasks) if (t.id) byId.set(t.id, t);
    // a 2-item spec whose first item is neither a date nor "after" is "id, duration"
    for (const t of tasks) {
      if (!t.id && t.startSpec && !/^after\s/i.test(t.startSpec) && toDate(t.startSpec) === null && /^[\w-]+$/.test(t.startSpec)) {
        t.id = t.startSpec;
        t.startSpec = null;
        byId.set(t.id, t);
      }
    }
    // resolve in passes so forward references work
    for (let pass = 0, left = tasks.length; left > 0; pass++) {
      let progress = 0;
      tasks.forEach((t, k) => {
        if (t.end != null) return;
        let start = null;
        if (t.startSpec) {
          const a = /^after\s+(.+)$/i.exec(t.startSpec);
          if (a) {
            let mx = -Infinity;
            for (const id of a[1].trim().split(/\s+/)) {
              const r = byId.get(id);
              if (!r) fail('Line ' + t.n + ': unknown task id "' + clip(id, 30) + '"');
              if (r.end == null) return;
              mx = Math.max(mx, r.end);
            }
            start = mx;
          } else {
            start = toDate(t.startSpec);
            if (start === null) fail('Line ' + t.n + ': invalid date "' + clip(t.startSpec, 30) + '" for format ' + dateFmt);
          }
        } else {
          if (k === 0) fail('Line ' + t.n + ': the first task needs a start date');
          const prev = tasks[k - 1];
          if (prev.end == null) return;
          start = prev.end;
        }
        let end;
        const d = DUR.exec(t.endSpec), u = /^until\s+(.+)$/i.exec(t.endSpec);
        if (d) end = start + parseFloat(d[1]) * DUR_MS[d[2]];
        else if (u) {
          let mn = Infinity;
          for (const id of u[1].trim().split(/\s+/)) {
            const r = byId.get(id);
            if (!r) fail('Line ' + t.n + ': unknown task id "' + clip(id, 30) + '"');
            if (r.start == null) return;
            mn = Math.min(mn, r.start);
          }
          end = mn;
        } else {
          end = toDate(t.endSpec);
          if (end === null) fail('Line ' + t.n + ': invalid end "' + clip(t.endSpec, 30) + '"');
        }
        t.start = start;
        t.end = Math.max(end, start);
        progress++;
        left--;
      });
      if (!progress) fail('Cannot resolve task dates (check "after" references)');
      if (pass > tasks.length + 2) break;
    }
    let t0 = Infinity, t1 = -Infinity;
    for (const t of tasks) {
      t0 = Math.min(t0, t.start);
      t1 = Math.max(t1, t.end);
    }
    if (!(t1 > t0)) t1 = t0 + DAY;
    const span = t1 - t0;
    if (span > 200 * 365 * DAY) fail('The gantt chart covers too long a period');
    // ticks
    const M = 10;
    const timeW = Math.max(420, Math.min(960, (span / DAY) * 26));
    const scale = timeW / span;
    const steps = [
      [36e5, '%H:%M'], [3 * 36e5, '%H:%M'], [6 * 36e5, '%H:%M'], [12 * 36e5, '%b %d %H:%M'], [DAY, '%b %d'], [2 * DAY, '%b %d'],
      [7 * DAY, '%b %d'], [14 * DAY, '%b %d'], ['M1', '%b %Y'], ['M3', '%b %Y'], ['M6', '%b %Y'], ['Y1', '%Y'], ['Y5', '%Y'], ['Y10', '%Y'], ['Y50', '%Y']
    ];
    let ticks = [], tickFmt = axisFmt || '%Y-%m-%d';
    for (const [st, df] of steps) {
      const fmtS = axisFmt || df;
      const lw = textW(fmtDate(t0, fmtS), fs * 0.85) + fs * 1.2;
      const approx = typeof st === 'number' ? st : st[0] === 'M' ? +st.slice(1) * 30.4 * DAY : +st.slice(1) * 365.25 * DAY;
      if (approx * scale < lw && st !== 'Y50') continue;
      ticks = [];
      tickFmt = fmtS;
      if (typeof st === 'number') {
        const base = st >= DAY ? Date.UTC(new Date(t0).getUTCFullYear(), new Date(t0).getUTCMonth(), new Date(t0).getUTCDate()) : Math.floor(t0 / st) * st;
        for (let t = base; t <= t1 && ticks.length < 400; t += st) if (t >= t0) ticks.push(t);
      } else {
        const unitM = st[0] === 'M' ? +st.slice(1) : +st.slice(1) * 12;
        const d0 = new Date(t0);
        let y = d0.getUTCFullYear(), mo = d0.getUTCMonth();
        if (st[0] === 'Y') mo = 0;
        for (let k = 0; k < 400; k++) {
          const t = Date.UTC(y, mo + k * unitM, 1);
          if (t > t1) break;
          if (t >= t0) ticks.push(t);
        }
      }
      break;
    }
    // rows
    const nameMax = 22 * fs;
    const trunc = (s) => {
      if (textW(s, fs) <= nameMax) return s;
      const ch = Array.from(s);
      while (ch.length > 1 && textW(ch.join('') + '\u2026', fs) > nameMax) ch.pop();
      return ch.join('') + '\u2026';
    };
    for (const t of tasks) t.label = trunc(labelLines(t.name).join(' '));
    for (const s of sections) s.label = s.name ? trunc(labelLines(s.name).join(' ')) : '';
    let leftW = 0;
    for (const t of tasks) leftW = Math.max(leftW, textW(t.label, fs) + fs * 1.8);
    for (const s of sections) if (s.label) leftW = Math.max(leftW, textW(s.label, fs, true) + fs);
    const titleLines = title ? wrap(labelLines(title), leftW + timeW, fs * 1.15, true) : [];
    const titleH = titleLines.length ? titleLines.length * fs * 1.5 + fs * 0.6 : 0;
    const rowH = lh + fs * 0.75, axisH = lh + fs * 0.5;
    const x0 = M + leftW + fs * 0.5;
    let y = M + titleH + axisH;
    const top = y;
    let rows = '', bars = '', bands = '';
    sections.forEach((s, si) => {
      const sy = y;
      if (s.label) {
        rows += textBlock([s.label], M, y + rowH / 2, ctx, 'mm-text mm-bold', 'start');
        y += rowH;
      }
      for (const t of s.tasks) {
        rows += textBlock([t.label], M + (s.label ? fs : 0), y + rowH / 2, ctx, 'mm-text', 'start');
        const tip = '<title>' + esc(t.name + ': ' + fmtDate(t.start, '%Y-%m-%d %H:%M') + ' \u2192 ' + fmtDate(t.end, '%Y-%m-%d %H:%M')) + '</title>';
        const col = t.tags.has('crit') ? 'mm-c6' : t.tags.has('active') ? 'mm-c1' : 'mm-c5';
        const cls = col + (t.tags.has('done') ? ' mm-bar-done' : '') + (t.tags.has('active') ? ' mm-bar-active' : '');
        const bx = x0 + (t.start - t0) * scale;
        if (t.tags.has('milestone')) {
          const cx = x0 + ((t.start + t.end) / 2 - t0) * scale, cy = y + rowH / 2, r = rowH * 0.32;
          bars += '<polygon class="' + cls + '" points="' + pts([cx, cy - r, cx + r, cy, cx, cy + r, cx - r, cy]) + '">' + tip + '</polygon>';
        } else {
          bars += '<rect class="' + cls + '" x="' + f(bx) + '" y="' + f(y + rowH * 0.18) + '" width="' + f(Math.max(2, (t.end - t.start) * scale)) + '" height="' + f(rowH * 0.64) + '" rx="3">' + tip + '</rect>';
        }
        y += rowH;
      }
      if (si % 2 === 1) bands = '<rect class="mm-band" x="' + f(M - 4) + '" y="' + f(sy) + '" width="' + f(leftW + timeW + fs * 0.5 + 8) + '" height="' + f(y - sy) + '"/>' + bands;
    });
    let grid = '';
    for (const t of ticks) {
      const x = x0 + (t - t0) * scale;
      grid += '<path class="mm-grid" d="M' + f(x) + ',' + f(top - 4) + 'V' + f(y) + '"/>' +
        textBlock([fmtDate(t, tickFmt)], x, top - axisH / 2 - 2, ctx, 'mm-text mm-muted', 'middle', fs * 0.85);
    }
    grid += '<path class="mm-grid" d="M' + f(x0) + ',' + f(top - 4) + 'V' + f(y) + '"/>';
    let W = x0 + timeW + M + fs * 2;
    const tw = titleLines.length ? maxW(titleLines, fs * 1.15, true) + 2 * M : 0;
    W = Math.max(W, tw);
    let body = '';
    if (titleLines.length) body += textBlock(titleLines, W / 2, M + titleH / 2 - 4, ctx, 'mm-text mm-bold', 'middle', fs * 1.15);
    body += bands + grid + rows + bars;
    return { body, w: W, h: y + M };
  }

  // ------------------------------------------------------------------ mindmap
  const MIND_SHAPES = [['((', '))', 'circle'], ['))', '((', 'bang'], ['{{', '}}', 'hexagon'], ['(', ')', 'round'], [')', '(', 'cloud'], ['[', ']', 'square']];

  function renderMindmap(p, ctx) {
    const fs = ctx.fs, lh = ctx.lh;
    const items = [];
    for (let i = 1; i < p.lines.length; i++) {
      const L = p.lines[i];
      let t = L.t;
      if (/^::icon\(/.test(t) || /^:::/.test(t)) continue;
      t = t.replace(/\s*:::[\w\s-]*$/, '').trim();
      if (!t) continue;
      const ind = /^[ \t]*/.exec(L.s)[0].replace(/\t/g, '    ').length;
      let shape = 'default', text = t;
      for (const [o, c, k] of MIND_SHAPES) {
        const at = t.indexOf(o);
        if (at >= 0 && t.length >= at + o.length + c.length && t.endsWith(c) && /^[\w-]*$/.test(t.slice(0, at))) {
          shape = k;
          text = t.slice(at + o.length, t.length - c.length);
          break;
        }
      }
      items.push({ ind, shape, text: unquote(text.trim()), children: [] });
      if (items.length > LIMIT.nodes) fail('Too many mindmap nodes');
    }
    if (!items.length) fail('The mindmap is empty');
    const root = items[0];
    root.depth = 0;
    const stack = [root];
    for (let i = 1; i < items.length; i++) {
      const it = items[i];
      while (stack.length > 1 && stack[stack.length - 1].ind >= it.ind) stack.pop();
      const parent = stack[stack.length - 1];
      parent.children.push(it);
      it.depth = parent.depth + 1;
      if (it.depth > 60) fail('The mindmap is nested too deeply');
      stack.push(it);
    }
    const KIND = { default: 'round', square: 'rect', round: 'stadium', circle: 'circle', bang: 'round', cloud: 'round', hexagon: 'hexagon' };
    const size = (n) => {
      const bold = n.depth === 0;
      n.lines = wrap(labelLines(n.text), (n.depth === 0 ? 12 : 15) * fs, fs, bold);
      n.geom = shapeFor(KIND[n.shape] || 'round', maxW(n.lines, fs, bold), n.lines.length * lh, ctx);
      for (const c of n.children) size(c);
    };
    size(root);
    const vgap = fs * 0.7, hgap = fs * 3;
    const measure = (n) => {
      let s = 0;
      for (const c of n.children) s += measure(c);
      s += Math.max(0, n.children.length - 1) * vgap;
      n.sh = Math.max(n.geom.h, s);
      return n.sh;
    };
    for (const c of root.children) measure(c);
    // split first-level branches between right and left, keeping order
    const total = root.children.reduce((a, c) => a + c.sh + vgap, 0);
    const right = [], left = [];
    let acc = 0;
    root.children.forEach((c, i) => {
      c.branch = (i % 7) + 2;
      if (acc < total / 2 || !right.length) {
        right.push(c);
        acc += c.sh + vgap;
      } else left.push(c);
    });
    const placed = [];
    const place = (n, x, yTop, side) => {
      n.x = x;
      n.y = yTop + n.sh / 2;
      n.side = side;
      placed.push(n);
      let cs = n.children.reduce((a, c) => a + c.sh, 0) + Math.max(0, n.children.length - 1) * vgap;
      let yy = yTop + (n.sh - cs) / 2;
      for (const c of n.children) {
        c.branch = n.branch;
        place(c, x + side * (n.geom.w / 2 + hgap + c.geom.w / 2), yy, side);
        yy += c.sh + vgap;
      }
    };
    const stackH = (arr) => arr.reduce((a, c) => a + c.sh, 0) + Math.max(0, arr.length - 1) * vgap * 2;
    root.x = 0;
    root.y = 0;
    root.side = 0;
    placed.push(root);
    for (const [arr, side] of [[right, 1], [left, -1]]) {
      let yy = -stackH(arr) / 2;
      for (const c of arr) {
        place(c, side * (root.geom.w / 2 + hgap * 1.3 + c.geom.w / 2), yy, side);
        yy += c.sh + vgap * 2;
      }
    }
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const n of placed) {
      minX = Math.min(minX, n.x - n.geom.w / 2);
      maxX = Math.max(maxX, n.x + n.geom.w / 2);
      minY = Math.min(minY, n.y - n.geom.h / 2);
      maxY = Math.max(maxY, n.y + n.geom.h / 2);
    }
    const M = 10, dx = M - minX, dy = M - minY;
    let edges = '', nodes = '';
    const walk = (n) => {
      for (const c of n.children) {
        const sx = n.x + dx + (c.side * n.geom.w) / 2, sy = n.y + dy;
        const ex = c.x + dx - (c.side * c.geom.w) / 2, ey = c.y + dy;
        const mx = (sx + ex) / 2;
        const sw = c.depth === 1 ? 3 : c.depth === 2 ? 2 : 1.5;
        edges += '<path class="mm-edge mm-k' + c.branch + '" style="stroke-width:' + sw + 'px" d="M' + f(sx) + ',' + f(sy) + 'C' + f(mx) + ',' + f(sy) + ' ' + f(mx) + ',' + f(ey) + ' ' + f(ex) + ',' + f(ey) + '"/>';
        walk(c);
      }
    };
    walk(root);
    for (const n of placed) {
      const cx = n.x + dx, cy = n.y + dy;
      const isRoot = n.depth === 0;
      const cls = isRoot ? 'mm-node mm-c1 mm-mind-root' : 'mm-node mm-k' + n.branch + ' mm-mind-' + n.shape;
      nodes += '<g class="mm-node-g">' + n.geom.svg(cx, cy, ' class="' + cls + '"' + (isRoot ? '' : ' style="stroke-width:' + (n.depth === 1 ? 2 : 1.3) + 'px"')) +
        textBlock(n.lines, cx, cy, ctx, isRoot ? 'mm-on-accent mm-bold' : 'mm-text', 'middle') + '</g>';
    }
    const res = { body: edges + nodes, w: maxX - minX + 2 * M, h: maxY - minY + 2 * M };
    return withTitle(ctx, p.title, res);
  }

  // ------------------------------------------------------------------ entry points
  function render(src, opts) {
    const o = opts || {};
    let fs = Number(o.fontSize);
    if (!(fs >= 6 && fs <= 40)) fs = 14;
    const ff = String(o.fontFamily || 'system-ui, sans-serif').replace(/[^\w\s,'".-]/g, '').slice(0, 200) || 'sans-serif';
    const p = prep(src);
    if (!p.lines.length) fail('The diagram is empty');
    const type = typeOf(p.lines[0].t);
    if (!type) fail('Unsupported diagram type "' + clip(p.lines[0].t.split(/[\s;]/)[0], 30) + '"');
    SEQ = (SEQ + 1) % 1e9;
    const ctx = { fs, ff, lh: fs * 1.3, uid: 'mm' + LOAD + '-' + SEQ.toString(36), markers: new Set(), accTitle: p.accTitle, accDescr: p.accDescr };
    const RENDER = {
      flowchart: renderFlowchart, sequence: renderSequence, pie: renderPie, state: renderState,
      class: renderClass, er: renderER, gantt: renderGantt, mindmap: renderMindmap
    };
    let res;
    try {
      res = RENDER[type](p, ctx);
    } catch (e) {
      if (e && e.mmUser) throw new Error(e.message);
      throw new Error('Could not render this ' + type + ' diagram' + (e && e.message ? ' (' + clip(e.message, 80) + ')' : ''));
    }
    return finish(ctx, res.w, res.h, res.body);
  }

  globalThis.BaabaaMermaid = { render, detect };
})();
