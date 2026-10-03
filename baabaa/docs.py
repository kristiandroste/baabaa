"""Text extraction from uploaded documents, standard library only: PDF, DOCX, XLSX, PPTX, ODT and text.

The PDF reader handles the common cases: Flate-compressed content streams, literal and hex strings,
TJ arrays, and ToUnicode character maps (so most modern PDFs, including CID fonts, extract cleanly).
Scanned PDFs have no text layer; for those the extraction is empty and the caller says so.
"""

import re
import zipfile
import zlib
from xml.etree import ElementTree as ET

TEXT_EXT = {".txt", ".md", ".markdown", ".rst", ".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".json", ".jsonl", ".csv",
            ".tsv", ".html", ".htm", ".css", ".scss", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".sh",
            ".bash", ".zsh", ".c", ".h", ".cpp", ".hpp", ".cc", ".java", ".kt", ".go", ".rs", ".rb", ".php", ".swift",
            ".sql", ".r", ".m", ".tex", ".bib", ".log", ".env", ".gitignore", ".dockerfile", ".lua", ".pl", ".scala",
            ".cs", ".vue", ".svelte", ".ipynb", ".srt", ".vtt", ".org", ".adoc"}
IMAGE_MIME = {"image/png", "image/jpeg", "image/gif", "image/webp"}
MAX_TEXT = 400_000


def classify(name: str, mime: str, data: bytes) -> str:
    lower = name.lower()
    ext = lower[lower.rfind("."):] if "." in lower else ""
    if mime in IMAGE_MIME or ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        return "image"
    if ext in (".pdf", ".docx", ".xlsx", ".pptx", ".odt") or mime == "application/pdf":
        return "document"
    if ext in TEXT_EXT or mime.startswith("text/") or mime in ("application/json", "application/xml"):
        return "text"
    if b"\x00" not in data[:4096]:
        try:
            data[:4096].decode("utf-8")
            return "text"
        except UnicodeDecodeError:
            pass
    return "binary"


def extract(name: str, data: bytes) -> str | None:
    lower = name.lower()
    try:
        if lower.endswith(".pdf") or data[:5] == b"%PDF-":
            text = pdf_text(data)
        elif lower.endswith(".docx"):
            text = docx_text(data)
        elif lower.endswith(".xlsx"):
            text = xlsx_text(data)
        elif lower.endswith(".pptx"):
            text = pptx_text(data)
        elif lower.endswith(".odt"):
            text = odt_text(data)
        elif lower.endswith(".ipynb"):
            text = ipynb_text(data)
        else:
            text = data.decode("utf-8", "replace")
    except Exception:  # a broken document must not break the upload
        return None
    if text is None:
        return None
    text = text.replace("\r\n", "\n").strip()
    return text[:MAX_TEXT] if text else None


# Office Open XML ----------------------------------------------------------------------------------
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def docx_text(data: bytes) -> str:
    import io
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    out = []
    for p in root.iter(f"{W}p"):
        parts = []
        for el in p.iter():
            if el.tag == f"{W}t" and el.text:
                parts.append(el.text)
            elif el.tag == f"{W}tab":
                parts.append("\t")
            elif el.tag in (f"{W}br", f"{W}cr"):
                parts.append("\n")
        out.append("".join(parts))
    return "\n".join(out)


def xlsx_text(data: bytes) -> str:
    import io
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{S}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{S}t")))
        names = {}
        try:
            wb = ET.fromstring(z.read("xl/workbook.xml"))
            for i, sh in enumerate(wb.iter(f"{S}sheet"), start=1):
                names[f"xl/worksheets/sheet{i}.xml"] = sh.get("name")
        except KeyError:
            pass
        out = []
        for path in sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n)):
            out.append(f"## Sheet: {names.get(path, path)}")
            for row in ET.fromstring(z.read(path)).iter(f"{S}row"):
                cells = []
                for c in row.iter(f"{S}c"):
                    v = c.find(f"{S}v")
                    t = c.get("t")
                    if t == "s" and v is not None:
                        cells.append(shared[int(v.text)] if v.text and int(v.text) < len(shared) else "")
                    elif t == "inlineStr":
                        cells.append("".join(x.text or "" for x in c.iter(f"{S}t")))
                    else:
                        cells.append(v.text if v is not None and v.text else "")
                out.append(",".join(_csv(x) for x in cells))
        return "\n".join(out)


def _csv(s: str) -> str:
    return f'"{s.replace(chr(34), chr(34) * 2)}"' if ("," in s or '"' in s or "\n" in s) else s


def pptx_text(data: bytes) -> str:
    import io
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        slides = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                        key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)))
        out = []
        for i, path in enumerate(slides, start=1):
            root = ET.fromstring(z.read(path))
            paras = []
            for p in root.iter(f"{A}p"):
                t = "".join(r.text or "" for r in p.iter(f"{A}t"))
                if t.strip():
                    paras.append(t)
            out.append(f"## Slide {i}\n" + "\n".join(paras))
        return "\n\n".join(out)


def odt_text(data: bytes) -> str:
    import io
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = ET.fromstring(z.read("content.xml"))
    textns = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
    out = []
    for el in root.iter():
        if el.tag in (f"{textns}p", f"{textns}h"):
            out.append("".join(el.itertext()))
    return "\n".join(out)


def ipynb_text(data: bytes) -> str:
    import json
    nb = json.loads(data)
    out = []
    for cell in nb.get("cells", []):
        src = "".join(cell.get("source", []))
        out.append(f"```python\n{src}\n```" if cell.get("cell_type") == "code" else src)
    return "\n\n".join(out)


# PDF -----------------------------------------------------------------------------------------------
_OBJ = re.compile(rb"(\d+)\s+(\d+)\s+obj\b(.*?)\bendobj", re.S)
_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\n?endstream", re.S)


def _pdf_objects(data: bytes) -> dict[int, bytes]:
    objs = {}
    for m in _OBJ.finditer(data):
        objs[int(m.group(1))] = m.group(3)
    # objects inside compressed object streams (/Type /ObjStm)
    for num, body in list(objs.items()):
        if b"/ObjStm" not in body[:400]:
            continue
        raw = _stream(body)
        if raw is None:
            continue
        first = re.search(rb"/First\s+(\d+)", body)
        n = re.search(rb"/N\s+(\d+)", body)
        if not first or not n:
            continue
        header = raw[: int(first.group(1))].split()
        offsets = [(int(header[i]), int(header[i + 1])) for i in range(0, min(len(header) - 1, 2 * int(n.group(1))), 2)]
        for i, (onum, off) in enumerate(offsets):
            start = int(first.group(1)) + off
            end = int(first.group(1)) + offsets[i + 1][1] if i + 1 < len(offsets) else len(raw)
            objs.setdefault(onum, raw[start:end])
    return objs


def _stream(body: bytes) -> bytes | None:
    m = _STREAM.search(body)
    if not m:
        return None
    raw = m.group(1)
    head = body[: m.start()]
    if b"/FlateDecode" in head:
        try:
            return zlib.decompress(raw)
        except zlib.error:
            try:
                return zlib.decompressobj().decompress(raw)
            except zlib.error:
                return None
    if b"/Filter" in head:
        return None  # other filters (DCT images, LZW, ...) carry no text we can read
    return raw


def _cmap(data: bytes) -> dict[int, str]:
    m = {}
    for block in re.findall(rb"beginbfchar(.*?)endbfchar", data, re.S):
        for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block):
            m[int(src, 16)] = _utf16(dst)
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", data, re.S):
        for lo, hi, rest in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\[[^\]]*\]|<[0-9A-Fa-f]*>)", block):
            lo_i, hi_i = int(lo, 16), int(hi, 16)
            if rest.startswith(b"["):
                for i, dst in enumerate(re.findall(rb"<([0-9A-Fa-f]*)>", rest)):
                    m[lo_i + i] = _utf16(dst)
            else:
                base = int(rest[1:-1] or b"0", 16)
                width = len(rest[1:-1]) // 2 or 2
                for i in range(min(hi_i - lo_i + 1, 65536)):
                    m[lo_i + i] = _utf16((base + i).to_bytes(width, "big").hex().encode())
    return m


def _utf16(hexbytes: bytes) -> str:
    try:
        return bytes.fromhex(hexbytes.decode()).decode("utf-16-be", "replace")
    except ValueError:
        return ""


def _literal(s: bytes) -> bytes:
    out, i = bytearray(), 0
    esc = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12, ord("("): 40, ord(")"): 41, ord("\\"): 92}
    while i < len(s):
        c = s[i]
        if c == 92 and i + 1 < len(s):
            n = s[i + 1]
            if n in esc:
                out.append(esc[n])
                i += 2
            elif 48 <= n <= 55:
                j = i + 1
                while j < len(s) and j < i + 4 and 48 <= s[j] <= 55:
                    j += 1
                out.append(int(s[i + 1:j], 8) & 255)
                i = j
            elif n in (10, 13):
                i += 2
            else:
                out.append(n)
                i += 2
        else:
            out.append(c)
            i += 1
    return bytes(out)


_TOKENS = re.compile(rb"\((?:\\.|[^\\()]|\((?:\\.|[^\\()])*\))*\)|<[0-9A-Fa-f\s]*>|\[|\]|/[^\s/<>\[\]()]+|[^\s/<>\[\]()]+")


def _decode_str(tok: bytes, cmap: dict | None) -> str:
    if tok.startswith(b"("):
        raw = _literal(tok[1:-1])
    else:
        hexs = re.sub(rb"\s", b"", tok[1:-1])
        if len(hexs) % 2:
            hexs += b"0"
        raw = bytes.fromhex(hexs.decode())
    if cmap:
        width = 2 if max(cmap) > 255 else 1
        chars = []
        for i in range(0, len(raw) - width + 1, width):
            code = int.from_bytes(raw[i:i + width], "big")
            chars.append(cmap.get(code, ""))
        return "".join(chars)
    return raw.decode("latin-1")


def _content_text(stream: bytes, fonts: dict) -> str:
    out, stack, cmap, in_text = [], [], None, False
    for tok in _TOKENS.findall(stream):
        if tok == b"BT":
            in_text = True
            continue
        if tok == b"ET":
            in_text = False
            out.append("\n")
            continue
        if tok in (b"Tj", b"'", b'"'):
            if stack and isinstance(stack[-1], bytes) and stack[-1][:1] in (b"(", b"<"):
                if tok != b"Tj":
                    out.append("\n")
                out.append(_decode_str(stack[-1], cmap))
            stack.clear()
        elif tok == b"TJ":
            if "[" in stack:
                arr = stack[stack.index("[") + 1:]
                for x in arr:
                    if isinstance(x, bytes) and x[:1] in (b"(", b"<"):
                        out.append(_decode_str(x, cmap))
                    elif isinstance(x, bytes):
                        try:
                            if float(x) < -200:
                                out.append(" ")
                        except ValueError:
                            pass
            stack.clear()
        elif tok == b"Tf":
            name = next((x for x in reversed(stack) if isinstance(x, bytes) and x.startswith(b"/")), None)
            cmap = fonts.get(name[1:].decode("latin-1")) if name else None
            stack.clear()
        elif tok in (b"Td", b"TD", b"T*"):
            if tok == b"T*" or (len(stack) >= 2 and _num(stack[-1]) not in (None, 0.0)):
                out.append("\n")
            elif in_text:
                out.append(" ")
            stack.clear()
        elif tok == b"[":
            stack.append("[")
        elif tok == b"]":
            pass
        elif tok[:1] in (b"(", b"<", b"/") or _num(tok) is not None:
            stack.append(tok)
        else:
            stack.clear()
    text = "".join(out)
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", text))


def _num(tok):
    try:
        return float(tok)
    except (TypeError, ValueError):
        return None


def pdf_text(data: bytes) -> str:
    objs = _pdf_objects(data)
    cmaps = {}
    for num, body in objs.items():
        if b"beginbfchar" in body or b"beginbfrange" in body:
            cmaps[num] = _cmap(body)
        elif b"/ToUnicode" not in body and b"stream" in body[:4000] and len(body) < 400_000:
            s = _stream(body)
            if s and (b"beginbfchar" in s or b"beginbfrange" in s):
                cmaps[num] = _cmap(s)
    # font objects -> their ToUnicode map
    font_maps = {}
    for num, body in objs.items():
        m = re.search(rb"/ToUnicode\s+(\d+)\s+0\s+R", body)
        if m and int(m.group(1)) in cmaps:
            font_maps[num] = cmaps[int(m.group(1))]
    pages = []
    page_objs = [(n, b) for n, b in sorted(objs.items()) if re.search(rb"/Type\s*/Page\b", b)]
    for num, body in page_objs:
        fonts = {}
        res = body
        rm = re.search(rb"/Resources\s+(\d+)\s+0\s+R", body)
        if rm and int(rm.group(1)) in objs:
            res = objs[int(rm.group(1))]
        fm = re.search(rb"/Font\s*(<<.*?>>|\d+\s+0\s+R)", res, re.S)
        if fm:
            fdict = fm.group(1)
            if not fdict.startswith(b"<<"):
                fdict = objs.get(int(fdict.split()[0]), b"")
            for fname, fnum in re.findall(rb"/([^\s/<>]+)\s+(\d+)\s+0\s+R", fdict):
                if int(fnum) in font_maps:
                    fonts[fname.decode("latin-1")] = font_maps[int(fnum)]
        contents = []
        cm = re.search(rb"/Contents\s*(\[[^\]]*\]|\d+\s+0\s+R)", body)
        if cm:
            for ref in re.findall(rb"(\d+)\s+0\s+R", cm.group(1)):
                s = _stream(objs.get(int(ref), b""))
                if s:
                    contents.append(s)
        text = "".join(_content_text(s, fonts) for s in contents)
        pages.append(text.strip())
    if not any(pages):
        return ""
    return "\n\n".join(f"--- page {i} ---\n{t}" for i, t in enumerate(pages, 1) if t)
