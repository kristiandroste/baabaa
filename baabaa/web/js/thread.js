// The thinking thread: what baabaa draws while it works on a reply. A strand of wool leaves the sheep and
// curls as the model's text arrives; it hangs slack while nothing arrives, carries a bead while a tool
// runs, calms to a ripple while the answer is written, and winds each thought into a ball of yarn whose
// size follows the thought's length. A classic script like the renderers: it sets globalThis.BaabaaThread
// = { Line, Thread, ballRadius, ballMarkup, liveMode, liveLabel, thoughtLabel }. Line (the strand's state
// and shape) needs no page, so tests/js/thread.test.mjs runs it under plain Node.
(function () {
  'use strict';

  const TAU = Math.PI * 2, STEP = 0.5, N = 1024, MASK = N - 1;
  const QUIET_S = 1.4;   // this long without text and a strand that was running goes slack
  const sstep = x => (x <= 0 ? 0 : x >= 1 ? 1 : x * x * (3 - 2 * x));
  const toward = (cur, target, dt, tau) => cur + (target - cur) * (1 - Math.exp(-dt / tau));
  let seq = 0;

  // How big a thought's ball is: 20 tokens or fewer is the smallest, 4,000 or more the largest, log scale between.
  function ballRadius(tokens) {
    if (!(tokens > 0)) return 2.6;
    return 3.5 + 7.5 * Math.min(1, Math.max(0, Math.log(Math.max(tokens, 20) / 20) / Math.log(200)));
  }

  // The strands on a ball of radius r: two bands of arcs that cross, cut off at the ball's edge. A small ball gets fewer.
  function strands(r) {
    const band = (turn, big, offsets) => `<g transform="rotate(${turn})">`
      + offsets.map(h => `<circle cx="0" cy="${((big + h) * r).toFixed(2)}" r="${(big * r).toFixed(2)}"/>`).join('') + '</g>';
    return band(-38, 1.15, r >= 8.5 ? [-0.7, -0.3, 0.1, 0.52] : r >= 5.5 ? [-0.55, -0.08, 0.4] : [-0.35, 0.25])
      + band(40, 1.5, r >= 5.5 ? [0.05, 0.48] : [0.3]);
  }

  // A still ball, the mark of a finished thought. It draws in the text colour around it (currentColor).
  function ballMarkup(tokens, turn = -28) {
    const r = ballRadius(tokens), size = Math.ceil(2 * r + 3), id = 'thb' + (++seq);
    return `<svg width="${size}" height="${size}" viewBox="${-size / 2} ${-size / 2} ${size} ${size}" aria-hidden="true">`
      + `<clipPath id="${id}"><circle r="${(r - 0.4).toFixed(2)}"/></clipPath>`
      + `<circle r="${r.toFixed(2)}" fill="var(--th-surface, none)" stroke="currentColor" stroke-width="1.4"/>`
      + `<g clip-path="url(#${id})"><g transform="rotate(${turn})" fill="none" stroke="currentColor" stroke-width=".95" opacity=".62">${strands(r)}</g></g></svg>`;
  }

  // The strand's state and shape. Text pieces go in with pulse(), time with step(), and path() gives the curve.
  class Line {
    // `still` is for people who asked their system for less motion: the strand becomes a still picture of its state.
    constructor({ width = 190, still = false, clock = () => performance.now() / 1000 } = {}) {
      this.W = width; this.still = still; this.clock = clock;
      this.x0 = 26.5; this.y0 = 20.5; this.right = width - 1.5;
      this.Rb = new Float32Array(N); this.Pb = new Float64Array(N); this.Lb = new Float32Array(N);   // each sample of strand: curl size, phase, loopiness
      this.S = 0; this.k = 0;                                           // strand paid out so far, and samples laid on it
      this.mode = 'off'; this.lastPulse = -1e9; this.gap = 0; this.since = 0; this.loop = 1; this.held = 0;
      this.rate = 0; this.rateAt = clock(); this.tokens = 0; this.began = clock(); this.t = 0;
      this.v = 0; this.R = 0; this.ph = 0; this.slack = 1; this.reach = 0;
      this.ballR = 0; this.fade = 0; this.lift = 0; this.beadA = 0; this.turn = 0;
    }

    // 'off', 'wait' (slack), 'think' (curls and a growing ball), 'tool' (a bead) or 'write' (a ripple)
    set(mode) {
      if (mode === 'think' && this.mode !== 'think') this.newThought();
      if (mode !== this.mode) this.held = 0;            // a flat stretch between one kind of curve and the next
      this.mode = mode;
      this.lastPulse = this.clock();
      if (this.still) this.settle();
    }

    // The still picture: even curls for thinking, an even ripple for writing, a plain strand otherwise.
    settle() {
      const m = this.mode, R = m === 'think' ? 4.2 : m === 'write' ? 1.3 : 0;
      this.k = 700; this.S = 700 * STEP;
      for (let k = 1; k <= 700; k++) { this.Rb[k & MASK] = R; this.Pb[k & MASK] = k * STEP * 0.5; this.Lb[k & MASK] = m === 'write' ? 0 : 1; }
    }

    newThought() { this.tokens = 0; this.began = this.clock(); }

    get seconds() { return this.clock() - this.began; }
    get quiet() { return this.clock() - this.lastPulse; }

    // Text arriving per second, as tokens (about four characters each), smoothed over half a second.
    flow() {
      const now = this.clock();
      this.rate *= Math.exp(-Math.max(0, now - this.rateAt) / 0.45);
      this.rateAt = now;
      return this.rate;
    }

    // A piece of text arrived from the model.
    pulse(text) {
      const n = Math.max(String(text).length, 1) / 4;
      this.rate = this.flow() + n / 0.45;
      if (this.mode === 'think') this.tokens += n;
      this.lastPulse = this.clock();
      if (text.includes('\n')) this.gap = 2;                                           // a paragraph ended
      else if (/[.!?]["')\]]?(\s|$)/.test(text)) this.gap = Math.max(this.gap, 1);    // a sentence ended
    }

    step(dt) {
      dt = Math.max(0, dt);                              // a frame's time can read a hair before the last one's
      const now = this.t += dt;
      const m = this.mode, busy = m === 'think' || m === 'write', e = this.still ? 10 : dt;   // still: values jump into place
      const flowing = busy && this.quiet < QUIET_S;
      const a = 1 - Math.exp(-this.flow() / 14);        // how busy the stream is, 0 to 1
      let vT = 0, RT = 0, f = 0;
      if (flowing) {
        vT = 14 + 26 * a;
        f = m === 'think' ? 0.9 + 2.7 * a : 1.5 + 1.2 * a;
        RT = (m === 'think' ? 3.0 + 2.3 * a : 1.0 + 0.7 * a) * (1 + 0.16 * Math.sin(now * 2.3) + 0.08 * Math.sin(now * 5.1 + 1));
        RT *= Math.min(1, a * 8);                        // no text yet, no curls
      }
      this.loop = toward(this.loop, m === 'write' ? 0 : 1, dt, 0.15);
      const slackT = m === 'wait' || m === 'off' || (busy && !flowing) ? 1 : 0;
      this.v = this.still ? 0 : toward(this.v, vT, dt, vT > this.v ? 0.18 : m === 'tool' ? 0.15 : 0.45);   // a tool stops it short
      this.R = toward(this.R, RT, dt, 0.07);
      this.slack = toward(this.slack, slackT, e, slackT ? 0.7 : 0.22);
      this.ballR = toward(this.ballR, m === 'think' ? ballRadius(this.tokens) : m === 'write' || m === 'off' ? 0 : 2.6, e, 0.25);
      this.fade = toward(this.fade, m === 'write' ? 1 : 0, e, 0.3);
      this.lift = toward(this.lift, m === 'write' ? 1 : 0, e, 0.4);
      this.beadA = toward(this.beadA, m === 'tool' ? 1 : 0, e, 0.2);
      this.reach = toward(this.reach, m === 'off' ? 0 : 1, e, 0.2);
      if (!flowing && !this.still) {                     // a strand that stands still lets its curls relax
        const keep = Math.exp(-dt / (m === 'tool' ? 0.25 : 0.9));
        for (let i = 0; i < N; i++) this.Rb[i] *= keep;
      }
      const ds = this.v * dt, dph = TAU * f * dt, S0 = this.S;
      this.S += ds;
      while ((this.k + 1) * STEP <= this.S) {            // lay new samples at the sheep's end
        const k = ++this.k, ph = this.ph + dph * ((k * STEP - S0) / ds);
        if (Math.floor(ph / TAU) > Math.floor(this.Pb[(k - 1) & MASK] / TAU)) {   // a curl is complete, so every curl is whole.
          // The next one is left out after a paragraph end (if three curls have passed since the last gap) or a sentence end (five).
          const skip = this.gap === 2 ? this.since >= 3 : this.gap === 1 && this.since >= 5;
          this.held = skip ? 0 : this.R;
          this.since = skip ? 0 : this.since + 1;
          this.gap = 0;
        }
        this.Rb[k & MASK] = this.held; this.Lb[k & MASK] = this.loop; this.Pb[k & MASK] = ph;
      }
      this.ph += dph;
      this.turn += ds / Math.max(this.ballR, 3);
    }

    // Where the strand ends: at the ball's near edge, or at the sheep while the strand is drawn in.
    get end() { return this.x0 + this.reach * (this.right - 2 * this.ballR - this.x0); }

    // The curve, as an SVG path.
    path() {
      const { x0, y0 } = this, xe = this.end, span = Math.max(xe - x0, 0.01);
      const sway = this.still ? 6.5 : 6.5 + 1.1 * Math.sin(this.t * 1.9), rise = this.lift * 12;
      let d = `M${x0} ${y0}`;
      for (let k = this.k; ; k--) {
        const x = x0 + this.S - k * STEP;
        if (x >= xe) break;
        const live = k >= 1 && k > this.k - N;
        const R = live ? this.Rb[k & MASK] : 0, ph = live ? this.Pb[k & MASK] : 0, L = live ? this.Lb[k & MASK] : 1;
        const u = (x - x0) / span, env = sstep((x - x0) / 9) * sstep((xe - x) / 17), l = sstep((x - xe + 50) / 50);
        const up = L * (1 - Math.cos(ph)) + (1 - L) * Math.sin(ph);          // a loop, or a plain wave
        const y = y0 - R * env * up + this.slack * 4 * u * (1 - u) * sway - rise * l * l;
        d += `L${(x - L * R * env * Math.sin(ph)).toFixed(2)} ${y.toFixed(2)}`;
      }
      return d + `L${xe.toFixed(2)} ${(y0 - rise).toFixed(2)}`;
    }
  }

  const SHEEP = '<g fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">'
    + '<path d="M22 50v6M44 50v6"/>'
    + '<path class="wool" d="M18 30c-5 0-8 4-7 8 1 5 6 6 9 5 1 5 6 8 11 7 4 4 11 4 15 0 5 1 9-3 9-8 3-2 4-7 1-10 1-5-3-9-8-8-2-5-8-6-12-3-4-3-10-2-12 3-3-1-6 1-6 6z" fill="var(--th-surface, none)"/>'
    + '<ellipse cx="17" cy="33" rx="6.5" ry="8" fill="var(--th-surface, none)"/><circle class="eye" cx="15" cy="31.5" r="1.3" fill="currentColor" stroke="none"/>'
    + '<path d="M11 28c-2-1-3-3-2-5M22 27c2-1 3-3 2-5"/></g>';

  // The strand on the page: an SVG inside `host`, redrawn with draw() after each step().
  class Thread extends Line {
    constructor(host, opts) {
      super(opts);
      const id = ++seq, W = this.W;
      host.innerHTML = `<svg viewBox="0 0 ${W} 34" aria-hidden="true">`
        + `<defs><linearGradient id="thg${id}" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="${W}" y2="0">`
        + `<stop offset="0" stop-color="currentColor"/><stop offset=".45" stop-color="currentColor"/><stop class="end" offset="1" stop-color="currentColor"/></linearGradient>`
        + `<clipPath id="thc${id}"><circle r="1"/></clipPath></defs>`
        + `<path class="line" fill="none" stroke="url(#thg${id})" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round"/>`
        + `<g class="ballg"><circle class="base" fill="var(--th-surface, none)" stroke="currentColor" stroke-width="1.4"/>`
        + `<g clip-path="url(#thc${id})"><g class="str" fill="none" stroke="currentColor" stroke-width=".95"></g></g>`
        + `<circle class="seed" fill="currentColor"/></g>`
        + `<circle class="bead" r="2.9" fill="var(--th-surface, none)" stroke="currentColor" stroke-width="1.35"/>`
        + `<g transform="translate(0 2.5) scale(.46875)">${SHEEP}</g></svg>`;
      const q = s => host.querySelector(s);
      this.el = { grad: q('linearGradient'), end: q('.end'), clip: q('clipPath circle'), line: q('.line'), ballg: q('.ballg'),
                  base: q('.base'), str: q('.str'), seed: q('.seed'), bead: q('.bead') };
      this.drawnR = -1;
      this.draw();
    }

    draw() {
      const { x0, y0, el } = this, br = this.ballR, xe = this.end;
      el.line.setAttribute('d', this.path());
      el.grad.setAttribute('x1', x0); el.grad.setAttribute('x2', Math.max(xe, x0 + 1).toFixed(1));
      el.end.setAttribute('stop-opacity', (1 - 0.96 * this.fade).toFixed(3));
      el.ballg.setAttribute('transform', `translate(${(xe + br).toFixed(2)} ${(y0 - this.lift * 12).toFixed(2)})`);   // the end of the strand, wherever it is
      el.ballg.style.opacity = br < 0.4 ? 0 : 1;
      el.base.setAttribute('r', br.toFixed(2));
      el.seed.setAttribute('r', Math.min(br, 2.6).toFixed(2));
      el.seed.style.opacity = 1 - sstep((br - 2.9) / 1.2);
      if (Math.abs(br - this.drawnR) > 0.04) {
        this.drawnR = br; el.clip.setAttribute('r', Math.max(br - 0.4, 0.01).toFixed(2)); el.str.innerHTML = strands(br);
      }
      el.str.setAttribute('transform', `rotate(${(this.turn * 57.3 % 360).toFixed(1)})`);
      el.str.style.opacity = 0.62 * sstep((br - 3.3) / 1.6);
      const p = this.still ? 0.4 : 0.5 - 0.5 * Math.cos(this.t * TAU / 1.7);   // the bead goes to and fro
      el.bead.setAttribute('cx', (x0 + 12 + (xe - x0 - 22) * p).toFixed(2)); el.bead.setAttribute('cy', y0);
      el.bead.style.opacity = this.beadA.toFixed(2);
      el.bead.setAttribute('r', (2.9 * (0.6 + 0.4 * this.beadA)).toFixed(2));
    }
  }

  // What the strand should show for a reply that is being written: `message` is the reply, `turn` its state.
  function liveMode(message, turn) {
    const blocks = message.blocks || [], b = blocks[blocks.length - 1];
    if ((turn && turn.queue_position) || !b) return 'wait';
    if (b.type === 'thinking') return b.ms == null ? 'think' : 'wait';   // a finished thought: the next step is being prepared
    if (b.type === 'text') return 'write';
    if (b.type === 'tool' || b.type === 'image') return ['pending', 'checking', 'running'].includes(b.status) ? 'tool' : 'wait';
    return 'wait';
  }

  const DOING = { bash: 'Running a command', bash_output: 'Checking a command', kill_shell: 'Stopping a command',
    web_search: 'Searching the web', web_fetch: 'Reading a web page', read_file: 'Reading a file', list_files: 'Looking at the files',
    search: 'Searching the files', write_file: 'Writing a file', create_file: 'Writing a file', edit_file: 'Editing a file',
    task: 'A helper is working', generate_image: 'Making an image', search_project: 'Searching the project', memory: 'Updating memory',
    past_chats: 'Looking at earlier chats', create_artifact: 'Making an artifact', update_artifact: 'Updating the artifact',
    rewrite_artifact: 'Updating the artifact', read_artifact: 'Reading the artifact', research_plan: 'Planning the research',
    research_notes: 'Taking notes', todo_write: 'Updating the task list' };

  function span(seconds) {
    const s = Math.max(1, Math.round(seconds));
    return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`;
  }

  // The words beside the strand. `line` is the strand (its mode, seconds and quiet are read).
  function liveLabel(message, turn, line) {
    const blocks = message.blocks || [], b = blocks[blocks.length - 1] || {};
    const quiet = line.quiet >= 3 ? `quiet for ${span(line.quiet)}` : '';
    switch (line.mode) {
      case 'wait': return turn && turn.queue_position ? `Waiting for the GPU (${turn.queue_position} ahead)` : '';
      case 'think': return `Thinking… ${span(line.seconds)}` + (quiet ? ` · ${quiet}` : '');
      case 'tool': return (b.type === 'image' ? DOING.generate_image : DOING[b.name] || 'Using a tool') + '…';
      case 'write': return quiet ? quiet[0].toUpperCase() + quiet.slice(1) : '';
      default: return '';
    }
  }

  // The words beside a finished thought's ball. The server records how long a thought took (`ms`) since 0.9.3.
  function thoughtLabel(block) {
    return block.ms == null ? 'Thoughts' : `Thought for ${span(block.ms / 1000)}`;
  }

  globalThis.BaabaaThread = { Line, Thread, ballRadius, ballMarkup, liveMode, liveLabel, thoughtLabel };
})();
