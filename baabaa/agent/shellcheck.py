"""Classify a shell command line before it runs: 'safe' (read-only), 'dangerous' (with a reason) or 'unknown'.

This is auto mode's second layer, ahead of the model judge. It parses the line with `shlex`, splits it
into simple commands at ; && || | & and at line breaks, unwraps wrappers (sudo, env, nohup, timeout,
xargs, sh -c ...) and looks at each command. Anything it cannot parse confidently is 'unknown', never 'safe'.
"""

import os
import re
import shlex
import sys

SAFE_COMMANDS = {
    "ls", "cat", "head", "tail", "wc", "grep", "egrep", "fgrep", "rg", "stat", "file", "du", "df", "pwd",
    "echo", "printf", "which", "type", "whoami", "id", "date", "uname", "hostname", "tree", "sort", "uniq",
    "cut", "tr", "nl", "less", "more", "diff", "cmp", "comm", "basename", "dirname", "realpath", "readlink",
    "md5sum", "sha1sum", "sha256sum", "true", "false", "test", "[", "seq", "column", "jq", "env", "printenv",
    "ps", "free", "uptime", "nproc", "lscpu", "whereis", "man", "od", "hexdump", "xxd", "strings", "fold",
}
GIT_SAFE = {"status", "log", "diff", "show", "branch", "remote", "rev-parse", "ls-files", "blame", "describe",
            "shortlog", "tag", "config", "grep", "reflog", "ls-tree", "cat-file", "whatchanged"}
WRAPPERS = {"nohup", "time", "nice", "ionice", "stdbuf", "command", "builtin", "exec"}
INTERPRETER_EXEC = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
DANGEROUS_ANYWHERE = {
    "mkfs": "formats a filesystem", "fdisk": "partitions a disk", "parted": "partitions a disk",
    "wipefs": "wipes filesystem signatures", "shred": "destroys file contents", "dd": "writes raw data",
    "shutdown": "shuts the machine down", "reboot": "reboots the machine", "poweroff": "powers the machine off",
    "halt": "halts the machine", "init": "changes the run level", "telinit": "changes the run level",
    "mkswap": "formats swap", "swapoff": "disables swap", "insmod": "loads a kernel module",
    "rmmod": "unloads a kernel module", "modprobe": "loads a kernel module", "crontab": "changes scheduled jobs",
    "passwd": "changes a password", "useradd": "adds a user", "userdel": "deletes a user", "usermod": "changes a user",
    "chpasswd": "changes passwords", "visudo": "changes sudo rules", "iptables": "changes the firewall",
    "nft": "changes the firewall", "ufw": "changes the firewall",
    # macOS
    "diskutil": "manages disks", "asr": "restores disk images", "launchctl": "changes services and startup items",
    "osascript": "controls other applications", "security": "reads or changes the keychain",
    "csrutil": "changes system integrity protection", "nvram": "changes firmware settings", "pmset": "changes power settings",
    "spctl": "changes app security", "tccutil": "changes privacy permissions", "systemsetup": "changes system settings",
    "dscl": "changes users and groups", "kextload": "loads a kernel extension",
}
SENSITIVE_PATHS = (".ssh", ".gnupg", ".bashrc", ".zshrc", ".profile", ".bash_profile", ".config/autostart",
                   ".git/hooks", "authorized_keys", ".netrc", ".aws", ".kube", "/etc/", "/boot", "/usr/", "/bin/", "/sbin/",
                   # macOS: startup items, shell start files, keychains, system folders
                   ".zshenv", ".zprofile", ".zlogin", "Library/LaunchAgents", "Library/LaunchDaemons", "Library/Keychains",
                   "/System/", "/Library/", "/private/etc/")


class Verdict:
    def __init__(self, kind: str, reason: str = ""):
        self.kind = kind  # 'safe' | 'dangerous' | 'unknown'
        self.reason = reason

    def __repr__(self):
        return f"Verdict({self.kind!r}, {self.reason!r})"


_PIPE_TO_INTERPRETER = re.compile(
    r"\b(curl|wget|fetch)\b[^|;&]*\|\s*(sudo\s+)?(sh|bash|zsh|dash|ksh|python3?|perl|ruby|node)\b")
# another program's environment or memory (secrets live there), whatever command reads it
_PROC_PRIVATE = re.compile(r"/proc/[^/\s]+/(environ|mem|auxv)\b")


def classify(command: str, folder: str | None = None) -> Verdict:
    if _PIPE_TO_INTERPRETER.search(command):
        return Verdict("dangerous", "runs downloaded code (a download piped into an interpreter)")
    if _PROC_PRIVATE.search(command):
        return Verdict("unknown", "reads another program's environment or memory")
    try:
        commands = split_commands(command)
    except ValueError as exc:
        return Verdict("unknown", f"could not parse the command: {exc}")
    if not commands:
        return Verdict("unknown", "empty command")
    worst = Verdict("safe")
    for words, redirects in commands:
        v = _one(words, redirects, folder, depth=0)
        if v.kind == "dangerous":
            return v
        if v.kind == "unknown" and worst.kind == "safe":
            worst = v
    return worst


def split_commands(line: str) -> list[tuple[list[str], list[str]]]:
    """[(words, redirect_targets)] for each simple command. Raises ValueError on unbalanced quotes."""
    if "$(" in line or "`" in line or "<(" in line or ">(" in line:
        inner = re.findall(r"\$\(([^()]*)\)|`([^`]*)`", line)
        parts = [a or b for a, b in inner]
        base = re.sub(r"\$\([^()]*\)|`[^`]*`", " SUBST ", line)
        out = split_commands(base)
        for p in parts:
            out += split_commands(p)
        return out
    lex = shlex.shlex(_line_breaks(line), posix=True, punctuation_chars=";&|<>")
    lex.whitespace_split = True
    lex.commenters = ""
    tokens = list(lex)
    out, words, redirects, i = [], [], [], 0
    while i < len(tokens):
        t = tokens[i]
        if t in (";", "&&", "||", "|", "&", "|&", ";;"):
            if words:
                out.append((words, redirects))
            words, redirects = [], []
        elif set(t) <= set("<>&") and t:
            target = tokens[i + 1] if i + 1 < len(tokens) else ""
            if ">" in t:
                redirects.append(target)
            i += 1
        elif re.fullmatch(r"\d?[<>]{1,2}&?\d*", t):
            redirects.append(tokens[i + 1] if i + 1 < len(tokens) else "")
            i += 1
        else:
            words.append(t)
        i += 1
    if words:
        out.append((words, redirects))
    return out


def _line_breaks(line: str) -> str:
    """A line break outside quotes ends a command, as in the shell: turn it into ';' so each line is checked
    on its own. A backslash before it continues the line."""
    out, quote, i = [], None, 0
    while i < len(line):
        c = line[i]
        if c == "\\" and quote != "'" and i + 1 < len(line):
            if line[i + 1] in "\r\n":
                out.append(" ")  # continuation
            else:
                out.append(line[i:i + 2])
            i += 2
            continue
        if quote:
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
        elif c in "\r\n":
            c = ";"
        out.append(c)
        i += 1
    return "".join(out)


def _one(words: list[str], redirects: list[str], folder, depth: int) -> Verdict:
    if depth > 6:
        return Verdict("unknown", "too deeply nested")
    # leading VAR=value assignments
    while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
        words = words[1:]
    if not words:
        return Verdict("safe")
    for target in redirects:
        if target in ("/dev/null", "/dev/stdout", "/dev/stderr") or re.fullmatch(r"&?\d", target or ""):
            continue
        if _sensitive(target):
            return Verdict("dangerous", f"writes to a sensitive location ({target})")
        if not _inside(target, folder):
            return Verdict("dangerous", f"writes outside the working folder ({target})")
        return Verdict("unknown", "writes a file")
    cmd = os.path.basename(words[0])
    args = words[1:]
    if cmd == "sudo" or cmd == "su" or cmd == "doas" or cmd == "pkexec":
        return Verdict("dangerous", "runs with elevated privileges")
    if cmd in WRAPPERS:
        return _one(args, redirects, folder, depth + 1)
    if cmd == "env":
        rest = [a for a in args if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", a) and not a.startswith("-")]
        return _one(rest, redirects, folder, depth + 1) if rest else Verdict("safe")
    if cmd == "timeout":
        rest = [a for a in args if not a.startswith("-")]
        return _one(rest[1:], redirects, folder, depth + 1) if len(rest) > 1 else Verdict("unknown")
    if cmd == "xargs":
        rest = [a for a in args if not a.startswith("-")]
        return _one(rest, redirects, folder, depth + 1) if rest else Verdict("unknown", "xargs")
    if cmd in INTERPRETER_EXEC:
        if "-c" in args:
            script = args[args.index("-c") + 1] if args.index("-c") + 1 < len(args) else ""
            try:
                inner = split_commands(script)
            except ValueError:
                return Verdict("unknown", "unparsable script")
            worst = Verdict("safe")
            for w, r in inner:
                v = _one(w, r, folder, depth + 1)
                if v.kind == "dangerous":
                    return v
                if v.kind == "unknown":
                    worst = v
            return worst
        return Verdict("unknown", f"runs a {cmd} script")
    if cmd in DANGEROUS_ANYWHERE or cmd.startswith("mkfs."):
        return Verdict("dangerous", DANGEROUS_ANYWHERE.get(cmd, "formats a filesystem"))
    if cmd == "rm":
        return _rm(args, folder)
    if cmd in ("chmod", "chown", "chgrp"):
        targets = [a for a in args[1:] if not a.startswith("-")]
        if any(_sensitive(t) or not _inside(t, folder) for t in targets):
            return Verdict("dangerous", f"{cmd} outside the working folder")
        return Verdict("unknown", f"{cmd} changes permissions")
    if cmd in ("mv", "cp", "ln", "rsync", "install", "tee", "touch", "mkdir", "rmdir", "truncate"):
        targets = [a for a in args if not a.startswith("-")]
        if any(_sensitive(t) for t in targets):
            return Verdict("dangerous", f"{cmd} touches a sensitive location")
        if any(not _inside(t, folder) for t in targets[-1:]):
            return Verdict("dangerous", f"{cmd} writes outside the working folder")
        return Verdict("unknown", f"{cmd} changes files")
    if cmd == "kill" or cmd == "pkill" or cmd == "killall":
        if "-1" in args or (args and args[-1] == "-1"):
            return Verdict("dangerous", "kills every process")
        return Verdict("unknown", "stops processes")
    if cmd == "git":
        return _git(args)
    if cmd in ("curl", "wget"):
        return _fetch(args)
    if cmd in ("nc", "ncat", "netcat", "socat", "ssh", "scp", "sftp", "telnet", "ftp"):
        return Verdict("unknown", f"{cmd} opens a network connection")
    if cmd == "find":
        if any(a in ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls") for a in args):
            if "-delete" in args:
                return Verdict("unknown", "find -delete removes files")
            return Verdict("unknown", "find runs commands")
        return Verdict("safe")
    if cmd == "sed":
        return Verdict("unknown", "sed edits files in place") if any(a.startswith("-i") or a == "--in-place" for a in args) else Verdict("safe")
    if cmd in ("python", "python3", "node", "perl", "ruby", "php", "lua"):
        if args and args[0] in ("--version", "-V", "-v"):
            return Verdict("safe")
        return Verdict("unknown", f"runs {cmd} code")
    if cmd == "systemctl":
        if args and args[0] in ("status", "show", "list-units", "list-unit-files", "is-active", "is-enabled", "cat"):
            return Verdict("safe")
        return Verdict("dangerous", "changes system services")
    if cmd == "man" and any(a.startswith(("-P", "--pager", "-H", "--html")) for a in args):
        return Verdict("unknown", "man runs another program")
    if cmd in SAFE_COMMANDS:
        return Verdict("safe")
    return Verdict("unknown", f"{cmd} is not on the read-only list")


def _rm(args, folder) -> Verdict:
    flags = "".join(a.lstrip("-") for a in args if a.startswith("-") and a != "--")
    targets = [a for a in args if not a.startswith("-")]
    forced = "f" in flags or "--force" in args
    recursive = "r" in flags.lower() or "--recursive" in args
    for t in targets:
        expanded = os.path.expanduser(t)
        if t in ("/", "/*", "~", "~/", "*", ".", "..", "./*", "../", "$HOME", "${HOME}") or expanded in ("/", os.path.expanduser("~")):
            return Verdict("dangerous", f"deletes {t}")
        if _sensitive(t):
            return Verdict("dangerous", f"deletes a sensitive location ({t})")
        if not _inside(t, folder):
            return Verdict("dangerous", f"deletes outside the working folder ({t})")
        if folder and os.path.normpath(os.path.join(folder, os.path.expanduser(t))) == os.path.normpath(folder):
            return Verdict("dangerous", "deletes the whole working folder")
    if recursive and forced:
        return Verdict("unknown", "force-deletes a directory tree inside the working folder")
    return Verdict("unknown", "deletes files")


def _git(args) -> Verdict:
    sub = next((a for a in args if not a.startswith("-")), "")
    if sub in GIT_SAFE and not any(a in ("--delete", "-d", "-D", "--unset", "--add", "--global") for a in args):
        if sub in ("branch", "tag", "remote", "config", "reflog") and len([a for a in args if not a.startswith("-")]) > 1:
            return Verdict("unknown", f"git {sub} with arguments may change the repository")
        return Verdict("safe")
    if sub == "push":
        if any(a in ("--force", "-f", "--force-with-lease", "--mirror", "--delete") or a.startswith("+") for a in args):
            return Verdict("dangerous", "force-pushes or deletes remote history")
        return Verdict("dangerous", "publishes commits to a remote repository")
    if sub == "reset" and "--hard" in args:
        return Verdict("unknown", "discards uncommitted changes")
    if sub == "clean" and any("f" in a for a in args if a.startswith("-")):
        return Verdict("unknown", "deletes untracked files")
    return Verdict("unknown", f"git {sub}")


def _fetch(args) -> Verdict:
    uploads = {"-d", "--data", "--data-binary", "--data-raw", "--data-urlencode", "-F", "--form", "-T",
               "--upload-file", "--post-data", "--post-file", "-X"}
    if any(a in uploads or a.startswith("--data") for a in args):
        return Verdict("unknown", "sends data to a server")
    if any(a in ("-o", "-O", "--output", "--remote-name") for a in args):
        return Verdict("unknown", "downloads a file")
    return Verdict("unknown", "fetches a URL")


def _sensitive(path: str) -> bool:
    p = os.path.expanduser(path)
    if sys.platform == "darwin":  # macOS disks ignore case: .SSH is .ssh
        return any(s.lower() in p.lower() for s in SENSITIVE_PATHS) and not p.startswith(("/tmp/", "./"))
    return any(s in p for s in SENSITIVE_PATHS) and not p.startswith(("/tmp/", "./"))


def _inside(path: str, folder: str | None) -> bool:
    if not folder:
        return False
    if not path or path.startswith("-"):
        return True
    p = os.path.expanduser(path)
    if "$" in p or "*" in p and p.startswith("/"):
        return False
    full = os.path.normpath(p if os.path.isabs(p) else os.path.join(folder, p))
    root = os.path.normpath(folder)
    return full == root or full.startswith(root + os.sep)
