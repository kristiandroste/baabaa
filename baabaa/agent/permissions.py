"""Modes and permission rules: decide whether a tool call runs, asks, or is refused.

Modes (as in Claude Code): manual, accept edits, plan, auto. Rules use Claude Code's syntax:
`Bash`, `Bash(npm run test:*)`, `Bash(git status)`, `Read(src/**)`, `Edit(docs/*.md)`,
`WebFetch(domain:example.com)`, `WebSearch`, or a tool's own name. Deny beats ask beats allow.

Auto mode's layers: (1) safe tools and rules, (2) the shell check, (3) the model judge. This module does
layers 1 and 2 and returns action 'judge' when layer 3 must decide.
"""

import fnmatch
import json
import os
import re
from dataclasses import dataclass, field

from .shellcheck import classify

MODES = ("manual", "accept_edits", "plan", "auto")
PLAN_DENIAL = ("Plan mode: nothing may be changed yet. Call propose_plan with the complete plan; "
               "after the user approves it you can make the changes.")
MODE_LABELS = {"manual": "Manual", "accept_edits": "Accept edits", "plan": "Plan", "auto": "Auto"}
MODE_CYCLE = ["auto", "manual", "accept_edits", "plan"]  # Shift+Tab order, as in Claude Code

RULE_TOOLS = {
    "bash": "Bash", "bash_output": "Bash", "kill_shell": "Bash",
    "read_file": "Read", "list_files": "Read", "search": "Read",
    "write_file": "Edit", "edit_file": "Edit",
    "web_fetch": "WebFetch", "web_search": "WebSearch",
}
PROTECTED = (".git/", ".baabaa/", ".env", "BAABAA.md", "AGENTS.md", ".ssh/", ".bashrc", ".zshrc", ".profile",
             ".github/workflows/", ".gitlab-ci.yml")
_RULE_RE = re.compile(r"^\s*([A-Za-z_][\w-]*)\s*(?:\((.*)\))?\s*$")


@dataclass
class Decision:
    action: str            # 'allow' | 'ask' | 'deny' | 'judge'
    layer: str             # 'rule' | 'safe' | 'mode' | 'shell' | 'path' | 'judge' | 'user'
    reason: str = ""
    rule: str | None = None
    extra: dict = field(default_factory=dict)

    def public(self) -> dict:
        return {"action": self.action, "layer": self.layer, "reason": self.reason, "rule": self.rule, **self.extra}


def parse_rule(pattern: str) -> tuple[str, str | None]:
    m = _RULE_RE.match(pattern)
    if not m:
        raise ValueError(f"not a rule: {pattern!r}")
    tool, spec = m.group(1), m.group(2)
    if tool == "Write":
        tool = "Edit"
    return tool, (spec.strip() if spec is not None else None)


def _glob_re(glob: str) -> re.Pattern:
    out, i = [], 0
    while i < len(glob):
        c = glob[i]
        if glob.startswith("**", i):
            out.append(".*")
            i += 2
            if i < len(glob) and glob[i] == "/":
                i += 1
            continue
        out.append("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def rule_matches(pattern: str, tool: str, args: dict, folder: str | None) -> bool:
    try:
        rtool, spec = parse_rule(pattern)
    except ValueError:
        return False
    family = RULE_TOOLS.get(tool, tool)
    if tool.startswith("mcp__") and (rtool == tool or tool.startswith(rtool + "__")):
        return True if spec in (None, "", "*") else fnmatch.fnmatchcase(json.dumps(args, sort_keys=True), spec)
    if rtool != family and rtool != tool:
        return False
    if spec is None or spec == "" or spec == "*":
        return True
    if family == "Bash":
        cmd = " ".join((args.get("command") or "").split())
        if spec.endswith(":*"):
            prefix = spec[:-2].strip()
            return cmd == prefix or cmd.startswith(prefix + " ")
        if "*" in spec:
            return fnmatch.fnmatchcase(cmd, spec)
        return cmd == " ".join(spec.split())
    if family in ("Read", "Edit"):
        path = args.get("path") or args.get("pattern") or "."
        full = _abs(path, folder)
        pat = spec[2:] if spec.startswith("./") else spec
        if pat.startswith("~"):
            pat = os.path.expanduser(pat)
        rel = os.path.relpath(full, folder) if folder and not os.path.isabs(pat) else full
        return bool(_glob_re(pat).match(rel)) or bool(_glob_re(pat).match(full))
    if family == "WebFetch":
        if spec.startswith("domain:"):
            host = re.sub(r"^[a-z]+://", "", (args.get("url") or "").lower()).split("/", 1)[0].split(":")[0]
            dom = spec[7:].lower()
            return host == dom or host.endswith("." + dom)
        return fnmatch.fnmatchcase(args.get("url") or "", spec)
    return fnmatch.fnmatchcase(str(args), spec)


def _abs(path: str, folder: str | None) -> str:
    p = os.path.expanduser(path or ".")
    if not os.path.isabs(p):
        p = os.path.join(folder or "/", p)
    return os.path.realpath(p)


def inside(path: str, root: str | None) -> bool:
    if not root:
        return False
    full = os.path.realpath(path)
    root = os.path.realpath(root)
    return full == root or full.startswith(root.rstrip("/") + "/")


def protected(path: str, folder: str | None) -> bool:
    rel = os.path.relpath(os.path.realpath(path), os.path.realpath(folder)) if folder else path
    rel = rel.replace("\\", "/")
    return any(rel == p.rstrip("/") or rel.startswith(p) or f"/{p}" in f"/{rel}" for p in PROTECTED)


@dataclass
class Context:
    mode: str
    folder: str | None
    rules: list[dict]
    readable: list[str]        # folders the account may read (the working folder and grants)
    writable: list[str]        # folders the account may write
    network: bool = False      # conversation-level network setting for shell commands
    is_owner: bool = False
    scratch: str | None = None  # a chat's private scratch folder: its work runs sandboxed without asking


def decide(tool: str, category: str, args: dict, ctx: Context) -> Decision:
    # In plan mode nothing may change, so rules that would let something run or ask do not apply; reading
    # a web page changes nothing, so its rules do.
    planning = ctx.mode == "plan" and category != "net"
    for kind in ("deny", "ask", "allow"):
        for r in ctx.rules:
            if r["kind"] == kind and rule_matches(r["pattern"], tool, args, ctx.folder):
                if kind == "deny":
                    return Decision("deny", "rule", f"Denied by the rule {r['pattern']}", r["pattern"])
                if kind == "ask" and not planning:
                    return Decision("ask", "rule", f"The rule {r['pattern']} asks first", r["pattern"])
                if kind == "allow" and not planning and _within_reach(tool, category, args, ctx):
                    return Decision("allow", "rule", f"Allowed by the rule {r['pattern']}", r["pattern"])

    if tool == "web_fetch" and ctx.folder and not ctx.scratch:
        # Working in a folder, a page's address could carry what the model has read there to someone else.
        # So it asks per site, as Claude Code does ("Always allow" adds WebFetch(domain:…)); Auto's judge
        # decides. A chat without a folder reads pages freely, as the chat apps do.
        if ctx.mode == "auto":
            return Decision("judge", "net", "A web page read while working in a folder")
        return Decision("ask", "mode", "Reading a web page asks first while working in a folder")

    if category in ("meta", "net"):
        return Decision("allow", "safe", "Read-only or conversation-only tool")

    if ctx.scratch and category in ("read", "edit", "exec"):
        # The chat's own scratch folder: the sandbox lets commands change only that folder and, unless
        # network access is requested, reach nothing. Like code execution in a chat app, it runs directly.
        if category == "exec":
            if args.get("network") and not ctx.network:
                return Decision("ask", "path", "The command asks for network access")
            return Decision("allow", "sandbox", "Runs in this chat's private scratch folder")
        path = _abs(args.get("path") or ".", ctx.scratch)
        if inside(path, ctx.scratch):
            return Decision("allow", "sandbox", "Inside this chat's private scratch folder")
        if category == "read" and any(inside(path, r) for r in ctx.readable):
            return Decision("allow", "safe", "Reading inside an allowed folder")
        return Decision("deny", "path", "Only this chat's scratch folder can be used here")

    if category in ("mcp", "mcp_ro"):
        read_only = category == "mcp_ro"
        if read_only:
            return Decision("allow", "safe", "The connector marks this tool as read-only")
        if ctx.mode == "plan":
            return Decision("deny", "mode", PLAN_DENIAL)
        if ctx.mode == "auto":
            return Decision("judge", "mcp", "A connector tool that may change things")
        return Decision("ask", "mode", "Connector tools ask first in this mode")

    if category == "read":
        path = _abs(args.get("path") or ".", ctx.folder)
        if any(inside(path, r) for r in ctx.readable):
            return Decision("allow", "safe", "Reading inside an allowed folder")
        if not ctx.is_owner:
            return Decision("deny", "path", "That path is outside the folders this account may read")
        if ctx.mode == "plan" or ctx.mode == "auto":
            return Decision("judge", "path", "Reading outside the working folder")
        return Decision("ask", "path", "Reading outside the working folder")

    if category == "edit":
        path = _abs(args.get("path") or ".", ctx.folder)
        if ctx.mode == "plan":
            return Decision("deny", "mode", PLAN_DENIAL)
        in_folder = any(inside(path, w) for w in ctx.writable)
        if not in_folder and not ctx.is_owner:
            return Decision("deny", "path", "That path is outside the folders this account may change")
        if _sensitive_path(path):
            return Decision("ask", "path", "A sensitive file (shell start-up, keys, credentials or system configuration)")
        if in_folder and protected(path, ctx.folder):
            return Decision("ask", "path", "A protected file (settings, hooks, secrets or instructions)")
        if ctx.mode == "manual":
            return Decision("ask", "mode", "Manual mode asks before every edit")
        if in_folder:
            return Decision("allow", "mode", "Edits inside the working folder are accepted")
        if ctx.mode == "auto":
            return Decision("judge", "path", "An edit outside the working folder")
        return Decision("ask", "path", "An edit outside the working folder")

    if category == "exec":
        command = args.get("command") or ""
        verdict = classify(command, ctx.folder)
        wants_net = bool(args.get("network")) and not ctx.network
        if ctx.mode == "plan":
            if verdict.kind == "safe" and not wants_net:
                return Decision("allow", "shell", "A read-only command")
            return Decision("deny", "mode", PLAN_DENIAL + " Only read-only commands run in plan mode.")
        if verdict.kind == "dangerous":
            return Decision("ask", "shell", verdict.reason, extra={"danger": True})
        if ctx.mode in ("manual", "accept_edits"):
            return Decision("ask", "mode", "Commands ask first in this mode")
        if verdict.kind == "safe" and not wants_net:
            return Decision("allow", "shell", "A read-only command")
        reason = verdict.reason or "Not a known read-only command"
        if wants_net:
            reason += "; needs network access"
        return Decision("judge", "shell", reason)

    return Decision("ask", "mode", "Unknown kind of action")


def _sensitive_path(path: str) -> bool:
    from .shellcheck import SENSITIVE_PATHS
    return any(s in path for s in SENSITIVE_PATHS)


def _within_reach(tool: str, category: str, args: dict, ctx: Context) -> bool:
    """An allow rule never widens what an account may touch: paths must still be within its folders."""
    if category == "read":
        path = _abs(args.get("path") or ".", ctx.folder)
        return ctx.is_owner or any(inside(path, r) for r in ctx.readable)
    if category == "edit":
        path = _abs(args.get("path") or ".", ctx.folder)
        return any(inside(path, w) for w in ctx.writable) or ctx.is_owner
    return True
