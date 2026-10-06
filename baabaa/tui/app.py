"""baabaa in the terminal: the same conversations as the browser, drawn with curses.

Enter sends; Ctrl+J, Alt+Enter or a trailing backslash adds a line; Shift+Tab changes the mode; Esc
stops the reply; Ctrl+O shows thinking and full tool output; PgUp/PgDn scroll; /help lists commands.
"""

import curses
import getpass
import locale
import os
import sys
import textwrap
import time
import unicodedata

from .. import __version__
from .client import ApiError, Client
from .thread import ThreadLine, glyphs, knot, live_label, live_mode, thought_label

MODE_GLYPH = {"auto": "⏵⏵", "manual": "⏸", "accept_edits": "✎", "plan": "▤"}
COMMANDS = [
    ("/help", "Show commands and keys"), ("/new", "Start a new conversation"), ("/resume", "Open an earlier conversation"),
    ("/model", "Choose the model"), ("/mode", "auto, manual, accept_edits or plan"), ("/think", "Thinking on or off"),
    ("/network", "Network for commands on or off"), ("/folder", "Set the working folder"), ("/compact", "Summarize the conversation"),
    ("/retry", "Write the last reply again"), ("/rewind", "Go back to an earlier message"), ("/rename", "Rename the conversation"),
    ("/export", "Save as md, html or json"), ("/usage", "Your usage"), ("/status", "Server and GPU status"),
    ("/verbose", "Show or hide thinking and full tool output"), ("/research", "Research a question on the web"),
    ("/review", "Review the folder's changes"), ("/init", "Write BAABAA.md for this folder"),
    ("/update", "Install a baabaa update that is waiting"), ("/restart", "Restart the server (after running replies)"),
    ("/quit", "Leave (the conversation stays)"),
]


def width(s: str) -> int:
    w = 0
    for ch in s:
        if unicodedata.combining(ch):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def wrap(text: str, cols: int) -> list[str]:
    out = []
    for line in text.split("\n"):
        if not line:
            out.append("")
            continue
        if width(line) <= cols:
            out.append(line)
            continue
        out.extend(textwrap.wrap(line, cols, replace_whitespace=False, drop_whitespace=True, break_long_words=True) or [""])
    return out


class TUI:
    def __init__(self, client: Client, me: dict, conv_id: str | None, use_folder: bool):
        self.c = client
        self.me = me["account"]
        self.modes = [m["id"] for m in me["modes"]]
        self.conv = None
        self.thread: list[dict] = []
        self.pending: list[dict] = []
        self.turn = None
        self.context = None
        self.queue = {}
        self.models = []
        self.default_model = None
        self.verbose = False
        self.scroll = 0
        self.input = ""
        self.cursor = 0
        self.history: list[str] = []
        self.hist_i = None
        self.status_msg = ""
        self.status_until = 0
        self.dirty = True
        self.overlay = None       # a picker: {"title", "items": [(label, value)], "i", "on_pick"}
        self.lines_cache = None
        self.connected = False
        self._update_said = None
        self.want_conv = conv_id
        self.use_folder = use_folder
        self.quit_armed = 0
        self.paste = None
        self.live = ThreadLine(glyphs(locale.getpreferredencoding(False)))  # the thread under a reply being written
        self.live_shown = None

    # data ----------------------------------------------------------------------------------------------
    def load_models(self):
        d = self.c.get("/api/models")
        self.models = d["models"]
        self.default_model = d["default"]

    def open(self, conv_id: str):
        d = self.c.get(f"/api/conversations/{conv_id}")
        self.conv = d["conversation"]
        self.thread = d["thread"]
        self.turn = d["state"]
        self.pending = (d["state"] or {}).get("pending", [])
        self.context = d["context"]
        self.scroll = 0
        self.invalidate()

    def new_conversation(self):
        self.conv = None
        self.thread, self.pending, self.turn, self.context = [], [], None, None
        self.invalidate()

    def invalidate(self):
        self.lines_cache = None
        self.dirty = True

    def note(self, text: str, secs: float = 4):
        self.status_msg, self.status_until = text, time.time() + secs
        self.dirty = True

    # events -----------------------------------------------------------------------------------------------
    def handle(self, ev: str, d: dict):
        mine = self.conv and d.get("conv_id") == self.conv["id"]
        if ev == "connected":
            self.connected = True
            if self.conv:
                try:
                    self.open(self.conv["id"])
                except ApiError:
                    pass
            self.update_notice()
        elif ev == "update":
            self.update_notice()
        elif ev == "hello" and d.get("version") and d["version"] != __version__:
            self.note(f"The server now runs baabaa {d['version']}; this window still runs {__version__}. "
                      f"/quit and open it again to update it.", 20)
        elif ev == "restart":
            p = d.get("pending")
            if d.get("error"):
                self.note(d["error"].splitlines()[0], 12)
            elif p and p.get("phase") == "restarting":
                self.note("baabaa is restarting; this window reconnects by itself.", 12)
            elif p:
                self.note("baabaa restarts " + ("now." if p.get("now") else "when the replies it is writing are finished."), 12)
        elif ev == "disconnected":
            self.connected = False
        elif ev == "queue":
            self.queue = d
        elif ev in ("msg.new", "msg.done") and mine:
            m = d["message"]
            for i, x in enumerate(self.thread):
                if x["id"] == m["id"]:
                    self.thread[i] = {**x, **m}
                    break
            else:
                self.thread.append(m)
            self.invalidate()
        elif ev == "msg.block" and mine:
            m = self._msg(d["msg_id"])
            if m is not None:
                while len(m["blocks"]) <= d["index"]:
                    m["blocks"].append({})
                m["blocks"][d["index"]] = d["block"]
                self.invalidate()
        elif ev == "msg.delta" and mine:
            m = self._msg(d["msg_id"])
            if m is not None and d["index"] < len(m["blocks"]):
                b = m["blocks"][d["index"]]
                cur = b.get("text", "")
                if len(cur) + len(d["text"]) == d["len"]:
                    b["text"] = cur + d["text"]
                    self.live.pulse(d["text"])
                elif len(cur) < d["len"]:
                    self.open(self.conv["id"])
                self.invalidate()
        elif ev == "turn" and mine:
            self.turn = None if d.get("state") == "idle" else d
        elif ev == "pending" and mine:
            if not any(p["id"] == d["id"] for p in self.pending):
                self.pending.append(d)
            curses.beep()
        elif ev == "pending.done" and mine:
            self.pending = [p for p in self.pending if p["id"] != d["id"]]
        elif ev == "context" and mine and d.get("used"):
            self.context = {"used": d["used"], "num_ctx": d["num_ctx"]}
        elif ev == "conv" and self.conv and d["conversation"]["id"] == self.conv["id"]:
            self.conv.update(d["conversation"])
        elif ev == "conv.reload" and mine:
            self.open(self.conv["id"])
        elif ev == "notice" and mine:
            self.note(d.get("text", ""))
        elif ev == "tool.output" and mine:
            for m in self.thread:
                for b in m["blocks"]:
                    if b.get("id") == d["block_id"]:
                        b["_live"] = (b.get("_live", "") + d["text"])[-4000:]
                        self.invalidate()
        elif ev == "models.updated":
            try:
                self.load_models()
            except ApiError:
                pass
        self.dirty = True

    def _msg(self, mid):
        return next((m for m in self.thread if m["id"] == mid), None)

    # actions --------------------------------------------------------------------------------------------------
    def send(self, text: str, as_message: bool = False):
        text = text.rstrip()
        if not text:
            return
        if text.startswith("/") and not text.startswith("//") and not as_message:
            return self.command(text)
        if text.startswith("//"):
            text = text[1:]
        q = next((p for p in self.pending if p["kind"] == "question"), None)
        if q:
            return self.answer(q, {"text": text})
        try:
            if self.conv is None:
                body = {}
                folder = os.getcwd()
                if self.use_folder and folder not in ("/", os.path.expanduser("~")):
                    body["folder"] = folder
                conv = self.c.post("/api/conversations", body)["conversation"]
                if conv.get("folder") and not self.c.get(f"/api/conversations/{conv['id']}").get("trusted"):
                    self.note(f"Working in {conv['folder']}. /trust lets baabaa read its BAABAA.md or AGENTS.md.", 8)
                self.open(conv["id"])
            self.c.post(f"/api/conversations/{self.conv['id']}/messages", {"text": text})
            self.scroll = 0
        except ApiError as e:
            self.note(f"Error: {e}", 8)

    def answer(self, p, body):
        try:
            self.c.post(f"/api/pending/{p['id']}", body)
        except ApiError as e:
            self.note(f"Error: {e}", 8)

    def stop(self):
        if self.conv and self.turn:
            try:
                self.c.post(f"/api/conversations/{self.conv['id']}/stop")
                self.note("Stopped.")
            except ApiError as e:
                self.note(f"Error: {e}")

    def set_mode(self, mode):
        if not self.conv:
            self.note("Send a message first; the mode belongs to a conversation.")
            return
        try:
            self.conv = self.c.patch(f"/api/conversations/{self.conv['id']}", {"mode": mode})["conversation"]
            self.note(f"Mode: {mode.replace('_', ' ')}")
        except ApiError as e:
            self.note(f"Error: {e}")

    def cycle_mode(self):
        if not self.conv:
            return self.note("The mode applies once the conversation starts.")
        order = self.modes
        self.set_mode(order[(order.index(self.conv["mode"]) + 1) % len(order)])

    def command(self, line: str):
        parts = line.split(None, 1)
        cmd, arg = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")
        conv = self.conv
        try:
            if cmd in ("/quit", "/exit", "/q"):
                raise SystemExit
            elif cmd == "/help":
                self.overlay = {"title": "Commands (Esc to close)", "items": [(f"{c:<10} {d}", c) for c, d in COMMANDS]
                                + [("", None), ("Enter send · Ctrl+J newline · Shift+Tab mode · Esc stop · Ctrl+O verbose · PgUp/PgDn scroll", None)],
                                "i": 0, "on_pick": lambda v: None}
            elif cmd in ("/new", "/clear"):
                self.new_conversation()
                self.note("New conversation")
            elif cmd == "/resume":
                convs = self.c.get("/api/conversations?limit=60")["conversations"]
                items = [(f"{(c['title'] or 'Untitled')[:60]:<60} {time.strftime('%d %b %H:%M', time.localtime(c['updated_ms'] / 1000))}"
                          + (f"  [{os.path.basename(c['folder'])}]" if c.get("folder") else ""), c["id"]) for c in convs]
                self.overlay = {"title": "Resume a conversation", "items": items, "i": 0, "on_pick": self.open}
            elif cmd == "/model":
                if arg:
                    self._set_model(arg)
                else:
                    cur = (conv or {}).get("model") or self.default_model
                    items = [(f"{'●' if m['name'] == cur else ' '} {m['name']:<28} {m['num_ctx'] // 1024}k context"
                              + (f" · {round(m['tokens_per_s'])} tok/s" if m.get("tokens_per_s") else ""), m["name"]) for m in self.models]
                    self.overlay = {"title": "Model", "items": items, "i": 0, "on_pick": self._set_model}
            elif cmd == "/mode":
                if arg in self.modes:
                    self.set_mode(arg)
                else:
                    self.overlay = {"title": "Mode", "items": [(m.replace("_", " "), m) for m in self.modes], "i": 0, "on_pick": self.set_mode}
            elif cmd in ("/think", "/network"):
                if not conv:
                    return self.note("Start the conversation first.")
                key = cmd[1:]
                cur = bool((conv.get("settings") or {}).get(key))
                val = {"on": True, "off": False}.get(arg, not cur)
                self.conv = self.c.patch(f"/api/conversations/{conv['id']}", {"settings": {key: val}})["conversation"]
                self.note(f"{key.capitalize()}: {'on' if val else 'off'}")
            elif cmd == "/folder":
                if not conv:
                    return self.note("Start the conversation first (it uses the current folder).")
                self.conv = self.c.patch(f"/api/conversations/{conv['id']}", {"folder": os.path.abspath(os.path.expanduser(arg or '.'))})["conversation"]
                self.note(f"Folder: {self.conv['folder']}")
            elif cmd == "/trust":
                if conv and conv.get("folder"):
                    self.c.post("/api/folders/trust", {"path": conv["folder"]})
                    self.note("Folder trusted.")
            elif cmd == "/compact":
                if conv:
                    self.note("Summarizing…", 30)
                    self.c.post(f"/api/conversations/{conv['id']}/compact")
                    self.open(conv["id"])
                    self.note("Compacted.")
            elif cmd == "/retry":
                last = next((m for m in reversed(self.thread) if m["role"] == "assistant"), None)
                if last:
                    self.c.post(f"/api/conversations/{conv['id']}/regenerate", {"message_id": last["id"]})
            elif cmd == "/rewind":
                users = [m for m in self.thread if m["role"] == "user"]
                items = [(" ".join(b.get("text", "") for b in m["blocks"] if b.get("type") == "text")[:90].replace("\n", " "), m["id"]) for m in users]
                self.overlay = {"title": "Rewind to before which message? (files are restored too)", "items": items[::-1], "i": 0, "on_pick": self._rewind}
            elif cmd == "/rename":
                if conv and arg:
                    self.conv = self.c.patch(f"/api/conversations/{conv['id']}", {"title": arg})["conversation"]
            elif cmd == "/export":
                if not conv:
                    return
                fmt = (arg.split() or ["md"])[0]
                data, disp = self.c.request("GET", f"/api/conversations/{conv['id']}/export?format={fmt}", raw=True)
                name = arg.split()[1] if len(arg.split()) > 1 else (disp.split('filename="')[-1].rstrip('"') or f"conversation.{fmt}")
                with open(name, "wb") as f:
                    f.write(data)
                self.note(f"Saved {name}")
            elif cmd == "/usage":
                now = int(time.time() * 1000)
                s = self.c.get(f"/api/stats/summary?from={now - 30 * 86400000}&to={now + 60000}&group=model")
                t = s["total"]
                lines = [f"Last 30 days: {t.get('requests') or 0} requests, {t.get('prompt_tokens') or 0:,} tokens in, "
                         f"{t.get('output_tokens') or 0:,} out, GPU {round((t.get('gpu_ms') or 0) / 1000)} s, "
                         f"{(t.get('energy_mj') or 0) / 3.6e6:.2f} Wh, median {t.get('tokens_per_s_median') or '-'} tok/s"]
                lines += [f"  {g['k']:<28} {g['requests']:>5} req  {g['output_tokens']:>8} out" for g in s["groups"]]
                self.overlay = {"title": "Usage (Esc to close)", "items": [(l, None) for l in lines], "i": 0, "on_pick": lambda v: None}
            elif cmd == "/status":
                st = self.c.get("/api/status")
                g = st.get("gpu") or {}
                lines = [f"Ollama: {st.get('ollama') or 'not reachable'}", f"GPU: {g.get('name')} · {round((g.get('vram_used') or 0) / 2**30, 2)} GiB used"
                         + (f" · {g.get('temp_c')} °C · {round((g.get('power_mw') or 0) / 1000)} W" if g.get("temp_c") is not None else ""),
                         f"Queue: {'busy' if st['queue']['busy'] else 'idle'}, {st['queue']['waiting']} waiting", f"Version {st.get('version')}"]
                self.overlay = {"title": "Status (Esc to close)", "items": [(l, None) for l in lines], "i": 0, "on_pick": lambda v: None}
            elif cmd == "/restart":
                p = self.c.post("/api/restart", {"now": arg == "now"}).get("pending") or {}
                self.note("baabaa is restarting." if p.get("now") else
                          "baabaa restarts when the replies it is writing are finished (/restart now does not wait).", 10)
            elif cmd == "/update":
                st = self.c.get("/api/update")
                if st.get("state") not in ("available", "ready", "installed", "changed"):
                    self.note(f"baabaa {st.get('running')} is up to date." + (f" ({st['error']})" if st.get("error") else ""), 8)
                else:
                    if st["state"] == "available":
                        self.note(f"Downloading baabaa {st.get('target')}…", 30)
                        self.draw_now()
                    p = self.c.post("/api/update/install", {"now": arg == "now"}).get("pending") or {}
                    self.note("baabaa is restarting into the update." if p.get("now") else
                              "baabaa restarts into the update when the replies it is writing are finished.", 10)
            elif cmd == "/verbose":
                self.verbose = not self.verbose
                self.invalidate()
            else:
                # the server's commands: /research, /review, /init and custom commands from files
                return self.send(line, as_message=True)
        except ApiError as e:
            self.note(f"Error: {e}", 8)

    def update_notice(self) -> None:
        """One line when an update or a restart waits for this account to act (never again for the same one)."""
        try:
            st = self.c.get("/api/update")
        except ApiError:
            return
        if not st.get("can_act") or st.get("pending"):
            return
        text = {"available": f"baabaa {st.get('target')} is available: /update installs it",
                "ready": f"baabaa {st.get('target')} is ready to install: /update restarts into it",
                "installed": f"baabaa {st.get('target')} is installed: /restart starts it",
                "changed": "baabaa's files changed since it started: /restart runs them"}.get(st.get("state"))
        if text and text != self._update_said:
            self._update_said = text
            self.note(text, 15)

    def draw_now(self) -> None:
        self.dirty = True

    def _set_model(self, name):
        if not self.conv:
            self.default_model = name
            return self.note(f"Model for the next conversation: {name}")
        try:
            self.conv = self.c.patch(f"/api/conversations/{self.conv['id']}", {"model": name})["conversation"]
            self.note(f"Model: {name}")
        except ApiError as e:
            self.note(f"Error: {e}")

    def _rewind(self, mid):
        try:
            r = self.c.post(f"/api/conversations/{self.conv['id']}/rewind", {"message_id": mid, "files": True, "conversation": True})
            self.open(self.conv["id"])
            self.input = r.get("text") or ""
            self.cursor = len(self.input)
            f = r.get("files") or {}
            self.note(f"Rewound. Files: {len(f.get('restored', []))} restored, {len(f.get('removed', []))} removed.")
        except ApiError as e:
            self.note(f"Error: {e}")

    # drawing ---------------------------------------------------------------------------------------------------
    def build_lines(self, cols: int) -> list[list[tuple[str, int]]]:
        if self.lines_cache and self.lines_cache[0] == cols:
            return self.lines_cache[1]
        L = []
        A, DIM, ERR, WARN, USER, CODE, TOOL = (curses.color_pair(i) for i in range(1, 8))
        B = curses.A_BOLD

        def add(text, attr=0, indent=""):
            for w in wrap(text, max(10, cols - len(indent))):
                L.append([(indent + w, attr)])

        if not self.thread:
            L.append([("", 0)])
            L.append([("  baabaa", A | B)])
            L.append([(f"  {self.me['display_name']} · " + ("new conversation in " + os.getcwd() if self.use_folder else "new chat"), DIM)])
            L.append([("  Type a message, or /help.", DIM)])
        for m in self.thread:
            L.append([("", 0)])
            if m["role"] == "user":
                text = "\n".join(b.get("text", "") for b in m["blocks"] if b.get("type") == "text")
                atts = [b["name"] for b in m["blocks"] if b.get("type") == "attachment"]
                first = True
                for w in wrap(text, cols - 2):
                    L.append([("› " if first else "  ", USER | B), (w, USER)])
                    first = False
                if atts:
                    L.append([("  ", 0), ("attached: " + ", ".join(atts), DIM)])
            elif m["role"] == "compaction":
                L.append([("── earlier messages summarized ──", DIM)])
                if self.verbose:
                    add("\n".join(b.get("text", "") for b in m["blocks"]), DIM, "  ")
            else:
                for b in m["blocks"]:
                    t = b.get("type")
                    if t == "thinking":
                        # a thought being thought has only its text here (the thread under the reply says so);
                        # a finished one is a knot, bigger for a longer thought
                        if not (m.get("status") == "streaming" and b is m["blocks"][-1] and b.get("ms") is None):
                            L.append([(f"{knot(len(b.get('text', '')) / 4, self.live.g)} {thought_label(b)}", DIM | curses.A_ITALIC)])
                        if self.verbose:
                            add(b.get("text", ""), DIM, "  ")
                    elif t == "text":
                        self._markdown(L, b.get("text", ""), cols)
                    elif t == "tool":
                        self._tool(L, b, cols)
                    elif t == "error":
                        add("✗ " + b.get("text", ""), ERR)
                    elif t == "notice":
                        add("· " + b.get("text", ""), DIM)
                meta = m.get("meta") or {}
                if m.get("status") != "streaming" and (meta.get("output_tokens") or m.get("status") == "stopped"):
                    info = [m.get("model") or "", f"{meta.get('output_tokens', 0)} tokens", f"{(meta.get('duration_ms') or 0) / 1000:.1f} s"]
                    if m.get("status") == "stopped":
                        info.append("stopped")
                    L.append([(" · ".join(i for i in info if i), DIM)])
        self.lines_cache = (cols, L)
        return L

    # the thread under a reply being written (thread.py) -----------------------------------------------------
    def live_message(self):
        m = self.thread[-1] if self.thread else None
        return m if m and m.get("role") == "assistant" and m.get("status") == "streaming" else None

    def live_text(self) -> tuple[str, str] | None:
        """The thread and the words beside it, as they are now; None when no reply is being written."""
        m = self.live_message()
        if m is None:
            self.live.set("off")
            return None
        self.live.set(live_mode(m, self.turn))
        if self.live.mode == "think":  # a window opened part-way through a thought still gets a knot of the right size
            self.live.tokens = max(self.live.tokens, len(m["blocks"][-1].get("text", "")) / 4)
        self.live.tick()
        return f"{self.live.g['mark']} {self.live.text()}", live_label(m, self.turn, self.live)

    def live_lines(self) -> list[list[tuple[str, int]]]:
        now = self.live_shown = self.live_text()
        return [[(now[0], curses.color_pair(1)), (" " + now[1], curses.color_pair(2))]] if now else []

    def _markdown(self, L, text, cols):
        A, DIM, CODE = curses.color_pair(1), curses.color_pair(2), curses.color_pair(6)
        in_code = False
        for raw in text.split("\n"):
            if raw.strip().startswith("```"):
                in_code = not in_code
                lang = raw.strip()[3:].strip()
                L.append([("  ┌" + (" " + lang if lang else ""), DIM)] if in_code else [("  └", DIM)])
                continue
            if in_code:
                for w in wrap(raw, cols - 4) or [""]:
                    L.append([("  │ ", DIM), (w, CODE)])
                continue
            s = raw.lstrip()
            if s.startswith("#"):
                title = s.lstrip("#").strip()
                for w in wrap(title, cols):
                    L.append([(w, curses.A_BOLD | A)])
                continue
            for w in wrap(raw, cols) or [""]:
                L.append(self._inline(w))

    def _inline(self, w):
        CODE = curses.color_pair(6)
        segs, i, bold = [], 0, False
        buf = ""
        while i < len(w):
            if w.startswith("**", i):
                if buf:
                    segs.append((buf, curses.A_BOLD if bold else 0))
                    buf = ""
                bold = not bold
                i += 2
                continue
            if w[i] == "`":
                j = w.find("`", i + 1)
                if j > i:
                    if buf:
                        segs.append((buf, curses.A_BOLD if bold else 0))
                        buf = ""
                    segs.append((w[i + 1:j], CODE))
                    i = j + 1
                    continue
            buf += w[i]
            i += 1
        if buf:
            segs.append((buf, curses.A_BOLD if bold else 0))
        return segs or [("", 0)]

    def _tool(self, L, b, cols):
        A, DIM, ERR, WARN, TOOL = (curses.color_pair(i) for i in (1, 2, 3, 4, 7))
        a = b.get("args") or {}
        name = b.get("name", "")
        label = {"read_file": "Read", "write_file": "Write", "edit_file": "Edit", "list_files": "List", "search": "Search",
                 "bash": "Bash", "web_fetch": "Fetch", "web_search": "Web search", "todo_write": "Tasks", "task": "Helper",
                 "create_artifact": "Artifact", "update_artifact": "Artifact", "rewrite_artifact": "Artifact",
                 "ask_user": "Question", "propose_plan": "Plan"}.get(name, name)
        obj = a.get("command") or a.get("path") or a.get("pattern") or a.get("query") or a.get("url") or a.get("title") or a.get("description") or ""
        st = b.get("status", "")
        color = {"done": A, "error": ERR, "denied": ERR, "waiting": WARN, "running": TOOL, "checking": TOOL, "stopped": DIM}.get(st, DIM)
        head = f"⏺ {label}({str(obj)[:cols - len(label) - 8]})"
        L.append([(head, color | curses.A_BOLD)] + ([(f"  {st}", DIM)] if st not in ("done", "") else []))
        d = b.get("decision") or {}
        j = b.get("judge")
        if j and st != "waiting":
            L.append([("  ⎿ ", DIM), (f"auto mode: {j.get('risk')} risk — {j.get('reason', '')}"[:cols - 6], DIM)])
        elif d.get("action") == "deny" or st == "denied":
            L.append([("  ⎿ ", DIM), ((b.get("output") or d.get("reason") or "")[:cols - 6], ERR)])
        if name == "todo_write":
            marks = {"completed": "☒", "in_progress": "▣", "pending": "☐"}
            for t in a.get("todos") or []:
                L.append([("  ⎿ " if t is (a.get("todos") or [None])[0] else "    ", DIM), (f"{marks.get(t.get('status'), '☐')} {t.get('content', '')}"[:cols - 6], 0)])
            return
        out = b.get("_live") if st == "running" else b.get("output")
        if b.get("diff") and (self.verbose or st == "done"):
            dl = b["diff"].split("\n")[2:]
            shown = dl if self.verbose else dl[:12]
            for line in shown:
                c = A if line.startswith("+") else ERR if line.startswith("-") else DIM
                L.append([("    ", 0), (line[:cols - 5], c)])
            if len(dl) > len(shown):
                L.append([(f"    … {len(dl) - len(shown)} more lines (Ctrl+O)", DIM)])
        elif out and st in ("done", "error", "running"):
            lines = out.rstrip("\n").split("\n")
            shown = lines if self.verbose else lines[:4]
            for i, line in enumerate(shown):
                L.append([("  ⎿ " if i == 0 else "    ", DIM), (line[:cols - 5], ERR if st == "error" else DIM)])
            if len(lines) > len(shown):
                L.append([(f"    … {len(lines) - len(shown)} more lines (Ctrl+O)", DIM)])

    def draw(self, scr):
        rows, cols = scr.getmaxyx()
        scr.erase()
        if rows < 8 or cols < 30:
            scr.addstr(0, 0, "Window too small")
            scr.refresh()
            return
        if self.overlay:
            return self.draw_overlay(scr, rows, cols)
        # input box height
        in_lines = self.input_lines(cols - 3)
        in_h = min(8, len(in_lines))
        prompt_lines = self.pending_lines(cols)
        hint_lines = self.command_hints(cols)
        bottom = in_h + 2 + len(prompt_lines) + len(hint_lines)
        area = rows - bottom
        lines = self.build_lines(cols - 1) + self.live_lines()
        total = len(lines)
        self.scroll = max(0, min(self.scroll, max(0, total - area)))
        start = max(0, total - area - self.scroll)
        for y, segs in enumerate(lines[start:start + area]):
            x = 0
            for text, attr in segs:
                if x >= cols - 1:
                    break
                try:
                    scr.addstr(y, x, text[: cols - 1 - x], attr)
                except curses.error:
                    pass
                x += width(text)
        y = area
        for text, attr in prompt_lines:
            self._put(scr, y, 0, text, attr, cols)
            y += 1
        self._put(scr, y, 0, "─" * (cols - 1), curses.color_pair(2), cols)
        y += 1
        cur_y, cur_x = y, 3
        first_visible = max(0, len(in_lines) - in_h)
        for i, (text, pos) in enumerate(in_lines[first_visible:first_visible + in_h]):
            self._put(scr, y + i, 0, ("› " if i + first_visible == 0 else "  ") + text, curses.A_BOLD if i + first_visible == 0 and not text else 0, cols)
            if pos is not None:
                cur_y, cur_x = y + i, 2 + pos
        y += in_h
        for text, attr in hint_lines:
            self._put(scr, y, 0, text, attr, cols)
            y += 1
        self._put(scr, rows - 1, 0, self.status_line(cols), curses.color_pair(2), cols)
        try:
            scr.move(min(cur_y, rows - 2), min(cur_x, cols - 1))
        except curses.error:
            pass
        scr.refresh()

    def _put(self, scr, y, x, text, attr, cols):
        try:
            scr.addstr(y, x, text[: max(0, cols - 1 - x)], attr)
        except curses.error:
            pass

    def input_lines(self, cols):
        """[(text, cursor_col or None)] for the input box, wrapped."""
        out, pos = [], 0
        logical = self.input.split("\n")
        for li, line in enumerate(logical):
            chunks = [line[i:i + cols] for i in range(0, max(1, len(line)), cols)] or [""]
            for ci, chunk in enumerate(chunks):
                start = pos + ci * cols
                cur = None
                if start <= self.cursor <= start + len(chunk) and (ci == len(chunks) - 1 or self.cursor < start + len(chunk)):
                    cur = width(chunk[: self.cursor - start])
                out.append((chunk, cur))
            pos += len(line) + 1
        if not self.input:
            hint = "Answer the question…" if any(p["kind"] == "question" for p in self.pending) else "Message baabaa, or /help"
            return [(hint, 0)]
        return out

    def command_hints(self, cols):
        if not self.input.startswith("/") or " " in self.input or "\n" in self.input:
            return []
        matches = [(c, d) for c, d in COMMANDS if c.startswith(self.input.lower())][:6]
        return [(f"  {c:<10} {d}", curses.color_pair(2)) for c, d in matches]

    def pending_lines(self, cols):
        if not self.pending:
            return []
        p = self.pending[0]
        W, B = curses.color_pair(4), curses.A_BOLD
        out = []
        if p["kind"] == "approval":
            a = p.get("args") or {}
            what = a.get("command") or a.get("path") or a.get("url") or p.get("tool")
            out.append((f"  Allow {p.get('tool')}: {what}"[: cols - 2], W | B))
            reason = (p.get("decision") or {}).get("reason")
            if reason:
                out.append((f"  {reason}"[: cols - 2], curses.color_pair(2)))
            if p.get("for_owner") and self.me["role"] != "owner":
                out.append(("  Waiting for the owner's approval.", curses.color_pair(2)))
            else:
                rule = p.get("suggested_rule")
                out.append((f"  [1] allow once   " + (f"[2] always allow {rule}   " if rule else "") + "[3] deny", W))
        elif p["kind"] == "question":
            out.append((f"  ? {p.get('question')}"[: cols - 2], W | B))
            for i, o in enumerate(p.get("options") or [], start=1):
                out.append((f"    [{i}] {o}"[: cols - 2], W))
            out.append(("  Type an answer, or press a number.", curses.color_pair(2)))
        elif p["kind"] == "plan":
            out.append(("  Plan ready (shown above). [1] approve · auto  [2] approve · accept edits  [3] approve · manual  [4] keep planning", W | B))
        return out

    def status_line(self, cols):
        parts = []
        if self.conv:
            mode = self.conv["mode"]
            parts.append(f"{MODE_GLYPH.get(mode, '')} {mode.replace('_', ' ')} (shift+tab)")
            parts.append(self.conv.get("model") or self.default_model or "no model")
            if self.conv.get("folder"):
                parts.append(os.path.basename(self.conv["folder"]) or "/")
            if (self.conv.get("settings") or {}).get("think"):
                parts.append("thinking on")
            if (self.conv.get("settings") or {}).get("network"):
                parts.append("network on")
        else:
            parts.append(self.default_model or "no model")
            if self.use_folder:
                parts.append(os.path.basename(os.getcwd()))
        if self.context and self.context.get("num_ctx"):
            parts.append(f"{round(100 * (self.context.get('used') or 0) / self.context['num_ctx'])}% context")
        if self.turn:
            parts.append("working… esc to stop" + (f" (GPU queue: {self.turn['queue_position']} ahead)" if self.turn.get("queue_position") else ""))
        elif self.queue.get("busy"):
            parts.append("GPU busy")
        if not self.connected:
            parts.append("reconnecting…")
        if self.verbose:
            parts.append("verbose")
        text = " · ".join(parts)
        if self.status_msg and time.time() < self.status_until:
            text = self.status_msg
        return " " + text

    def draw_overlay(self, scr, rows, cols):
        o = self.overlay
        self._put(scr, 0, 0, " " + o["title"], curses.A_BOLD | curses.color_pair(1), cols)
        items = o["items"]
        h = rows - 3
        top = max(0, min(o["i"] - h // 2, len(items) - h))
        for y, (label, value) in enumerate(items[top:top + h], start=1):
            idx = top + y - 1
            attr = curses.A_REVERSE if idx == o["i"] and value is not None else 0
            self._put(scr, y, 1, label, attr, cols)
        self._put(scr, rows - 1, 0, " ↑↓ choose · Enter select · Esc close", curses.color_pair(2), cols)
        scr.refresh()

    # input ---------------------------------------------------------------------------------------------------
    def key(self, k):
        if self.paste is not None:
            if k == "\x1b":
                self.paste += "\x1b"
            else:
                self.paste += k if isinstance(k, str) else ""
            if self.paste.endswith("\x1b[201~"):
                text = self.paste[:-6].replace("\r\n", "\n").replace("\r", "\n")
                self.paste = None
                self.insert(text)
            return
        if self.overlay:
            return self.overlay_key(k)
        if k == curses.KEY_RESIZE:
            self.invalidate()
            return
        p = self.pending[0] if self.pending else None
        if p and not self.input and isinstance(k, str) and k.isdigit():
            n = int(k)
            if p["kind"] == "approval" and not (p.get("for_owner") and self.me["role"] != "owner"):
                decision = {1: "allow_once", 2: "allow_always", 3: "deny"}.get(n)
                if decision:
                    body = {"decision": decision}
                    if decision == "allow_always":
                        body["rule"] = p.get("suggested_rule")
                    return self.answer(p, body)
            if p["kind"] == "question" and 1 <= n <= len(p.get("options") or []):
                return self.answer(p, {"text": p["options"][n - 1]})
            if p["kind"] == "plan" and 1 <= n <= 4:
                if n == 4:
                    return self.note("Type what should change, then press Enter.", 6) or setattr(self, "_plan_feedback", p)
                return self.answer(p, {"decision": "approve", "mode": {1: "auto", 2: "accept_edits", 3: "manual"}[n]})
        if k in ("\r", curses.KEY_ENTER):
            if self.input.endswith("\\"):
                self.input = self.input[:-1]
                self.cursor = min(self.cursor, len(self.input))
                return self.insert("\n")
            text = self.input
            plan = getattr(self, "_plan_feedback", None)
            self.input, self.cursor, self.hist_i = "", 0, None
            if text.strip():
                self.history.append(text)
            if plan and text.strip():
                self._plan_feedback = None
                return self.answer(plan, {"decision": "revise", "text": text})
            return self.send(text)
        if k == "\n":  # Ctrl+J
            return self.insert("\n")
        if k == curses.KEY_BTAB:
            return self.cycle_mode()
        if k == "\x1b":
            return "escape"
        if k == "\x0f":  # Ctrl+O
            self.verbose = not self.verbose
            return self.invalidate()
        if k == "\x03":  # Ctrl+C
            if self.turn:
                return self.stop()
            if self.input:
                self.input, self.cursor = "", 0
                return
            if time.time() - self.quit_armed < 2:
                raise SystemExit
            self.quit_armed = time.time()
            return self.note("Press Ctrl+C again to leave.", 2)
        if k == "\x04" and not self.input:
            raise SystemExit
        if k == "\x0c":
            return self.invalidate()
        if k in (curses.KEY_BACKSPACE, "\x7f", "\x08"):
            if self.cursor > 0:
                self.input = self.input[: self.cursor - 1] + self.input[self.cursor:]
                self.cursor -= 1
            return
        if k == curses.KEY_DC:
            self.input = self.input[: self.cursor] + self.input[self.cursor + 1:]
            return
        if k == curses.KEY_LEFT:
            self.cursor = max(0, self.cursor - 1)
            return
        if k == curses.KEY_RIGHT:
            self.cursor = min(len(self.input), self.cursor + 1)
            return
        if k in (curses.KEY_HOME, "\x01"):
            self.cursor = self.input.rfind("\n", 0, self.cursor) + 1
            return
        if k in (curses.KEY_END, "\x05"):
            e = self.input.find("\n", self.cursor)
            self.cursor = len(self.input) if e < 0 else e
            return
        if k == "\x15":  # Ctrl+U
            self.input, self.cursor = "", 0
            return
        if k == "\x17":  # Ctrl+W
            before = self.input[: self.cursor].rstrip()
            cut = before.rfind(" ") + 1
            self.input = self.input[:cut] + self.input[self.cursor:]
            self.cursor = cut
            return
        if k == curses.KEY_PPAGE:
            self.scroll += 10
            return
        if k == curses.KEY_NPAGE:
            self.scroll = max(0, self.scroll - 10)
            return
        if k == curses.KEY_UP:
            if "\n" in self.input[: self.cursor]:
                line_start = self.input.rfind("\n", 0, self.cursor)
                prev_start = self.input.rfind("\n", 0, line_start) + 1
                self.cursor = min(line_start, prev_start + (self.cursor - line_start - 1))
            elif self.history:
                self.hist_i = len(self.history) - 1 if self.hist_i is None else max(0, self.hist_i - 1)
                self.input = self.history[self.hist_i]
                self.cursor = len(self.input)
            return
        if k == curses.KEY_DOWN:
            if self.hist_i is not None:
                self.hist_i += 1
                if self.hist_i >= len(self.history):
                    self.hist_i, self.input = None, ""
                else:
                    self.input = self.history[self.hist_i]
                self.cursor = len(self.input)
            return
        if k == "\t" and self.input.startswith("/"):
            m = [c for c, _ in COMMANDS if c.startswith(self.input)]
            if m:
                self.input = m[0] + " "
                self.cursor = len(self.input)
            return
        if isinstance(k, str) and (k.isprintable() or k == " "):
            self.insert(k)

    def insert(self, text):
        self.input = self.input[: self.cursor] + text + self.input[self.cursor:]
        self.cursor += len(text)

    def overlay_key(self, k):
        o = self.overlay
        pickable = [i for i, (_, v) in enumerate(o["items"]) if v is not None]
        if k in ("\x1b", "q"):
            self.overlay = None
        elif k == curses.KEY_UP and pickable:
            before = [i for i in pickable if i < o["i"]]
            o["i"] = before[-1] if before else o["i"]
        elif k == curses.KEY_DOWN and pickable:
            after = [i for i in pickable if i > o["i"]]
            o["i"] = after[0] if after else o["i"]
        elif k in ("\r", "\n", curses.KEY_ENTER):
            value = o["items"][o["i"]][1] if o["items"] else None
            self.overlay = None
            if value is not None:
                o["on_pick"](value)
        self.invalidate()

    # main loop -------------------------------------------------------------------------------------------------
    def run(self, scr):
        curses.use_default_colors()
        for i, fg in enumerate([curses.COLOR_GREEN, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, curses.COLOR_RED,
                                curses.COLOR_YELLOW, curses.COLOR_BLUE, curses.COLOR_CYAN, curses.COLOR_MAGENTA], start=1):
            curses.init_pair(i, fg, -1)
        curses.nonl()
        scr.keypad(True)
        scr.timeout(50)
        sys.stdout.write("\x1b[?2004h")  # bracketed paste
        sys.stdout.flush()
        self.load_models()
        if self.want_conv:
            self.open(self.want_conv)
        self.c.start_events()
        pending_esc = None
        try:
            while True:
                while not self.c.events.empty():
                    self.handle(*self.c.events.get_nowait())
                try:
                    k = scr.get_wch()
                except curses.error:
                    k = None
                if pending_esc is not None and k is None and time.time() - pending_esc > 0.05:
                    pending_esc = None
                    if self.overlay:
                        self.overlay = None
                    elif self.turn:
                        self.stop()
                    self.dirty = True
                if k is not None:
                    if pending_esc is not None:
                        pending_esc = None
                        if k in ("\r", "\n"):
                            self.insert("\n")
                        elif k == "[":
                            seq = "\x1b["
                            scr.timeout(5)
                            for _ in range(8):
                                try:
                                    c2 = scr.get_wch()
                                except curses.error:
                                    break
                                seq += c2 if isinstance(c2, str) else ""
                                if seq.endswith("~"):
                                    break
                            scr.timeout(50)
                            if seq == "\x1b[200~":
                                self.paste = ""
                        self.dirty = True
                    elif self.key(k) == "escape":
                        pending_esc = time.time()
                    self.dirty = True
                live = self.live_text()
                if live != self.live_shown:  # the thread moved, or its words changed
                    self.live_shown = live
                    self.dirty = True
                if self.dirty:
                    self.dirty = False
                    self.draw(scr)
        except SystemExit:
            pass
        finally:
            sys.stdout.write("\x1b[?2004l")
            sys.stdout.flush()
            self.c.stop()


def run(account_name: str | None = None, conversation: str | None = None, use_folder: bool = True) -> None:
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    client = Client()
    if not client.available():
        sys.exit("baabaa is not running here. Start it with: baabaa start")
    if not client.local_key:
        sys.exit("The local key is missing; run the terminal client as the same user as the server.")
    me = client.resume(account_name)
    if me is None:
        try:
            profiles = client.get("/api/profiles")["profiles"]
        except ApiError as e:
            sys.exit(str(e))
        if not profiles:
            sys.exit("No accounts yet. Open baabaa in a browser to create the owner account.")
        if account_name:
            account = next((p for p in profiles if p["name"] == account_name), None)
            if account is None:
                sys.exit(f"No account named {account_name!r}.")
        elif len(profiles) == 1:
            account = profiles[0]
        else:
            for i, p in enumerate(profiles, 1):
                print(f"  {i}. {p['display_name']} ({p['name']})")
            choice = input("Who are you? ").strip()
            account = profiles[int(choice) - 1] if choice.isdigit() and 0 < int(choice) <= len(profiles) else None
            if account is None:
                sys.exit("No such account.")
        pw = getpass.getpass(f"Password for {account['display_name']}: ") if account["has_password"] else None
        try:
            me = client.login(account, pw)
        except ApiError as e:
            sys.exit(str(e))
    tui = TUI(client, me, conversation, use_folder)
    curses.wrapper(tui.run)
    if tui.conv:
        print(f"Conversation: {tui.conv.get('title') or tui.conv['id']}  (resume with: baabaa chat {tui.conv['id']})")
