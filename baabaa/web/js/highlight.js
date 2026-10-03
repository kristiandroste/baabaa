// Syntax highlighter for code blocks. No dependencies. Safe against arbitrary/partial input:
// highlight() never throws and every byte of output is HTML-escaped except the <span> wrappers
// this file adds itself. Classic script: attaches to globalThis so it works from <script src>,
// from `import './highlight.js'` (side effect), and under plain Node for the test file.
(function () {
  'use strict';

  var BT = '`'; // backtick, kept out of template-literal sources below

  // ---- escaping & token rendering ------------------------------------------------------------

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      switch (c) {
        case '&': return '&amp;';
        case '<': return '&lt;';
        case '>': return '&gt;';
        case '"': return '&quot;';
        default: return '&#39;';
      }
    });
  }

  function render(tokens) {
    var out = '';
    for (var i = 0; i < tokens.length; i++) {
      var t = tokens[i];
      var piece = escapeHtml(t.text);
      out += t.cls ? '<span class="hl-' + t.cls + '">' + piece + '</span>' : piece;
    }
    return out;
  }

  // ---- generic named-group scanner -----------------------------------------------------------
  // Each language builds one combined regex `(?<name1>pat1)|(?<name2>pat2)|...` and we walk it
  // with exec()/lastIndex, emitting the unmatched gaps between matches as plain (unclassed) text.
  // This keeps every tokenizer a single linear pass over the whole input.

  function scan(code, re, groupList) {
    var tokens = [];
    var lastIndex = 0;
    var n = code.length;
    re.lastIndex = 0;
    var guard = n * 2 + 1000; // never loop more than this many matches; belt-and-braces only
    var m;
    while (guard-- > 0 && (m = re.exec(code)) !== null) {
      if (m.index > lastIndex) tokens.push({ text: code.slice(lastIndex, m.index), cls: null });
      var cls = null;
      var g = m.groups;
      if (g) {
        for (var i = 0; i < groupList.length; i++) {
          if (g[groupList[i][0]] !== undefined) { cls = groupList[i][1]; break; }
        }
      }
      tokens.push({ text: m[0], cls: cls });
      lastIndex = m.index + m[0].length;
      if (m[0].length === 0) { re.lastIndex = lastIndex + 1; } else { re.lastIndex = lastIndex; }
    }
    if (lastIndex < n) tokens.push({ text: code.slice(lastIndex), cls: null });
    return tokens;
  }

  function build(groupList, flags) {
    var src = groupList.map(function (g) { return '(?<' + g[0] + '>' + g[2] + ')'; }).join('|');
    var re = new RegExp(src, flags || 'gm');
    return function (code) { return scan(code, re, groupList); };
  }

  function kw(name, cls, words) {
    return [name, cls, '\\b(?:' + words.join('|') + ')\\b'];
  }

  function afterKw(word) {
    return String.raw`(?<=\b` + word + String.raw`\s+)[A-Za-z_]\w*`;
  }

  // ---- shared pattern fragments ---------------------------------------------------------------

  var DQ = String.raw`"(?:\\.|[^"\\\n])*"?`;
  var SQ = String.raw`'(?:\\.|[^'\\\n])*'?`;
  var BLOCK_C = String.raw`/\*[\s\S]*?\*/|/\*[\s\S]*`;
  var LINE_SLASH = String.raw`//[^\n]*`;
  var LINE_HASH = String.raw`#[^\n]*`;
  var LINE_DASH = String.raw`--[^\n]*`;
  // Identifier length is capped before a lookahead: an uncapped greedy run (`\w*`) followed by a
  // lookahead that never finds its target (e.g. thousands of identifier chars with no trailing
  // "(") forces the engine to backtrack one character at a time across the whole run — O(n) per
  // start position and O(n^2) overall. Real identifiers are far short of 100 chars.
  var FUNCCALL = String.raw`[A-Za-z_]\w{0,47}(?=\s*\()`;
  var FUNCCALL_DOLLAR = String.raw`[A-Za-z_$][\w$]{0,47}(?=\s*\()`;
  var NUM_GENERIC = String.raw`[+-]?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b`;
  var NUM_C = String.raw`\b0[xX][0-9a-fA-F_]+[uUlL]*\b|\b0[bB][01_]+[uUlL]*\b|\b0[oO][0-7_]+\b|\b\d[\d_]*\.[\d_]+(?:[eE][+-]?\d+)?[fFlLdD]?\b|\b\d[\d_]*[eE][+-]?\d+[fFlLdD]?\b|\b\d[\d_]*[uUlLfFdD]*\b`;
  var TEMPLATE = BT + String.raw`(?:\\[\s\S]|[^` + BT + String.raw`\\])*` + BT + '?';

  // ================================================================================================
  // Python
  // ================================================================================================

  var PY_DOC = String.raw`'''[\s\S]*?'''|"""[\s\S]*?"""|'''[\s\S]*|"""[\s\S]*`;
  var PY_NUM = String.raw`\b0[xX][0-9a-fA-F_]+\b|\b0[bB][01_]+\b|\b0[oO][0-7_]+\b|\b\d[\d_]*\.?[\d_]*(?:[eE][+-]?\d+)?[jJ]?\b|\B\.\d[\d_]*(?:[eE][+-]?\d+)?[jJ]?\b`;
  var pythonKw = ['and', 'as', 'assert', 'async', 'await', 'break', 'class', 'continue', 'def', 'del', 'elif', 'else',
    'except', 'finally', 'for', 'from', 'global', 'if', 'import', 'in', 'is', 'lambda', 'nonlocal', 'not', 'or',
    'pass', 'raise', 'return', 'try', 'while', 'with', 'yield', 'match', 'case'];
  var pythonTypes = ['int', 'str', 'float', 'bool', 'list', 'dict', 'set', 'tuple', 'object', 'bytes', 'frozenset', 'complex', 'bytearray'];
  var python = build([
    ['cmt', 'c', LINE_HASH],
    ['doc', 's', PY_DOC],
    ['str', 's', DQ + '|' + SQ],
    ['dec', 'm', String.raw`@[A-Za-z_][\w.]*`],
    ['num', 'n', PY_NUM],
    ['bool', 'b', String.raw`\b(?:True|False|None)\b`],
    kw('kwd', 'k', pythonKw),
    ['klass', 't', afterKw('class')],
    ['fdef', 'f', afterKw('def')],
    kw('btype', 't', pythonTypes),
    ['fcall', 'f', FUNCCALL],
  ]);

  // ================================================================================================
  // JavaScript / TypeScript
  // ================================================================================================

  var REGEX_PRECEDE = String.raw`(?:[-+*/%=&|^!~<>?:;,([{]|^|\b(?:return|typeof|case|delete|void|throw|yield|new|instanceof|in|of|await))\s{0,50}`;
  var JS_REGEX = String.raw`(?<=` + REGEX_PRECEDE + String.raw`)/(?:\\.|\[(?:\\.|[^\]\\\n])*\]|[^/\\\n])*/[a-zA-Z]*`;
  var JS_NUM = String.raw`\b0[xX][0-9a-fA-F_]+n?\b|\b0[bB][01_]+n?\b|\b0[oO][0-7_]+n?\b|\b\d[\d_]*\.?[\d_]*(?:[eE][+-]?\d+)?n?\b|\B\.\d[\d_]*(?:[eE][+-]?\d+)?\b`;

  function jsFamily(isTs) {
    var keywords = ['break', 'case', 'catch', 'class', 'const', 'continue', 'debugger', 'default', 'delete', 'do',
      'else', 'export', 'extends', 'finally', 'for', 'function', 'if', 'import', 'in', 'instanceof', 'new', 'of',
      'return', 'super', 'switch', 'this', 'throw', 'try', 'typeof', 'var', 'let', 'void', 'while', 'with',
      'yield', 'async', 'await', 'static', 'get', 'set', 'from', 'as'];
    var types = ['String', 'Number', 'Boolean', 'Array', 'Object', 'Promise', 'Map', 'Set', 'WeakMap', 'WeakSet',
      'Symbol', 'RegExp', 'Error', 'TypeError', 'RangeError', 'Date', 'Math', 'JSON', 'Proxy', 'Reflect'];
    if (isTs) {
      keywords = keywords.concat(['interface', 'type', 'enum', 'implements', 'namespace', 'declare', 'module',
        'public', 'private', 'protected', 'readonly', 'abstract', 'is', 'keyof', 'infer', 'satisfies', 'override']);
      types = types.concat(['string', 'number', 'boolean', 'any', 'unknown', 'never', 'void', 'object', 'symbol', 'bigint']);
    }
    var groups = [
      ['cmt', 'c', LINE_SLASH + '|' + BLOCK_C],
      ['tmpl', 's', TEMPLATE],
      ['str', 's', DQ + '|' + SQ],
      ['rgx', 'r', JS_REGEX],
      ['dec', 'm', String.raw`@[A-Za-z_$][\w$]*`],
      ['num', 'n', JS_NUM],
      ['bool', 'b', String.raw`\b(?:true|false|null|undefined|NaN|Infinity)\b`],
      kw('kwd', 'k', keywords),
      ['klass', 't', String.raw`(?<=\bclass\s+)[A-Za-z_$][\w$]*`],
      ['fdef', 'f', String.raw`(?<=\bfunction\*?\s+)[A-Za-z_$][\w$]*`],
      kw('btype', 't', types),
      ['fcall', 'f', FUNCCALL_DOLLAR],
    ];
    return build(groups, 'gm');
  }

  var javascript = jsFamily(false);
  var typescript = jsFamily(true);

  // ================================================================================================
  // Bash
  // ================================================================================================

  var bashKw = ['if', 'then', 'else', 'elif', 'fi', 'for', 'while', 'until', 'do', 'done', 'case', 'esac',
    'function', 'select', 'in', 'time', 'return', 'exit', 'break', 'continue', 'local', 'export', 'readonly',
    'declare', 'unset', 'shift', 'trap', 'eval', 'source'];
  var bash = build([
    ['cmt', 'c', String.raw`(?<=^|[\s;&|(){}` + BT + String.raw`])#[^\n]*`],
    ['str', 's', DQ + '|' + SQ],
    ['var', 'v', String.raw`\$\{[^}\n]*\}|\$\w+|\$[0-9@*#?$!_-]`],
    kw('kwd', 'k', bashKw),
    ['bool', 'b', String.raw`\b(?:true|false)\b`],
    ['flag', 'a', String.raw`(?<=\s|^)--?[A-Za-z][\w-]*`],
    ['num', 'n', String.raw`\b\d+\b`],
  ]);

  // ================================================================================================
  // HTML / XML / SVG — hand-rolled scanner (tag structure needs state, not a flat regex)
  // ================================================================================================

  // Sticky ('y') regexes match only at re.lastIndex, so the tag body is scanned by moving an
  // index through the ORIGINAL slice instead of calling s.slice(i) (and re-copying the remaining
  // tail) for every whitespace run / attribute / value — that repeated slicing is O(len^2) on a
  // tag with many attributes.
  var TAG_OPEN_Y = /<\/?/y;
  var TAG_NAME_Y = /[A-Za-z][\w:-]*/y;
  var WS_Y = /\s+/y;
  var ATTR_NAME_Y = /[A-Za-z_:][\w:.-]*/y;
  var ATTR_EQ_Y = /\s*=\s*/y;
  var DQ_VAL_Y = /"[^"]*"?/y;
  var SQ_VAL_Y = /'[^']*'?/y;
  var BARE_VAL_Y = /[^\s>]+/y;

  function stickyMatch(re, s, i) {
    re.lastIndex = i;
    var m = re.exec(s);
    return m && m.index === i ? m[0] : null;
  }

  function tokenizeTagInto(tokens, s) {
    if (/^<!DOCTYPE/i.test(s)) { tokens.push({ text: s, cls: 'm' }); return; }
    if (s.indexOf('<![CDATA[') === 0) { tokens.push({ text: s, cls: 's' }); return; }
    var n = s.length;
    var open = stickyMatch(TAG_OPEN_Y, s, 0) || '';
    tokens.push({ text: open, cls: null });
    var i = open.length;
    var name = stickyMatch(TAG_NAME_Y, s, i);
    if (name) { tokens.push({ text: name, cls: 'g' }); i += name.length; }
    while (i < n) {
      var ws = stickyMatch(WS_Y, s, i);
      if (ws) { tokens.push({ text: ws, cls: null }); i += ws.length; continue; }
      var ch = s.charAt(i);
      if (ch === '>' || (ch === '/' && s.charAt(i + 1) === '>')) {
        tokens.push({ text: s.slice(i), cls: null });
        break;
      }
      var attr = stickyMatch(ATTR_NAME_Y, s, i);
      if (attr) {
        tokens.push({ text: attr, cls: 'a' });
        i += attr.length;
        var eq = stickyMatch(ATTR_EQ_Y, s, i);
        if (eq) {
          tokens.push({ text: eq, cls: null });
          i += eq.length;
          var val = stickyMatch(DQ_VAL_Y, s, i) || stickyMatch(SQ_VAL_Y, s, i) || stickyMatch(BARE_VAL_Y, s, i);
          if (val) { tokens.push({ text: val, cls: 's' }); i += val.length; }
        }
        continue;
      }
      tokens.push({ text: ch, cls: null });
      i++;
    }
  }

  var ENTITY_RE = /^&(?:#\d+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);/;

  function tokenizeHtml(code) {
    var tokens = [];
    var i = 0;
    var n = code.length;
    while (i < n) {
      if (code.indexOf('<!--', i) === i) {
        var end = code.indexOf('-->', i + 4);
        end = end === -1 ? n : end + 3;
        tokens.push({ text: code.slice(i, end), cls: 'c' });
        i = end;
        continue;
      }
      if (code.charAt(i) === '<') {
        var j = i + 1;
        var tagEnd = -1;
        var inQuote = '';
        for (; j < n; j++) {
          var ch = code.charAt(j);
          if (inQuote) { if (ch === inQuote) inQuote = ''; }
          else if (ch === '"' || ch === "'") { inQuote = ch; }
          else if (ch === '>') { tagEnd = j; break; }
          else if (ch === '<') { break; } // unterminated tag: stop before a new one starts
        }
        if (tagEnd === -1) {
          var stop = j < n ? j : n;
          tokens.push({ text: code.slice(i, stop), cls: 'g' });
          i = stop;
          continue;
        }
        tokenizeTagInto(tokens, code.slice(i, tagEnd + 1));
        i = tagEnd + 1;
        continue;
      }
      if (code.charAt(i) === '&') {
        var em = ENTITY_RE.exec(code.slice(i, i + 32));
        if (em) { tokens.push({ text: em[0], cls: 'm' }); i += em[0].length; continue; }
      }
      var k = i + 1;
      while (k < n && code.charAt(k) !== '<' && code.charAt(k) !== '&') k++;
      tokens.push({ text: code.slice(i, k), cls: null });
      i = k;
    }
    return tokens;
  }

  // ================================================================================================
  // CSS (scss/less share this — property/value distinctions are approximate for preprocessors)
  // ================================================================================================

  var cssKeywords = ['important', 'inherit', 'initial', 'unset', 'auto', 'none', 'solid', 'dashed', 'dotted',
    'double', 'groove', 'ridge', 'bold', 'bolder', 'lighter', 'italic', 'oblique', 'normal', 'flex', 'inline-flex',
    'grid', 'inline-grid', 'block', 'inline', 'inline-block', 'contents', 'absolute', 'relative', 'fixed',
    'sticky', 'static', 'center', 'left', 'right', 'top', 'bottom', 'middle', 'baseline', 'stretch',
    'space-between', 'space-around', 'space-evenly', 'row', 'column', 'wrap', 'nowrap', 'hidden', 'visible',
    'scroll', 'pointer', 'transparent', 'uppercase', 'lowercase', 'capitalize', 'underline', 'line-through'];
  var css = build([
    ['cmt', 'c', BLOCK_C],
    ['str', 's', DQ + '|' + SQ],
    ['atrule', 'm', String.raw`@[A-Za-z-]+`],
    ['color', 'n', String.raw`#[0-9a-fA-F]{3,8}\b`],
    ['prop', 'a', String.raw`(?<=(?:^|[{;])[ \t]*)[A-Za-z-][A-Za-z0-9-]{0,63}(?=\s*:)`],
    ['num', 'n', String.raw`[+-]?\b\d+(?:\.\d+)?(?:px|em|rem|%|vh|vw|vmin|vmax|pt|pc|in|cm|mm|s|ms|deg|rad|turn|fr|ex|ch)?\b`],
    kw('kwd', 'k', cssKeywords),
  ]);

  // ================================================================================================
  // JSON (jsonc/json5 share this — // and /* */ comments tolerated as a superset of strict JSON)
  // ================================================================================================

  var json = build([
    ['cmt', 'c', LINE_SLASH + '|' + BLOCK_C],
    ['key', 'a', DQ + String.raw`(?=\s*:)`],
    ['str', 's', DQ],
    ['num', 'n', String.raw`-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b`],
    ['bool', 'b', String.raw`\b(?:true|false|null)\b`],
  ]);

  // ================================================================================================
  // YAML
  // ================================================================================================

  var yaml = build([
    ['cmt', 'c', LINE_HASH],
    ['str', 's', DQ + '|' + SQ],
    ['doc', 'm', String.raw`^(?:---|\.\.\.)[ \t]*$`],
    ['anchor', 'v', String.raw`[&*][A-Za-z_][\w-]*`],
    ['key', 'a', String.raw`(?<=^[ \t]*(?:-[ \t]+)?)[A-Za-z_][\w.-]{0,98}(?=[ \t]*:(?:\s|$))`],
    ['bool', 'b', String.raw`\b(?:true|True|TRUE|false|False|FALSE|null|Null|NULL|yes|Yes|YES|no|No|NO|~)\b`],
    ['num', 'n', NUM_GENERIC],
  ]);

  // ================================================================================================
  // TOML (also ini/cfg/conf)
  // ================================================================================================

  var toml = build([
    ['cmt', 'c', LINE_HASH + '|' + String.raw`;[^\n]*`],
    ['section', 'm', String.raw`^\[[^\]\n]*\]`],
    ['str', 's', String.raw`"""[\s\S]*?"""|"""[\s\S]*|` + DQ + String.raw`|'''[\s\S]*?'''|'''[\s\S]*|'[^'\n]*'?`],
    ['key', 'a', String.raw`(?<=^[ \t]*)[A-Za-z0-9_.-]{1,99}(?=[ \t]*=)`],
    ['bool', 'b', String.raw`\b(?:true|false)\b`],
    ['num', 'n', NUM_GENERIC],
  ]);

  // ================================================================================================
  // Markdown — line-oriented with fence tracking; inline code spans and links
  // ================================================================================================

  var MD_INLINE = /(?<code>`(?:[^`\n]|``)*`?)|(?<link>\[[^\]\n]*\]\([^)\n]*\))/g;
  var MD_INLINE_GROUPS = [['code', 's'], ['link', 'a']];

  function tokenizeMarkdown(code) {
    var tokens = [];
    var lines = code.split('\n');
    var inFence = false;
    for (var idx = 0; idx < lines.length; idx++) {
      var line = lines[idx];
      if (/^ {0,3}(```+|~~~+)/.test(line)) {
        tokens.push({ text: line, cls: 's' });
        inFence = !inFence;
      } else if (inFence) {
        tokens.push({ text: line, cls: 's' });
      } else if (/^ {0,3}#{1,6}(\s|$)/.test(line)) {
        tokens.push({ text: line, cls: 'm' });
      } else if (/^ {0,3}>/.test(line)) {
        tokens.push({ text: line, cls: 'm' });
      } else {
        var inline = scan(line, MD_INLINE, MD_INLINE_GROUPS);
        for (var k = 0; k < inline.length; k++) tokens.push(inline[k]);
      }
      if (idx < lines.length - 1) tokens.push({ text: '\n', cls: null });
    }
    return tokens;
  }

  // ================================================================================================
  // Diff / patch — classified purely by line prefix
  // ================================================================================================

  function tokenizeDiff(code) {
    var tokens = [];
    var lines = code.split('\n');
    for (var idx = 0; idx < lines.length; idx++) {
      var line = lines[idx];
      var cls = null;
      if (line.indexOf('@@') === 0) cls = 'meta';
      else if (line.indexOf('+++') === 0 || line.indexOf('---') === 0) cls = 'meta';
      else if (line.indexOf('diff ') === 0 || line.indexOf('index ') === 0) cls = 'meta';
      else if (line.charAt(0) === '+') cls = 'add';
      else if (line.charAt(0) === '-') cls = 'del';
      tokens.push({ text: line, cls: cls });
      if (idx < lines.length - 1) tokens.push({ text: '\n', cls: null });
    }
    return tokens;
  }

  // ================================================================================================
  // C / C++
  // ================================================================================================

  var cKw = ['auto', 'break', 'case', 'const', 'continue', 'default', 'do', 'else', 'enum', 'extern', 'for',
    'goto', 'if', 'inline', 'register', 'return', 'sizeof', 'static', 'struct', 'switch', 'typedef', 'union',
    'volatile', 'while', 'restrict'];
  var cTypes = ['int', 'char', 'float', 'double', 'void', 'long', 'short', 'unsigned', 'signed', 'bool', 'size_t',
    'ssize_t', 'int8_t', 'int16_t', 'int32_t', 'int64_t', 'uint8_t', 'uint16_t', 'uint32_t', 'uint64_t', 'FILE', 'wchar_t'];
  var c = build([
    ['cmt', 'c', LINE_SLASH + '|' + BLOCK_C],
    ['str', 's', DQ],
    ['chr', 's', SQ],
    ['pre', 'm', String.raw`^[ \t]*#[ \t]*\w+.*`],
    ['num', 'n', NUM_C],
    kw('kwd', 'k', cKw),
    ['bool', 'b', String.raw`\b(?:true|false|NULL)\b`],
    kw('btype', 't', cTypes),
    ['fcall', 'f', FUNCCALL],
  ]);

  var cppKw = cKw.concat(['class', 'public', 'private', 'protected', 'virtual', 'friend', 'template', 'typename',
    'namespace', 'using', 'new', 'delete', 'try', 'catch', 'throw', 'this', 'operator', 'explicit', 'mutable',
    'override', 'final', 'constexpr', 'decltype', 'noexcept', 'static_cast', 'dynamic_cast', 'const_cast', 'reinterpret_cast']);
  var cppTypes = cTypes.concat(['string', 'vector', 'map', 'unordered_map', 'unordered_set', 'pair', 'unique_ptr', 'shared_ptr', 'weak_ptr']);
  var cpp = build([
    ['cmt', 'c', LINE_SLASH + '|' + BLOCK_C],
    ['str', 's', DQ],
    ['chr', 's', SQ],
    ['pre', 'm', String.raw`^[ \t]*#[ \t]*\w+.*`],
    ['num', 'n', NUM_C],
    kw('kwd', 'k', cppKw),
    ['bool', 'b', String.raw`\b(?:true|false|nullptr|NULL)\b`],
    ['klass', 't', String.raw`(?<=\b(?:class|struct)\s+)[A-Za-z_]\w*`],
    kw('btype', 't', cppTypes),
    ['fcall', 'f', FUNCCALL],
  ]);

  // ================================================================================================
  // Java
  // ================================================================================================

  var javaKw = ['abstract', 'assert', 'boolean', 'break', 'byte', 'case', 'catch', 'char', 'class', 'const',
    'continue', 'default', 'do', 'double', 'else', 'enum', 'extends', 'final', 'finally', 'float', 'for', 'goto',
    'if', 'implements', 'import', 'instanceof', 'int', 'interface', 'long', 'native', 'new', 'package', 'private',
    'protected', 'public', 'return', 'short', 'static', 'strictfp', 'super', 'switch', 'synchronized', 'this',
    'throw', 'throws', 'transient', 'try', 'void', 'volatile', 'while', 'var', 'record', 'sealed', 'permits', 'yield'];
  var javaTypes = ['String', 'Integer', 'Long', 'Double', 'Float', 'Boolean', 'Character', 'Byte', 'Short',
    'Object', 'List', 'Map', 'Set', 'ArrayList', 'HashMap', 'Optional'];
  var java = build([
    ['cmt', 'c', LINE_SLASH + '|' + BLOCK_C],
    ['str', 's', DQ],
    ['chr', 's', SQ],
    ['dec', 'm', String.raw`@[A-Za-z_][\w.]*`],
    ['num', 'n', NUM_C],
    kw('kwd', 'k', javaKw),
    ['bool', 'b', String.raw`\b(?:true|false|null)\b`],
    ['klass', 't', String.raw`(?<=\b(?:class|interface|enum)\s+)[A-Za-z_]\w*`],
    kw('btype', 't', javaTypes),
    ['fcall', 'f', FUNCCALL],
  ]);

  // ================================================================================================
  // Go
  // ================================================================================================

  var goKw = ['break', 'case', 'chan', 'const', 'continue', 'default', 'defer', 'else', 'fallthrough', 'for',
    'func', 'go', 'goto', 'if', 'import', 'interface', 'map', 'package', 'range', 'return', 'select', 'struct',
    'switch', 'type', 'var'];
  var goTypes = ['bool', 'string', 'int', 'int8', 'int16', 'int32', 'int64', 'uint', 'uint8', 'uint16', 'uint32',
    'uint64', 'uintptr', 'byte', 'rune', 'float32', 'float64', 'complex64', 'complex128', 'error', 'any'];
  var GO_RAW = BT + String.raw`[^` + BT + String.raw`]*` + BT + '?';
  var go = build([
    ['cmt', 'c', LINE_SLASH + '|' + BLOCK_C],
    ['str', 's', DQ],
    ['raw', 's', GO_RAW],
    ['chr', 's', SQ],
    ['num', 'n', NUM_C],
    kw('kwd', 'k', goKw),
    ['bool', 'b', String.raw`\b(?:true|false|nil|iota)\b`],
    ['tdef', 't', afterKw('type')],
    ['fdef', 'f', afterKw('func')],
    kw('btype', 't', goTypes),
    ['fcall', 'f', FUNCCALL],
  ]);

  // ================================================================================================
  // Rust
  // ================================================================================================

  var rustKw = ['as', 'async', 'await', 'break', 'const', 'continue', 'crate', 'dyn', 'else', 'enum', 'extern',
    'fn', 'for', 'if', 'impl', 'in', 'let', 'loop', 'match', 'mod', 'move', 'mut', 'pub', 'ref', 'return', 'self',
    'Self', 'static', 'struct', 'super', 'trait', 'type', 'unsafe', 'use', 'where', 'while'];
  var rustTypes = ['i8', 'i16', 'i32', 'i64', 'i128', 'isize', 'u8', 'u16', 'u32', 'u64', 'u128', 'usize', 'f32',
    'f64', 'bool', 'char', 'str', 'String', 'Vec', 'Option', 'Result', 'Box', 'Rc', 'Arc'];
  var rust = build([
    ['cmt', 'c', LINE_SLASH + '|' + BLOCK_C],
    ['str', 's', DQ],
    ['chr', 's', SQ],
    ['attr', 'm', String.raw`#!?\[[^\]\n]*\]?`],
    ['num', 'n', NUM_C],
    ['bool', 'b', String.raw`\b(?:true|false)\b`],
    kw('kwd', 'k', rustKw),
    ['tdef', 't', String.raw`(?<=\b(?:struct|enum|trait)\s+)[A-Za-z_]\w*`],
    ['fdef', 'f', afterKw('fn')],
    kw('btype', 't', rustTypes),
    ['fcall', 'f', FUNCCALL],
  ]);

  // ================================================================================================
  // C#
  // ================================================================================================

  var csKw = ['abstract', 'as', 'base', 'break', 'case', 'catch', 'checked', 'class', 'const', 'continue',
    'decimal', 'default', 'delegate', 'do', 'else', 'enum', 'event', 'explicit', 'extern', 'finally', 'fixed',
    'for', 'foreach', 'goto', 'if', 'implicit', 'in', 'interface', 'internal', 'is', 'lock', 'namespace', 'new',
    'object', 'operator', 'out', 'override', 'params', 'private', 'protected', 'public', 'readonly', 'ref',
    'return', 'sealed', 'sizeof', 'stackalloc', 'static', 'struct', 'switch', 'this', 'throw', 'try', 'typeof',
    'unchecked', 'unsafe', 'using', 'virtual', 'void', 'volatile', 'while', 'var', 'async', 'await', 'get',
    'set', 'yield', 'record'];
  var csTypes = ['string', 'int', 'long', 'short', 'byte', 'bool', 'double', 'float', 'decimal', 'char', 'object',
    'List', 'Dictionary', 'String', 'Int32', 'Boolean'];
  var csharp = build([
    ['cmt', 'c', LINE_SLASH + '|' + BLOCK_C],
    ['str', 's', DQ],
    ['chr', 's', SQ],
    ['dec', 'm', String.raw`^[ \t]*\[[A-Za-z_][\w.]*(?:\([^\]]*\))?\]`],
    ['num', 'n', NUM_C],
    kw('kwd', 'k', csKw),
    ['bool', 'b', String.raw`\b(?:true|false|null)\b`],
    ['klass', 't', String.raw`(?<=\b(?:class|interface|struct)\s+)[A-Za-z_]\w*`],
    kw('btype', 't', csTypes),
    ['fcall', 'f', FUNCCALL],
  ]);

  // ================================================================================================
  // Kotlin
  // ================================================================================================

  var ktKw = ['as', 'break', 'class', 'continue', 'do', 'else', 'for', 'fun', 'if', 'in', 'interface', 'is',
    'object', 'package', 'return', 'super', 'this', 'throw', 'try', 'typealias', 'typeof', 'val', 'var', 'when',
    'while', 'by', 'catch', 'constructor', 'finally', 'get', 'import', 'init', 'set', 'where', 'actual',
    'abstract', 'annotation', 'companion', 'const', 'crossinline', 'data', 'enum', 'expect', 'external', 'final',
    'infix', 'inline', 'inner', 'internal', 'lateinit', 'noinline', 'open', 'operator', 'out', 'override',
    'private', 'protected', 'public', 'reified', 'sealed', 'suspend', 'tailrec', 'vararg'];
  var ktTypes = ['Int', 'Long', 'Short', 'Byte', 'Float', 'Double', 'Boolean', 'Char', 'String', 'Unit', 'Any',
    'Nothing', 'List', 'Map', 'Set', 'Array'];
  var kotlin = build([
    ['cmt', 'c', LINE_SLASH + '|' + BLOCK_C],
    ['str', 's', DQ],
    ['chr', 's', SQ],
    ['dec', 'm', String.raw`@[A-Za-z_][\w.]*`],
    ['num', 'n', NUM_C],
    kw('kwd', 'k', ktKw),
    ['bool', 'b', String.raw`\b(?:true|false|null)\b`],
    ['klass', 't', String.raw`(?<=\b(?:class|interface|object)\s+)[A-Za-z_]\w*`],
    ['fdef', 'f', afterKw('fun')],
    kw('btype', 't', ktTypes),
    ['fcall', 'f', FUNCCALL],
  ]);

  // ================================================================================================
  // Swift
  // ================================================================================================

  var swiftKw = ['associatedtype', 'class', 'deinit', 'enum', 'extension', 'fileprivate', 'func', 'import', 'init',
    'inout', 'internal', 'let', 'open', 'operator', 'private', 'protocol', 'public', 'rethrows', 'static',
    'struct', 'subscript', 'typealias', 'var', 'break', 'case', 'continue', 'default', 'defer', 'do', 'else',
    'fallthrough', 'for', 'guard', 'if', 'in', 'repeat', 'return', 'switch', 'where', 'while', 'as', 'catch',
    'is', 'super', 'self', 'Self', 'throw', 'throws', 'try'];
  var swiftTypes = ['Int', 'Double', 'Float', 'Bool', 'String', 'Character', 'Array', 'Dictionary', 'Set', 'Optional', 'Any'];
  var swift = build([
    ['cmt', 'c', LINE_SLASH + '|' + BLOCK_C],
    ['str', 's', DQ],
    ['dec', 'm', String.raw`@[A-Za-z_][\w.]*`],
    ['num', 'n', NUM_C],
    kw('kwd', 'k', swiftKw),
    ['bool', 'b', String.raw`\b(?:true|false|nil)\b`],
    ['klass', 't', String.raw`(?<=\b(?:class|struct|enum|protocol)\s+)[A-Za-z_]\w*`],
    ['fdef', 'f', afterKw('func')],
    kw('btype', 't', swiftTypes),
    ['fcall', 'f', FUNCCALL],
  ]);

  // ================================================================================================
  // PHP
  // ================================================================================================

  var phpKw = ['abstract', 'and', 'array', 'as', 'break', 'callable', 'case', 'catch', 'class', 'clone', 'const',
    'continue', 'declare', 'default', 'do', 'echo', 'else', 'elseif', 'empty', 'extends', 'final', 'finally',
    'fn', 'for', 'foreach', 'function', 'global', 'goto', 'if', 'implements', 'include', 'include_once',
    'instanceof', 'insteadof', 'interface', 'isset', 'list', 'match', 'namespace', 'new', 'or', 'print',
    'private', 'protected', 'public', 'require', 'require_once', 'return', 'static', 'switch', 'throw', 'trait',
    'try', 'unset', 'use', 'var', 'while', 'xor', 'yield'];
  var phpTypes = ['int', 'float', 'string', 'bool', 'array', 'object', 'callable', 'iterable', 'void', 'mixed', 'self', 'parent'];
  var php = build([
    ['cmt', 'c', LINE_HASH + '|' + LINE_SLASH + '|' + BLOCK_C],
    ['str', 's', DQ + '|' + SQ],
    ['var', 'v', String.raw`\$[A-Za-z_]\w*`],
    ['num', 'n', NUM_C],
    ['bool', 'b', String.raw`\b(?:true|false|null)\b`],
    kw('kwd', 'k', phpKw),
    ['klass', 't', afterKw('class')],
    ['fdef', 'f', afterKw('function')],
    kw('btype', 't', phpTypes),
    ['fcall', 'f', FUNCCALL],
  ], 'gmi');

  // ================================================================================================
  // Ruby
  // ================================================================================================

  var rubyKw = ['begin', 'end', 'def', 'class', 'module', 'if', 'unless', 'else', 'elsif', 'while', 'until',
    'for', 'in', 'do', 'case', 'when', 'then', 'yield', 'return', 'break', 'next', 'redo', 'retry', 'rescue',
    'ensure', 'raise', 'require', 'require_relative', 'include', 'extend', 'self', 'super', 'and', 'or', 'not',
    'lambda', 'proc', 'attr_accessor', 'attr_reader', 'attr_writer'];
  var ruby = build([
    ['cmt', 'c', LINE_HASH],
    ['str', 's', DQ + '|' + SQ],
    ['num', 'n', NUM_C],
    ['bool', 'b', String.raw`\b(?:true|false|nil)\b`],
    kw('kwd', 'k', rubyKw),
    ['klass', 't', afterKw('class')],
    ['mod', 't', afterKw('module')],
    ['fdef', 'f', afterKw('def')],
    ['ivar', 'v', String.raw`@{1,2}[A-Za-z_]\w*|\$[A-Za-z_]\w*`],
    ['fcall', 'f', FUNCCALL],
  ]);

  // ================================================================================================
  // Lua
  // ================================================================================================

  var luaKw = ['and', 'break', 'do', 'else', 'elseif', 'end', 'for', 'function', 'goto', 'if', 'in', 'local',
    'not', 'or', 'repeat', 'return', 'then', 'until', 'while'];
  var lua = build([
    ['cmtblock', 'c', String.raw`--\[\[[\s\S]*?\]\]|--\[\[[\s\S]*`],
    ['cmt', 'c', String.raw`--[^\n]*`],
    ['str', 's', DQ + '|' + SQ],
    ['num', 'n', NUM_C],
    kw('kwd', 'k', luaKw),
    ['bool', 'b', String.raw`\b(?:true|false|nil)\b`],
    ['fdef', 'f', afterKw('function')],
    ['fcall', 'f', FUNCCALL],
  ]);

  // ================================================================================================
  // R
  // ================================================================================================

  var rKw = ['if', 'else', 'repeat', 'while', 'function', 'for', 'in', 'next', 'break', 'library', 'require'];
  var rLang = build([
    ['cmt', 'c', LINE_HASH],
    ['str', 's', DQ + '|' + SQ],
    ['num', 'n', String.raw`\b\d+(?:\.\d+)?[Li]?\b|\B\.\d+\b`],
    kw('kwd', 'k', rKw),
    ['bool', 'b', String.raw`\b(?:TRUE|FALSE|NULL|NA|Inf|NaN|T|F)\b`],
    ['fcall', 'f', FUNCCALL],
  ]);

  // ================================================================================================
  // SQL
  // ================================================================================================

  var sqlKw = ['select', 'from', 'where', 'insert', 'into', 'values', 'update', 'set', 'delete', 'create',
    'table', 'alter', 'drop', 'index', 'view', 'join', 'inner', 'left', 'right', 'full', 'outer', 'on', 'group',
    'by', 'order', 'having', 'limit', 'offset', 'union', 'all', 'distinct', 'as', 'and', 'or', 'not', 'is', 'in',
    'like', 'between', 'exists', 'case', 'when', 'then', 'else', 'end', 'primary', 'key', 'foreign', 'references',
    'default', 'constraint', 'unique', 'check', 'with', 'cascade', 'begin', 'commit', 'rollback', 'transaction',
    'procedure', 'function', 'trigger', 'database', 'schema'];
  var sql = build([
    ['cmt', 'c', LINE_DASH + '|' + BLOCK_C],
    ['qid', 'a', String.raw`"(?:""|[^"])*"?`],
    ['str', 's', String.raw`'(?:''|[^'])*'?`],
    ['num', 'n', NUM_GENERIC],
    ['bool', 'b', String.raw`\b(?:null|true|false)\b`],
    kw('kwd', 'k', sqlKw),
    ['fcall', 'f', FUNCCALL],
  ], 'gim');

  // ================================================================================================
  // Dockerfile
  // ================================================================================================

  var dockerfileKw = ['FROM', 'RUN', 'CMD', 'LABEL', 'MAINTAINER', 'EXPOSE', 'ENV', 'ADD', 'COPY', 'ENTRYPOINT',
    'VOLUME', 'USER', 'WORKDIR', 'ARG', 'ONBUILD', 'STOPSIGNAL', 'HEALTHCHECK', 'SHELL'];
  var dockerfile = build([
    ['cmt', 'c', LINE_HASH],
    ['str', 's', DQ + '|' + SQ],
    ['var', 'v', String.raw`\$\{[^}\n]*\}|\$[A-Za-z_][A-Za-z0-9_]*`],
    kw('kwd', 'k', dockerfileKw),
  ], 'gmi');

  // ================================================================================================
  // Makefile
  // ================================================================================================

  var makefileKw = ['ifeq', 'ifneq', 'ifdef', 'ifndef', 'else', 'endif', 'include', 'define', 'endef', 'export',
    'unexport', 'override', 'vpath'];
  var makefile = build([
    ['cmt', 'c', LINE_HASH],
    ['str', 's', DQ + '|' + SQ],
    ['var', 'v', String.raw`\$\([^)\n]*\)|\$\{[^}\n]*\}|\$[A-Za-z_@^<*?%+]`],
    ['dotdir', 'k', String.raw`\.(?:PHONY|SUFFIXES|DEFAULT|PRECIOUS|INTERMEDIATE|SECONDARY|DELETE_ON_ERROR|EXPORT_ALL_VARIABLES|NOTPARALLEL)\b`],
    kw('kwd', 'k', makefileKw),
    ['target', 'f', String.raw`^[A-Za-z0-9_.%/+-]{1,99}(?=\s*:(?!=))`],
  ]);

  // ---- registry ---------------------------------------------------------------------------------

  var LANGUAGES = ['python', 'javascript', 'typescript', 'bash', 'html', 'css', 'c', 'cpp', 'java', 'go', 'rust',
    'sql', 'yaml', 'toml', 'json', 'markdown', 'diff', 'ruby', 'php', 'swift', 'kotlin', 'csharp', 'lua', 'r',
    'dockerfile', 'makefile'];

  var TOKENIZERS = {
    python: python, javascript: javascript, typescript: typescript, bash: bash, html: tokenizeHtml, css: css,
    c: c, cpp: cpp, java: java, go: go, rust: rust, sql: sql, yaml: yaml, toml: toml, json: json,
    markdown: tokenizeMarkdown, diff: tokenizeDiff, ruby: ruby, php: php, swift: swift, kotlin: kotlin,
    csharp: csharp, lua: lua, r: rLang, dockerfile: dockerfile, makefile: makefile,
  };

  var ALIASES = {
    py: 'python', python3: 'python', python: 'python',
    js: 'javascript', javascript: 'javascript', mjs: 'javascript', cjs: 'javascript', jsx: 'javascript',
    ts: 'typescript', typescript: 'typescript', tsx: 'typescript',
    sh: 'bash', bash: 'bash', shell: 'bash', zsh: 'bash', console: 'bash',
    html: 'html', xml: 'html', svg: 'html', xhtml: 'html',
    css: 'css', scss: 'css', less: 'css',
    c: 'c', h: 'c',
    cpp: 'cpp', 'c++': 'cpp', cc: 'cpp', hpp: 'cpp', cxx: 'cpp',
    java: 'java',
    go: 'go', golang: 'go',
    rs: 'rust', rust: 'rust',
    sql: 'sql',
    yaml: 'yaml', yml: 'yaml',
    toml: 'toml', ini: 'toml', cfg: 'toml', conf: 'toml',
    json: 'json', jsonc: 'json', json5: 'json',
    md: 'markdown', markdown: 'markdown',
    diff: 'diff', patch: 'diff',
    rb: 'ruby', ruby: 'ruby',
    php: 'php',
    swift: 'swift',
    kotlin: 'kotlin', kt: 'kotlin',
    csharp: 'csharp', cs: 'csharp', 'c#': 'csharp',
    lua: 'lua',
    r: 'r',
    dockerfile: 'dockerfile', docker: 'dockerfile',
    makefile: 'makefile', make: 'makefile',
  };

  function normalize(lang) {
    if (typeof lang !== 'string') return '';
    var key = lang.trim().toLowerCase();
    if (!key) return '';
    return Object.prototype.hasOwnProperty.call(ALIASES, key) ? ALIASES[key] : key;
  }

  function highlight(code, lang) {
    try {
      var text = typeof code === 'string' ? code : (code == null ? '' : String(code));
      var canon = normalize(lang);
      var tokenizer = Object.prototype.hasOwnProperty.call(TOKENIZERS, canon) ? TOKENIZERS[canon] : null;
      if (!tokenizer) return escapeHtml(text);
      return render(tokenizer(text));
    } catch (e) {
      try { return escapeHtml(typeof code === 'string' ? code : String(code == null ? '' : code)); }
      catch (e2) { return ''; }
    }
  }

  globalThis.BaabaaHighlight = { highlight: highlight, languages: LANGUAGES.slice(), normalize: normalize };
})();
