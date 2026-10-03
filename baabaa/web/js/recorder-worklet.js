// Collects microphone samples for recorder.js (an AudioWorklet processor; runs in the audio thread).
class Capture extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch && ch.length) this.port.postMessage(ch.slice(0));
    return true;
  }
}
registerProcessor('baabaa-capture', Capture);
