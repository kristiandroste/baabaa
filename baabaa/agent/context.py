"""Turn a conversation thread into Ollama chat messages, fitting the model's context window.

Two ways of saving space, cheapest first:
1. clearing old tool output (no model call): the oldest tool results are replaced by a short note;
2. summarizing (a model call, in compaction.py) when clearing is not enough.
The stored conversation is never changed by either; only the view sent to the model is.
"""

import base64
import os

CLEARED = "[Old tool output cleared to save space. Run the tool again if you need it.]"
KEEP_RECENT_TOOLS = 4
LONG_ARG = 2000   # a failed or refused call's arguments longer than this are cut in the model's view
DEFAULT_CHARS_PER_TOKEN = 3.3


def estimate_tokens(messages: list[dict], chars_per_token: float = DEFAULT_CHARS_PER_TOKEN) -> int:
    chars = 0
    for m in messages:
        chars += len(m.get("content") or "") + 16
        for c in m.get("tool_calls") or []:
            chars += len(str(c.get("function", {}).get("arguments", ""))) + 40
        chars += 1100 * 4 * len(m.get("images") or [])  # an image costs roughly a thousand tokens
    return int(chars / chars_per_token)


def tools_tokens(tools: list[dict], chars_per_token: float = DEFAULT_CHARS_PER_TOKEN) -> int:
    import json
    return int(len(json.dumps(tools)) / chars_per_token)


def user_content(msg: dict, store, vision: bool) -> tuple[str, list[str]]:
    texts, images = [], []
    for b in msg["blocks"]:
        t = b.get("type")
        if t == "text":
            texts.append(b.get("expanded") or b.get("text", ""))
        elif t == "hook_context":
            texts.append(b.get("text", ""))
        elif t == "attachment":
            att = store.attachment(b.get("id")) if store else None
            if att is None:
                continue
            if att["kind"] == "image":
                if vision:
                    try:
                        with open(att["path"], "rb") as f:
                            images.append(base64.b64encode(f.read()).decode())
                        texts.append(f"[Image attached: {att['name']}]")
                    except OSError:
                        texts.append(f"[Image {att['name']} could not be read]")
                else:
                    texts.append(f"[Image attached: {att['name']}; the current model cannot see images]")
            elif att.get("text"):
                body = att["text"]
                if len(body) > 60000:
                    body = body[:60000] + "\n[… truncated]"
                texts.append(f'<file name="{att["name"]}">\n{body}\n</file>')
            else:
                texts.append(f"[File attached: {att['name']} ({att['mime']}, {att['size']} bytes); no text could be extracted]")
    return "\n\n".join(t for t in texts if t), images


def assistant_messages(blocks: list[dict]) -> list[dict]:
    """One stored turn -> assistant/tool messages in Ollama's format."""
    out, text, calls, results = [], [], [], []

    def flush():
        nonlocal text, calls, results
        if text or calls:
            m = {"role": "assistant", "content": "".join(text)}
            if calls:
                m["tool_calls"] = calls
            out.append(m)
            out.extend(results)
        text, calls, results = [], [], []

    for b in blocks:
        t = b.get("type")
        if b.get("research"):
            continue  # a research turn's steps: later turns see only its report
        if t == "image" and b.get("status") == "done":
            if calls:
                flush()
            text.append(f"\n[I made an image{' (an edit)' if b.get('edit') else ''}: {b.get('prompt', '')}]\n")
            continue
        if t == "text":
            if calls:
                flush()
            text.append(b.get("text", ""))
            for a in b.get("artifacts") or []:  # pages from the reply that baabaa showed as artifacts
                text.append(f"\n[baabaa showed this to the user as artifact {a['id']} (\"{a['title']}\", version {a['version']}).]")
        elif t == "tool":
            if b.get("status") in ("pending", "running", "waiting"):
                continue
            args = b.get("args") or {}
            if b.get("status") in ("error", "denied"):
                args = _cut_args(args)
            calls.append({"function": {"name": b["name"], "arguments": args}})
            results.append({"role": "tool", "tool_name": b["name"], "content": b.get("output") or ""})
    flush()
    return out


def _cut_args(args: dict) -> dict:
    """A failed call's long arguments, cut. The model needs to know what failed, not to read the text again: one
    failed edit of a whole page (2026-10-01) carried 43,000 characters and filled the context window."""
    return {k: (v[:300] + f"\n[… {len(v):,} characters in all; not repeated because the call failed]"
                if isinstance(v, str) and len(v) > LONG_ARG else v) for k, v in args.items()}


def build(thread: list[dict], system: str, store=None, vision: bool = False, current_blocks: list | None = None) -> list[dict]:
    """Messages for the model: system prompt, latest summary, then the messages after it."""
    start, summary = 0, None
    for i, m in enumerate(thread):
        if m["role"] == "compaction":
            start, summary = i + 1, "\n".join(b.get("text", "") for b in m["blocks"] if b.get("type") == "text")
    messages = [{"role": "system", "content": system}]
    if summary:
        # As an exchange, not in the system prompt: small models treat it as memory that way.
        messages.append({"role": "user", "content": "Here is a summary of our conversation so far (the earlier "
                         "messages were compacted to save space):\n\n" + summary})
        messages.append({"role": "assistant", "content": "Understood. I have the summary of our earlier "
                         "conversation and will continue from there."})
    # Images are re-sent only for the three most recent user messages; older ones become a note.
    user_idx = [i for i, m in enumerate(thread) if m["role"] == "user"]
    recent = set(user_idx[-3:])
    for i, m in enumerate(thread[start:], start=start):
        if m["role"] == "user":
            content, images = user_content(m, store, vision and i in recent)
            msg = {"role": "user", "content": content or "(empty message)"}
            if images:
                msg["images"] = images
            messages.append(msg)
        elif m["role"] == "assistant":
            messages.extend(assistant_messages(m["blocks"]))
    if current_blocks:
        messages.extend(assistant_messages(current_blocks))
    return messages


def clear_old_tool_output(messages: list[dict], budget_tokens: int, cpt: float) -> tuple[list[dict], int]:
    """Replace the oldest tool results until the estimate fits `budget_tokens`. Returns (messages, cleared)."""
    tool_idx = [i for i, m in enumerate(messages) if m["role"] == "tool"]
    candidates = tool_idx[:-KEEP_RECENT_TOOLS] if len(tool_idx) > KEEP_RECENT_TOOLS else []
    cleared = 0
    if estimate_tokens(messages, cpt) <= budget_tokens:
        return messages, 0
    messages = [dict(m) for m in messages]
    for i in candidates:
        if len(messages[i].get("content") or "") > len(CLEARED):
            messages[i]["content"] = CLEARED
            cleared += 1
            if estimate_tokens(messages, cpt) <= budget_tokens * 0.8:
                break
    return messages, cleared


def folder_notes(folder: str | None) -> str:
    """Project instructions from the working folder: BAABAA.md, else AGENTS.md, else CLAUDE.md."""
    if not folder:
        return ""
    for name in ("BAABAA.md", "AGENTS.md", "CLAUDE.md"):
        p = os.path.join(folder, name)
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                return f"({name})\n" + f.read(8000)
        except OSError:
            continue
    return ""


def transcript(thread: list[dict], max_chars: int = 200_000) -> str:
    """The conversation as plain text, for summarizing: user, assistant, tool calls and short results."""
    out = []
    for m in thread:
        if m["role"] == "user":
            text = "\n".join(b.get("text", "") for b in m["blocks"] if b.get("type") == "text")
            atts = [b.get("name", "") for b in m["blocks"] if b.get("type") == "attachment"]
            out.append("USER: " + text + (f"\n(attached: {', '.join(atts)})" if atts else ""))
        elif m["role"] == "compaction":
            out.append("EARLIER SUMMARY: " + "\n".join(b.get("text", "") for b in m["blocks"]))
        elif m["role"] == "assistant":
            for b in m["blocks"]:
                if b.get("type") == "text" and b.get("text"):
                    out.append("ASSISTANT: " + b["text"])
                elif b.get("type") == "tool":
                    args = b.get("args") or {}
                    what = args.get("command") or args.get("path") or args.get("query") or args.get("url") or args.get("title") or ""
                    result = (b.get("output") or "").strip()
                    if len(result) > 600:
                        result = result[:400] + " … " + result[-150:]
                    out.append(f"TOOL {b.get('name')}({what}) -> {b.get('status')}: {result}")
    text = "\n\n".join(out)
    if len(text) > max_chars:
        text = "[… the beginning is omitted …]\n" + text[-max_chars:]
    return text
