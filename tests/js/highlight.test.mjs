// Tests for highlight.js. Run: node tests/js/highlight.test.mjs
import assert from 'node:assert';
import '../../baabaa/web/js/highlight.js';

const H = globalThis.BaabaaHighlight;
assert.ok(H, 'BaabaaHighlight must be attached to globalThis');

let passed = 0;
function ok(cond, msg) { assert.ok(cond, msg); passed++; }
function eq(actual, expected, msg) { assert.strictEqual(actual, expected, msg); passed++; }
function has(str, needle, msg) { assert.ok(str.indexOf(needle) !== -1, (msg || '') + ' -- expected to find ' + JSON.stringify(needle) + ' in ' + JSON.stringify(str)); passed++; }
function lacks(str, needle, msg) { assert.ok(str.indexOf(needle) === -1, (msg || '') + ' -- did not expect to find ' + JSON.stringify(needle) + ' in ' + JSON.stringify(str)); passed++; }
// No raw '<' except as part of an opening '<span' or closing '</span'.
function noStrayAngleBrackets(html, msg) {
  const stripped = html.replace(/<\/?span(?: class="hl-[\w-]+")?>/g, '');
  ok(stripped.indexOf('<') === -1, (msg || 'stray <') + ' -- ' + JSON.stringify(html));
}

// ---- escaping --------------------------------------------------------------------------------

{
  const out = H.highlight('<script>alert(1)</script>', 'python');
  has(out, '&lt;script&gt;', 'script tag escaped in plain code');
  lacks(out, '<script>', 'raw script tag must not appear');
  noStrayAngleBrackets(out, 'python script-tag escaping');
}
{
  const out = H.highlight('x = "<script>alert(1)</script>"', 'python');
  has(out, '&lt;script&gt;', 'script tag escaped inside a string');
  lacks(out, '<script>', 'raw script tag inside string must not appear');
  noStrayAngleBrackets(out, 'python string script-tag escaping');
}
{
  const out = H.highlight(`<img src=x onerror="alert('&')">`, 'html');
  lacks(out, 'onerror="alert(\'&\')"', 'raw ampersand/quote must be escaped');
  has(out, '&amp;', 'ampersand escaped');
  noStrayAngleBrackets(out, 'html attribute escaping');
}
{
  const out = H.highlight(`const s = "it's \\"quoted\\" & <b>";`, 'javascript');
  noStrayAngleBrackets(out, 'javascript mixed-quote escaping');
  has(out, '&lt;b&gt;', 'tag-like text inside JS string escaped');
}

// ---- unknown language --------------------------------------------------------------------------

{
  const src = '<b>plain & unknown "text"</b>';
  const out = H.highlight(src, 'brainfuck');
  eq(out, '&lt;b&gt;plain &amp; unknown &quot;text&quot;&lt;/b&gt;', 'unknown language returns escaped text with no spans');
}
{
  const out = H.highlight('anything', '');
  eq(out, 'anything', 'empty language string is treated as unknown/plain');
}

// ---- normalize / aliases -----------------------------------------------------------------------

const aliasCases = [
  ['py', 'python'], ['python3', 'python'], ['PY', 'python'],
  ['js', 'javascript'], ['mjs', 'javascript'], ['cjs', 'javascript'], ['jsx', 'javascript'],
  ['ts', 'typescript'], ['tsx', 'typescript'],
  ['sh', 'bash'], ['shell', 'bash'], ['zsh', 'bash'], ['console', 'bash'],
  ['xml', 'html'], ['svg', 'html'], ['xhtml', 'html'],
  ['scss', 'css'], ['less', 'css'],
  ['h', 'c'], ['c++', 'cpp'], ['cc', 'cpp'], ['cxx', 'cpp'],
  ['golang', 'go'], ['rs', 'rust'],
  ['yml', 'yaml'], ['ini', 'toml'], ['cfg', 'toml'], ['conf', 'toml'],
  ['jsonc', 'json'], ['json5', 'json'],
  ['md', 'markdown'], ['patch', 'diff'],
  ['rb', 'ruby'], ['kt', 'kotlin'], ['cs', 'csharp'], ['c#', 'csharp'],
  ['docker', 'dockerfile'], ['make', 'makefile'],
];
for (const [alias, canon] of aliasCases) eq(H.normalize(alias), canon, `normalize(${JSON.stringify(alias)})`);
ok(Array.isArray(H.languages) && H.languages.length >= 20, 'languages list is populated');
for (const lang of H.languages) eq(H.normalize(lang), lang, `canonical id ${lang} normalizes to itself`);

// ---- Python: keywords, strings, comments, numbers, docstrings, decorators ----------------------

{
  const out = H.highlight('# hi\ndef foo(x):\n    """doc"""\n    return x + 1', 'python');
  has(out, 'hl-c">', 'python comment class');
  has(out, 'hl-k">def</span>', 'python keyword "def"');
  has(out, 'hl-f">foo</span>', 'python function name after def');
  has(out, 'hl-s">&quot;&quot;&quot;doc&quot;&quot;&quot;</span>', 'python docstring as one string token');
  has(out, 'hl-n">1</span>', 'python number');
}
{
  const out = H.highlight('@staticmethod\ndef bar():\n    pass', 'python');
  has(out, 'hl-m">@staticmethod</span>', 'python decorator');
}
{
  const out = H.highlight('class Foo:\n    pass', 'python');
  has(out, 'hl-t">Foo</span>', 'python class name after class keyword');
}

// ---- JavaScript / TypeScript: template literals, regex vs division -----------------------------

{
  const out = H.highlight('const s = `hi ${name}!`;', 'javascript');
  has(out, 'hl-s">`hi ${name}!`</span>', 'template literal (including ${}) stays one string token');
}
{
  const out = H.highlight('const x = a / b / c;', 'javascript');
  lacks(out, 'hl-r', 'plain division must not be classified as a regex literal');
}
{
  const out = H.highlight('const re = /ab+c/gi; return /foo/.test(s);', 'javascript');
  has(out, 'hl-r">/ab+c/gi</span>', 'regex literal after assignment');
  has(out, 'hl-r">/foo/</span>', 'regex literal after return');
}
{
  const out = H.highlight('function foo(a) { return a; }\nclass Bar {}', 'javascript');
  has(out, 'hl-k">function</span>', 'javascript keyword "function"');
  has(out, 'hl-f">foo</span>', 'javascript function name after function keyword');
  has(out, 'hl-t">Bar</span>', 'javascript class name after class keyword');
}
{
  const out = H.highlight('interface Foo { x: number; }', 'typescript');
  has(out, 'hl-k">interface</span>', 'typescript-only keyword "interface"');
  has(out, 'hl-t">number</span>', 'typescript builtin type');
}

// ---- Bash: variables, comments (word-internal # is not a comment), keywords --------------------

{
  const out = H.highlight('echo hi # comment\nfoo=a#b\nVAR=$HOME/${OTHER}', 'bash');
  has(out, 'hl-c"># comment</span>', 'bash line comment after whitespace');
  has(out, 'a#b', 'bash: # inside a word is left untouched');
  lacks(out, 'hl-c">#b</span>', '# inside a word must not become a comment');
  has(out, 'hl-v">$HOME</span>', 'bash simple variable');
  has(out, 'hl-v">${OTHER}</span>', 'bash braced variable');
}
{
  const out = H.highlight('if [ -f x ]; then\n  echo ok\nfi', 'bash');
  has(out, 'hl-k">if</span>', 'bash keyword "if"');
  has(out, 'hl-k">then</span>', 'bash keyword "then"');
  has(out, 'hl-k">fi</span>', 'bash keyword "fi"');
}

// ---- JSON: keys vs string values, booleans, numbers ---------------------------------------------

{
  const out = H.highlight('{"name": "Ada", "age": 37, "ok": true, "x": null}', 'json');
  has(out, 'hl-a">&quot;name&quot;</span>', 'json object key gets key class');
  has(out, 'hl-s">&quot;Ada&quot;</span>', 'json string value gets string class (not key class)');
  has(out, 'hl-n">37</span>', 'json number');
  has(out, 'hl-b">true</span>', 'json boolean');
  has(out, 'hl-b">null</span>', 'json null');
}

// ---- diff classes --------------------------------------------------------------------------------

{
  const out = H.highlight('@@ -1,2 +1,2 @@\n-old\n+new\n context', 'diff');
  has(out, 'hl-meta">@@ -1,2 +1,2 @@</span>', 'diff hunk header uses hl-meta');
  has(out, 'hl-add">+new</span>', 'diff added line uses hl-add');
  has(out, 'hl-del">-old</span>', 'diff removed line uses hl-del');
}

// ---- other languages: keyword / string / comment / number spot checks ---------------------------

{
  const out = H.highlight('// c\nint main() { return 0x1F; }', 'c');
  has(out, 'hl-c">// c</span>', 'c line comment');
  has(out, 'hl-t">int</span>', 'c builtin type');
  has(out, 'hl-n">0x1F</span>', 'c hex number');
}
{
  const out = H.highlight('/* c */\nclass Foo { void bar() {} }', 'java');
  has(out, 'hl-c">/* c */</span>', 'java block comment');
  has(out, 'hl-k">class</span>', 'java keyword');
  has(out, 'hl-t">Foo</span>', 'java class name');
}
{
  const out = H.highlight('func main() { x := "hi" }', 'go');
  has(out, 'hl-k">func</span>', 'go keyword');
  has(out, 'hl-f">main</span>', 'go function name');
  has(out, 'hl-s">&quot;hi&quot;</span>', 'go string');
}
{
  const out = H.highlight('fn main() { let x: i32 = 5; }', 'rust');
  has(out, 'hl-f">main</span>', 'rust function name after fn');
  has(out, 'hl-t">i32</span>', 'rust builtin type');
  has(out, 'hl-n">5</span>', 'rust number');
}
{
  const out = H.highlight('SELECT * FROM t WHERE id = 1; -- c', 'sql');
  has(out, 'hl-k">SELECT</span>', 'sql keyword uppercase');
  has(out, 'hl-c">-- c</span>', 'sql line comment');
}
{
  const out = H.highlight('key: "value" # c', 'yaml');
  has(out, 'hl-a">key</span>', 'yaml key');
  has(out, 'hl-c"># c</span>', 'yaml comment');
}

// ---- robustness: unterminated strings/comments never throw --------------------------------------

const unterminated = [
  ['python', 'x = "never closes'],
  ['python', 'x = """never closes either'],
  ['javascript', 'const x = /* never closes'],
  ['javascript', 'const s = "never closes'],
  ['c', 'char *s = "never closes'],
  ['css', '.a { color: "never closes'],
  ['html', '<div class="never closes'],
  ['bash', "echo 'never closes"],
];
for (const [lang, src] of unterminated) {
  let out;
  assert.doesNotThrow(() => { out = H.highlight(src, lang); }, `${lang} unterminated input must not throw`);
  passed++;
  ok(typeof out === 'string' && out.length > 0, `${lang} unterminated input still returns output`);
}

// ---- streaming: every prefix of a valid snippet highlights without throwing ---------------------

{
  const src = 'def foo(x):\n    """doc"""\n    return x + 1  # done\nclass Bar(Base):\n    pass\n';
  let threw = false;
  for (let i = 0; i <= src.length; i++) {
    try { H.highlight(src.slice(0, i), 'python'); } catch (e) { threw = true; break; }
  }
  ok(!threw, 'every prefix of a python snippet highlights without throwing');
}
{
  const src = 'function foo() {\n  const re = /a.b/g;\n  return `hi ${1+1}`;\n}\n';
  let threw = false;
  for (let i = 0; i <= src.length; i++) {
    try { H.highlight(src.slice(0, i), 'javascript'); } catch (e) { threw = true; break; }
  }
  ok(!threw, 'every prefix of a javascript snippet highlights without throwing');
}

// ---- non-string / null-ish input never throws ----------------------------------------------------

for (const bad of [null, undefined, 123, {}, []]) {
  assert.doesNotThrow(() => H.highlight(bad, 'python'), 'non-string code must not throw');
  passed++;
}
assert.doesNotThrow(() => H.highlight('x', null), 'null language must not throw');
passed++;
assert.doesNotThrow(() => H.highlight('x', undefined), 'undefined language must not throw');
passed++;

// ---- performance: 100 KB of realistic code stays well under budget -------------------------------

{
  const unit = 'function computeTotal(items, taxRate) {\n' +
    '  // sum line items and apply tax\n' +
    '  let total = 0;\n' +
    '  for (let i = 0; i < items.length; i++) {\n' +
    '    const price = items[i].price;\n' +
    '    total += price - price / 20;\n' +
    '  }\n' +
    '  const label = `Total: ${total.toFixed(2)}`;\n' +
    '  const re = /^\\d+(\\.\\d+)?$/;\n' +
    '  return { total, label, ok: re.test(String(total)) };\n' +
    '}\n';
  let big = '';
  while (big.length < 100000) big += unit;
  const t0 = process.hrtime.bigint();
  const out = H.highlight(big, 'javascript');
  const ms = Number(process.hrtime.bigint() - t0) / 1e6;
  ok(out.length > big.length, 'large input produces highlighted (longer) output');
  ok(ms < 50, `100 KB javascript highlights in under 50ms (took ${ms.toFixed(2)}ms)`);
}
{
  // adversarial: a long run of identifier characters with no delimiters must not blow up
  // (this is the shape that triggers catastrophic backtracking in a naive func-call pattern).
  const adversarial = 'x'.repeat(100000);
  const t0 = process.hrtime.bigint();
  H.highlight(adversarial, 'javascript');
  const ms = Number(process.hrtime.bigint() - t0) / 1e6;
  ok(ms < 500, `100 KB adversarial input (no delimiters) does not exhibit catastrophic backtracking (took ${ms.toFixed(2)}ms)`);
}

console.log(`highlight: ${passed} passed`);
