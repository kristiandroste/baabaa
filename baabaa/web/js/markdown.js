// Markdown to safe HTML for model output, with LaTeX math as MathML. A classic script: it sets
// globalThis.BaabaaMarkdown = { render, renderInline, escapeHtml, latexToMathML }.
// Model output is untrusted: raw HTML is never passed through, and only http(s), mailto and #fragment
// links are made clickable.
(function () {
  'use strict';

  var ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  function escapeHtml(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return ESC[c]; }); }
  var PUNCT = '!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~';

  function safeUrl(url) {
    url = String(url || '').trim();
    if (/^(https?:\/\/|mailto:)/i.test(url) || /^#[\w-]*$/.test(url)) return url;
    return null;
  }

  function link(href, inner) {
    return '<a href="' + escapeHtml(href) + '" target="_blank" rel="noopener noreferrer">' + inner + '</a>';
  }

  // ---------------------------------------------------------------------------------------------
  // LaTeX -> MathML
  // ---------------------------------------------------------------------------------------------
  var GREEK = {
    alpha: 'α', beta: 'β', gamma: 'γ', delta: 'δ', epsilon: 'ϵ', varepsilon: 'ε', zeta: 'ζ', eta: 'η', theta: 'θ',
    vartheta: 'ϑ', iota: 'ι', kappa: 'κ', lambda: 'λ', mu: 'μ', nu: 'ν', xi: 'ξ', omicron: 'ο', pi: 'π', varpi: 'ϖ',
    rho: 'ρ', varrho: 'ϱ', sigma: 'σ', varsigma: 'ς', tau: 'τ', upsilon: 'υ', phi: 'ϕ', varphi: 'φ', chi: 'χ',
    psi: 'ψ', omega: 'ω', Gamma: 'Γ', Delta: 'Δ', Theta: 'Θ', Lambda: 'Λ', Xi: 'Ξ', Pi: 'Π', Sigma: 'Σ',
    Upsilon: 'Υ', Phi: 'Φ', Psi: 'Ψ', Omega: 'Ω',
  };
  var SYMBOLS = {
    infty: ['mi', '∞'], partial: ['mi', '∂'], nabla: ['mi', '∇'], emptyset: ['mi', '∅'], varnothing: ['mi', '∅'],
    ell: ['mi', 'ℓ'], hbar: ['mi', 'ℏ'], Re: ['mi', 'ℜ'], Im: ['mi', 'ℑ'], aleph: ['mi', 'ℵ'], prime: ['mo', '′'],
    forall: ['mo', '∀'], exists: ['mo', '∃'], nexists: ['mo', '∄'], neg: ['mo', '¬'], lnot: ['mo', '¬'],
    leq: ['mo', '≤'], le: ['mo', '≤'], geq: ['mo', '≥'], ge: ['mo', '≥'], neq: ['mo', '≠'], ne: ['mo', '≠'],
    approx: ['mo', '≈'], equiv: ['mo', '≡'], sim: ['mo', '∼'], simeq: ['mo', '≃'], cong: ['mo', '≅'], propto: ['mo', '∝'],
    ll: ['mo', '≪'], gg: ['mo', '≫'], pm: ['mo', '±'], mp: ['mo', '∓'], times: ['mo', '×'], div: ['mo', '÷'],
    cdot: ['mo', '⋅'], ast: ['mo', '∗'], star: ['mo', '⋆'], circ: ['mo', '∘'], bullet: ['mo', '∙'], oplus: ['mo', '⊕'],
    otimes: ['mo', '⊗'], to: ['mo', '→'], rightarrow: ['mo', '→'], leftarrow: ['mo', '←'], gets: ['mo', '←'],
    leftrightarrow: ['mo', '↔'], Rightarrow: ['mo', '⇒'], Leftarrow: ['mo', '⇐'], Leftrightarrow: ['mo', '⇔'],
    implies: ['mo', '⟹'], iff: ['mo', '⟺'], mapsto: ['mo', '↦'], uparrow: ['mo', '↑'], downarrow: ['mo', '↓'],
    longrightarrow: ['mo', '⟶'], longleftarrow: ['mo', '⟵'], in: ['mo', '∈'], notin: ['mo', '∉'], ni: ['mo', '∋'],
    subset: ['mo', '⊂'], supset: ['mo', '⊃'], subseteq: ['mo', '⊆'], supseteq: ['mo', '⊇'], cup: ['mo', '∪'],
    cap: ['mo', '∩'], setminus: ['mo', '∖'], land: ['mo', '∧'], wedge: ['mo', '∧'], lor: ['mo', '∨'], vee: ['mo', '∨'],
    ldots: ['mo', '…'], cdots: ['mo', '⋯'], vdots: ['mo', '⋮'], ddots: ['mo', '⋱'], dots: ['mo', '…'],
    angle: ['mo', '∠'], perp: ['mo', '⊥'], parallel: ['mo', '∥'], mid: ['mo', '∣'], vert: ['mo', '|'], Vert: ['mo', '‖'],
    langle: ['mo', '⟨'], rangle: ['mo', '⟩'], lceil: ['mo', '⌈'], rceil: ['mo', '⌉'], lfloor: ['mo', '⌊'], rfloor: ['mo', '⌋'],
    therefore: ['mo', '∴'], because: ['mo', '∵'], deg: ['mi', 'deg'], degree: ['mo', '°'], colon: ['mo', ':'],
    lbrace: ['mo', '{'], rbrace: ['mo', '}'], backslash: ['mo', '\\'], '%': ['mo', '%'], '$': ['mo', '$'], '#': ['mo', '#'],
    '&': ['mo', '&'], '_': ['mo', '_'], '{': ['mo', '{'], '}': ['mo', '}'], '|': ['mo', '‖'],
  };
  var BIGOPS = { sum: '∑', prod: '∏', coprod: '∐', int: '∫', iint: '∬', iiint: '∭', oint: '∮', bigcup: '⋃',
    bigcap: '⋂', bigoplus: '⨁', bigotimes: '⨂', bigvee: '⋁', bigwedge: '⋀' };
  var LIMOPS = { lim: 'lim', limsup: 'lim sup', liminf: 'lim inf', max: 'max', min: 'min', sup: 'sup', inf: 'inf',
    argmax: 'arg max', argmin: 'arg min', det: 'det', gcd: 'gcd' };
  var FUNCS = ['sin', 'cos', 'tan', 'cot', 'sec', 'csc', 'sinh', 'cosh', 'tanh', 'coth', 'arcsin', 'arccos', 'arctan',
    'log', 'ln', 'lg', 'exp', 'ker', 'dim', 'hom', 'arg', 'Pr', 'mod', 'deg'];
  var ACCENTS = { hat: '^', widehat: '^', bar: '¯', overline: '¯', vec: '→', tilde: '~', widetilde: '~', dot: '˙',
    ddot: '¨', check: 'ˇ', breve: '˘', acute: '´', grave: '`', overrightarrow: '→', overleftarrow: '←' };
  var FONTS = { mathbf: 'bold', mathit: 'italic', mathbb: 'double-struck', mathcal: 'script', mathscr: 'script',
    mathfrak: 'fraktur', mathsf: 'sans-serif', mathtt: 'monospace', mathrm: 'normal', textbf: 'bold', textit: 'italic',
    boldsymbol: 'bold', bm: 'bold', operatorname: 'normal' };
  var SPACES = { ',': '0.1667em', ':': '0.2222em', '>': '0.2222em', ';': '0.2778em', ' ': '0.25em', quad: '1em',
    qquad: '2em', enspace: '0.5em', thinspace: '0.1667em', '!': '-0.1667em' };
  var FENCES = { '(': '(', ')': ')', '[': '[', ']': ']', '\\{': '{', '\\}': '}', '|': '|', '\\|': '‖', '.': '',
    '\\langle': '⟨', '\\rangle': '⟩', '\\lfloor': '⌊', '\\rfloor': '⌋', '\\lceil': '⌈', '\\rceil': '⌉', '/': '/', '\\vert': '|', '\\Vert': '‖' };

  function MathParser(src, display) {
    this.s = src;
    this.i = 0;
    this.display = display;
    this.depth = 0;
  }
  MathParser.prototype.peek = function () { return this.s[this.i]; };
  MathParser.prototype.skipSpace = function () { while (this.i < this.s.length && /\s/.test(this.s[this.i])) this.i++; };
  MathParser.prototype.command = function () {
    // at '\\'
    var j = this.i + 1;
    if (j >= this.s.length) { this.i = j; return ''; }
    if (/[A-Za-z]/.test(this.s[j])) {
      while (j < this.s.length && /[A-Za-z]/.test(this.s[j])) j++;
      var name = this.s.slice(this.i + 1, j);
      this.i = j;
      return name;
    }
    this.i = j + 1;
    return this.s[j];
  };
  MathParser.prototype.group = function () {
    // a braced group or a single atom, as MathML
    this.skipSpace();
    if (this.peek() === '{') {
      this.i++;
      var inner = this.expr(['}']);
      if (this.peek() === '}') this.i++;
      return inner;
    }
    return this.atom();
  };
  MathParser.prototype.rawGroup = function () {
    this.skipSpace();
    if (this.peek() !== '{') {
      var c = this.s[this.i] || '';
      this.i++;
      return c;
    }
    var depth = 0, start = this.i + 1;
    for (var j = this.i; j < this.s.length; j++) {
      if (this.s[j] === '\\') { j++; continue; }
      if (this.s[j] === '{') depth++;
      else if (this.s[j] === '}') { depth--; if (depth === 0) { this.i = j + 1; return this.s.slice(start, j); } }
    }
    this.i = this.s.length;
    return this.s.slice(start);
  };
  MathParser.prototype.optional = function () {
    this.skipSpace();
    if (this.peek() !== '[') return null;
    var depth = 0, start = this.i + 1;
    for (var j = this.i; j < this.s.length; j++) {
      if (this.s[j] === '[') depth++;
      else if (this.s[j] === ']') { depth--; if (depth === 0) { this.i = j + 1; return this.s.slice(start, j); } }
    }
    return null;
  };
  MathParser.prototype.sub = function (tex) {
    var p = new MathParser(tex, this.display);
    p.depth = this.depth + 1;
    if (p.depth > 40) throw new Error('too deep');
    return p.expr([]);
  };
  function mrow(items) { return items.length === 1 ? items[0] : '<mrow>' + items.join('') + '</mrow>'; }

  MathParser.prototype.expr = function (stops) {
    var items = [];
    while (this.i < this.s.length) {
      this.skipSpace();
      var c = this.peek();
      if (c === undefined) break;
      if (stops.indexOf(c) >= 0) break;
      if (c === '\\') {
        var save = this.i;
        var name = this.command();
        if (name === 'right' || name === 'end' || name === '\\' || name === 'cr') { this.i = save; break; }
        if (name === 'middle') {
          items.push('<mo stretchy="true">' + escapeHtml(this.fence()) + '</mo>');
          continue;
        }
        this.i = save;
      }
      if (c === '&' && stops.indexOf('&') >= 0) break;
      var atom = this.atom();
      if (atom === null) continue;
      items.push(typeof atom === 'object' ? this.scripts(atom.xml, atom.big) : this.scripts(atom, false));
    }
    return mrow(items.length ? items : ['<mrow></mrow>']);
  };

  MathParser.prototype.fence = function () {
    this.skipSpace();
    var c = this.peek();
    if (c === '\\') {
      var save = this.i;
      var name = this.command();
      var key = '\\' + name;
      if (key in FENCES) return FENCES[key];
      this.i = save + 1;
      return '';
    }
    this.i++;
    return c in FENCES ? FENCES[c] : (c || '');
  };

  MathParser.prototype.scripts = function (base, big) {
    var sub = null, sup = null;
    for (var k = 0; k < 4; k++) {
      this.skipSpace();
      var c = this.peek();
      if (c === '^' && sup === null) { this.i++; sup = this.group(); }
      else if (c === '_' && sub === null) { this.i++; sub = this.group(); }
      else if (c === "'" && sup === null) {
        var n = 0;
        while (this.peek() === "'") { n++; this.i++; }
        sup = '<mo>' + '′′′′'.slice(0, n) + '</mo>';
      } else break;
    }
    if (sub === null && sup === null) return base;
    var under = big && this.display;
    if (sub !== null && sup !== null) return under ? '<munderover>' + base + sub + sup + '</munderover>' : '<msubsup>' + base + sub + sup + '</msubsup>';
    if (sub !== null) return under ? '<munder>' + base + sub + '</munder>' : '<msub>' + base + sub + '</msub>';
    return under ? '<mover>' + base + sup + '</mover>' : '<msup>' + base + sup + '</msup>';
  };

  MathParser.prototype.atom = function () {
    var c = this.peek();
    if (c === '{') { this.i++; var g = this.expr(['}']); if (this.peek() === '}') this.i++; return g; }
    if (c === '}') { this.i++; return null; }
    if (/[0-9.]/.test(c)) {
      var m = /^[0-9]*\.?[0-9]+|^[0-9]+/.exec(this.s.slice(this.i));
      if (m) { this.i += m[0].length; return '<mn>' + m[0] + '</mn>'; }
      this.i++;
      return '<mo>.</mo>';
    }
    if (/[A-Za-z]/.test(c)) { this.i++; return '<mi>' + c + '</mi>'; }
    if (c === '\\') return this.commandAtom();
    this.i++;
    if (c === '~') return '<mspace width="0.25em"></mspace>';
    if ('+-=<>*/|!,;:()[]?'.indexOf(c) >= 0 || c === '−') {
      var ch = c === '-' ? '−' : c === '*' ? '∗' : c;
      if ('([|'.indexOf(c) >= 0 || ')]'.indexOf(c) >= 0) return '<mo stretchy="false">' + escapeHtml(ch) + '</mo>';
      return '<mo>' + escapeHtml(ch) + '</mo>';
    }
    if (c === '^' || c === '_') return '<mi></mi>';
    if (c === '&') return '<mo>&amp;</mo>';
    return '<mi>' + escapeHtml(c) + '</mi>';
  };

  MathParser.prototype.commandAtom = function () {
    var name = this.command();
    if (!name) return null;
    if (name in GREEK) return /[A-Z]/.test(name[0]) ? '<mi mathvariant="normal">' + GREEK[name] + '</mi>' : '<mi>' + GREEK[name] + '</mi>';
    if (name in SYMBOLS) { var sy = SYMBOLS[name]; return '<' + sy[0] + '>' + escapeHtml(sy[1]) + '</' + sy[0] + '>'; }
    if (name in BIGOPS) return { xml: '<mo largeop="true" movablelimits="true">' + BIGOPS[name] + '</mo>', big: true };
    if (name in LIMOPS) return { xml: '<mo movablelimits="true" form="prefix">' + LIMOPS[name] + '</mo>', big: true };
    if (FUNCS.indexOf(name) >= 0) return '<mi>' + name + '</mi><mo>&#x2061;</mo>';
    if (name in SPACES) return '<mspace width="' + SPACES[name] + '"></mspace>';
    if (name === 'frac' || name === 'dfrac' || name === 'tfrac' || name === 'cfrac') {
      var num = this.group(), den = this.group();
      return '<mfrac>' + num + den + '</mfrac>';
    }
    if (name === 'binom' || name === 'dbinom' || name === 'tbinom') {
      var n = this.group(), k = this.group();
      return '<mrow><mo>(</mo><mfrac linethickness="0">' + n + k + '</mfrac><mo>)</mo></mrow>';
    }
    if (name === 'sqrt') {
      var idx = this.optional();
      var body = this.group();
      return idx !== null ? '<mroot>' + body + this.sub(idx) + '</mroot>' : '<msqrt>' + body + '</msqrt>';
    }
    if (name in ACCENTS) {
      var base = this.group();
      if (name === 'underline') return '<munder>' + base + '<mo>_</mo></munder>';
      return '<mover accent="true">' + base + '<mo>' + escapeHtml(ACCENTS[name]) + '</mo></mover>';
    }
    if (name === 'underline') return '<munder accentunder="true">' + this.group() + '<mo>_</mo></munder>';
    if (name === 'text' || name === 'textrm' || name === 'mbox' || name === 'textnormal' || name === 'hbox') {
      return '<mtext>' + escapeHtml(this.rawGroup()) + '</mtext>';
    }
    if (name in FONTS) {
      var raw = this.rawGroup();
      if (name === 'operatorname') return '<mi mathvariant="normal">' + escapeHtml(raw) + '</mi><mo>&#x2061;</mo>';
      if (/^[A-Za-z0-9 ]*$/.test(raw)) return '<mi mathvariant="' + FONTS[name] + '">' + escapeHtml(raw) + '</mi>';
      return '<mstyle mathvariant="' + FONTS[name] + '">' + this.sub(raw) + '</mstyle>';
    }
    if (name === 'left') {
      var open = this.fence();
      var inner = this.expr([]);
      var close = '';
      var save = this.i;
      if (this.s.slice(this.i, this.i + 6) === '\\right') { this.i += 6; close = this.fence(); } else this.i = save;
      return '<mrow><mo fence="true" stretchy="true">' + escapeHtml(open) + '</mo>' + inner + '<mo fence="true" stretchy="true">' + escapeHtml(close) + '</mo></mrow>';
    }
    if (name === 'big' || name === 'Big' || name === 'bigg' || name === 'Bigg' || /^[Bb]igg?[lr]$/.test(name)) {
      return '<mo stretchy="false">' + escapeHtml(this.fence()) + '</mo>';
    }
    if (name === 'begin') return this.environment(this.rawGroup());
    if (name === 'displaystyle' || name === 'textstyle' || name === 'limits' || name === 'nolimits' || name === 'label' && (this.rawGroup(), true)) return null;
    if (name === 'not') {
      var next = this.atom();
      return '<mrow><menclose notation="updiagonalstrike">' + (next && next.xml || next || '') + '</menclose></mrow>';
    }
    if (name === 'overbrace' || name === 'underbrace') {
      var b = this.group();
      return name === 'overbrace' ? '<mover>' + b + '<mo>⏞</mo></mover>' : '<munder>' + b + '<mo>⏟</mo></munder>';
    }
    if (name === 'pmod') return '<mrow><mo>(</mo><mi>mod</mi><mspace width="0.25em"></mspace>' + this.group() + '<mo>)</mo></mrow>';
    if (name === 'bmod') return '<mo>mod</mo>';
    if (name === 'boxed') return '<menclose notation="box">' + this.group() + '</menclose>';
    if (name === 'color' || name === 'textcolor') { this.rawGroup(); return name === 'textcolor' ? this.group() : null; }
    if (name === 'phantom') { this.group(); return '<mspace width="1em"></mspace>'; }
    if (name === '\\') return null;
    return '<mi mathvariant="normal">\\' + escapeHtml(name) + '</mi>';
  };

  MathParser.prototype.environment = function (env) {
    var cols = null;
    if (env === 'array') cols = this.rawGroup();
    var rows = [], row = [], start;
    for (;;) {
      this.skipSpace();
      if (this.i >= this.s.length) break;
      if (this.s.slice(this.i, this.i + 4) === '\\end') {
        this.i += 4;
        this.rawGroup();
        break;
      }
      start = this.i;
      var cell = this.expr(['&']);
      row.push(cell);
      if (this.peek() === '&') { this.i++; continue; }
      if (this.s.slice(this.i, this.i + 2) === '\\\\') {
        this.i += 2;
        this.optional();
        rows.push(row);
        row = [];
        continue;
      }
      if (this.s.slice(this.i, this.i + 3) === '\\cr') { this.i += 3; rows.push(row); row = []; continue; }
      if (this.i === start) this.i++;
    }
    if (row.length) rows.push(row);
    var align = env === 'aligned' || env === 'align' || env === 'align*' || env === 'split';
    var table = '<mtable' + (env === 'cases' ? ' columnalign="left left"' : align ? ' columnalign="right left"' : '') + '>'
      + rows.map(function (r) { return '<mtr>' + r.map(function (c) { return '<mtd>' + c + '</mtd>'; }).join('') + '</mtr>'; }).join('')
      + '</mtable>';
    var wrap = { pmatrix: ['(', ')'], bmatrix: ['[', ']'], Bmatrix: ['{', '}'], vmatrix: ['|', '|'], Vmatrix: ['‖', '‖'],
      cases: ['{', ''], 'smallmatrix': ['', ''] }[env];
    if (wrap) {
      return '<mrow>' + (wrap[0] ? '<mo fence="true" stretchy="true">' + escapeHtml(wrap[0]) + '</mo>' : '') + table
        + (wrap[1] ? '<mo fence="true" stretchy="true">' + escapeHtml(wrap[1]) + '</mo>' : '') + '</mrow>';
    }
    return table;
  };

  function latexToMathML(tex, display) {
    try {
      var p = new MathParser(String(tex), !!display);
      var body = p.expr([]);
      var guard = 0;
      while (p.i < p.s.length && guard++ < 100) { p.i++; body += p.expr([]); }
      return '<math xmlns="http://www.w3.org/1998/Math/MathML" display="' + (display ? 'block' : 'inline') + '">' + body + '</math>';
    } catch (e) {
      return '<code class="math-error">' + escapeHtml(tex) + '</code>';
    }
  }

  // ---------------------------------------------------------------------------------------------
  // Inline markdown
  // ---------------------------------------------------------------------------------------------
  function isSpace(c) { return c === undefined || c === ' ' || c === '\t' || c === '\n'; }
  function isAlnum(c) { return c !== undefined && /[\p{L}\p{N}]/u.test(c); }

  function findClosingBacktick(s, from, n) {
    var i = from;
    while (i < s.length) {
      var j = s.indexOf('`', i);
      if (j < 0) return -1;
      var k = j;
      while (s[k] === '`') k++;
      if (k - j === n) return j;
      i = k;
    }
    return -1;
  }

  function linkTarget(s, i) {
    // s[i] === '(' ; returns {url, end} with balanced parentheses, or null
    var j = i + 1;
    while (s[j] === ' ') j++;
    var start = j, depth = 0;
    if (s[j] === '<') {
      var close = s.indexOf('>', j);
      if (close < 0) return null;
      var url0 = s.slice(j + 1, close);
      j = close + 1;
      while (s[j] === ' ') j++;
      if (s[j] === '"') { var e0 = s.indexOf('"', j + 1); if (e0 < 0) return null; j = e0 + 1; while (s[j] === ' ') j++; }
      return s[j] === ')' ? { url: url0, end: j + 1 } : null;
    }
    for (; j < s.length; j++) {
      var c = s[j];
      if (c === '\\') { j++; continue; }
      if (c === '(') depth++;
      else if (c === ')') { if (depth === 0) break; depth--; }
      else if (c === ' ' || c === '\n') break;
    }
    var url = s.slice(start, j);
    while (s[j] === ' ') j++;
    if (s[j] === '"' || s[j] === "'") {
      var q = s[j], e = s.indexOf(q, j + 1);
      if (e < 0) return null;
      j = e + 1;
      while (s[j] === ' ') j++;
    }
    if (s[j] !== ')') return null;
    return { url: url, end: j + 1 };
  }

  function findBracketClose(s, i) {
    var depth = 0;
    for (var j = i; j < s.length; j++) {
      if (s[j] === '\\') { j++; continue; }
      if (s[j] === '`') { var n = 0, k = j; while (s[k] === '`') { n++; k++; } var cl = findClosingBacktick(s, k, n); if (cl >= 0) { j = cl + n - 1; continue; } }
      if (s[j] === '[') depth++;
      else if (s[j] === ']') { depth--; if (depth === 0) return j; }
    }
    return -1;
  }

  function trimUrl(url) {
    var m = /[.,;:!?'"*_]+$/.exec(url);
    if (m) url = url.slice(0, -m[0].length);
    while (url.endsWith(')') && (url.split('(').length - 1) < (url.split(')').length - 1)) url = url.slice(0, -1);
    return url;
  }

  function mathInline(s, i, opts) {
    // returns {html, end} or null
    if (opts.math === false) return null;
    if (s[i] === '\\' && (s[i + 1] === '(' || s[i + 1] === '[')) {
      var closer = s[i + 1] === '(' ? '\\)' : '\\]';
      var e = s.indexOf(closer, i + 2);
      if (e < 0) return null;
      return { html: latexToMathML(s.slice(i + 2, e), s[i + 1] === '['), end: e + 2 };
    }
    if (s[i] !== '$') return null;
    if (s[i + 1] === '$') {
      var e2 = s.indexOf('$$', i + 2);
      if (e2 < 0) return null;
      return { html: latexToMathML(s.slice(i + 2, e2), true), end: e2 + 2 };
    }
    if (isSpace(s[i + 1])) return null;
    for (var j = i + 1; j < s.length; j++) {
      if (s[j] === '\\') { j++; continue; }
      if (s[j] === '\n' && s[j + 1] === '\n') return null;
      if (s[j] === '$') {
        if (isSpace(s[j - 1]) || /[0-9]/.test(s[j + 1] || '')) return null;
        var tex = s.slice(i + 1, j);
        if (/^[0-9.,]+$/.test(tex) && /^\s*[0-9]/.test(s.slice(j + 1))) return null;
        return { html: latexToMathML(tex, false), end: j + 1 };
      }
    }
    return null;
  }

  function inline(s, opts, depth) {
    depth = depth || 0;
    if (depth > 20) return escapeHtml(s);
    var out = '', buf = '', i = 0, n = s.length;
    function flush() { if (buf) { out += autolinks(buf, opts); buf = ''; } }
    while (i < n) {
      var c = s[i];
      if (c === '\\' && i + 1 < n) {
        var nx = s[i + 1];
        if (nx === '(' || nx === '[') {
          var mm = mathInline(s, i, opts);
          if (mm) { flush(); out += mm.html; i = mm.end; continue; }
        }
        if (PUNCT.indexOf(nx) >= 0) { buf += nx; i += 2; continue; }
        if (nx === '\n') { flush(); out += '<br>'; i += 2; continue; }
      }
      if (c === '`') {
        var k = i;
        while (s[k] === '`') k++;
        var ticks = k - i;
        var close = findClosingBacktick(s, k, ticks);
        if (close >= 0) {
          flush();
          var code = s.slice(k, close).replace(/\n/g, ' ');
          if (/^ .* $/.test(code) && code.trim()) code = code.slice(1, -1);
          out += '<code>' + escapeHtml(code) + '</code>';
          i = close + ticks;
          continue;
        }
        if (opts.streaming && close < 0) { buf += s.slice(i, k); i = k; continue; }
        buf += s.slice(i, k);
        i = k;
        continue;
      }
      if (c === '$') {
        var m1 = mathInline(s, i, opts);
        if (m1) { flush(); out += m1.html; i = m1.end; continue; }
      }
      if (c === '!' && s[i + 1] === '[') {
        var cb = findBracketClose(s, i + 1);
        if (cb > 0 && s[cb + 1] === '(') {
          var t = linkTarget(s, cb + 1);
          if (t) {
            flush();
            var alt = s.slice(i + 2, cb);
            var url = t.url.trim();
            if (/^data:image\/(png|jpeg|gif|webp);base64,[A-Za-z0-9+/=\s]+$/.test(url)) {
              out += '<img class="md-img" src="' + escapeHtml(url.replace(/\s+/g, '')) + '" alt="' + escapeHtml(alt) + '">';
            } else if (safeUrl(url) && /^https?:/i.test(url)) {
              out += '<a class="md-image-link" href="' + escapeHtml(url) + '" target="_blank" rel="noopener noreferrer">🖼 ' + escapeHtml(alt || url) + '</a>';
            } else {
              out += escapeHtml(alt);
            }
            i = t.end;
            continue;
          }
        }
      }
      if (c === '[') {
        var cb2 = findBracketClose(s, i);
        if (cb2 > 0 && s[cb2 + 1] === '(') {
          var t2 = linkTarget(s, cb2 + 1);
          if (t2) {
            flush();
            var text = inline(s.slice(i + 1, cb2), opts, depth + 1);
            var href = safeUrl(t2.url);
            out += href ? link(href, text) : text;
            i = t2.end;
            continue;
          }
        }
      }
      if (c === '<') {
        var am = /^<((?:https?:\/\/|mailto:)[^\s<>]+)>/i.exec(s.slice(i));
        if (am) { flush(); out += link(am[1], escapeHtml(am[1])); i += am[0].length; continue; }
      }
      if (c === '*' || c === '_' || c === '~') {
        var em = emphasis(s, i, opts, depth);
        if (em) { flush(); out += em.html; i = em.end; continue; }
      }
      if (c === '\n') {             // a line end is a line break, as in a chat; a document wrapped to a width says breaks: false
        flush();
        out += opts.breaks === false ? '\n' : '<br>';
        i++;
        continue;
      }
      buf += c;
      i++;
    }
    flush();
    return out;
  }

  function emphasis(s, i, opts, depth) {
    var c = s[i];
    var k = i;
    while (s[k] === c) k++;
    var run = k - i;
    if (c === '~') {
      if (run !== 2) return null;
      var e = s.indexOf('~~', k);
      if (e < 0 || e === k || isSpace(s[k]) || isSpace(s[e - 1])) return null;
      return { html: '<del>' + inline(s.slice(k, e), opts, depth + 1) + '</del>', end: e + 2 };
    }
    if (isSpace(s[k])) return null;  // not left-flanking
    if (c === '_' && isAlnum(s[i - 1])) return null;  // snake_case stays literal
    var want = Math.min(run, 3);
    var search = k;
    while (search < s.length) {
      var e2 = s.indexOf(c.repeat(want), search);
      if (e2 < 0) break;
      var before = s[e2 - 1];
      var after = s[e2 + want];
      var closeRun = 0, q = e2;
      while (s[q] === c) { closeRun++; q++; }
      var ok = !isSpace(before) && e2 > k && (c !== '_' || !isAlnum(after)) && (closeRun === want || (closeRun > want && want === 3));
      if (ok && s.slice(k, e2).indexOf('\n\n') < 0) {
        var inner = inline(s.slice(k, e2), opts, depth + 1);
        var html = want === 3 ? '<strong><em>' + inner + '</em></strong>' : want === 2 ? '<strong>' + inner + '</strong>' : '<em>' + inner + '</em>';
        var extra = run - want;
        return { html: (extra > 0 ? escapeHtml(c.repeat(extra)) : '') + html, end: e2 + want };
      }
      search = e2 + Math.max(1, closeRun);
    }
    if (run >= 2 && want > 1) {
      // try a shorter delimiter (e.g. **a* b) → emphasis of the inner part)
      return null;
    }
    return null;
  }

  function autolinks(text, opts) {
    // bare URLs, then escape everything else
    var out = '', last = 0, re = /\bhttps?:\/\/[^\s<>"'`]+/gi, m;
    while ((m = re.exec(text))) {
      var prev = text[m.index - 1];
      if (prev && /[\w@/]/.test(prev)) continue;
      var url = trimUrl(m[0]);
      out += escapeHtml(text.slice(last, m.index)) + link(url, escapeHtml(url));
      last = m.index + url.length;
      re.lastIndex = last;
    }
    return out + escapeHtml(text.slice(last));
  }

  function renderInline(src, opts) { return inline(String(src || ''), opts || {}); }

  // ---------------------------------------------------------------------------------------------
  // Blocks
  // ---------------------------------------------------------------------------------------------
  var RE_FENCE = /^( {0,3})(`{3,}|~{3,})\s*([^`\s]*)[^`]*$/;
  var RE_HEADING = /^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*#*[ \t]*$/;
  var RE_HR = /^ {0,3}(?:(?:-[ \t]*){3,}|(?:\*[ \t]*){3,}|(?:_[ \t]*){3,})$/;
  var RE_LIST = /^( *)([-*+]|\d{1,9}[.)])( +|\t|$)(.*)$/;
  var RE_QUOTE = /^ {0,3}> ?/;
  var RE_TABLE_DELIM = /^ *\|? *:?-{1,}:? *(\| *:?-{1,}:? *)*\|? *$/;

  function indentOf(line) { var m = /^ */.exec(line.replace(/\t/g, '    ')); return m[0].length; }
  function blank(line) { return !line || !line.trim(); }

  function splitRow(line) {
    var s = line.trim();
    if (s.startsWith('|')) s = s.slice(1);
    if (s.endsWith('|') && !s.endsWith('\\|')) s = s.slice(0, -1);
    var cells = [], cur = '', inCode = false;
    for (var i = 0; i < s.length; i++) {
      var c = s[i];
      if (c === '\\' && s[i + 1] === '|') { cur += '|'; i++; continue; }
      if (c === '`') inCode = !inCode;
      if (c === '|' && !inCode) { cells.push(cur.trim()); cur = ''; continue; }
      cur += c;
    }
    cells.push(cur.trim());
    return cells;
  }

  function codeBlock(code, lang, opts) {
    lang = (lang || '').toLowerCase().replace(/[^\w+#.-]/g, '');
    if (lang === 'mermaid' && opts.mermaid !== false) {
      var esc = escapeHtml(code);
      return '<div class="mermaid-block" data-src="' + esc + '"><pre class="mermaid-src">' + esc + '</pre></div>';
    }
    var body = null;
    if (opts.highlight) { try { body = opts.highlight(code, lang); } catch (e) { body = null; } }
    if (body == null) body = escapeHtml(code);
    return '<div class="code-block" data-lang="' + escapeHtml(lang) + '"><div class="code-head"><span class="code-lang">'
      + escapeHtml(lang || 'text') + '</span><button type="button" class="code-copy" title="Copy">Copy</button></div><pre><code class="language-'
      + escapeHtml(lang || 'text') + '">' + body + '</code></pre></div>';
  }

  function startsBlock(line) {
    return RE_FENCE.test(line) || RE_HEADING.test(line) || RE_QUOTE.test(line) || RE_HR.test(line)
      || /^ {0,3}\$\$/.test(line) || /^ {0,3}\\\[/.test(line);
  }

  function blocks(lines, opts, depth) {
    depth = depth || 0;
    if (depth > 12) return '<p>' + escapeHtml(lines.join('\n')) + '</p>';
    var out = [], i = 0, n = lines.length;
    while (i < n) {
      var line = lines[i];
      if (blank(line)) { i++; continue; }
      var m;
      // fenced code
      if ((m = RE_FENCE.exec(line))) {
        var fence = m[2], ind = m[1].length, lang = m[3], body = [];
        i++;
        var closed = false;
        while (i < n) {
          var l = lines[i];
          var cm = /^ {0,3}(`{3,}|~{3,})\s*$/.exec(l);
          if (cm && cm[1][0] === fence[0] && cm[1].length >= fence.length) { closed = true; i++; break; }
          body.push(ind ? l.replace(new RegExp('^ {0,' + ind + '}'), '') : l);
          i++;
        }
        out.push(codeBlock(body.join('\n'), lang, opts));
        continue;
      }
      // display math
      if (/^ {0,3}(\$\$|\\\[)/.test(line) && opts.math !== false) {
        var opener = line.trim().startsWith('$$') ? '$$' : '\\[';
        var closer = opener === '$$' ? '$$' : '\\]';
        var rest = line.trim().slice(2);
        var tex = [], done = false;
        if (rest.indexOf(closer) >= 0) {
          var at = rest.indexOf(closer);
          tex.push(rest.slice(0, at));
          done = true;
          i++;
        } else {
          if (rest) tex.push(rest);
          i++;
          while (i < n) {
            var l2 = lines[i];
            var at2 = l2.indexOf(closer);
            if (at2 >= 0) { tex.push(l2.slice(0, at2)); i++; done = true; break; }
            if (!opts.streaming && (blank(l2) || RE_FENCE.test(l2))) break;  // an unclosed block ends here
            tex.push(l2);
            i++;
          }
        }
        if (!done && opts.streaming) { out.push('<p>' + escapeHtml(opener + tex.join('\n')) + '</p>'); continue; }
        out.push('<div class="math-block">' + latexToMathML(tex.join('\n'), true) + '</div>');
        continue;
      }
      if ((m = RE_HEADING.exec(line))) {
        var lvl = m[1].length;
        out.push('<h' + lvl + '>' + inline(m[2] || '', opts) + '</h' + lvl + '>');
        i++;
        continue;
      }
      if (RE_HR.test(line)) { out.push('<hr>'); i++; continue; }
      if (RE_QUOTE.test(line)) {
        var q = [];
        while (i < n && (RE_QUOTE.test(lines[i]) || (!blank(lines[i]) && q.length && !blank(q[q.length - 1]) && !startsBlock(lines[i])))) {
          q.push(lines[i].replace(RE_QUOTE, ''));
          i++;
        }
        out.push('<blockquote>' + blocks(q, opts, depth + 1) + '</blockquote>');
        continue;
      }
      if ((m = RE_LIST.exec(line)) && !(RE_HR.test(line))) {
        var res = list(lines, i, opts, depth);
        out.push(res.html);
        i = res.end;
        continue;
      }
      // table
      if (line.indexOf('|') >= 0 && i + 1 < n && RE_TABLE_DELIM.test(lines[i + 1]) && lines[i + 1].indexOf('-') >= 0) {
        var head = splitRow(line), delim = splitRow(lines[i + 1]);
        if (delim.length >= 1) {
          var aligns = delim.map(function (d) {
            var l = d.startsWith(':'), r = d.endsWith(':');
            return l && r ? 'center' : r ? 'right' : l ? 'left' : '';
          });
          var cell = function (tag, text, j) {
            var a = aligns[j] ? ' style="text-align:' + aligns[j] + '"' : '';
            return '<' + tag + a + '>' + inline(text || '', opts) + '</' + tag + '>';
          };
          var html = '<div class="table-wrap"><table><thead><tr>' + head.map(function (t, j) { return cell('th', t, j); }).join('') + '</tr></thead><tbody>';
          i += 2;
          while (i < n && !blank(lines[i]) && lines[i].indexOf('|') >= 0) {
            var cells = splitRow(lines[i]);
            html += '<tr>' + head.map(function (_, j) { return cell('td', cells[j], j); }).join('') + '</tr>';
            i++;
          }
          out.push(html + '</tbody></table></div>');
          continue;
        }
      }
      // paragraph (with setext headings)
      var para = [line];
      i++;
      while (i < n && !blank(lines[i])) {
        var l3 = lines[i];
        if (/^ {0,3}=+\s*$/.test(l3)) { out.push('<h1>' + inline(para.join('\n'), opts) + '</h1>'); para = null; i++; break; }
        if (/^ {0,3}-+\s*$/.test(l3) && para.length) { out.push('<h2>' + inline(para.join('\n'), opts) + '</h2>'); para = null; i++; break; }
        if (startsBlock(l3) || (RE_LIST.test(l3) && /^( *)([-*+]|1[.)])( +)\S/.test(l3))) break;
        if (l3.indexOf('|') >= 0 && i + 1 < n && RE_TABLE_DELIM.test(lines[i + 1]) && lines[i + 1].indexOf('-') >= 0) break;
        para.push(l3);
        i++;
      }
      if (para) out.push('<p>' + inline(para.map(function (p) { return p.replace(/^ {1,3}/, ''); }).join('\n').replace(/ {2,}\n/g, '\n'), opts) + '</p>');
    }
    return out.join('\n');
  }

  function list(lines, i, opts, depth) {
    var first = RE_LIST.exec(lines[i]);
    var ordered = /\d/.test(first[2]);
    var baseIndent = first[1].length;
    var start = ordered ? parseInt(first[2], 10) : 1;
    var items = [], loose = false, n = lines.length;
    while (i < n) {
      var m = RE_LIST.exec(lines[i]);
      if (!m || m[1].length > baseIndent + 3 || m[1].length < baseIndent || /\d/.test(m[2]) !== ordered || RE_HR.test(lines[i])) break;
      var contentIndent = m[1].length + m[2].length + Math.min(Math.max(m[3].length, 1), 4);
      var itemLines = [m[4]];
      i++;
      var sawBlank = false;
      while (i < n) {
        var l = lines[i];
        if (blank(l)) {
          var j = i + 1;
          while (j < n && blank(lines[j])) j++;
          if (j < n && indentOf(lines[j]) >= contentIndent) { itemLines.push(''); i++; sawBlank = true; continue; }
          if (j < n && RE_LIST.test(lines[j]) && RE_LIST.exec(lines[j])[1].length === baseIndent) loose = true;
          break;
        }
        if (indentOf(l) >= contentIndent) { itemLines.push(l.replace(/\t/g, '    ').slice(contentIndent)); i++; continue; }
        var lm = RE_LIST.exec(l);
        if (lm && lm[1].length < contentIndent) break;
        if (startsBlock(l)) break;
        if (sawBlank) break;
        itemLines.push(l.trim());
        i++;
      }
      if (sawBlank && itemLines.slice(1).some(function (x) { return x === ''; })) loose = true;
      items.push(itemLines);
      if (i < n && blank(lines[i])) {
        var k = i;
        while (k < n && blank(lines[k])) k++;
        if (k < n && RE_LIST.test(lines[k]) && RE_LIST.exec(lines[k])[1].length === baseIndent) { loose = true; i = k; } else break;
      }
    }
    var html = (ordered ? '<ol' + (start !== 1 ? ' start="' + start + '"' : '') + '>' : '<ul>');
    items.forEach(function (itemLines) {
      var task = /^\[([ xX])\] +/.exec(itemLines[0]);
      var cls = '';
      var prefix = '';
      if (task) {
        itemLines[0] = itemLines[0].slice(task[0].length);
        cls = ' class="task"';
        prefix = '<input type="checkbox" disabled' + (task[1] !== ' ' ? ' checked' : '') + '> ';
      }
      var inner = blocks(itemLines, opts, depth + 1);
      if (!loose) inner = inner.replace(/^<p>([\s\S]*?)<\/p>/, '$1').replace(/\n<p>([\s\S]*?)<\/p>/g, '\n$1');
      html += '<li' + cls + '>' + prefix + inner + '</li>';
    });
    html += ordered ? '</ol>' : '</ul>';
    return { html: html, end: i };
  }

  function render(src, opts) {
    opts = opts || {};
    var text = String(src == null ? '' : src).replace(/\r\n?/g, '\n');
    // small models sometimes glue an opening fence to the end of a line: "text```python". A fence that
    // only has spaces before it is indented (inside a list item) and stays where it is.
    text = text.replace(/^([^\n]*[^\s`][ \t]*)(`{3,}[\w+#.-]*)[ \t]*$/gm, '$1\n$2');
    try {
      return blocks(text.split('\n'), opts, 0);
    } catch (e) {
      return '<p>' + escapeHtml(text).replace(/\n/g, '<br>') + '</p>';
    }
  }

  globalThis.BaabaaMarkdown = { render: render, renderInline: renderInline, escapeHtml: escapeHtml, latexToMathML: latexToMathML };
})();
