"""A small Markdown reader for the document writers: blocks and inline runs, no HTML.

Blocks: heading, paragraph, list item (flattened, with level and numbering), table, code, quote, rule,
and break (a line `<!-- pagebreak -->` or `\\pagebreak`: a new page, or a new slide in presentations).
Runs are (text, style) pairs; style keys: b, i, code, s (strikethrough), link (URL).
"""

import re

_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_LIST = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])\s+(?:\[([ xX])\]\s+)?(.*)$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```+|~~~+)\s*([\w+#.-]*)")
_BREAK = re.compile(r"^\s*(<!--\s*pagebreak\s*-->|\\pagebreak|\\newpage)\s*$", re.I)


def parse(text: str) -> list[dict]:
    lines = (text or "").replace("\r\n", "\n").replace("\t", "    ").split("\n")
    out: list[dict] = []
    para: list[str] = []
    counters: dict[int, int] = {}

    def flush():
        if para:
            out.append({"t": "p", "runs": inline(" ".join(s.strip() for s in para))})
            para.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            flush()
            i += 1
            continue
        if _BREAK.match(line):
            flush()
            out.append({"t": "break"})
            i += 1
            continue
        m = _FENCE.match(line)
        if m:
            flush()
            fence, lang, body = m.group(1), m.group(2), []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(fence[:3]):
                body.append(lines[i])
                i += 1
            out.append({"t": "code", "lang": lang, "text": "\n".join(body)})
            i += 1
            continue
        m = _HEADING.match(line)
        if m:
            flush()
            out.append({"t": "h", "level": len(m.group(1)), "runs": inline(m.group(2))})
            counters.clear()
            i += 1
            continue
        if re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", line):
            flush()
            out.append({"t": "hr"})
            i += 1
            continue
        if "|" in line and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            flush()
            head = _cells(line)
            align = [("center" if c.strip().startswith(":") and c.strip().endswith(":") else
                      "right" if c.strip().endswith(":") else "left") for c in _cells(lines[i + 1])]
            rows = []
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i]))
                i += 1
            n = len(head)
            rows = [(r + [""] * n)[:n] for r in rows]
            out.append({"t": "table", "head": [inline(c) for c in head], "rows": [[inline(c) for c in r] for r in rows],
                        "align": (align + ["left"] * n)[:n], "raw_head": head, "raw_rows": rows})
            continue
        if stripped.startswith(">"):
            flush()
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip()[1:].strip())
                i += 1
            out.append({"t": "quote", "runs": inline(" ".join(quote))})
            continue
        m = _LIST.match(line)
        if m:
            flush()
            indent, marker, check, body = m.groups()
            level = min(8, len(indent) // 2)
            ordered = marker[0].isdigit()
            # continuation lines belong to the item
            i += 1
            while i < len(lines) and lines[i].strip() and not _LIST.match(lines[i]) and lines[i].startswith(" " * (len(indent) + 2)):
                body += " " + lines[i].strip()
                i += 1
            prev = out[-1] if out else None
            if not (prev and prev["t"] == "li"):
                counters.clear()  # a new list starts its numbering again
            for lv in [k for k in counters if k > level]:
                del counters[lv]
            if ordered:
                counters[level] = counters.get(level, int(re.match(r"\d+", marker).group()) - 1) + 1
            prefix = "" if check is None else ("☒ " if check.lower() == "x" else "☐ ")
            new_list = not (prev and prev["t"] == "li")
            out.append({"t": "li", "level": level, "ordered": ordered, "num": counters.get(level, 0),
                        "runs": inline(prefix + body), "start": new_list})
            continue
        para.append(line)
        i += 1
    flush()
    return out


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells, cur, esc = [], [], False
    for ch in s:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
            cur.append(ch)
        elif ch == "|":
            cells.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    cells.append("".join(cur).strip())
    return [c.replace("\\|", "|") for c in cells]


_INLINE = re.compile(
    r"(?P<code>`+)(?P<codetext>.+?)(?P=code)"
    r"|\[(?P<ltext>[^\]]+)\]\((?P<url>[^)\s]+)(?:\s+\"[^\"]*\")?\)"
    r"|(?P<auto><https?://[^>\s]+>)"
    r"|(?P<b3>\*\*\*|___)(?P<b3text>.+?)(?P=b3)"
    r"|(?P<b2>\*\*|__)(?P<b2text>.+?)(?P=b2)"
    r"|(?P<st>~~)(?P<sttext>.+?)~~"
    r"|(?<![\w*])(?P<i1>\*|_)(?P<i1text>[^\s*_](?:.*?[^\s])?)(?P=i1)(?![\w*])", re.S)


def inline(text: str, style: dict | None = None) -> list[tuple[str, dict]]:
    """Inline Markdown to runs. Nested emphasis is supported one level deep (bold inside italic etc.)."""
    style = style or {}
    runs, pos = [], 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            runs.append((_unescape(text[pos:m.start()]), dict(style)))
        if m.group("code"):
            runs.append((m.group("codetext").strip(), {**style, "code": True}))
        elif m.group("ltext") is not None:
            runs += inline(m.group("ltext"), {**style, "link": m.group("url")})
        elif m.group("auto"):
            url = m.group("auto")[1:-1]
            runs.append((url, {**style, "link": url}))
        elif m.group("b3"):
            runs += inline(m.group("b3text"), {**style, "b": True, "i": True})
        elif m.group("b2"):
            runs += inline(m.group("b2text"), {**style, "b": True})
        elif m.group("st"):
            runs += inline(m.group("sttext"), {**style, "s": True})
        elif m.group("i1"):
            runs += inline(m.group("i1text"), {**style, "i": True})
        pos = m.end()
    if pos < len(text):
        runs.append((_unescape(text[pos:]), dict(style)))
    return [(t, s) for t, s in runs if t]


def _unescape(s: str) -> str:
    return re.sub(r"\\([\\`*_{}\[\]()#+\-.!|~>])", r"\1", s)


def plain(runs) -> str:
    return "".join(t for t, _ in runs)
