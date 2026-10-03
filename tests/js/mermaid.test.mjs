// node tests/js/mermaid.test.mjs
import assert from 'node:assert/strict';
import '../../baabaa/web/js/mermaid-lite.js';

const M = globalThis.BaabaaMermaid;
let passed = 0, failed = 0;
const t = (name, fn) => { try { fn(); passed++; } catch (e) { failed++; console.error('FAIL', name, '-', e.message.slice(0, 300)); } };
const safe = svg => assert.ok(!/<script|onload=|onerror=|href=|foreignObject/i.test(svg), 'unsafe markup');

const SAMPLES = {
  flowchart: 'flowchart TD\n  A[Start] --> B{Tea?}\n  B -->|yes| C[Boil water]\n  B -->|no| D[Coffee]\n  C --> E((Done))\n  D --> E',
  sequence: 'sequenceDiagram\n  participant A as Alice\n  A->>Bob: Hello\n  Bob-->>A: Hi\n  alt good\n    A->>Bob: great\n  else bad\n    A-xBob: oh\n  end\n  loop Every minute\n    A->>Bob: ping\n  end\n  Note over A,Bob: done',
  pie: 'pie title Sheep\n  "White" : 40\n  "Black" : 10',
  state: 'stateDiagram-v2\n  [*] --> Grazing\n  Grazing --> Sleeping : night\n  Sleeping --> [*]',
  class: 'classDiagram\n  class Sheep {\n    +String name\n    +baa()\n  }\n  Animal <|-- Sheep',
  er: 'erDiagram\n  FARM ||--o{ SHEEP : keeps',
  gantt: 'gantt\n  title Plan\n  dateFormat YYYY-MM-DD\n  section A\n  Shear :a1, 2026-01-01, 3d\n  Sell :after a1, 2d',
  mindmap: 'mindmap\n  root((Sheep))\n    Wool\n    Milk',
};

for (const [kind, src] of Object.entries(SAMPLES)) {
  t(`detect ${kind}`, () => assert.equal(M.detect(src), kind));
  t(`render ${kind}`, () => { const svg = M.render(src, {}); assert.ok(svg.startsWith('<svg')); safe(svg); });
}
t('all node shapes', () => {
  const svg = M.render('graph LR\n a[rect] --> b(round) --> c([stadium]) --> d[[sub]] --> e[(db)] --> f((circle)) --> g>asym] --> h{rhombus} --> i{{hex}} --> j[/para/] --> k[\\alt\\] --> l[/trap\\]');
  safe(svg);
  for (const w of ['rect', 'round', 'stadium', 'sub', 'db', 'circle', 'asym', 'rhombus', 'hex', 'para', 'alt', 'trap']) assert.ok(svg.includes(w), w);
});
t('edge types and labels', () => {
  const svg = M.render('flowchart TD\n A -->|one| B\n B -- two --> C\n C -.-> D\n D ==> E\n E --- F\n F -. three .-> G\n A & B --> H');
  ['one', 'two', 'three'].forEach(w => assert.ok(svg.includes(w), w));
});
t('labels escaped', () => { const svg = M.render('flowchart LR\n A["<script>alert(1)</script>"] --> B'); safe(svg); assert.ok(svg.includes('&lt;script&gt;')); });
t('TD vs LR aspect', () => {
  const dims = s => { const m = /viewBox="[-\d.]+ [-\d.]+ ([\d.]+) ([\d.]+)"/.exec(s); return m ? [+m[1], +m[2]] : [0, 0]; };
  const [w1, h1] = dims(M.render('graph TD\n A-->B-->C-->D'));
  const [w2, h2] = dims(M.render('graph LR\n A-->B-->C-->D'));
  assert.ok(h1 > w1 && w2 > h2, `TD ${w1}x${h1}, LR ${w2}x${h2}`);
});
t('cycle terminates', () => { assert.doesNotThrow(() => M.render('graph TD\n A-->B\n B-->C\n C-->A')); });
t('subgraph cluster', () => assert.ok(/mm-cluster/.test(M.render('flowchart TB\n subgraph one [First]\n  a-->b\n end\n b-->c'))));
t('80 nodes fast', () => {
  let src = 'graph TD\n';
  for (let i = 0; i < 80; i++) src += ` n${i} --> n${(i * 7 + 3) % 80}\n n${i} --> n${(i * 13 + 1) % 80}\n`;
  const t0 = performance.now(); M.render(src); const ms = performance.now() - t0;
  assert.ok(ms < 1500, `${ms}ms`);
});
t('pie percentages', () => { const svg = M.render('pie\n "a" : 1\n "b" : 3'); assert.ok(svg.includes('25') && svg.includes('75')); });
t('garbage throws Error', () => assert.throws(() => M.render('this is not a diagram'), Error));
t('empty throws Error', () => assert.throws(() => M.render(''), Error));

console.log(`mermaid: ${passed} passed${failed ? `, ${failed} FAILED` : ''}`);
if (failed) process.exit(1);
