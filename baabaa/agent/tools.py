"""The agent's tools: definitions the model sees (JSON Schema) and their implementations.

Categories drive permissions: read, edit, exec, net, meta. File tools run in the server with path
checks against the account's folders (symlinks resolved); shell commands run in the sandbox.
"""

import difflib
import fnmatch
import os
import re
from dataclasses import dataclass, field

from . import web
from .shell import cap_output

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache",
             ".cache", ".idea", ".next", ".nuxt", ".gradle", ".terraform", "site-packages"}
MAX_READ_LINES = 2000
MAX_LINE = 2000
MAX_FILE_BYTES = 20 * 2**20


class ToolError(Exception):
    pass


@dataclass
class Tool:
    name: str
    category: str
    description: str
    params: dict
    required: list = field(default_factory=list)
    needs_folder: bool = False
    special: bool = False       # handled by the turn runner itself (ask_user, propose_plan, task)

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": {"type": "object", "properties": self.params, "required": self.required}}}


S = lambda desc, **kw: {"type": "string", "description": desc, **kw}  # noqa: E731
I = lambda desc, **kw: {"type": "integer", "description": desc, **kw}  # noqa: E731
B = lambda desc: {"type": "boolean", "description": desc}  # noqa: E731

TOOLS = [
    Tool("read_file", "read", "Read a text file. Returns numbered lines. Use offset/limit for long files.",
         {"path": S("File path, relative to the working folder or absolute"),
          "offset": I("First line to read, 1-based"), "limit": I("Number of lines to read (max 2000)")},
         ["path"], needs_folder=True),
    Tool("list_files", "read", "List files under a folder, optionally filtered by a glob such as **/*.py.",
         {"path": S("Folder to list (default: the working folder)"), "pattern": S("Glob filter, e.g. src/**/*.ts")},
         [], needs_folder=True),
    Tool("search", "read", "Search file contents with a regular expression. Returns path:line: text matches.",
         {"pattern": S("Regular expression"), "path": S("Folder or file to search (default: working folder)"),
          "glob": S("Only files matching this glob, e.g. *.py"), "ignore_case": B("Case-insensitive")},
         ["pattern"], needs_folder=True),
    Tool("write_file", "edit", "Create a file or replace its whole content. Prefer edit_file for small changes.",
         {"path": S("File path"), "content": S("The complete new content")}, ["path", "content"], needs_folder=True),
    Tool("edit_file", "edit",
         "Replace old_text with new_text in a file. old_text must match the file exactly, including indentation, "
         "and be unique unless replace_all is true. Read the file first.",
         {"path": S("File path"), "old_text": S("Exact text to replace"), "new_text": S("Replacement text"),
          "replace_all": B("Replace every occurrence")}, ["path", "old_text", "new_text"], needs_folder=True),
    Tool("create_file", "edit",
         "Create a document the user can download: Word (.docx), Excel (.xlsx), PowerPoint (.pptx), PDF (.pdf), or any "
         "text format (.md, .csv, .html, .py …). For .docx and .pdf write Markdown (headings, lists, tables, code). For "
         ".pptx write Markdown where each # or ## heading starts a slide. For .xlsx give CSV, Markdown tables (one sheet "
         "each) or JSON; cells starting with = are formulas.",
         {"path": S("File name with its extension, e.g. report.docx"), "content": S("The content, as described above"),
          "title": S("Document title (optional)")}, ["path", "content"], needs_folder=True),
    Tool("bash", "exec",
         "Run a shell command in the working folder (bash, sandboxed). Network is off unless network is true "
         "and allowed. Use background for servers or long jobs, then read them with bash_output.",
         {"command": S("The command line"), "timeout": I("Seconds before it is stopped (default 120, max 1800)"),
          "background": B("Run in the background and return an id"), "network": B("The command needs network access")},
         ["command"], needs_folder=True),
    Tool("bash_output", "meta", "Read new output from a background command started with bash.",
         {"id": S("The background command id")}, ["id"], needs_folder=True),
    Tool("kill_shell", "meta", "Stop a background command.", {"id": S("The background command id")}, ["id"], needs_folder=True),
    Tool("web_fetch", "net", "Fetch a web page or file over http(s) and return its text.",
         {"url": S("The URL"), "max_chars": I("Maximum characters to return (default 20000)")}, ["url"]),
    Tool("web_search", "net", "Search the web. Returns titles, URLs and snippets.",
         {"query": S("Search query"), "max_results": I("Number of results (default 8)")}, ["query"]),
    Tool("todo_write", "meta",
         "Replace the task list shown to the user. Use it for multi-step work: mark one item in_progress at a "
         "time and mark items completed as soon as they are done.",
         {"todos": {"type": "array", "description": "The whole list", "items": {
             "type": "object", "properties": {"content": S("The task"),
                                              "status": S("Status", enum=["pending", "in_progress", "completed"])},
             "required": ["content", "status"]}}}, ["todos"]),
    Tool("ask_user", "meta", "Ask the user a question and wait for the answer, only when you cannot continue without "
         "their decision. Do not ask about things you can decide, look up or reasonably assume. Offer options when there are clear choices.",
         {"question": S("The question"), "options": {"type": "array", "items": {"type": "string"},
                                                     "description": "Suggested answers (optional)"}},
         ["question"], special=True),
    Tool("propose_plan", "meta",
         "In plan mode, present the finished plan for approval. Only call this when the plan is complete.",
         {"plan": S("The plan, in Markdown")}, ["plan"], special=True),
    Tool("create_artifact", "meta",
         "Create an artifact shown beside the conversation: a web page (html), an SVG image, a Markdown document, "
         "a slide deck (slides: Markdown where each # or ## heading starts a slide), a code file or a Mermaid diagram. "
         "Use it for substantial, self-contained content the user will reuse. Documents and slides can be downloaded "
         "as Word, PDF or PowerPoint.",
         {"title": S("Short title"), "kind": S("Kind", enum=["html", "svg", "markdown", "slides", "code", "mermaid"]),
          "content": S("The full content"), "language": S("Programming language, for code")},
         ["title", "kind", "content"]),
    Tool("update_artifact", "meta", "Change part of an artifact: replace old_text with new_text (exact, unique).",
         {"id": S("Artifact id"), "old_text": S("Exact text to replace"), "new_text": S("Replacement")},
         ["id", "old_text", "new_text"]),
    Tool("rewrite_artifact", "meta", "Replace an artifact's whole content.",
         {"id": S("Artifact id"), "content": S("The full new content"), "title": S("New title (optional)")},
         ["id", "content"]),
    Tool("read_artifact", "meta",
         "Read an artifact's current content, to check or change it. Without an id, lists this conversation's artifacts.",
         {"id": S("Artifact id or title (leave empty to list them)")}, []),
    Tool("use_skill", "meta", "Load a skill's instructions by name (see the Skills list) before a task it covers.",
         {"name": S("The skill's name")}, ["name"]),
    Tool("find_tools", "meta",
         "Search the connected tool servers (connectors) for tools that fit a task. Matching tools become "
         "available to call from your next step.",
         {"query": S("What you want to do, in a few words")}, ["query"]),
    Tool("search_project", "meta",
         "Search the documents in this conversation's project for passages relevant to a query. Use it when the "
         "excerpts you were given do not answer the question.",
         {"query": S("What to look for, in a few words or a question"), "limit": I("Number of passages (default 6)")},
         ["query"]),
    Tool("memory", "meta",
         "Save, change or delete a memory: a short fact about the user or their work that should carry over to future "
         "conversations. Only when the user asks you to remember or forget something, or states a lasting preference.",
         {"action": S("What to do", enum=["add", "update", "delete"]), "text": S("The fact, in one sentence (add, update)"),
          "id": S("The memory's id, from the Memory list in your instructions (update, delete)")}, ["action"]),
    Tool("past_chats", "meta",
         "Look through the user's earlier conversations: search them by words, or list the most recent. Use it when "
         "the user refers to something discussed before.",
         {"query": S("Words to search for (leave empty for the most recent conversations)"),
          "limit": I("How many conversations (default 5)")}, []),
    Tool("generate_image", "meta",
         "Make an image from a description, or change the image the user attached or the last one made here "
         "(edit: true). Describe the subject, style, composition, lighting and any text to show, in detail.",
         {"prompt": S("What the image should show, in detail"),
          "shape": S("Shape of the image", enum=["square", "portrait", "landscape", "wide"]),
          "edit": B("Change the attached or the most recent image instead of making a new one")}, ["prompt"]),
    Tool("task", "meta",
         "Hand a self-contained research or search task to a helper agent with its own context. It returns a "
         "summary. Use it to keep large searches out of this conversation.",
         {"description": S("Three to five words"), "prompt": S("Complete instructions for the helper"),
          "agent": S("explore (read-only search) or general", enum=["explore", "general"])},
         ["description", "prompt"], special=True),
]
BY_NAME = {t.name: t for t in TOOLS}
EXPLORE_TOOLS = {"read_file", "list_files", "search", "web_fetch", "web_search"}


SCRATCH_TOOLS = {"read_file", "list_files", "write_file", "edit_file", "create_file", "bash"}


def available(folder: bool, mode: str, subagent: str | None = None, connectors: bool = False,
              skills: bool = False, project_search: bool = False, memory: bool = False,
              past_chats: bool = False, scratch: bool = False, images: bool = False) -> list[Tool]:
    out = []
    for t in TOOLS:
        if t.name == "generate_image" and (not images or subagent):
            continue
        if scratch and t.needs_folder and t.name not in SCRATCH_TOOLS:
            continue  # a chat's scratch folder: files and commands, no background shells or code search
        if t.name == "find_tools" and not connectors:
            continue
        if t.name == "use_skill" and not skills:
            continue
        if (t.name == "search_project" and not project_search) or (t.name == "memory" and not memory) \
                or (t.name == "past_chats" and not past_chats):
            continue
        if subagent and t.name in ("memory", "past_chats"):
            continue
        if t.needs_folder and not folder:
            continue
        # Modes govern work in the user's folders, where the mode is shown; a chat's scratch folder is the
        # chat's own sandbox, so plan mode takes nothing away there.
        planning = mode == "plan" and folder and not scratch
        if t.name == "propose_plan" and not planning:
            continue
        if t.name == "ask_user" and not folder:
            continue  # in a plain chat the model can simply ask in its reply
        if planning and t.category == "edit":
            continue  # plan mode: nothing to change with yet (small models otherwise keep trying)
        if subagent == "explore" and t.name not in EXPLORE_TOOLS:
            continue
        if subagent and t.name in ("task", "ask_user", "propose_plan", "todo_write"):
            continue
        out.append(t)
    return out


def schemas(tool_list: list[Tool], scratch: bool = False) -> list[dict]:
    """The definitions the model sees. In a chat's scratch folder commands run to the end and return their
    output (it has no bash_output to read a background command with), so bash offers no background there."""
    out = []
    for t in tool_list:
        s = t.schema()
        if scratch and t.name == "bash":
            fn = s["function"]
            fn["description"] = ("Run a shell command in this chat's folder (bash, sandboxed). Network is off unless "
                                 "network is true and allowed. It returns the command's output when it finishes.")
            fn["parameters"] = {**fn["parameters"], "properties": {k: v for k, v in t.params.items() if k != "background"}}
        out.append(s)
    return out


# content a model escaped by mistake -----------------------------------------------------------
_PROSE = {"html", "svg", "markdown", "slides", "mermaid", "htm", "xml", "md", "txt", "css", "csv", "docx", "pdf", "pptx"}
_MARKUP = {"html", "svg", "htm", "xml"}
_JSON_ESCAPE = re.compile(r'\\([nrt"\'\\/])')


def unescape_content(text: str, kind: str = "") -> str:
    """Undo escaping a model added by mistake. Small models sometimes write a whole page as an escaped string:
    no line breaks but literal \\n, quotes as \\", or < and > as &lt; and &gt;. Saved as is, the page shows its
    own code. `kind` is an artifact kind or a file extension; only unmistakable cases change."""
    import html
    kind = (kind or "").lower().lstrip(".")
    if not text or kind not in _PROSE:
        return text
    if "\n" not in text and text.count("\\n") >= 3:
        text = _JSON_ESCAPE.sub(lambda m: {"n": "\n", "r": "", "t": "\t"}.get(m.group(1), m.group(1)), text)
    if kind in _MARKUP and text.lstrip().startswith("&lt;"):
        text = re.sub(r"<(?=<[A-Za-z!/])", "", html.unescape(text))
    return text


# helpers --------------------------------------------------------------------------------------
def resolve(path: str, folder: str | None) -> str:
    p = os.path.expanduser((path or ".").strip())
    if not os.path.isabs(p):
        if not folder:
            raise ToolError("No working folder; give an absolute path")
        p = os.path.join(folder, p)
    return os.path.realpath(p)


def rel(path: str, folder: str | None) -> str:
    if folder:
        r = os.path.relpath(path, folder)
        if not r.startswith(".."):
            return r
    return path


def is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def _gitignore(folder: str) -> list[str]:
    try:
        with open(os.path.join(folder, ".gitignore"), encoding="utf-8", errors="replace") as f:
            return [l.strip() for l in f if l.strip() and not l.startswith("#") and not l.startswith("!")]
    except OSError:
        return []


def _ignored(relpath: str, name: str, patterns: list[str]) -> bool:
    for p in patterns:
        pat = p.rstrip("/").lstrip("/")
        if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(relpath, pat) or relpath.startswith(pat + "/"):
            return True
    return False


def walk(root: str, folder: str | None, limit: int = 20000):
    ignore = _gitignore(folder) if folder else []
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        rdir = os.path.relpath(dirpath, folder or root)
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not _ignored(
            os.path.normpath(os.path.join(rdir, d)), d, ignore))
        for name in sorted(filenames):
            rp = os.path.normpath(os.path.join(rdir, name))
            if _ignored(rp, name, ignore):
                continue
            yield os.path.join(dirpath, name)
            count += 1
            if count >= limit:
                return


# file tools -----------------------------------------------------------------------------------
def read_file(args, folder) -> str:
    path = resolve(args["path"], folder)
    if os.path.isdir(path):
        raise ToolError(f"{rel(path, folder)} is a folder; use list_files")
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise ToolError(f"cannot read {rel(path, folder)}: {exc.strerror}") from exc
    if size > MAX_FILE_BYTES:
        raise ToolError(f"{rel(path, folder)} is {size} bytes; read parts with offset/limit after checking it with bash (head, wc)")
    with open(path, "rb") as f:
        data = f.read()
    if is_binary(data):
        return f"[{rel(path, folder)} is a binary file, {size} bytes]"
    text = data.decode("utf-8", "replace")
    lines = text.split("\n")
    offset = max(1, int(args.get("offset") or 1))
    limit = min(MAX_READ_LINES, max(1, int(args.get("limit") or MAX_READ_LINES)))
    chunk = lines[offset - 1: offset - 1 + limit]
    out = [f"{i:>6}\t{line[:MAX_LINE]}" for i, line in enumerate(chunk, start=offset)]
    tail = ""
    if offset - 1 + limit < len(lines):
        tail = f"\n[{len(lines)} lines in total; continue with offset={offset + limit}]"
    if not out:
        return f"[{rel(path, folder)} has {len(lines)} lines; offset {offset} is past the end]"
    return "\n".join(out) + tail


def list_files(args, folder) -> str:
    root = resolve(args.get("path") or ".", folder)
    if not os.path.isdir(root):
        raise ToolError(f"{rel(root, folder)} is not a folder")
    pattern = args.get("pattern")
    rx = None
    if pattern:
        from .permissions import _glob_re
        rx = _glob_re(pattern)
    items = []
    for p in walk(root, folder):
        r = os.path.relpath(p, root)
        if rx and not (rx.match(r) or rx.match(os.path.basename(r))):
            continue
        items.append(r)
        if len(items) >= 500:
            items.append("[… more files; narrow the pattern]")
            break
    return "\n".join(items) if items else "[no files]"


def search(args, folder) -> str:
    try:
        rx = re.compile(args["pattern"], re.IGNORECASE if args.get("ignore_case") else 0)
    except re.error as exc:
        raise ToolError(f"invalid regular expression: {exc}") from exc
    root = resolve(args.get("path") or ".", folder)
    files = [root] if os.path.isfile(root) else walk(root, folder)
    glob = args.get("glob")
    hits = []
    for p in files:
        if glob and not fnmatch.fnmatch(os.path.basename(p), glob) and not fnmatch.fnmatch(rel(p, folder), glob):
            continue
        try:
            if os.path.getsize(p) > 2 * 2**20:
                continue
            with open(p, "rb") as f:
                data = f.read()
        except OSError:
            continue
        if is_binary(data):
            continue
        for n, line in enumerate(data.decode("utf-8", "replace").split("\n"), start=1):
            if rx.search(line):
                hits.append(f"{rel(p, folder)}:{n}: {line.strip()[:300]}")
                if len(hits) >= 200:
                    return "\n".join(hits) + "\n[stopped at 200 matches; narrow the search]"
    return "\n".join(hits) if hits else "[no matches]"


def write_file(args, folder) -> dict:
    path = resolve(args["path"], folder)
    content = unescape_content(args.get("content", ""), os.path.splitext(path)[1])
    existed = os.path.exists(path)
    old = ""
    if existed:
        if os.path.isdir(path):
            raise ToolError(f"{rel(path, folder)} is a folder")
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            old = f.read()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    return {"output": f"{'Replaced' if existed else 'Created'} {rel(path, folder)} ({lines} lines)",
            "diff": _diff(old, content, rel(path, folder)), "path": rel(path, folder)}


def edit_file(args, folder) -> dict:
    path = resolve(args["path"], folder)
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError as exc:
        raise ToolError(f"{rel(path, folder)} does not exist; use write_file to create it") from exc
    except UnicodeDecodeError as exc:
        raise ToolError(f"{rel(path, folder)} is not UTF-8 text") from exc
    new, how, count = apply_edit(text, args.get("old_text", ""), args.get("new_text", ""), bool(args.get("replace_all")))
    fd = os.open(path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(new)
    note = "" if how == "exact" else f" (matched with {how} differences; check the result)"
    return {"output": f"Edited {rel(path, folder)}: {count} replacement{'s' if count != 1 else ''}{note}\n"
                      + _snippet(new, args.get("new_text", "")),
            "diff": _diff(text, new, rel(path, folder)), "path": rel(path, folder)}


def apply_edit(text: str, old: str, new: str, replace_all: bool = False) -> tuple[str, str, int]:
    if not old:
        raise ToolError("old_text is empty; use write_file to replace a whole file")
    if old == new:
        raise ToolError("old_text and new_text are identical")
    count = text.count(old)
    if count == 1 or (count > 1 and replace_all):
        return text.replace(old, new) if replace_all else text.replace(old, new, 1), "exact", count
    if count > 1:
        raise ToolError(f"old_text appears {count} times; include more surrounding lines, or set replace_all")
    lines, old_lines = text.split("\n"), old.split("\n")
    n = len(old_lines)
    for how, norm in (("trailing-space", str.rstrip), ("indentation", str.strip)):
        target = [norm(l) for l in old_lines]
        found = [i for i in range(len(lines) - n + 1) if [norm(l) for l in lines[i:i + n]] == target]
        if len(found) == 1:
            i = found[0]
            new_lines = _reindent(new.split("\n"), old_lines, lines[i:i + n]) if how == "indentation" else new.split("\n")
            return "\n".join(lines[:i] + new_lines + lines[i + n:]), how, 1
    if n >= 2:
        scored = []
        joined_old = "\n".join(l.strip() for l in old_lines)
        for i in range(len(lines) - n + 1):
            window = "\n".join(l.strip() for l in lines[i:i + n])
            scored.append((difflib.SequenceMatcher(None, joined_old, window).ratio(), i))
        scored.sort(reverse=True)
        if scored and scored[0][0] >= 0.9 and (len(scored) == 1 or scored[1][0] < scored[0][0] - 0.05):
            i = scored[0][1]
            new_lines = _reindent(new.split("\n"), old_lines, lines[i:i + n])
            return "\n".join(lines[:i] + new_lines + lines[i + n:]), "approximate", 1
    close = difflib.get_close_matches(old_lines[0].strip(), [l.strip() for l in lines], n=1, cutoff=0.6)
    hint = ""
    if close:
        idx = [l.strip() for l in lines].index(close[0])
        hint = f" The closest line is {idx + 1}: {lines[idx].strip()[:120]!r}. Read the file around it."
    raise ToolError("old_text was not found in the file." + hint)


def _indent(s: str) -> str:
    return s[: len(s) - len(s.lstrip())]


def _reindent(new_lines, old_lines, matched):
    """Carry the file's real indentation over to new_lines when old_text was indented differently."""
    pairs = [(len(_indent(o)), _indent(m)) for o, m in zip(old_lines, matched) if o.strip() and m.strip()]
    if all(len(mi) == oi for oi, mi in pairs):
        return new_lines
    unit = "\t" if any("\t" in mi for _, mi in pairs) else " "
    mapping = {oi: mi for oi, mi in pairs}
    ratios = {len(mi) / oi for oi, mi in pairs if oi}
    offsets = {len(mi) - oi for oi, mi in pairs}
    out = []
    for line in new_lines:
        if not line.strip():
            out.append(line)
            continue
        w = len(_indent(line))
        if w in mapping:
            ind = mapping[w]
        elif len(ratios) == 1 and w:
            ind = unit * round(w * ratios.pop()) if unit == " " else unit * w
            ratios = {len(mi) / oi for oi, mi in pairs if oi}
        elif len(offsets) == 1:
            ind = unit * max(0, w + next(iter(offsets)))
        else:
            out.append(line)
            continue
        out.append(ind + line.lstrip())
    return out


def _diff(old: str, new: str, name: str) -> str:
    d = difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{name}", f"b/{name}", lineterm="", n=3)
    text = "\n".join(d)
    return text[:60000]


def _snippet(text: str, needle: str) -> str:
    if not needle:
        return ""
    i = text.find(needle)
    if i < 0:
        return ""
    start_line = text.count("\n", 0, i) + 1
    lines = text.split("\n")
    a, b = max(0, start_line - 3), min(len(lines), start_line + needle.count("\n") + 2)
    return "\n".join(f"{n + 1:>6}\t{lines[n][:MAX_LINE]}" for n in range(a, b))


# web --------------------------------------------------------------------------------------------
async def web_fetch(args) -> str:
    try:
        r = await web.fetch(args["url"], int(args.get("max_chars") or 20000))
    except web.FetchError as exc:
        raise ToolError(str(exc)) from exc
    head = f"# {r['title']}\n" if r["title"] else ""
    tail = "\n[truncated]" if r["truncated"] else ""
    return f"{head}URL: {r['url']}\n\n{r['content']}{tail}"


async def web_search(args) -> str:
    try:
        results = await web.search(args["query"], int(args.get("max_results") or 8))
    except web.FetchError as exc:
        raise ToolError(str(exc)) from exc
    if not results:
        return "[no results]"
    return "\n\n".join(f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet']}" for i, r in enumerate(results, 1))


def todos_text(todos: list) -> str:
    marks = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}
    return "\n".join(f"{marks.get(t.get('status'), '[ ]')} {t.get('content', '')}" for t in todos)


__all__ = ["TOOLS", "BY_NAME", "Tool", "ToolError", "available", "schemas", "unescape_content", "read_file", "list_files",
           "search", "write_file", "edit_file", "apply_edit", "web_fetch", "web_search", "todos_text", "cap_output",
           "resolve", "rel"]
