// The server API: JSON requests with the CSRF token, uploads, downloads, and the live event stream.

let csrf = null;
export function setCsrf(token) { csrf = token; }

export class ApiError extends Error {
  constructor(status, message, data) { super(message); this.status = status; this.data = data; }
}

export async function api(method, path, body, opts = {}) {
  const headers = { 'X-Baabaa-Client': 'browser' };
  if (csrf) headers['X-CSRF-Token'] = csrf;
  let payload;
  if (body instanceof Blob || body instanceof ArrayBuffer) {
    payload = body;
    if (opts.contentType) headers['Content-Type'] = opts.contentType;
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, { method, headers, body: payload, credentials: 'same-origin', signal: opts.signal });
  } catch (e) {
    throw new ApiError(0, 'baabaa is not reachable. Is the server running?');
  }
  if (opts.raw) {
    if (!res.ok) throw new ApiError(res.status, await errorText(res));
    return res;
  }
  let data = null;
  const type = res.headers.get('content-type') || '';
  if (type.includes('json')) data = await res.json();
  if (!res.ok) {
    const msg = (data && data.error) || res.statusText || `HTTP ${res.status}`;
    throw new ApiError(res.status, msg, data);
  }
  return data;
}

async function errorText(res) {
  try { const d = await res.json(); return d.error || res.statusText; } catch { return res.statusText; }
}

export const get = (p, o) => api('GET', p, undefined, o);
export const post = (p, b, o) => api('POST', p, b ?? {}, o);
export const patch = (p, b) => api('PATCH', p, b ?? {});
export const del = (p, b) => api('DELETE', p, b);

export function upload(file, convId, onProgress) {
  const q = new URLSearchParams({ name: file.name });
  if (convId) q.set('conv_id', convId);
  return uploadTo(`/api/uploads?${q}`, file, onProgress).then(d => d.attachment);
}

export function uploadTo(url, file, onProgress) {
  // XMLHttpRequest for upload progress. Resolves with the JSON reply.
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url);
    xhr.setRequestHeader('X-CSRF-Token', csrf || '');
    xhr.setRequestHeader('X-Baabaa-Client', 'browser');
    xhr.setRequestHeader('Content-Type', file.type || 'application/octet-stream');
    xhr.upload.onprogress = e => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch { data = null; }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new ApiError(xhr.status, (data && data.error) || 'Upload failed'));
    };
    xhr.onerror = () => reject(new ApiError(0, 'Upload failed'));
    xhr.send(file);
  });
}

export async function downloadFrom(path, fallbackName) {
  const res = await api('GET', path, undefined, { raw: true });
  const disp = res.headers.get('content-disposition') || '';
  const m = /filename="([^"]+)"/.exec(disp);
  const blob = await res.blob();
  const { download } = await import('./dom.js');
  download(m ? m[1] : fallbackName, blob);
}

// Live events --------------------------------------------------------------------------------------
const listeners = new Map();
let source = null;
let retry = 1000;
let onStatus = () => {};

export function on(type, fn) {
  if (!listeners.has(type)) listeners.set(type, new Set());
  listeners.get(type).add(fn);
  return () => listeners.get(type).delete(fn);
}

function emit(type, data) {
  for (const fn of listeners.get(type) || []) {
    try { fn(data); } catch (e) { console.error('event handler', type, e); }
  }
  for (const fn of listeners.get('*') || []) {
    try { fn(type, data); } catch (e) { console.error(e); }
  }
}

const TYPES = ['hello', 'msg.new', 'msg.delta', 'msg.block', 'msg.done', 'turn', 'pending', 'pending.done', 'conv',
  'conv.reload', 'conv.deleted', 'context', 'todos', 'artifact', 'tool.output', 'notice', 'job', 'models.updated',
  'queue', 'rules', 'project', 'project.file', 'memory', 'schedule', 'share', 'image.progress', 'update', 'restart'];

export function connectEvents(statusFn) {
  if (statusFn) onStatus = statusFn;
  if (source) source.close();
  source = new EventSource('/api/events');
  for (const t of TYPES) {
    source.addEventListener(t, e => {
      let data = null;
      try { data = JSON.parse(e.data); } catch { return; }
      emit(t, data);
    });
  }
  source.onopen = () => { retry = 1000; onStatus(true); };
  source.onerror = () => {
    onStatus(false);
    if (source.readyState === EventSource.CLOSED) {
      setTimeout(() => connectEvents(), retry);
      retry = Math.min(retry * 2, 15000);
    }
  };
}

export function disconnectEvents() {
  if (source) source.close();
  source = null;
}
