"""Exports: a conversation as Markdown, JSON or a self-contained HTML page; artifacts as documents.

The HTML export inlines the same renderer the browser app uses, so it looks the same and works offline.
"""

import html
import json
import time
from pathlib import Path

from . import __version__
from .util import clip

WEB = Path(__file__).resolve().parent / "web"


def _texts(blocks, kind="text") -> str:
    return "\n\n".join(b.get("text", "") for b in blocks if b.get("type") == kind)


def to_markdown(conv: dict, messages: list[dict]) -> str:
    out = [f"# {conv['title'] or 'Conversation'}", "",
           f"*Exported from baabaa {__version__} on {time.strftime('%Y-%m-%d %H:%M')}*", ""]
    for m in messages:
        if m["role"] == "user":
            out += ["## You", ""]
            for b in m["blocks"]:
                if b.get("type") == "text":
                    out += [b["text"], ""]
                elif b.get("type") == "attachment":
                    out += [f"*Attached: {b.get('name')}*", ""]
        elif m["role"] == "assistant":
            out += [f"## baabaa ({m.get('model') or 'model'})", ""]
            for b in m["blocks"]:
                t = b.get("type")
                if t == "thinking" and b.get("text"):
                    out += ["<details><summary>Thinking</summary>", "", b["text"], "", "</details>", ""]
                elif t == "text":
                    out += [b.get("text", ""), ""]
                elif t == "tool":
                    args = json.dumps(b.get("args"), ensure_ascii=False, indent=1)
                    out += [f"**Tool: {b.get('name')}** ({b.get('status')})", "", "```json", clip(args, 4000), "```", ""]
                    if b.get("output"):
                        out += ["```", clip(b["output"], 8000), "```", ""]
                elif t in ("error", "notice"):
                    out += [f"> {b.get('text', '')}", ""]
        elif m["role"] == "compaction":
            out += ["---", "", "*Earlier messages were summarized:*", "", _texts(m["blocks"]), "", "---", ""]
    return "\n".join(out).rstrip() + "\n"


def export_conversation(store, conv: dict, fmt: str, scope: str = "branch"):
    messages = store.all_messages(conv["id"]) if scope == "all" else store.thread(conv["id"])
    if fmt == "md":
        return to_markdown(conv, messages).encode(), "text/markdown; charset=utf-8", "md"
    if fmt == "json":
        data = {"format": "baabaa-conversation", "version": 1, "exported_ms": int(time.time() * 1000),
                "conversation": conv, "messages": messages, "artifacts": [store.artifact(a["id"]) for a in store.artifacts(conv["id"])]}
        return json.dumps(data, ensure_ascii=False, indent=1).encode(), "application/json; charset=utf-8", "json"
    if fmt == "html":
        return to_html(conv, messages).encode(), "text/html; charset=utf-8", "html"
    raise ValueError("Export format is md, json or html")


def _asset(rel: str) -> str:
    try:
        return (WEB / rel).read_text(encoding="utf-8")
    except OSError:
        return ""


def to_html(conv: dict, messages: list[dict]) -> str:
    data = json.dumps({"conversation": {"title": conv["title"], "model": conv["model"]}, "messages": messages},
                      ensure_ascii=False).replace("</", "<\\/")
    css = _asset("css/app.css")
    js = "\n".join(_asset(f"js/{name}") for name in ("markdown.js", "highlight.js", "export-view.js"))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(conv['title'] or 'Conversation')} · baabaa</title>
<style>{css}</style></head>
<body class="export"><main id="export" class="thread export-thread"></main>
<script id="data" type="application/json">{data}</script>
<script>{js}</script></body></html>"""


def artifact_document(a: dict) -> str:
    kind, content = a["kind"], a["content"]
    title = html.escape(a["title"])
    if kind == "html":
        if "<html" in content[:500].lower() or "<!doctype" in content[:200].lower():
            return content
        return f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title></head><body>{content}</body></html>"
    base_css = "body{font:15px/1.6 system-ui,sans-serif;margin:24px;color:#1d1b18;background:#fff}pre{white-space:pre-wrap}"
    if kind == "svg":
        return (f"<!doctype html><html><head><meta charset='utf-8'><style>body{{margin:0;display:grid;place-items:center;"
                f"min-height:100vh;background:#fff}}svg{{max-width:100%;height:auto}}</style></head><body>{content}</body></html>")
    payload = json.dumps({"kind": kind, "language": a.get("language"), "content": content}).replace("</", "<\\/")
    js = "\n".join(_asset(f"js/{n}") for n in ("markdown.js", "highlight.js", "mermaid-lite.js", "artifact-view.js"))
    css = _asset("css/artifact.css") or base_css
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title><style>{css}</style></head>"
            f"<body><div id='root'></div><script id='data' type='application/json'>{payload}</script>"
            f"<script>{js}</script></body></html>")
