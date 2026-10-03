"""PDF documents from Markdown, laid out and written by hand. No packages.

Text uses a TrueType font found on the system (DejaVu, Liberation or Noto Sans), embedded as a subset
with Unicode mapping, so Greek letters, symbols and most scripts print and can be copied. Without such a
font the standard PDF fonts are used and characters outside Latin-1 become '?'.
Supports headings, paragraphs with bold/italic/code/strikethrough and links, nested lists, tables that
break across pages (header repeated), code blocks, quotes, rules, page breaks and page numbers.
"""

import os
import struct
import zlib

from . import md

PAGE_W, PAGE_H = 595.28, 841.89          # A4 in points
MARGIN = 56.7                            # 2 cm
BODY, LEADING = 10.5, 1.38
HEAD_SIZES = {1: 20, 2: 15.5, 3: 13, 4: 11.5, 5: 10.5, 6: 10.5}
FONT_DIRS = ["/usr/share/fonts", "/usr/local/share/fonts", os.path.expanduser("~/.local/share/fonts"),
             "/Library/Fonts", "/System/Library/Fonts/Supplemental", "C:\\Windows\\Fonts"]
FAMILIES = [  # (regular, bold, italic, bold italic, mono)
    ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans-Oblique.ttf", "DejaVuSans-BoldOblique.ttf", "DejaVuSansMono.ttf"),
    ("LiberationSans-Regular.ttf", "LiberationSans-Bold.ttf", "LiberationSans-Italic.ttf", "LiberationSans-BoldItalic.ttf",
     "LiberationMono-Regular.ttf"),
    ("NotoSans-Regular.ttf", "NotoSans-Bold.ttf", "NotoSans-Italic.ttf", "NotoSans-BoldItalic.ttf", "NotoSansMono-Regular.ttf"),
]
STYLES = ("regular", "bold", "italic", "bolditalic", "mono")


# TrueType fonts ----------------------------------------------------------------------------------
class TTFont:
    def __init__(self, path: str):
        with open(path, "rb") as f:
            self.data = data = f.read()
        self.path = path
        num = struct.unpack(">H", data[4:6])[0]
        self.tables = {}
        for i in range(num):
            tag, _, off, length = struct.unpack(">4sIII", data[12 + 16 * i:28 + 16 * i])
            self.tables[tag.decode("latin-1")] = (off, length)
        head = self.table("head")
        self.upem = struct.unpack(">H", head[18:20])[0]
        self.bbox = struct.unpack(">hhhh", head[36:44])
        self.long_loca = struct.unpack(">h", head[50:52])[0] == 1
        hhea = self.table("hhea")
        self.ascent, self.descent = struct.unpack(">hh", hhea[4:8])
        n_hm = struct.unpack(">H", hhea[34:36])[0]
        self.num_glyphs = struct.unpack(">H", self.table("maxp")[4:6])[0]
        hmtx = self.table("hmtx")
        adv = [struct.unpack(">H", hmtx[4 * i:4 * i + 2])[0] for i in range(n_hm)]
        self.advance = adv + [adv[-1]] * (self.num_glyphs - n_hm)
        os2 = self.table("OS/2") if "OS/2" in self.tables else b""
        self.cap_height = struct.unpack(">h", os2[88:90])[0] if len(os2) >= 90 else int(self.ascent * 0.7)
        post = self.table("post") if "post" in self.tables else b"\0" * 8
        self.italic_angle = struct.unpack(">i", post[4:8])[0] / 65536.0
        self.cmap = self._cmap()
        self.used: dict[int, str] = {}
        self.name = os.path.splitext(os.path.basename(path))[0].replace(" ", "")

    def table(self, tag: str) -> bytes:
        off, length = self.tables[tag]
        return self.data[off:off + length]

    def _cmap(self) -> dict[int, int]:
        cmap = self.table("cmap")
        n = struct.unpack(">H", cmap[2:4])[0]
        subs = {}
        for i in range(n):
            pid, eid, off = struct.unpack(">HHI", cmap[4 + 8 * i:12 + 8 * i])
            subs[(pid, eid)] = off
        out: dict[int, int] = {}
        for key in ((3, 10), (0, 4), (3, 1), (0, 3)):
            if key not in subs:
                continue
            off = subs[key]
            fmt = struct.unpack(">H", cmap[off:off + 2])[0]
            if fmt == 12:
                groups = struct.unpack(">I", cmap[off + 12:off + 16])[0]
                for g in range(groups):
                    start, end, gid = struct.unpack(">III", cmap[off + 16 + 12 * g:off + 28 + 12 * g])
                    for c in range(start, min(end, start + 70000) + 1):
                        out.setdefault(c, gid + c - start)
                return out
            if fmt == 4:
                seg2 = struct.unpack(">H", cmap[off + 6:off + 8])[0]
                seg = seg2 // 2
                ends = struct.unpack(f">{seg}H", cmap[off + 14:off + 14 + seg2])
                starts = struct.unpack(f">{seg}H", cmap[off + 16 + seg2:off + 16 + 2 * seg2])
                deltas = struct.unpack(f">{seg}h", cmap[off + 16 + 2 * seg2:off + 16 + 3 * seg2])
                ro_pos = off + 16 + 3 * seg2
                ranges = struct.unpack(f">{seg}H", cmap[ro_pos:ro_pos + seg2])
                for s in range(seg):
                    for c in range(starts[s], ends[s] + 1):
                        if c == 0xFFFF:
                            continue
                        if ranges[s] == 0:
                            gid = (c + deltas[s]) & 0xFFFF
                        else:
                            p = ro_pos + 2 * s + ranges[s] + 2 * (c - starts[s])
                            gid = struct.unpack(">H", cmap[p:p + 2])[0] if p + 2 <= len(cmap) else 0
                            if gid:
                                gid = (gid + deltas[s]) & 0xFFFF
                        if gid:
                            out.setdefault(c, gid)
                if out:
                    return out
        return out

    def has(self, ch: str) -> bool:
        return ord(ch) in self.cmap

    def width(self, text: str, size: float) -> float:
        return sum(self.advance[self.cmap.get(ord(c), 0)] for c in text) * size / self.upem

    def encode(self, text: str) -> str:
        out = []
        for c in text:
            gid = self.cmap.get(ord(c), 0)
            if gid:
                self.used.setdefault(gid, c)
            out.append(f"{gid:04X}")
        return "".join(out)

    def _glyph_range(self, loca: bytes, gid: int) -> tuple[int, int]:
        if self.long_loca:
            a, b = struct.unpack(">II", loca[4 * gid:4 * gid + 8])
        else:
            a, b = (x * 2 for x in struct.unpack(">HH", loca[2 * gid:2 * gid + 4]))
        return a, b

    def subset(self) -> bytes:
        """The font with every glyph not used emptied (glyph ids unchanged), as a TrueType file."""
        loca, glyf = self.table("loca"), self.table("glyf")
        keep = {0} | set(self.used)
        todo = list(keep)
        while todo:  # composite glyphs pull in their parts
            gid = todo.pop()
            a, b = self._glyph_range(loca, gid)
            g = glyf[a:b]
            if len(g) < 10 or struct.unpack(">h", g[:2])[0] >= 0:
                continue
            p = 10
            while p + 4 <= len(g):
                flags, comp = struct.unpack(">HH", g[p:p + 4])
                if comp not in keep:
                    keep.add(comp)
                    todo.append(comp)
                p += 4 + (4 if flags & 1 else 2)
                p += 8 if flags & 0x80 else 4 if flags & 0x40 else 2 if flags & 0x08 else 0
                if not flags & 0x20:
                    break
        new_glyf, offsets = bytearray(), []
        for gid in range(self.num_glyphs):
            offsets.append(len(new_glyf))
            if gid in keep:
                a, b = self._glyph_range(loca, gid)
                new_glyf += glyf[a:b]
                new_glyf += b"\0" * (-len(new_glyf) % 4)
        offsets.append(len(new_glyf))
        new_loca = struct.pack(f">{len(offsets)}I", *offsets)
        head = bytearray(self.table("head"))
        head[8:12] = b"\0\0\0\0"
        head[50:52] = struct.pack(">h", 1)
        tables = {"head": bytes(head), "loca": new_loca, "glyf": bytes(new_glyf)}
        for tag in ("hhea", "hmtx", "maxp", "cvt ", "fpgm", "prep", "OS/2", "cmap", "post", "name"):
            if tag in self.tables:
                tables[tag] = self.table(tag)
        return _sfnt(tables)


def _checksum(data: bytes) -> int:
    data += b"\0" * (-len(data) % 4)
    return sum(struct.unpack(f">{len(data) // 4}I", data)) & 0xFFFFFFFF


def _sfnt(tables: dict[str, bytes]) -> bytes:
    tags = sorted(tables)
    n = len(tags)
    es = max(i for i in range(16) if 2 ** i <= n)
    header = struct.pack(">IHHHH", 0x00010000, n, 16 * 2 ** es, es, 16 * n - 16 * 2 ** es)
    offset = 12 + 16 * n
    records, body = b"", b""
    for tag in tags:
        data = tables[tag]
        records += struct.pack(">4sIII", tag.encode("latin-1"), _checksum(data), offset + len(body), len(data))
        body += data + b"\0" * (-len(data) % 4)
    font = bytearray(header + records + body)
    head_off = offset + sum(len(tables[t]) + (-len(tables[t]) % 4) for t in tags[:tags.index("head")])
    font[head_off + 8:head_off + 12] = struct.pack(">I", (0xB1B0AFBA - _checksum(bytes(font))) & 0xFFFFFFFF)
    return bytes(font)


def find_family() -> dict[str, str] | None:
    found: dict[str, str] = {}
    wanted = {name for fam in FAMILIES for name in fam}
    for base in FONT_DIRS:
        if not os.path.isdir(base):
            continue
        for root, _, files in os.walk(base):
            for f in files:
                if f in wanted and f not in found:
                    found[f] = os.path.join(root, f)
    for fam in FAMILIES:
        if fam[0] in found:
            return {style: found.get(name) or found[fam[0]] for style, name in zip(STYLES, fam)}
    return None


# Standard fonts (fallback) --------------------------------------------------------------------------
_HELV = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278] + [556] * 10 + [
    278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667,
    778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500, 556, 556, 278, 556,
    556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]
_HELV_B = [278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278] + [556] * 10 + [
    333, 333, 584, 584, 584, 611, 975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778, 667,
    778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556, 333, 556, 611, 556, 611, 556, 333, 611,
    611, 278, 278, 556, 278, 889, 611, 611, 611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584]


class StdFont:
    NAMES = {"regular": "Helvetica", "bold": "Helvetica-Bold", "italic": "Helvetica-Oblique",
             "bolditalic": "Helvetica-BoldOblique", "mono": "Courier"}

    def __init__(self, style: str):
        self.style = style
        self.name = self.NAMES[style]
        self.table = None if style == "mono" else (_HELV_B if "bold" in style else _HELV)

    def has(self, ch: str) -> bool:
        return True

    def _char(self, c: str) -> int:
        try:
            b = c.encode("cp1252")
            return b[0] if len(b) == 1 else 63
        except UnicodeEncodeError:
            return 63

    def width(self, text: str, size: float) -> float:
        if self.table is None:
            return 600 * len(text) * size / 1000
        return sum(self.table[b - 32] if 32 <= b < 127 else 556 for b in map(self._char, text)) * size / 1000

    def encode(self, text: str) -> str:
        return "".join(f"{self._char(c):02X}" for c in text)


# Layout -----------------------------------------------------------------------------------------------
class _Layout:
    def __init__(self, fonts: dict):
        self.fonts = fonts
        self.pages: list[dict] = []
        self.new_page()

    def new_page(self):
        self.page = {"ops": [], "links": []}
        self.pages.append(self.page)
        self.y = PAGE_H - MARGIN

    def ensure(self, h: float):
        if self.y - h < MARGIN + 14:
            self.new_page()

    def font_for(self, st: dict) -> str:
        if st.get("code"):
            return "mono"
        return ("bold" if st.get("b") else "") + ("italic" if st.get("i") else "") or "regular"

    def wrap(self, runs, width: float, size: float, base: dict | None = None) -> list[list[tuple]]:
        """Runs -> lines of (text, style, font key, size, width)."""
        import re
        pieces = []
        for text, st in runs:
            st = {**(base or {}), **st}
            key = self.font_for(st)
            sz = size * 0.9 if key == "mono" else size
            for part in re.split(r"(\s+)", text.replace("\n", " ")):
                if part:
                    pieces.append((part, st, key, sz))
        lines, line, x = [], [], 0.0
        for part, st, key, sz in pieces:
            w = self.fonts[key].width(part, sz)
            if part.isspace():
                if line:
                    line.append((" ", st, key, sz, self.fonts[key].width(" ", sz)))
                    x += line[-1][4]
                continue
            if x + w > width and line:
                while line and line[-1][0] == " ":
                    x -= line.pop()[4]
                lines.append(line)
                line, x = [], 0.0
            while w > width:  # a word longer than the line: cut it
                cut = max(1, int(len(part) * width / w) - 1)
                lines.append([(part[:cut], st, key, sz, self.fonts[key].width(part[:cut], sz))])
                part = part[cut:]
                w = self.fonts[key].width(part, sz)
            line.append((part, st, key, sz, w))
            x += w
        while line and line[-1][0] == " ":
            line.pop()
        if line:
            lines.append(line)
        return lines or [[]]

    def draw_line(self, line, x: float, y: float, color=(0, 0, 0), align: str = "left", width: float = 0):
        total = sum(p[4] for p in line)
        if align == "right" and width:
            x += width - total
        elif align == "center" and width:
            x += (width - total) / 2
        for text, st, key, sz, w in line:
            col = (0.02, 0.39, 0.76) if st.get("link") else color
            if text != " ":
                if st.get("code"):
                    self.page["ops"].append(("rect", x - 1, y - sz * 0.25, w + 2, sz * 1.1, (0.94, 0.94, 0.94)))
                self.page["ops"].append(("text", key, sz, x, y, text, col))
                if st.get("s"):
                    self.page["ops"].append(("line", x, y + sz * 0.3, x + w, y + sz * 0.3, 0.6, col))
            if st.get("link"):
                self.page["ops"].append(("line", x, y - sz * 0.15, x + w, y - sz * 0.15, 0.5, col))
                self.page["links"].append((x, y - sz * 0.25, x + w, y + sz * 0.85, st["link"]))
            x += w

    def paragraph(self, runs, size=BODY, indent=0.0, color=(0, 0, 0), before=0.0, after=6.0, base=None, bar=False,
                  keep=0):
        width = PAGE_W - 2 * MARGIN - indent
        lines = self.wrap(runs, width, size, base)
        lh = size * LEADING
        self.y -= before
        self.ensure(lh * min(len(lines), 2 + keep))
        for line in lines:
            self.ensure(lh)
            self.y -= lh
            if bar:
                self.page["ops"].append(("line", MARGIN + indent - 9, self.y - 3, MARGIN + indent - 9, self.y + lh - 3, 2.2, (0.75, 0.75, 0.75)))
            self.draw_line(line, MARGIN + indent, self.y + size * 0.25, color)
        self.y -= after

    def list_item(self, b):
        size, lh = BODY, BODY * LEADING
        indent = 16 + 16 * b["level"]
        marker = f"{b['num']}." if b["ordered"] else "•◦▪"[b["level"] % 3]
        width = PAGE_W - 2 * MARGIN - indent
        lines = self.wrap(b["runs"], width, size)
        self.ensure(lh)
        for i, line in enumerate(lines):
            self.ensure(lh)
            self.y -= lh
            if i == 0:
                mk = self.fonts["regular"]
                mw = mk.width(marker, size)
                self.page["ops"].append(("text", "regular", size, MARGIN + indent - 5 - mw, self.y + size * 0.25, marker, (0.3, 0.3, 0.3)))
            self.draw_line(line, MARGIN + indent, self.y + size * 0.25)
        self.y -= 2.5

    def code(self, text: str):
        size = 8.6
        lh = size * 1.35
        width = PAGE_W - 2 * MARGIN - 12
        mono = self.fonts["mono"]
        cw = max(0.1, mono.width("M", size))
        per = max(10, int(width / cw))
        rows = []
        for line in text.split("\n"):
            while len(line) > per:
                rows.append(line[:per])
                line = line[per:]
            rows.append(line)
        self.y -= 2
        for row in rows:
            self.ensure(lh)
            self.page["ops"].append(("rect", MARGIN, self.y - lh, PAGE_W - 2 * MARGIN, lh, (0.95, 0.95, 0.95)))
            self.y -= lh
            if row:
                self.page["ops"].append(("text", "mono", size, MARGIN + 6, self.y + size * 0.32, row, (0.1, 0.1, 0.1)))
        self.y -= 8

    def table(self, b):
        n = max(1, len(b["head"]))
        size, pad = 9.2, 4.0
        lh = size * 1.3
        avail = PAGE_W - 2 * MARGIN
        natural = [0.0] * n
        for row in [b["head"]] + b["rows"]:
            for j, cell in enumerate(row[:n]):
                natural[j] = max(natural[j], sum(self.fonts[self.font_for(st)].width(t, size) for t, st in cell) + 2 * pad)
        minimum = [min(nat, 60.0) for nat in natural]
        total = sum(natural)
        if total <= avail:
            widths = natural[:]
            widths[-1] += 0 if total > avail * 0.6 else 0
        else:
            extra = avail - sum(minimum)
            flex = [max(0.0, nat - mn) for nat, mn in zip(natural, minimum)]
            fsum = sum(flex) or 1.0
            widths = [mn + max(0.0, extra) * f / fsum for mn, f in zip(minimum, flex)]

        def layout_row(cells, header):
            base = {"b": True} if header else None
            wrapped = [self.wrap(c or [("", {})], max(10.0, widths[j] - 2 * pad), size, base) for j, c in enumerate(cells[:n])]
            return wrapped, max(len(w) for w in wrapped) * lh + 2 * pad

        def draw_row(wrapped, h, header):
            x = MARGIN
            top = self.y
            for j, lines in enumerate(wrapped):
                if header:
                    self.page["ops"].append(("rect", x, top - h, widths[j], h, (0.91, 0.91, 0.91)))
                self.page["ops"].append(("box", x, top - h, widths[j], h, 0.5, (0.75, 0.75, 0.75)))
                y = top - pad
                for line in lines:
                    y -= lh
                    self.draw_line(line, x + pad, y + size * 0.28, align=b["align"][j] if j < len(b["align"]) else "left",
                                   width=widths[j] - 2 * pad)
                x += widths[j]
            self.y -= h

        head, hh = layout_row(b["head"], True)
        self.y -= 4
        self.ensure(hh + lh * 2 + 2 * pad)
        draw_row(head, hh, True)
        for r in b["rows"]:
            wrapped, h = layout_row(r, False)
            if self.y - h < MARGIN + 14:
                self.new_page()
                draw_row(head, hh, True)
            draw_row(wrapped, h, False)
        self.y -= 10

    def run(self, blocks, title: str | None):
        if title:
            self.paragraph([(title, {"b": True})], size=24, color=(0.12, 0.22, 0.39), after=12)
        pending_break = False
        for b in blocks:
            t = b["t"]
            if t == "break":
                pending_break = True
                continue
            if pending_break:
                if self.page["ops"]:
                    self.new_page()
                pending_break = False
            if t == "h":
                size = HEAD_SIZES[b["level"]]
                self.paragraph(b["runs"], size=size, color=(0.12, 0.22, 0.39), before=size * 0.7, after=size * 0.35,
                               base={"b": True}, keep=2)
            elif t == "p":
                self.paragraph(b["runs"])
            elif t == "li":
                if b.get("start"):
                    self.y -= 2
                self.list_item(b)
            elif t == "table":
                self.table(b)
            elif t == "code":
                self.code(b["text"])
            elif t == "quote":
                self.paragraph(b["runs"], indent=14, color=(0.35, 0.35, 0.35), base={"i": True}, bar=True, before=2, after=8)
            elif t == "hr":
                self.ensure(12)
                self.y -= 6
                self.page["ops"].append(("line", MARGIN, self.y, PAGE_W - MARGIN, self.y, 0.7, (0.75, 0.75, 0.75)))
                self.y -= 8


# Output ------------------------------------------------------------------------------------------------
def _pdf_str(s: str) -> str:
    try:
        s.encode("latin-1")
        return "(" + s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ")"
    except UnicodeEncodeError:
        return "<FEFF" + s.encode("utf-16-be").hex().upper() + ">"


def write(markdown: str, title: str | None = None) -> bytes:
    blocks = md.parse(markdown)
    family = find_family()
    if family:
        cache: dict[str, TTFont] = {}
        fonts = {}
        for style in STYLES:
            path = family[style]
            fonts[style] = cache.setdefault(path, TTFont(path))
    else:
        fonts = {style: StdFont(style) for style in STYLES}
    lay = _Layout(fonts)
    lay.run(blocks, title)
    doc_title = title or next((md.plain(b["runs"]) for b in blocks if b["t"] == "h"), "")
    total = len(lay.pages)

    objects: list[bytes] = [b""]  # 1-based; filled in order

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects) - 1

    def stream(data: bytes, extra: str = "") -> bytes:
        comp = zlib.compress(data, 6)
        return f"<< /Length {len(comp)} /Filter /FlateDecode{extra} >>\nstream\n".encode() + comp + b"\nendstream"

    # page contents first (so the fonts know which glyphs are used)
    font_ids = {id(f): f"F{i}" for i, f in enumerate(dict.fromkeys(fonts.values()), 1)}
    page_streams = []
    for num, page in enumerate(lay.pages, 1):
        out = []
        for op in page["ops"]:
            if op[0] == "rect":
                _, x, y, w, h, (r, g, b) = op
                out.append(f"{r:.3f} {g:.3f} {b:.3f} rg {x:.2f} {y:.2f} {w:.2f} {h:.2f} re f")
            elif op[0] == "box":
                _, x, y, w, h, lw, (r, g, b) = op
                out.append(f"{r:.3f} {g:.3f} {b:.3f} RG {lw:.2f} w {x:.2f} {y:.2f} {w:.2f} {h:.2f} re S")
            elif op[0] == "line":
                _, x1, y1, x2, y2, lw, (r, g, b) = op
                out.append(f"{r:.3f} {g:.3f} {b:.3f} RG {lw:.2f} w {x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S")
            elif op[0] == "text":
                _, key, sz, x, y, text, (r, g, b) = op
                f = fonts[key]
                out.append(f"BT {r:.3f} {g:.3f} {b:.3f} rg /{font_ids[id(f)]} {sz:.2f} Tf {x:.2f} {y:.2f} Td <{f.encode(text)}> Tj ET")
        label = f"{num} / {total}"
        f = fonts["regular"]
        out.append(f"BT 0.5 0.5 0.5 rg /{font_ids[id(f)]} 8.5 Tf {(PAGE_W - f.width(label, 8.5)) / 2:.2f} {MARGIN / 2:.2f} Td "
                   f"<{f.encode(label)}> Tj ET")
        page_streams.append("\n".join(out).encode("latin-1"))

    catalog = add(b"")
    pages_id = add(b"")
    font_objs = {}
    for f in dict.fromkeys(fonts.values()):
        if isinstance(f, StdFont):
            font_objs[font_ids[id(f)]] = add(f"<< /Type /Font /Subtype /Type1 /BaseFont /{f.name} /Encoding /WinAnsiEncoding >>".encode())
            continue
        tag = "".join(chr(65 + (hash((f.name, i)) % 26)) for i in range(6))
        base = f"{tag}+{f.name}"
        data = f.subset()
        ff = add(stream(data, f" /Length1 {len(data)}"))
        scale = 1000 / f.upem
        desc = add((f"<< /Type /FontDescriptor /FontName /{base} /Flags {32 + (64 if f.italic_angle else 0)} "
                    f"/FontBBox [{' '.join(str(int(v * scale)) for v in f.bbox)}] /ItalicAngle {f.italic_angle:.1f} "
                    f"/Ascent {int(f.ascent * scale)} /Descent {int(f.descent * scale)} /CapHeight {int(f.cap_height * scale)} "
                    f"/StemV 80 /FontFile2 {ff} 0 R >>").encode())
        gids = sorted(f.used)
        widths = " ".join(f"{g} [{int(f.advance[g] * scale)}]" for g in gids)
        cid = add((f"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /{base} /CIDSystemInfo << /Registry (Adobe) "
                   f"/Ordering (Identity) /Supplement 0 >> /FontDescriptor {desc} 0 R /W [{widths}] /CIDToGIDMap /Identity >>").encode())
        cmap_lines = []
        for i in range(0, len(gids), 100):
            chunk = gids[i:i + 100]
            cmap_lines.append(f"{len(chunk)} beginbfchar")
            cmap_lines += [f"<{g:04X}> <{f.used[g].encode('utf-16-be').hex().upper()}>" for g in chunk]
            cmap_lines.append("endbfchar")
        tounicode = ("/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n/CIDSystemInfo << /Registry (Adobe) "
                     "/Ordering (UCS) /Supplement 0 >> def\n/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
                     "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n" + "\n".join(cmap_lines) +
                     "\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend")
        tu = add(stream(tounicode.encode()))
        font_objs[font_ids[id(f)]] = add((f"<< /Type /Font /Subtype /Type0 /BaseFont /{base} /Encoding /Identity-H "
                                          f"/DescendantFonts [{cid} 0 R] /ToUnicode {tu} 0 R >>").encode())
    fonts_dict = " ".join(f"/{k} {v} 0 R" for k, v in font_objs.items())
    page_ids = []
    for page, content in zip(lay.pages, page_streams):
        cs = add(stream(content))
        annots = []
        for x1, y1, x2, y2, url in page["links"]:
            annots.append(add((f"<< /Type /Annot /Subtype /Link /Rect [{x1:.2f} {y1:.2f} {x2:.2f} {y2:.2f}] /Border [0 0 0] "
                               f"/A << /S /URI /URI {_pdf_str(url)} >> >>").encode()))
        ann = f" /Annots [{' '.join(f'{a} 0 R' for a in annots)}]" if annots else ""
        page_ids.append(add((f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                             f"/Resources << /Font << {fonts_dict} >> >> /Contents {cs} 0 R{ann} >>").encode()))
    objects[pages_id] = f"<< /Type /Pages /Kids [{' '.join(f'{p} 0 R' for p in page_ids)}] /Count {len(page_ids)} >>".encode()
    objects[catalog] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode()
    info = add(f"<< /Title {_pdf_str(doc_title)} /Producer (baabaa) >>".encode())

    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for i in range(1, len(objects)):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + objects[i] + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects)}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets[1:]).encode()
    out += f"trailer\n<< /Size {len(objects)} /Root {catalog} 0 R /Info {info} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
