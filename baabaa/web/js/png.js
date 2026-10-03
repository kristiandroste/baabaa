// Save part of the page as a PNG, drawn by the browser itself (SVG foreignObject onto a canvas).
import { download } from './dom.js';

let cssText = null;
async function pageCss() {
  if (cssText !== null) return cssText;
  try { cssText = await (await fetch('/css/app.css')).text(); } catch { cssText = ''; }
  return cssText;
}

export async function nodeToPng(node, filename, scale = 2) {
  const width = Math.ceil(node.getBoundingClientRect().width);
  const clone = node.cloneNode(true);
  clone.querySelectorAll('.msg-actions, .pending-card, button.code-copy').forEach(e => e.remove());
  clone.querySelectorAll('details').forEach(d => d.setAttribute('open', ''));
  clone.style.width = width + 'px';
  clone.style.margin = '0';
  const root = document.documentElement;
  const theme = root.dataset.theme, font = root.dataset.font;
  const bg = getComputedStyle(document.body).backgroundColor;
  const wrapper = document.createElement('div');
  wrapper.setAttribute('xmlns', 'http://www.w3.org/1999/xhtml');
  wrapper.className = 'png-root';
  wrapper.style.cssText = `width:${width}px;padding:24px;background:${bg};box-sizing:content-box`;
  wrapper.appendChild(clone);
  // measure offscreen
  const probe = document.createElement('div');
  probe.style.cssText = 'position:fixed;left:-99999px;top:0';
  probe.appendChild(wrapper);
  document.body.appendChild(probe);
  const height = Math.ceil(wrapper.getBoundingClientRect().height);
  probe.remove();
  if (height > 30000) throw new Error('That is too long to save as one image; save a single message instead.');
  const css = (await pageCss()).replace(/<\/style/gi, '');
  const xhtml = new XMLSerializer().serializeToString(wrapper);
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width + 48}" height="${height}">`
    + `<foreignObject width="100%" height="100%"><div xmlns="http://www.w3.org/1999/xhtml" data-theme="${theme}" data-font="${font}" class="png-html">`
    + `<style>${css}</style>${xhtml}</div></foreignObject></svg>`;
  const img = new Image();
  const url = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
  await new Promise((res, rej) => { img.onload = res; img.onerror = () => rej(new Error('The browser could not draw the image')); img.src = url; });
  const canvas = document.createElement('canvas');
  canvas.width = (width + 48) * scale;
  canvas.height = height * scale;
  const g = canvas.getContext('2d');
  g.scale(scale, scale);
  g.drawImage(img, 0, 0);
  const blob = await new Promise(res => canvas.toBlob(res, 'image/png'));
  if (!blob) throw new Error('The image could not be created');
  download(filename, blob);
}
