// node tests/js/thread.test.mjs
import assert from 'node:assert/strict';
import '../../baabaa/web/js/thread.js';

const { Line, ballRadius, ballMarkup, liveMode, liveLabel, thoughtLabel } = globalThis.BaabaaThread;
let passed = 0, failed = 0;
const t = (name, fn) => { try { fn(); passed++; } catch (e) { failed++; console.error('FAIL', name, '-', e.message.slice(0, 300)); } };

// A strand on a clock the test moves; run() steps it at 60 frames a second and feeds it `rate` tokens a second.
const TEXT = 'The script stops at the download step, and only on a Mac. So the cause is probably the shell.\n\n'
  + 'What is on that line? A message with the version number in it. Let me read the line before guessing.\n\n';
function strand(opts = {}) {
  let now = 100;
  const line = new Line({ clock: () => now, ...opts });
  let at = 0, owed = 0;
  line.run = (seconds, rate = 0) => {
    for (let i = 0; i < Math.round(seconds * 60); i++) {
      now += 1 / 60;
      owed += rate * 4 / 60;
      if (owed >= 12) { const piece = (TEXT + TEXT).slice(at, at + 12); at = (at + 12) % TEXT.length; owed -= 12; line.pulse(piece); }
      line.step(1 / 60);
    }
    return line;
  };
  return line;
}
const points = line => line.path().slice(1).split('L').map(p => p.split(' ').map(Number));
const rise = line => Math.max(...points(line).map(([, y]) => line.y0 - y));   // how far the strand goes above its line
const sag = line => Math.max(...points(line).map(([, y]) => y - line.y0));    // ... and below it

t('ball size', () => {
  assert.equal(ballRadius(0), 2.6);
  assert.equal(ballRadius(5), 3.5);
  assert.equal(ballRadius(20), 3.5);
  assert.ok(ballRadius(100) > ballRadius(20) && ballRadius(1000) > ballRadius(100));
  assert.equal(ballRadius(4000), 11);
  assert.equal(ballRadius(1e6), 11);
  assert.equal(ballRadius(undefined), 2.6);
});
t('ball markup', () => {
  const small = ballMarkup(30), big = ballMarkup(4000);
  assert.ok(small.startsWith('<svg') && small.includes('currentColor'));
  assert.equal((small.match(/<circle cx=/g) || []).length, 3);      // few strands on a small ball
  assert.equal((big.match(/<circle cx=/g) || []).length, 6);
  assert.notEqual(small.match(/id="(\w+)"/)[1], big.match(/id="(\w+)"/)[1]);
});
t('waiting: slack and nothing runs', () => {
  const l = strand(); l.set('wait'); l.run(3);
  assert.equal(l.S, 0);
  assert.ok(sag(l) > 4, `sag ${sag(l)}`);
  assert.ok(rise(l) < 0.01);
});
t('thinking: curls, a running strand and a growing ball', () => {
  const l = strand(); l.set('think'); l.run(6, 30);
  assert.ok(l.v > 30, `speed ${l.v}`);
  assert.ok(rise(l) > 6, `rise ${rise(l)}`);
  assert.ok(sag(l) < 0.5, `sag ${sag(l)}`);
  assert.ok(l.tokens > 150 && l.ballR > 5, `tokens ${l.tokens} ball ${l.ballR}`);
});
t('a faster model makes bigger curls and a faster strand', () => {
  const slow = strand(); slow.set('think'); slow.run(6, 6);
  const fast = strand(); fast.set('think'); fast.run(6, 60);
  assert.ok(fast.v > slow.v + 10 && rise(fast) > rise(slow));
});
t('no text, no curls', () => {
  const l = strand(); l.set('think'); l.run(1);
  assert.ok(rise(l) < 0.2, `rise ${rise(l)}`);
});
t('every curl is whole, and paragraph ends leave gaps', () => {
  const l = strand(); l.set('think'); l.run(8, 30);
  const sizes = [];                                 // the curl size held over each turn of the phase
  for (let k = l.k - 500; k < l.k; k++) {
    const turn = Math.floor(l.Pb[k & 1023] / (2 * Math.PI));
    (sizes[turn] = sizes[turn] || new Set()).add(l.Rb[k & 1023]);
  }
  const turns = Object.values(sizes).slice(1, -1);
  assert.ok(turns.length > 8);
  assert.ok(turns.every(s => s.size === 1), 'a curl changed size part-way');
  const gaps = turns.filter(s => s.has(0)).length;
  assert.ok(gaps >= 1 && gaps < turns.length / 2, `${gaps} gaps in ${turns.length} curls`);
});
t('stalled: the strand goes slack and its curls relax', () => {
  const l = strand(); l.set('think'); l.run(4, 30);
  l.run(1);
  assert.ok(rise(l) > 3, 'still curled a second after the last word');
  l.run(4);
  assert.ok(l.v < 1 && l.slack > 0.9, `speed ${l.v} slack ${l.slack}`);
  assert.ok(rise(l) < 1 && sag(l) > 4, `rise ${rise(l)} sag ${sag(l)}`);
  assert.ok(l.quiet > 4.9);
  l.run(2, 30);
  assert.ok(l.slack < 0.1 && l.v > 30, 'runs again when words come back');
});
t('a tool: the strand holds still, straight, with a bead', () => {
  const l = strand(); l.set('think'); l.run(3, 30); l.set('tool'); l.run(1.5);
  assert.ok(l.beadA > 0.95 && l.v < 1);
  assert.ok(rise(l) < 0.2 && sag(l) < 0.2);
  assert.ok(Math.abs(l.ballR - 2.6) < 0.1);          // the thought's ball is gone; a knot holds the end
});
t('writing: a ripple that rises and fades, no ball', () => {
  const l = strand(); l.set('write'); l.run(5, 30);
  assert.ok(l.fade > 0.95 && l.lift > 0.95 && l.ballR < 0.1);
  const pts = points(l), mid = pts.filter(([x]) => x > 40 && x < 90).map(([, y]) => l.y0 - y);
  assert.ok(Math.max(...mid) > 0.5 && Math.max(...mid) < 2.6 && Math.min(...mid) < -0.5, 'a small wave on both sides of the line');
  assert.ok(l.y0 - pts[pts.length - 1][1] > 11, 'the end rises toward the text');
});
t('a new thought starts a new ball and a new count', () => {
  const l = strand(); l.set('think'); l.run(5, 40);
  const tokens = l.tokens;
  l.set('tool'); l.run(1); l.set('think');
  assert.equal(l.tokens, 0);
  assert.ok(l.seconds < 0.01 && tokens > 100);
});
t('off: the strand is drawn in', () => {
  const l = strand(); l.set('think'); l.run(3, 30); l.set('off'); l.run(2);
  assert.ok(l.reach < 0.01 && l.end - l.x0 < 1);
});
t('less motion: a still picture of each state', () => {
  const l = strand({ still: true }); l.set('think'); l.run(0.1, 30);
  const curls = () => points(l).slice(0, 150).join(' ');      // the strand near the sheep
  const before = curls(), ball = l.ballR;
  l.run(2, 30);
  assert.equal(curls(), before);                               // nothing flows
  assert.ok(rise(l) > 6 && l.ballR > ball + 1);                // the ball still follows the thought
  l.set('wait'); l.run(0.1);
  assert.ok(sag(l) > 4 && rise(l) < 0.01);
  l.set('tool'); l.run(0.1);
  assert.ok(l.beadA > 0.99 && sag(l) < 0.01);
});
t('time never runs backwards', () => {            // a frame's timestamp can be a hair earlier than the clock read before it
  const l = strand(); l.set('wait'); l.step(-0.02); l.step(-0.5);
  assert.ok(l.ballR >= 0 && l.slack <= 1 && l.reach >= 0 && l.t === 0, `ball ${l.ballR}`);
});
t('time away does not pile up', () => {            // a hidden window gets text but no frames
  const l = strand(); l.set('think'); l.run(2, 30);
  let now = 500;
  l.clock = () => now;
  for (let i = 0; i < 400; i++) { now += 0.04; l.pulse('some words '); }      // 2.75 tokens 25 times a second
  assert.ok(l.flow() > 55 && l.flow() < 85, `rate ${l.flow()}`);
});

const msg = (...blocks) => ({ status: 'streaming', blocks });
t('what the strand shows', () => {
  assert.equal(liveMode(msg(), null), 'wait');
  assert.equal(liveMode(msg({ type: 'thinking', text: 'x' }), { queue_position: 2 }), 'wait');
  assert.equal(liveMode(msg({ type: 'thinking', text: 'x' }), { queue_position: null }), 'think');
  assert.equal(liveMode(msg({ type: 'thinking', text: 'x', ms: 900 }), {}), 'wait');
  assert.equal(liveMode(msg({ type: 'thinking', text: 'x', ms: 900 }, { type: 'text', text: 'Hi' }), {}), 'write');
  assert.equal(liveMode(msg({ type: 'tool', name: 'bash', status: 'running' }), {}), 'tool');
  assert.equal(liveMode(msg({ type: 'tool', name: 'bash', status: 'waiting' }), {}), 'wait');
  assert.equal(liveMode(msg({ type: 'tool', name: 'bash', status: 'done' }), {}), 'wait');
  assert.equal(liveMode(msg({ type: 'image', status: 'running' }), {}), 'tool');
  assert.equal(liveMode(msg({ type: 'notice', text: 'x' }), {}), 'wait');
});
t('the words beside it', () => {
  const at = (mode, seconds, quiet) => ({ mode, seconds, quiet });
  assert.equal(liveLabel(msg(), { queue_position: 2 }, at('wait', 0, 0)), 'Waiting for the GPU (2 ahead)');
  assert.equal(liveLabel(msg(), {}, at('wait', 0, 9)), '');
  assert.equal(liveLabel(msg({ type: 'thinking' }), {}, at('think', 0.2, 0)), 'Thinking… 1 s');
  assert.equal(liveLabel(msg({ type: 'thinking' }), {}, at('think', 75.4, 0)), 'Thinking… 1 min 15 s');
  assert.equal(liveLabel(msg({ type: 'thinking' }), {}, at('think', 12, 5.2)), 'Thinking… 12 s · quiet for 5 s');
  assert.equal(liveLabel(msg({ type: 'tool', name: 'bash' }), {}, at('tool', 0, 0)), 'Running a command…');
  assert.equal(liveLabel(msg({ type: 'tool', name: 'files__list' }), {}, at('tool', 0, 0)), 'Using a tool…');
  assert.equal(liveLabel(msg({ type: 'image' }), {}, at('tool', 0, 0)), 'Making an image…');
  assert.equal(liveLabel(msg({ type: 'text' }), {}, at('write', 0, 1)), '');
  assert.equal(liveLabel(msg({ type: 'text' }), {}, at('write', 0, 8)), 'Quiet for 8 s');
});
t('a finished thought', () => {
  assert.equal(thoughtLabel({ type: 'thinking', text: 'x' }), 'Thoughts');     // written before thoughts were timed
  assert.equal(thoughtLabel({ ms: 300 }), 'Thought for 1 s');
  assert.equal(thoughtLabel({ ms: 41200 }), 'Thought for 41 s');
  assert.equal(thoughtLabel({ ms: 125000 }), 'Thought for 2 min 5 s');
});

console.log(`thread: ${passed} passed${failed ? `, ${failed} FAILED` : ''}`);
if (failed) process.exit(1);
