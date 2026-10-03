"""Reading harmony-format replies: the format of OpenAI's gpt-oss, also used by llm-jp-4.1-thinking.

A reply is a series of messages: `<|channel|>analysis<|message|>…<|end|>` is thinking, `final` is the answer,
and `commentary` with a recipient (`to=functions.NAME`) is a tool call whose message is the JSON arguments.
Ollama's own reader ("PARSER harmony") needs the markers exactly as gpt-oss writes them. llm-jp's tokenizer
puts a space after each marker (`<|channel|> analysis`, `<|message|> {"city": …}`), which that reader does not
accept (measured 2026-10-02: thinking, answer and tool call all came back as one block of text). For such a
model Ollama passes the text through unread ("PARSER passthrough") and baabaa reads it here.
"""

import json
import re

from .ollama import OllamaError

MARKERS = ("<|start|>", "<|channel|>", "<|message|>", "<|end|>", "<|call|>", "<|return|>", "<|constrain|>")
LONGEST = max(len(m) for m in MARKERS)
CHANNELS = ("analysis", "commentary", "final")
LOOSE_HEADER = 200   # this much text with no <|message|> means the model left the format: show it as the answer


class HarmonyError(OllamaError):
    """A tool call whose arguments are not JSON: reported like Ollama's own tool-call parse errors, so a turn
    asks the model again (agent/loop.py, tool_call_unreadable)."""


class Reader:
    """Feed the reply's text as it streams; get (kind, text) pieces, kind "thinking" or "content". Tool calls
    collect in `calls` (Ollama's shape). The prompt ends with `<|start|>assistant`, so a reply starts in a header."""

    def __init__(self):
        self.buf = ""
        self.in_body = False
        self.header = ""
        self.channel = None
        self.recipient = None
        self.fresh = True        # at the start of a message body (its first character may be a tokenizer space)
        self.args = ""
        self.calls: list[dict] = []

    def feed(self, text: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        self.buf += text or ""
        while self.buf:
            i = self.buf.find("<|")
            if i < 0:
                keep = 1 if self.buf.endswith("<") else 0  # it may start a marker
                self._text(self.buf[:len(self.buf) - keep], out)
                self.buf = self.buf[len(self.buf) - keep:]
                break
            if i > 0:
                self._text(self.buf[:i], out)
                self.buf = self.buf[i:]
            marker = next((m for m in MARKERS if self.buf.startswith(m)), None)
            if marker:
                self.buf = self.buf[len(marker):]
                self._marker(marker, out)
            elif len(self.buf) < LONGEST and any(m.startswith(self.buf) for m in MARKERS):
                break  # the rest of a marker is still to come
            else:
                self._text(self.buf[:2], out)  # "<|" that starts no marker is text
                self.buf = self.buf[2:]
        return out

    def finish(self) -> list[tuple[str, str]]:
        """The end of the reply (a stop word ends it before <|call|> or <|return|>): settle what is open."""
        out: list[tuple[str, str]] = []
        if self.buf:
            self._text(self.buf, out)
            self.buf = ""
        if self.in_body and self.recipient:
            self._call()
        elif not self.in_body and self.header.strip() and not self.calls:
            out.append(("content", self.header.strip()))  # text outside the format: keep it
        self.header = ""
        return out

    # internals ---------------------------------------------------------------------------------
    def _text(self, text: str, out: list) -> None:
        if not self.in_body:
            self.header += text
            if len(self.header) > LOOSE_HEADER:  # no format at all: what was written is the answer
                self.in_body, self.channel, self.recipient, self.fresh = True, "final", None, False
                text, self.header = self.header, ""
            else:
                return
        if self.fresh:
            if text[:1] == " ":
                text = text[1:]
            if not text:
                return
            self.fresh = False
        if self.recipient:
            self.args += text
        else:
            out.append(("thinking" if self.channel == "analysis" else "content", text))

    def _marker(self, marker: str, out: list) -> None:
        if marker == "<|message|>":
            words = self.header.split()
            self.channel = next((w for w in words if w in CHANNELS), self.channel or "final")
            to = re.search(r"\bto=([^\s<]+)", self.header)
            self.recipient = to.group(1) if to and self.channel != "final" else None
            self.in_body, self.fresh, self.args, self.header = True, True, "", ""
        elif marker in ("<|channel|>", "<|constrain|>"):
            if self.in_body:  # a header marker inside a body starts the next message's header
                self._close()
            self.header += " "
        else:  # <|start|>, <|end|>, <|call|>, <|return|>: a message ends
            self._close()

    def _close(self) -> None:
        if self.in_body and self.recipient:
            self._call()
        self.in_body, self.channel, self.recipient, self.header = False, None, None, ""

    def _call(self) -> None:
        raw = self.args.strip()
        name = self.recipient.split(".", 1)[1] if self.recipient.startswith("functions.") else self.recipient
        try:
            args = json.loads(raw) if raw else {}
        except ValueError as exc:
            raise HarmonyError(f"error parsing tool call: raw='{raw[:200]}', err={exc}") from None
        self.calls.append({"function": {"name": name, "arguments": args if isinstance(args, dict) else {"value": args}}})
        self.recipient, self.args = None, ""


def reads_raw(info: dict) -> bool:
    """True for an Ollama model whose harmony replies baabaa reads itself (see the module's note)."""
    return info.get("output") == "harmony"


async def chunks(stream):
    """Ollama's chat stream with a harmony reply read: thinking, content and tool calls in their own fields."""
    reader = Reader()
    try:
        async for chunk in stream:
            msg = chunk.get("message") or {}
            parts = reader.feed(msg.get("content") or "")
            if chunk.get("done"):
                parts += reader.finish()
            new = {**chunk, "message": {**msg, "content": "".join(t for k, t in parts if k == "content")}}
            thinking = "".join(t for k, t in parts if k == "thinking")
            if thinking:
                new["message"]["thinking"] = (msg.get("thinking") or "") + thinking
            if reader.calls and chunk.get("done"):
                new["message"]["tool_calls"] = (msg.get("tool_calls") or []) + reader.calls
            yield new
    finally:
        await stream.aclose()  # Stop closes Ollama's connection at once, as without this reader
