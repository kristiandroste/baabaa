// node tests/js/markdown.test.mjs
import assert from 'node:assert/strict';
import '../../baabaa/web/js/markdown.js';

const { render, renderInline, escapeHtml, latexToMathML } = globalThis.BaabaaMarkdown;
let passed = 0, failed = 0;
const t = (name, fn) => { try { fn(); passed++; } catch (e) { failed++; console.error('FAIL', name, '-', e.message.slice(0, 300)); } };
const has = (html, s) => assert.ok(html.includes(s), `expected ${JSON.stringify(s)} in ${html}`);
const lacks = (html, s) => assert.ok(!html.includes(s), `unexpected ${JSON.stringify(s)} in ${html}`);

// security
t('script escaped', () => { const h = render('<script>alert(1)</script>'); lacks(h, '<script'); has(h, '&lt;script&gt;'); });
t('img onerror escaped', () => lacks(render('<img src=x onerror=alert(1)>'), '<img'));
t('javascript link', () => { const h = render('[x](javascript:alert(1))'); lacks(h, 'href'); has(h, 'x'); });
t('data link', () => lacks(render('[x](data:text/html,<b>)'), 'href'));
t('quote in url', () => { const h = render('[x](https://a.com/"onmouseover="alert(1))'); lacks(h, '"onmouseover'); });
t('html in code', () => has(render('`<b>`'), '<code>&lt;b&gt;</code>'));
t('html in fence', () => has(render('```\n<b>x</b>\n```'), '&lt;b&gt;x&lt;/b&gt;'));
t('escapeHtml', () => assert.equal(escapeHtml(`<a href="x">'&`), '&lt;a href=&quot;x&quot;&gt;&#39;&amp;'));

// blocks
t('headings', () => { has(render('# One'), '<h1>One</h1>'); has(render('### Three ###'), '<h3>Three</h3>'); lacks(render('#tag'), '<h1>'); });
t('setext', () => has(render('Title\n====='), '<h1>Title</h1>'));
t('paragraph breaks', () => has(render('a\nb'), 'a<br>b'));
t('a wrapped document', () => { const h = render('a\nb\n\n- c\n  d\\\n  e', { breaks: false }); has(h, '<p>a\nb</p>'); has(h, 'c\nd<br>e'); });
t('two paragraphs', () => assert.equal((render('a\n\nb').match(/<p>/g) || []).length, 2));
t('hr', () => has(render('a\n\n---\n\nb'), '<hr>'));
t('blockquote', () => has(render('> quoted **bold**'), '<blockquote><p>quoted <strong>bold</strong></p></blockquote>'));

// emphasis
t('strong/em', () => { const h = renderInline('**b** and *i* and ***bi***'); has(h, '<strong>b</strong>'); has(h, '<em>i</em>'); has(h, '<strong><em>bi</em></strong>'); });
t('underscore', () => has(renderInline('_it_ __st__'), '<em>it</em> <strong>st</strong>'));
t('snake_case', () => { const h = renderInline('snake_case_name here'); lacks(h, '<em>'); has(h, 'snake_case_name'); });
t('strike', () => has(renderInline('~~gone~~'), '<del>gone</del>'));
t('lone asterisk', () => assert.equal(renderInline('2 * 3'), '2 * 3'));

// code
t('inline code backticks', () => has(renderInline('``a ` b``'), '<code>a ` b</code>'));
t('fence with lang', () => { const h = render('```python\nprint(1)\n```'); has(h, 'data-lang="python"'); has(h, '<code class="language-python">print(1)</code>'); has(h, 'code-copy'); });
t('highlight hook', () => has(render('```js\nx\n```', { highlight: () => '<span class="hl-k">X</span>' }), '<span class="hl-k">X</span>'));
t('unclosed fence streaming', () => has(render('```js\nlet a = 1', { streaming: true }), 'let a = 1'));
t('tilde fence', () => has(render('~~~\ncode\n~~~'), '<code class="language-text">code</code>'));
t('fence in list', () => { const h = render('- item\n\n  ```sh\n  ls\n  ```'); has(h, '<li>'); has(h, 'language-sh'); });
t('mermaid placeholder', () => { const h = render('```mermaid\ngraph TD\nA-->B\n```'); has(h, 'class="mermaid-block"'); has(h, 'data-src="graph TD\nA--&gt;B"'); });

// lists
t('unordered', () => has(render('- a\n- b'), '<ul><li>a</li><li>b</li></ul>'));
t('ordered start', () => has(render('3. x\n4. y'), '<ol start="3"><li>x</li><li>y</li></ol>'));
t('nested', () => has(render('- a\n  - b\n- c'), '<li>a\n<ul><li>b</li></ul></li><li>c</li>'));
t('task list', () => { const h = render('- [ ] todo\n- [x] done'); has(h, '<li class="task"><input type="checkbox" disabled> todo'); has(h, 'checked> done'); });
t('loose list', () => has(render('- a\n\n- b'), '<li><p>a</p></li>'));

// tables
t('table', () => {
  const h = render('| a | b |\n|:--|--:|\n| 1 | 2 \\| 3 |');
  has(h, '<div class="table-wrap"><table>'); has(h, '<th style="text-align:left">a</th>'); has(h, '<td style="text-align:right">2 | 3</td>');
});
t('table inline md', () => has(render('| x |\n|---|\n| **b** |'), '<td><strong>b</strong></td>'));

// links
t('link', () => has(renderInline('[site](https://example.com)'), '<a href="https://example.com" target="_blank" rel="noopener noreferrer">site</a>'));
t('link parens', () => has(renderInline('[w](https://en.wikipedia.org/wiki/Sheep_(disambiguation))'), 'href="https://en.wikipedia.org/wiki/Sheep_(disambiguation)"'));
t('autolink trailing punct', () => { const h = renderInline('see https://example.com/a.'); has(h, 'href="https://example.com/a"'); has(h, '</a>.'); });
t('autolink in parens', () => has(renderInline('(https://example.com)'), 'href="https://example.com"'));
t('angle autolink', () => has(renderInline('<https://x.org>'), 'href="https://x.org"'));
t('image link', () => { const h = renderInline('![a sheep](https://x.org/s.png)'); has(h, 'md-image-link'); lacks(h, '<img'); });
t('data image', () => has(renderInline('![dot](data:image/png;base64,iVBORw0KGgo=)'), '<img class="md-img" src="data:image/png;base64,iVBORw0KGgo="'));

// math
t('inline math', () => has(renderInline('area $\\pi r^2$ here'), '<math xmlns="http://www.w3.org/1998/Math/MathML" display="inline">'));
t('display math', () => has(render('$$\n\\frac{a}{b}\n$$'), 'display="block"'));
t('bracket math', () => has(render('\\[ x^2 \\]'), '<msup><mi>x</mi><mn>2</mn></msup>'));
t('currency', () => { const h = renderInline('costs $5 and $10 today'); lacks(h, '<math'); has(h, '$5 and $10'); });
t('frac/sqrt', () => { const m = latexToMathML('\\frac{1}{\\sqrt{x}}'); has(m, '<mfrac><mn>1</mn><msqrt><mi>x</mi></msqrt></mfrac>'); });
t('sum limits display', () => has(latexToMathML('\\sum_{i=1}^{n} i', true), '<munderover>'));
t('sum limits inline', () => has(latexToMathML('\\sum_{i=1}^{n} i', false), '<msubsup>'));
t('greek', () => { const m = latexToMathML('\\alpha + \\Omega'); has(m, 'α'); has(m, 'Ω'); });
t('text', () => has(latexToMathML('x \\text{ if } y'), '<mtext> if </mtext>'));
t('matrix', () => { const m = latexToMathML('\\begin{pmatrix} a & b \\\\ c & d \\end{pmatrix}'); has(m, '<mtable>'); assert.equal((m.match(/<mtr>/g) || []).length, 2); has(m, '(</mo>'); });
t('cases', () => has(latexToMathML('f(x) = \\begin{cases} 1 & x>0 \\\\ 0 & \\text{else} \\end{cases}'), 'columnalign="left left"'));
t('left right', () => has(latexToMathML('\\left( \\frac{a}{b} \\right)'), 'stretchy="true">(</mo>'));
t('malformed', () => { assert.doesNotThrow(() => latexToMathML('\\frac{a}{')); assert.doesNotThrow(() => latexToMathML('}}}{{{\\\\')); });
t('unknown command', () => has(latexToMathML('\\foo'), '\\foo'));
t('no math in code', () => lacks(renderInline('`$x$`'), '<math'));

// robustness and speed
t('streaming partial', () => { assert.doesNotThrow(() => render('**bold and `code', { streaming: true })); });
t('speed', () => {
  const big = ('## Title\n\nSome **bold** text with `code`, a [link](https://x.org) and $x^2$.\n\n- one\n- two\n\n```py\nprint(1)\n```\n\n').repeat(120);
  const t0 = performance.now(); render(big); const ms = performance.now() - t0;
  assert.ok(ms < 150, `too slow: ${ms}ms for ${big.length} chars`);
});
t('pathological asterisks', () => {
  const t0 = performance.now(); renderInline('*a '.repeat(3000)); const ms = performance.now() - t0;
  assert.ok(ms < 500, `too slow: ${ms}ms`);
});

t('glued fence and unclosed math', () => {
  const h = render('$$x = \\frac{-b}{2a}```mermaid\nflowchart TD\nA --> B');
  has(h, 'display="block"'); has(h, 'class="mermaid-block"'); lacks(h, '<mi>m</mi><mi>e</mi>');
});
t('code block inside a list item', () => {
  const h = render('1. Install it:\n\n   ```bash\n   curl -fsSL https://example.com/install.sh | sh\n   ```\n\n2. Run it.');
  has(h, '<code class="language-bash">curl -fsSL'); lacks(h, 'start="2"'); has(h, 'Run it.</p></li></ol>');
  has(render('text ```js\nx()\n```'), '<code class="language-js">x()</code>');
});
console.log(`markdown: ${passed} passed${failed ? `, ${failed} FAILED` : ''}`);
if (failed) process.exit(1);
