"""Customization from files: skills, slash commands, helper agents, reply styles and hooks.

Two places, the second overriding the first by name:
- the account's own folder: `<data>/accounts/<id>/files/extend/{skills,commands,agents,styles}` and `hooks.json`;
- a trusted working folder: `<folder>/.baabaa/{skills,commands,agents,styles}` and `.baabaa/settings.json`
  (`{"hooks": …}`), plus `<folder>/.agents/skills` (the open Agent Skills layout).

Files are Markdown with a small front matter block (`---` … `---`, `key: value` lines).
A skill is a folder with `SKILL.md`; the model sees only names and descriptions until it loads one.
"""

import asyncio
import fnmatch
import json
import os
import re
import shlex

from .util import clip

STYLES = {
    "default": ("Default", ""),
    "concise": ("Concise", "Answer as briefly as possible: short sentences, no preamble, no summary at the end."),
    "explanatory": ("Explanatory", "Explain your reasoning and the why behind choices, with short examples, as a patient "
                    "teacher would, while still completing the task."),
    "learning": ("Learning", "Help the user learn by doing: explain concepts, and at suitable points leave a small, "
                 "clearly marked part of the work for the user to try, with hints."),
}
HOOK_EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop")

# Commands that come with baabaa; a custom command of the same name replaces one.
BUILTIN_COMMANDS = {
    "review": ("Review the folder's changes for bugs",
               "Review the changes in this working folder. Run `git status`, `git diff` and `git diff --cached` (and, if "
               "a base is given here: $ARGUMENTS, `git diff` against it) and read the surrounding code where you need to. "
               "Report bugs and logic errors, edge cases that are not handled, missing or failing tests, and unclear code. "
               "For each finding give the file and line, why it is a problem and a concrete fix, most serious first. Do not "
               "change any files. If there are no changes, say so."),
    "security-review": ("Review the folder's changes for security problems",
                        "Review the changes in this working folder for security problems. Run `git diff` and `git diff "
                        "--cached` ($ARGUMENTS) and read the code they touch. Look for injection (SQL, shell, template), "
                        "missing authentication or authorization checks, path traversal, unsafe deserialization, secrets "
                        "in code, weak cryptography, and user input reaching dangerous calls. For each finding give the "
                        "file and line, how it could be exploited and the fix. Do not change any files."),
    "init": ("Write BAABAA.md: notes on this project for future conversations",
             "Look through this project: its README, build and package files, main folders and tests. Then write "
             "BAABAA.md at the root of the working folder with what the project is, how to build, run and test it, "
             "how the code is laid out, and the conventions to follow. Keep it short and factual. If BAABAA.md "
             "already exists, improve it instead. $ARGUMENTS"),
}


def front_matter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    meta, key = {}, None
    for line in text[3:end].strip("\n").splitlines():
        if re.match(r"^\s*-\s+", line) and key:
            meta.setdefault(key, [])
            if isinstance(meta[key], list):
                meta[key].append(line.split("-", 1)[1].strip().strip("'\""))
            continue
        m = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if not m:
            continue
        key, value = m.group(1).strip(), m.group(2).strip()
        if value.startswith("[") and value.endswith("]"):
            meta[key] = [v.strip().strip("'\"") for v in value[1:-1].split(",") if v.strip()]
        elif value == "":
            meta[key] = []
        else:
            meta[key] = value.strip("'\"")
    body = text[end + 4:].lstrip("\n")
    return meta, body


def _read(path: str, limit: int = 200_000) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return None


class Extensions:
    def __init__(self, paths):
        self.paths = paths

    def roots(self, account_id: str, folder: str | None, trusted: bool) -> list[tuple[str, str]]:
        """[(label, root)] in increasing priority."""
        out = [("account", str(self.paths.account_files(account_id, "extend")))]
        if folder and trusted:
            out.append(("folder", os.path.join(folder, ".baabaa")))
        return out

    # skills ---------------------------------------------------------------------------------------
    def skills(self, account_id: str, folder: str | None, trusted: bool) -> dict[str, dict]:
        found = {}
        dirs = [(label, os.path.join(root, "skills")) for label, root in self.roots(account_id, folder, trusted)]
        if folder and trusted:
            dirs.insert(1, ("folder", os.path.join(folder, ".agents", "skills")))
        for label, d in dirs:
            try:
                names = sorted(os.listdir(d))
            except OSError:
                continue
            for name in names:
                path = os.path.join(d, name, "SKILL.md")
                text = _read(path, 20_000)
                if text is None:
                    continue
                meta, body = front_matter(text)
                sname = str(meta.get("name") or name)
                found[sname] = {"name": sname, "description": clip(str(meta.get("description") or body[:200]), 300),
                                "dir": os.path.join(d, name), "path": path, "source": label}
        return found

    def load_skill(self, skill: dict) -> str:
        text = _read(skill["path"]) or ""
        _, body = front_matter(text)
        files = []
        for root, _dirs, names in os.walk(skill["dir"]):
            for n in names:
                if n != "SKILL.md":
                    files.append(os.path.relpath(os.path.join(root, n), skill["dir"]))
        listing = ("\n\nFiles in this skill (read them with read_file when needed, full path shown):\n"
                   + "\n".join(f"- {os.path.join(skill['dir'], f)}" for f in sorted(files)[:50])) if files else ""
        return f"# Skill: {skill['name']}\n\n{body.strip()}{listing}"

    # commands, agents, styles ------------------------------------------------------------------------
    def _markdown_dir(self, kind: str, account_id: str, folder: str | None, trusted: bool) -> dict[str, dict]:
        found = {}
        for label, root in self.roots(account_id, folder, trusted):
            d = os.path.join(root, kind)
            try:
                names = sorted(os.listdir(d))
            except OSError:
                continue
            for fname in names:
                if not fname.endswith(".md"):
                    continue
                text = _read(os.path.join(d, fname))
                if text is None:
                    continue
                meta, body = front_matter(text)
                name = str(meta.get("name") or fname[:-3])
                found[name] = {"name": name, "description": clip(str(meta.get("description") or ""), 300), "body": body,
                               "meta": meta, "source": label, "path": os.path.join(d, fname)}
        return found

    def commands(self, account_id, folder, trusted) -> dict[str, dict]:
        out = {name: {"name": name, "description": desc, "body": body, "meta": {}, "source": "built-in", "path": None}
               for name, (desc, body) in BUILTIN_COMMANDS.items()}
        out["research"] = {"name": "research", "description": "Research a question on the web and write a cited report",
                           "body": "", "meta": {}, "source": "built-in", "path": None}
        out.update(self._markdown_dir("commands", account_id, folder, trusted))
        return out

    def agents(self, account_id, folder, trusted) -> dict[str, dict]:
        return self._markdown_dir("agents", account_id, folder, trusted)

    def styles(self, account_id, folder, trusted) -> dict[str, dict]:
        out = {k: {"name": k, "label": v[0], "body": v[1], "source": "built-in"} for k, v in STYLES.items()}
        for name, s in self._markdown_dir("styles", account_id, folder, trusted).items():
            out[name] = {"name": name, "label": s["meta"].get("label") or name, "body": s["body"], "source": s["source"]}
        return out

    def expand_command(self, text: str, account_id, folder, trusted) -> tuple[str, str] | None:
        """'/name args' -> (name, prompt) when a command file exists."""
        m = re.match(r"^/([A-Za-z0-9_.:-]+)(?:\s+(.*))?$", text.strip(), re.S)
        if not m:
            return None
        cmd = self.commands(account_id, folder, trusted).get(m.group(1))
        if cmd is None or (cmd["source"] == "built-in" and cmd["name"] == "research"):
            return None  # /research is handled by the agent
        args = (m.group(2) or "").strip()
        body = cmd["body"]
        prompt = body.replace("$ARGUMENTS", args) if "$ARGUMENTS" in body else (body + (f"\n\n{args}" if args else ""))
        for i, a in enumerate(args.split(), start=1):
            prompt = prompt.replace(f"${i}", a)
        return cmd["name"], prompt.strip()

    # hooks ----------------------------------------------------------------------------------------
    def hooks(self, account_id, folder, trusted) -> dict[str, list]:
        out = {e: [] for e in HOOK_EVENTS}
        sources = [os.path.join(str(self.paths.account_files(account_id, "extend")), "hooks.json")]
        if folder and trusted:
            sources.append(os.path.join(folder, ".baabaa", "settings.json"))
        for path in sources:
            text = _read(path)
            if not text:
                continue
            try:
                data = json.loads(text)
            except ValueError:
                continue
            hooks = data.get("hooks", data) if isinstance(data, dict) else {}
            for event in HOOK_EVENTS:
                for h in hooks.get(event, []) or []:
                    if isinstance(h, dict) and h.get("command"):
                        out[event].append({"matcher": h.get("matcher") or "*", "command": h["command"],
                                           "timeout": int(h.get("timeout") or 60), "source": path})
        return out


STOPWORDS = set("""a an the and or but if then else for to of in on at by with from as is are was were be been being it its
this that these those i you he she we they me my your our their what which who whom how when where why can could should would
will shall may might must do does did done have has had not no yes use using used any all each every some such only also more most
very just about into over under than too so up down out off again further once here there both few other own same s t don now
question questions answer answering task tasks""".split())


def _stem(w: str) -> str:
    for suffix in ("ing", "ers", "er", "ed", "es", "s"):
        if len(w) > len(suffix) + 3 and w.endswith(suffix):
            return w[: -len(suffix)]
    return w


def _words(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2 and w not in STOPWORDS}


def match_skills(skills: list[dict], text: str, limit: int = 1) -> list[dict]:
    """Skills whose name, or at least two meaningful words of their description, appear in the message."""
    words = _words(text)
    low = (text or "").lower()
    scored = []
    for s in skills:
        name_hit = s["name"].lower() in low or s["name"].lower().replace("-", " ") in low
        overlap = len(words & _words(s["name"] + " " + s["description"]))
        if name_hit or overlap >= 2:
            scored.append((overlap + (5 if name_hit else 0), s))
    scored.sort(key=lambda x: -x[0])
    return [s for _, s in scored[:limit]]


async def run_hook(shells, account_id: str, hook: dict, payload: dict, folder: str | None, writable, readable) -> dict:
    """Run one hook command in the sandbox with the event as JSON on stdin.

    Returns {'code', 'stdout', 'stderr'}. Exit code 2 means "block" (PreToolUse, UserPromptSubmit) or
    "keep going" (Stop); stderr is then shown to the model.
    """
    import tempfile
    cwd = folder or str(shells.paths.account_home(account_id))
    data = json.dumps(payload)
    tmp = tempfile.NamedTemporaryFile("w", delete=False, dir=str(shells.paths.account_tmp(account_id)), suffix=".json")
    tmp.write(data)
    tmp.close()
    try:
        cmd = f"( {hook['command']}\n) < {shlex.quote(tmp.name)}"  # the whole hook reads the event on stdin
        res = await shells.run(account_id, cmd, cwd, writable or [cwd], readable or [], False, hook.get("timeout", 60))
    finally:
        os.unlink(tmp.name)
    return {"code": res["exit_code"], "stdout": res["output"], "stderr": res["output"] if res["exit_code"] else ""}


def hook_matches(matcher: str, tool: str) -> bool:
    return any(fnmatch.fnmatchcase(tool, m.strip()) for m in re.split(r"[|,]", matcher or "*") if m.strip())
