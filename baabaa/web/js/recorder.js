// Records the microphone as 16 kHz mono WAV, in the browser, for dictation on this computer's GPU.
// (The browser's own speech recognition is not used: some browsers send the audio to a cloud service.)

export function supported() {
  return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.AudioWorkletNode && window.isSecureContext);
}

export async function start(onLevel) {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
  const ctx = new AudioContext();
  await ctx.audioWorklet.addModule('/js/recorder-worklet.js');
  const src = ctx.createMediaStreamSource(stream);
  const node = new AudioWorkletNode(ctx, 'baabaa-capture');
  const chunks = [];
  let total = 0;
  node.port.onmessage = e => {
    chunks.push(e.data);
    total += e.data.length;
    if (onLevel) {
      let peak = 0;
      for (const v of e.data) peak = Math.max(peak, Math.abs(v));
      onLevel(peak, total / ctx.sampleRate);
    }
  };
  src.connect(node);
  const rate = ctx.sampleRate;
  const cleanup = () => { try { src.disconnect(); node.disconnect(); } catch { /* already */ } stream.getTracks().forEach(t => t.stop()); ctx.close(); };
  return {
    cancel: cleanup,
    stop: () => { cleanup(); return wav(resample(join(chunks, total), rate, 16000), 16000); },
  };
}

function join(chunks, total) {
  const out = new Float32Array(total);
  let o = 0;
  for (const c of chunks) { out.set(c, o); o += c.length; }
  return out;
}

function resample(x, from, to) {
  if (from === to) return x;
  const ratio = from / to, n = Math.floor(x.length / ratio), out = new Float32Array(n);
  for (let i = 0; i < n; i++) {  // average over each output sample's span (a simple low-pass)
    const a = Math.floor(i * ratio), b = Math.min(x.length, Math.floor((i + 1) * ratio));
    let s = 0;
    for (let j = a; j < b; j++) s += x[j];
    out[i] = s / Math.max(1, b - a);
  }
  return out;
}

function wav(samples, rate) {
  const buf = new ArrayBuffer(44 + samples.length * 2), v = new DataView(buf);
  const str = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  str(0, 'RIFF'); v.setUint32(4, 36 + samples.length * 2, true); str(8, 'WAVE'); str(12, 'fmt ');
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, rate, true);
  v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); str(36, 'data');
  v.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) v.setInt16(44 + i * 2, Math.max(-1, Math.min(1, samples[i])) * 0x7fff, true);
  return new Blob([buf], { type: 'audio/wav' });
}
