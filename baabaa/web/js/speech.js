// Reading replies aloud with the browser's on-device voices. Voices the browser marks as remote (they send
// the text to a cloud service) are never offered or used.

export function supported() { return 'speechSynthesis' in window; }

export function localVoices() {
  if (!supported()) return [];
  return speechSynthesis.getVoices().filter(v => v.localService).sort((a, b) => a.lang.localeCompare(b.lang) || a.name.localeCompare(b.name));
}

export function voicesReady() {
  return new Promise(resolve => {
    if (!supported()) return resolve([]);
    const now = localVoices();
    if (now.length) return resolve(now);
    const done = () => resolve(localVoices());
    speechSynthesis.addEventListener('voiceschanged', done, { once: true });
    setTimeout(done, 1500);
  });
}

let current = null;
export function speaking(key) { return current && (key === undefined || current.key === key); }

export function stop() {
  if (supported()) speechSynthesis.cancel();
  const c = current;
  current = null;
  if (c && c.onend) c.onend();
}

export async function speak(text, { key, voiceURI, rate = 1, onend } = {}) {
  stop();
  const voices = await voicesReady();
  if (!voices.length) throw new Error('This browser has no on-device voices, so baabaa cannot read aloud here.');
  const lang = (navigator.language || 'en').slice(0, 2);
  const voice = voices.find(v => v.voiceURI === voiceURI) || voices.find(v => v.default && v.lang.startsWith(lang))
    || voices.find(v => v.lang.startsWith(lang)) || voices[0];
  const chunks = split(text, 220);
  const me = { key, onend };
  current = me;
  let i = 0;
  const next = () => {
    if (current !== me) return;
    if (i >= chunks.length) { current = null; if (onend) onend(); return; }
    const u = new SpeechSynthesisUtterance(chunks[i++]);
    u.voice = voice; u.lang = voice.lang; u.rate = rate;
    u.onend = next;
    u.onerror = () => { if (current === me) { current = null; if (onend) onend(); } };
    speechSynthesis.speak(u);
  };
  next();
}

// Short pieces: some browsers stop a long utterance part-way.
function split(text, max) {
  const out = [];
  for (const sentence of text.replace(/\s+/g, ' ').match(/[^.!?;:]+[.!?;:]*\s*/g) || []) {
    let s = sentence.trim();
    while (s.length > max) {
      const cut = s.lastIndexOf(' ', max) > max / 2 ? s.lastIndexOf(' ', max) : max;
      out.push(s.slice(0, cut));
      s = s.slice(cut).trim();
    }
    if (!s) continue;
    if (out.length && out[out.length - 1].length + s.length < max) out[out.length - 1] += ' ' + s;
    else out.push(s);
  }
  return out;
}

// The words of a rendered reply, without code, tool steps or thoughts.
export function textOf(el) {
  const c = el.cloneNode(true);
  for (const x of c.querySelectorAll('pre, .tool, .tool-wrap, details, .thinking, .msg-actions, .katex, math, .mermaid-block, .file-cards')) x.remove();
  return (c.textContent || '').replace(/\s+/g, ' ').trim();
}
