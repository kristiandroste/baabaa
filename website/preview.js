// baabaa's preview: a stand-in for the server that runs in the page. It answers the app's requests from a
// snapshot the real server made and plays recorded replies back as live events (tools/website.py makes both
// from website/script.py). It is loaded before the app, whose own files are unchanged. Nothing here runs a
// model and nothing leaves the page: every request the app makes ends in this file.
(function () {
  'use strict';

  const D = window.BAABAA_PREVIEW;
  const NOTE = 'This is a preview with recorded replies. Install baabaa to do this with your own models.';
  const HOME = D.recordings.filter(r => r.home).map(r => '“' + r.prompt + '”');
  const clone = x => JSON.parse(JSON.stringify(x));
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const norm = s => String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
  let seq = 0;
  const newId = () => 'pv' + Date.now().toString(36) + (seq++).toString(36) + Math.random().toString(36).slice(2, 7);

  // The recordings were made on some earlier day; here they happened just now.
  (function shift(x, by) {
    if (Array.isArray(x)) x.forEach(v => shift(v, by));
    else if (x && typeof x === 'object') {
      for (const k of Object.keys(x)) {
        if (typeof x[k] === 'number' && x[k] > 1e12 && /_ms$/.test(k)) x[k] += by;
        else shift(x[k], by);
      }
    }
  })(D, Date.now() - D.built_ms - 60000);

  // ---- what the stand-in remembers: the conversations, as the server would hold them ---------------------
  const convs = new Map();    // id -> { conversation, listed, thread, state, artifacts, context, trusted, shells, project, run }
  for (const r of D.recordings) {
    if (r.shown) convs.set(r.final.conversation.id, { ...clone(r.final), listed: clone(r.listed), run: null });
  }
  const me = D.snapshot['/api/me'];
  const sources = new Set();

  function publish(type, data) {
    remember(type, data);
    for (const s of sources) s.emit(type, data);
  }

  // Keep the conversation as the server would, so a page that opens it part-way sees where it stands.
  function remember(type, d) {
    const c = convs.get(d.conv_id || (d.conversation && d.conversation.id));
    if (!c) return;
    const msg = id => c.thread.find(m => m.id === id);
    if (type === 'msg.new' || type === 'msg.done') {
      const m = { siblings: [d.message.id], sibling_index: 0, ...clone(d.message) }, i = c.thread.findIndex(x => x.id === m.id);
      if (i >= 0) c.thread[i] = m; else c.thread.push(m);
      c.conversation.leaf_id = c.listed.leaf_id = m.id;
      c.conversation.updated_ms = c.listed.updated_ms = Date.now();
    } else if (type === 'msg.block') {
      const m = msg(d.msg_id);
      if (m) m.blocks[d.index] = clone(d.block);
    } else if (type === 'msg.delta') {
      const m = msg(d.msg_id), b = m && m.blocks[d.index];
      if (b) b.text = (b.text || '') + d.text;
    } else if (type === 'turn') {
      c.state = d.state === 'idle' ? null : { ...d, pending: (c.state && c.state.pending) || [] };
      c.listed.running = d.state !== 'idle';
    } else if (type === 'pending') {
      if (c.state) c.state.pending.push(clone(d));
    } else if (type === 'pending.done') {
      if (c.state) c.state.pending = c.state.pending.filter(p => p.id !== d.id);
    } else if (type === 'conv') {
      Object.assign(c.conversation, d.conversation);
      for (const k of Object.keys(c.listed)) if (k in d.conversation) c.listed[k] = d.conversation[k];
    } else if (type === 'artifact') {
      const i = c.artifacts.findIndex(a => a.id === d.artifact.id);
      if (i >= 0) c.artifacts[i] = d.artifact; else c.artifacts.push(d.artifact);
    } else if (type === 'context' && d.used) {
      c.context = { used: d.used, num_ctx: d.num_ctx };
    } else if (type === 'todos') {
      c.conversation.settings.todos = d.todos;
    }
  }

  // ---- playing a recording --------------------------------------------------------------------------------
  const TEMPLATE = D.recordings.find(r => !r.folder), FOLDER = D.recordings.find(r => r.folder);

  // A recording under new names, so the same one can be played in any number of conversations, after any message.
  function renamed(rec, convId, parent) {
    let text = JSON.stringify(rec.events).split(rec.created.id).join(convId);
    const ids = new Set();
    for (const [type, d] of rec.events) {
      if (d.message) ids.add(d.message.id);
      if (type === 'pending') ids.add(d.id);
    }
    for (const id of ids) text = text.split(id).join(newId());
    const events = JSON.parse(text), first = events.find(e => e[0] === 'msg.new' && e[1].message.role === 'user');
    if (first) first[1].message.parent_id = parent || null;
    return events;
  }

  // Text arrives a few words at a time, at the speed of a mid-sized model.
  async function trickle(run, d) {
    const pieces = d.text.match(/\s*\S+(?:\s+\S+)?\s*|\s+/g) || [d.text];
    let len = d.len - d.text.length;
    for (const piece of pieces) {
      await sleep(piece.length / 4 / D.rate * 1000);
      if (run.stopped) return;
      len += piece.length;
      publish('msg.delta', { ...d, text: piece, len });
    }
  }

  async function play(c, events) {
    const run = c.run = { stopped: false, always: false, answer: null, last: null };
    for (const [type, d] of events) {
      if (run.stopped) return;
      const b = type === 'msg.block' ? d.block : null;
      if (run.always && (type === 'pending' || type === 'pending.done' || (b && b.status === 'waiting')
          || (type === 'turn' && d.state === 'waiting'))) continue;      // the person said: always allow this
      if (b && b.type === 'thinking' && b.ms == null) await sleep(250);   // the model starts on a thought
      if (run.stopped) return;
      if (type === 'msg.delta') { await trickle(run, d); continue; }
      publish(type, d);
      if (b && b.type === 'tool') run.last = d;
      if (type === 'msg.new' && d.message.role === 'assistant') await sleep(800);       // the model reads the conversation
      else if (b && b.type === 'tool' && b.status === 'running') await sleep(Math.min(Math.max(b.duration_ms || 0, 1000), 2500));
      else if (b && b.type === 'tool' && b.status === 'done') await sleep(550);          // it reads what the tool said
      else if (type === 'pending') {
        const decision = await new Promise(r => { run.answer = r; });
        run.answer = null;
        if (run.stopped) return;
        if (decision === 'deny') return declined(c, run, d);
        if (decision === 'allow_always') run.always = true;
      }
    }
    c.run = null;
  }

  // The recording only knows the path where the command was allowed.
  function declined(c, run, p) {
    const m = c.thread.find(x => x.id === p.msg_id), i = m.blocks.findIndex(x => x.id === p.block_id);
    publish('pending.done', { id: p.id, conv_id: p.conv_id });
    publish('msg.block', { conv_id: p.conv_id, msg_id: m.id, index: i,
      block: { ...m.blocks[i], status: 'denied', decision: { ...(m.blocks[i].decision || {}), reason: 'Declined' }, output: 'Not run.' } });
    const text = 'I did not run it. In this preview only the path where the command is allowed was recorded; with a real model, '
      + 'baabaa would now tell it that you declined and let it try another way.';
    publish('msg.block', { conv_id: p.conv_id, msg_id: m.id, index: m.blocks.length, block: { type: 'text', text } });
    finish(c, 'ok');
  }

  function finish(c, status) {
    const m = c.thread[c.thread.length - 1];
    if (c.run) { c.run.stopped = true; if (c.run.answer) c.run.answer('stop'); }
    c.run = null;
    for (const b of m.blocks) {
      if (b.type === 'tool' && ['pending', 'checking', 'waiting', 'running'].includes(b.status)) { b.status = 'stopped'; b.output = b.output || 'Stopped.'; }
      if (b.type === 'thinking' && b.ms == null) b.ms = 1000;
    }
    for (const p of (c.state && c.state.pending) || []) publish('pending.done', { id: p.id, conv_id: c.conversation.id });
    publish('queue', { busy: false, running: null, waiting: 0, paused: null });
    publish('msg.done', { conv_id: c.conversation.id, message: { ...m, status, meta: { ...(m.meta || {}), duration_ms: Date.now() - m.created_ms } } });
    publish('turn', { conv_id: c.conversation.id, state: 'idle' });
  }

  // A message no recording answers gets a note, not a reply: nothing here writes like a model.
  function unrecorded(c, text) {
    const cid = c.conversation.id, user = TEMPLATE.events.find(e => e[0] === 'msg.new' && e[1].message.role === 'user')[1].message;
    const model = TEMPLATE.events.find(e => e[0] === 'msg.new' && e[1].message.role === 'assistant')[1].message;
    const now = Date.now(), parent = c.conversation.leaf_id || null, uid = newId(), aid = newId();
    const tried = c.conversation.folder
      ? 'In a working folder, the suggestion “' + FOLDER.prompt + '” is recorded.'
      : 'The suggestions on the home page are recorded: ' + HOME.join(', ') + '. Under Code, with the sample folder, so is “' + FOLDER.prompt + '”.';
    const note = 'Nothing runs a model in this preview, so there is no reply to that. ' + tried
      + ' Installed on your own computer, baabaa answers with the models you choose, and nothing leaves your machine.';
    publish('msg.new', { conv_id: cid, message: { ...user, id: uid, conv_id: cid, parent_id: parent, blocks: [{ type: 'text', text }], created_ms: now, updated_ms: now } });
    const reply = { ...model, id: aid, conv_id: cid, parent_id: uid, status: 'ok', blocks: [{ type: 'notice', text: note }], created_ms: now, updated_ms: now,
      meta: { ...(model.meta || {}), duration_ms: 0 } };
    publish('msg.new', { conv_id: cid, message: reply });
    publish('msg.done', { conv_id: cid, message: reply });
    if (!c.conversation.title) publish('conv', { conversation: { ...c.conversation, title: text.split(/\s+/).slice(0, 6).join(' ').slice(0, 60) } });
  }

  function send(c, body) {
    if (c.run || c.state) throw fail(409, 'baabaa is still writing the last reply.');
    const text = String(body.text || '').trim();
    if (!text) throw fail(400, 'The message is empty.');
    const rec = D.recordings.find(r => norm(r.prompt) === norm(text) && r.folder === !!c.conversation.folder);
    if (rec) play(c, renamed(rec, c.conversation.id, c.conversation.leaf_id));
    else unrecorded(c, text);
    return { ok: true };
  }

  // ---- the requests ------------------------------------------------------------------------------------------
  function fail(status, error) { return Object.assign(new Error(error), { status }); }

  function create(body) {
    const base = body.folder ? FOLDER : TEMPLATE, now = Date.now(), id = newId();
    const conversation = { ...clone(base.created), id, title: '', created_ms: now, updated_ms: now, folder: body.folder || null,
      incognito: !!body.incognito, mode: body.mode || base.created.mode, leaf_id: null };
    if (body.think !== undefined) conversation.settings = { ...conversation.settings, think: body.think };
    const listed = { ...clone(base.listed), ...conversation, running: false };
    delete listed.settings;
    convs.set(id, { ...clone(base.final), conversation, listed, thread: [], state: null, artifacts: [], context: null,
      trusted: body.folder ? true : null, run: null });
    return { conversation };
  }

  function listConversations(q) {
    let all = [...convs.values()].filter(c => !c.conversation.incognito && !!c.conversation.archived === (q.get('archived') === '1'));
    if (q.get('folders') === '1') all = all.filter(c => c.conversation.folder);
    if (q.get('project')) all = all.filter(c => c.conversation.project_id === q.get('project'));
    return { conversations: all.map(c => clone(c.listed)).sort((a, b) => b.updated_ms - a.updated_ms) };
  }

  function search(q) {
    const want = q.trim().toLowerCase(), results = [];
    if (!want) return { results };
    for (const c of convs.values()) {
      const texts = [c.conversation.title || ''].concat(c.thread.flatMap(m => m.blocks.map(b => (b.type === 'text' && b.text) || '')));
      const hit = texts.find(t => t.toLowerCase().includes(want));
      if (hit === undefined) continue;
      const at = hit.toLowerCase().indexOf(want), from = Math.max(0, at - 50);
      results.push({ conv_id: c.conversation.id, title: c.conversation.title, msg_id: null,
        snippet: (from ? '…' : '') + hit.slice(from, at) + '[[' + hit.slice(at, at + want.length) + ']]' + hit.slice(at + want.length, at + want.length + 70) });
    }
    return { results };
  }

  function handle(method, path, q, body) {
    let m;
    if (method === 'GET') {
      if (path === '/api/conversations') return listConversations(q);
      if ((m = /^\/api\/conversations\/(\w+)$/.exec(path))) {
        const c = convs.get(m[1]);
        if (!c) throw fail(404, 'That conversation is not in this preview.');
        const { run, listed, ...open } = c;      // eslint-disable-line no-unused-vars
        return clone(open);
      }
      if (path === '/api/search') return search(q.get('q') || '');
      if (path === '/api/folders') return clone(D.folders[q.get('path') || ''] || D.folders['']);
      if (path === '/api/me') return clone(me);
      if (path in D.snapshot) return clone(D.snapshot[path]);
      throw fail(404, 'That is not part of this preview.');
    }
    if (path === '/api/conversations' && method === 'POST') return create(body);
    if (path === '/api/me' && method === 'PATCH') {
      me.account = { ...me.account, ...body, settings: { ...me.account.settings, ...(body.settings || {}) } };
      return { account: clone(me.account) };
    }
    if (path === '/api/folders/trust') return { trusted: true, path: body.path };
    if (path === '/api/logout') return { ok: true };
    if ((m = /^\/api\/pending\/(\w+)$/.exec(path))) {
      for (const c of convs.values()) {
        if (c.run && c.run.answer && c.state && c.state.pending.some(p => p.id === m[1])) {
          c.run.answer(body.decision === 'deny' ? 'deny' : body.decision === 'allow_always' ? 'allow_always' : 'allow_once');
          return { ok: true };
        }
      }
      throw fail(404, 'That question has already been answered.');
    }
    if ((m = /^\/api\/conversations\/(\w+)(?:\/(\w+))?$/.exec(path))) {
      const c = convs.get(m[1]);
      if (!c) throw fail(404, 'That conversation is not in this preview.');
      if (!m[2] && method === 'PATCH') {
        const change = { ...body, settings: { ...c.conversation.settings, ...(body.settings || {}) } };
        publish('conv', { conversation: { ...c.conversation, ...change } });
        return { conversation: clone(c.conversation) };
      }
      if (!m[2] && method === 'DELETE') {
        if (D.recordings.some(r => r.shown && r.final.conversation.id === m[1])) throw fail(403, 'That conversation is part of the preview; the others can go.');
        if (c.run) c.run.stopped = true;
        convs.delete(m[1]);
        for (const s of sources) s.emit('conv.deleted', { conv_id: m[1] });
        return { ok: true };
      }
      if (m[2] === 'messages' && method === 'POST') return send(c, body);
      if (m[2] === 'stop' && method === 'POST') { if (c.state) finish(c, 'stopped'); return { ok: true }; }
      if (m[2] === 'feedback' && method === 'POST') return { ok: true };
    }
    throw fail(403, NOTE);
  }

  const answer = (status, data) => new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json' } });
  const realFetch = window.fetch.bind(window);
  window.fetch = async function (input, init) {
    const url = new URL(typeof input === 'string' ? input : input.url, location.href);
    init = init || {};
    if (url.origin !== location.origin) return answer(403, { error: NOTE });
    if (!url.pathname.startsWith('/api/')) return realFetch(local(url.pathname + url.search), init);   // the app's own files
    await sleep(15);
    let body = {};
    if (typeof init.body === 'string') { try { body = JSON.parse(init.body); } catch { body = {}; } }
    try {
      return answer(200, handle((init.method || 'GET').toUpperCase(), url.pathname, url.searchParams, body));
    } catch (e) {
      if (!e.status) console.error('preview', e);
      return answer(e.status || 500, { error: e.status ? e.message : 'The preview stumbled on that request.' });
    }
  };

  // Uploads use XMLHttpRequest; in the preview there is nowhere to put a file.
  window.XMLHttpRequest = class {
    constructor() { this.upload = {}; this.status = 0; this.responseText = ''; }
    open() {}
    setRequestHeader() {}
    send() { setTimeout(() => { this.status = 403; this.responseText = JSON.stringify({ error: NOTE }); if (this.onload) this.onload(); }, 30); }
  };

  // The live event stream.
  class Events {
    constructor() {
      this.readyState = 0;
      this.handlers = {};
      sources.add(this);
      setTimeout(() => {
        if (this.readyState === 2) return;
        this.readyState = 1;
        if (this.onopen) this.onopen({});
        this.emit('hello', { version: D.version, account_id: me.account.id, time: Date.now() });
      }, 20);
    }
    addEventListener(type, fn) { (this.handlers[type] = this.handlers[type] || []).push(fn); }
    removeEventListener(type, fn) { this.handlers[type] = (this.handlers[type] || []).filter(f => f !== fn); }
    emit(type, data) { const text = JSON.stringify(data); for (const fn of this.handlers[type] || []) fn({ type, data: text }); }
    close() { this.readyState = 2; sources.delete(this); }
  }
  Events.CONNECTING = 0; Events.OPEN = 1; Events.CLOSED = 2;
  window.EventSource = Events;

  // The app names its files and frames from the top of the site; the preview may live in a folder.
  function local(path) {
    const frame = /^\/artifact-frame\/(\w+)\?v=(\d+)/.exec(path);
    if (frame) return D.frames.includes(frame[1] + '-' + frame[2]) ? 'artifact-frame/' + frame[1] + '-' + frame[2] + '.html' : 'about:blank';
    if (/^\/(css|js|img)\//.test(path)) return path.slice(1);
    if (/^\/(api\/|ca\.crt)/.test(path)) return '#';
    return path;
  }
  const setAttribute = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function (name, value) {
    if ((name === 'src' || name === 'href') && typeof value === 'string' && value[0] === '/' && value[1] !== '/') value = local(value);
    return setAttribute.call(this, name, value);
  };
  const open = window.open.bind(window);
  window.open = (url, ...rest) => open(typeof url === 'string' && url[0] === '/' ? local(url) : url, ...rest);
  try { if (navigator.serviceWorker) navigator.serviceWorker.register = () => Promise.resolve({}); } catch { /* the preview has no service worker */ }

  // The bar that says what this is.
  document.addEventListener('DOMContentLoaded', () => {
    const bar = document.createElement('div');
    bar.className = 'pv-bar';
    bar.innerHTML = '<span><strong>Preview.</strong> The replies here are recorded; nothing runs a model.</span>'
      + '<a href="../">About baabaa</a><a href="../docs/?docs/TUTORIAL.md">Install it</a>';
    document.body.prepend(bar);
  });
})();
