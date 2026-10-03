"""PowerPoint presentations (.pptx) from Markdown, written as PresentationML with zipfile. No packages.

Each `#` or `##` heading starts a slide (its text is the slide title); a page break or `---` starts a
new slide too. A first `#` heading followed by a paragraph and then another heading becomes a title
slide. Lists become bullets (nested), paragraphs plain text, code monospace, tables real tables. Text
that would overflow is shrunk to fit.
"""

import io
import zipfile
from xml.sax.saxutils import escape

from . import md
from .docx import app_xml, core_xml

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
NS = f'xmlns:a="{A}" xmlns:r="{R}" xmlns:p="{P}"'
HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
SW, SH = 12192000, 6858000                     # 16:9
BODY_X, BODY_Y, BODY_W, BODY_H = 838200, 1690688, 10515600, 4486275
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

GRP = ('<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/>'
       '<a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>')


def _ph(id_: int, name: str, ph: str, xfrm: str = "", body: str = "", anchor: str = "") -> str:
    bp = f'<a:bodyPr{f" anchor={chr(34)}{anchor}{chr(34)}" if anchor else ""}/>'
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{id_}" name="{name}"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>'
            f'<p:nvPr>{ph}</p:nvPr></p:nvSpPr><p:spPr>{xfrm}</p:spPr><p:txBody>{bp}<a:lstStyle/>'
            f'{body or "<a:p><a:endParaRPr lang=" + chr(34) + "en-US" + chr(34) + "/></a:p>"}</p:txBody></p:sp>')


def _xfrm(x, y, w, h) -> str:
    return f'<a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'


class _Deck:
    def __init__(self):
        self.slides: list[dict] = []   # {"layout": 1|2, "xml": str, "links": [...]}

    def runs(self, runs, links: list, size: int | None = None, mono: bool = False, bold: bool = False) -> str:
        out = []
        for text, st in runs:
            attrs = ['lang="en-US"']
            if size:
                attrs.append(f'sz="{size}"')
            if st.get("b") or bold:
                attrs.append('b="1"')
            if st.get("i"):
                attrs.append('i="1"')
            if st.get("s"):
                attrs.append('strike="sngStrike"')
            inner = ""
            if st.get("code") or mono:
                inner += '<a:latin typeface="Consolas"/><a:cs typeface="Consolas"/>'
            if st.get("link"):
                links.append(st["link"])
                inner += f'<a:hlinkClick r:id="rIdH{len(links)}"/>'
            for k, part in enumerate(text.split("\n")):
                if k:
                    out.append(f'<a:br><a:rPr {" ".join(attrs)}/></a:br>')
                if part:
                    out.append(f'<a:r><a:rPr {" ".join(attrs)} dirty="0">{inner}</a:rPr><a:t>{escape(part)}</a:t></a:r>')
        return "".join(out)

    def add(self, title_runs, blocks, subtitle=None):
        links: list = []
        if subtitle is not None:  # title slide
            center = '<a:pPr marL="0" indent="0" algn="ctr"><a:buNone/></a:pPr>'
            sp = (_ph(2, "Title 1", '<p:ph type="ctrTitle"/>', body=f"<a:p>{center}{self.runs(title_runs, links)}</a:p>")
                  + _ph(3, "Subtitle 2", '<p:ph type="subTitle" idx="1"/>',
                        body=f"<a:p>{center}{self.runs(subtitle, links, size=2400)}</a:p>" if subtitle else ""))
            self.slides.append({"layout": 1, "xml": sp, "links": links})
            return
        paras, table = [], None
        measure: list[tuple[int, float, float]] = []   # (characters, chars per line at full size, line height)
        for b in blocks:
            t = b["t"]
            if t == "li":
                paras.append(f'<a:p><a:pPr lvl="{min(4, b["level"])}"/>{self.runs(b["runs"], links)}</a:p>')
                measure.append((len(md.plain(b["runs"])), 68 - 8 * min(4, b["level"]), 1.0))
            elif t in ("p", "quote"):
                runs = [(x, {**s, "i": True}) for x, s in b["runs"]] if t == "quote" else b["runs"]
                paras.append(f'<a:p><a:pPr marL="0" indent="0"><a:buNone/></a:pPr>{self.runs(runs, links)}</a:p>')
                measure.append((len(md.plain(b["runs"])), 70, 1.0))
            elif t == "h":
                paras.append(f'<a:p><a:pPr marL="0" indent="0"><a:buNone/></a:pPr>{self.runs(b["runs"], links, bold=True)}</a:p>')
                measure.append((len(md.plain(b["runs"])), 60, 1.1))
            elif t == "code":
                for line in b["text"].split("\n"):
                    paras.append(f'<a:p><a:pPr marL="0" indent="0"><a:buNone/></a:pPr>'
                                 f'{self.runs([(line or " ", {})], links, size=1400, mono=True)}</a:p>')
                    measure.append((len(line), 95, 0.6))
            elif t == "table" and table is None:
                table = b

        def height(scale: float) -> float:
            """Lines of 24 pt text the paragraphs take at `scale` (smaller text wraps less)."""
            return sum(lh * (1 + int(n // max(1, cpl / scale))) for n, cpl, lh in measure) * scale
        lines = height(1.0)
        body_h = BODY_H
        sp = _ph(2, "Title 1", '<p:ph type="title"/>', body=f"<a:p>{self.runs(title_runs, links)}</a:p>")
        frame = ""
        if table is not None:
            rows = 1 + len(table["rows"])
            row_h = 370840
            text_h = min(int(BODY_H * 0.45), int(lines * 457200)) if paras else 0
            ty = BODY_Y + text_h + (120000 if paras else 0)
            frame = self.table(table, links, ty, min(row_h, max(250000, (BODY_Y + BODY_H - ty) // rows)))
            body_h = text_h
        if paras:
            avail = body_h / 457200  # lines of 24 pt text that fit
            scale = 1.0
            while scale > 0.4 and height(scale) > avail:
                scale = round(scale - 0.05, 2)
            fit = f'<a:normAutofit fontScale="{int(scale * 100000)}" lnSpcReduction="{10000 if scale < 1 else 0}"/>'
            xfrm = _xfrm(BODY_X, BODY_Y, BODY_W, body_h) if table is not None else ""
            sp += (f'<p:sp><p:nvSpPr><p:cNvPr id="3" name="Content 2"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>'
                   f'<p:nvPr><p:ph idx="1"/></p:nvPr></p:nvSpPr><p:spPr>{xfrm}</p:spPr><p:txBody><a:bodyPr>{fit}</a:bodyPr>'
                   f'<a:lstStyle/>{"".join(paras)}</p:txBody></p:sp>')
        self.slides.append({"layout": 2, "xml": sp + frame, "links": links})

    def table(self, b, links, y, row_h) -> str:
        n = max(1, len(b["head"]))
        col_w = BODY_W // n
        algn = {"left": "l", "right": "r", "center": "ctr"}
        border = "".join(f'<a:{side} w="9525"><a:solidFill><a:srgbClr val="BFBFBF"/></a:solidFill></a:{side}>'
                         for side in ("lnL", "lnR", "lnT", "lnB"))

        def cell(runs, header, j):
            size = 1400 if len(b["rows"]) < 8 else 1100
            body = self.runs(runs or [("", {})], links, size=size, bold=header)
            fill = '<a:solidFill><a:srgbClr val="E7E6E6"/></a:solidFill>' if header else ""
            return (f'<a:tc><a:txBody><a:bodyPr/><a:lstStyle/><a:p><a:pPr algn="{algn.get(b["align"][j], "l")}"/>{body}</a:p>'
                    f'</a:txBody><a:tcPr marL="91440" marR="91440" marT="45720" marB="45720">{border}{fill}</a:tcPr></a:tc>')
        rows = [f'<a:tr h="{row_h}">' + "".join(cell(c, True, j) for j, c in enumerate(b["head"])) + "</a:tr>"]
        rows += [f'<a:tr h="{row_h}">' + "".join(cell(c, False, j) for j, c in enumerate(r)) + "</a:tr>" for r in b["rows"]]
        grid = "".join(f'<a:gridCol w="{col_w}"/>' for _ in range(n))
        h_total = row_h * (1 + len(b["rows"]))
        return (f'<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="4" name="Table 3"/><p:cNvGraphicFramePr>'
                f'<a:graphicFrameLocks noGrp="1"/></p:cNvGraphicFramePr><p:nvPr/></p:nvGraphicFramePr>'
                f'<p:xfrm><a:off x="{BODY_X}" y="{y}"/><a:ext cx="{col_w * n}" cy="{h_total}"/></p:xfrm>'
                f'<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table"><a:tbl>'
                f'<a:tblPr firstRow="1" bandRow="1"/><a:tblGrid>{grid}</a:tblGrid>{"".join(rows)}</a:tbl></a:graphicData>'
                f"</a:graphic></p:graphicFrame>")


def _slides(blocks) -> tuple[_Deck, str]:
    deck = _Deck()
    title_text = ""
    i = 0
    # a title slide: '# Title', optional paragraph, then another heading (or the end)
    if blocks and blocks[0]["t"] == "h" and blocks[0]["level"] == 1:
        j = 1
        sub = None
        if j < len(blocks) and blocks[j]["t"] == "p":
            sub = blocks[j]["runs"]
            j += 1
        if j >= len(blocks) or blocks[j]["t"] in ("h", "break", "hr"):
            title_text = md.plain(blocks[0]["runs"])
            deck.add(blocks[0]["runs"], [], subtitle=sub or [])
            i = j
    cur_title, cur = None, []

    def flush():
        nonlocal cur_title, cur
        if cur_title is not None or cur:
            deck.add(cur_title or [], cur)
        cur_title, cur = None, []
    while i < len(blocks):
        b = blocks[i]
        if b["t"] == "h" and b["level"] <= 2:
            flush()
            cur_title = b["runs"]
            title_text = title_text or md.plain(b["runs"])
        elif b["t"] in ("break", "hr"):
            flush()
        else:
            cur.append(b)
        i += 1
    flush()
    if not deck.slides:
        deck.add([("", {})], [])
    return deck, title_text


def _master() -> str:
    lvls = "".join(
        f'<a:lvl{i}pPr marL="{228600 + 457200 * (i - 1)}" indent="-228600" algn="l" defTabSz="914400" rtl="0" eaLnBrk="1" '
        f'latinLnBrk="0" hangingPunct="1"><a:lnSpc><a:spcPct val="90000"/></a:lnSpc><a:spcBef><a:spcPts val="{1000 if i == 1 else 500}"/></a:spcBef>'
        f'<a:buFont typeface="Arial" panose="020B0604020202020204" pitchFamily="34" charset="0"/><a:buChar char="{"•–•–•"[i - 1]}"/>'
        f'<a:defRPr sz="{[2400, 2000, 1800, 1600, 1600][i - 1]}" kern="1200"><a:solidFill><a:schemeClr val="tx1"/></a:solidFill>'
        f'<a:latin typeface="+mn-lt"/><a:ea typeface="+mn-ea"/><a:cs typeface="+mn-cs"/></a:defRPr></a:lvl{i}pPr>'
        for i in range(1, 6))
    title = _ph(2, "Title Placeholder 1", '<p:ph type="title"/>', _xfrm(838200, 365125, 10515600, 1325563), anchor="ctr")
    body = _ph(3, "Text Placeholder 2", '<p:ph type="body" idx="1"/>', _xfrm(BODY_X, BODY_Y, BODY_W, BODY_H))
    return (HEAD + f'<p:sldMaster {NS}><p:cSld><p:bg><p:bgRef idx="1001"><a:schemeClr val="bg1"/></p:bgRef></p:bg>'
            f'<p:spTree>{GRP}{title}{body}</p:spTree></p:cSld>'
            '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" '
            'accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
            '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/><p:sldLayoutId id="2147483650" r:id="rId2"/></p:sldLayoutIdLst>'
            '<p:txStyles><p:titleStyle><a:lvl1pPr algn="l" defTabSz="914400" rtl="0" eaLnBrk="1" latinLnBrk="0" hangingPunct="1">'
            '<a:lnSpc><a:spcPct val="90000"/></a:lnSpc><a:spcBef><a:spcPct val="0"/></a:spcBef><a:buNone/>'
            '<a:defRPr sz="4000" kern="1200"><a:solidFill><a:schemeClr val="tx2"/></a:solidFill><a:latin typeface="+mj-lt"/>'
            '<a:ea typeface="+mj-ea"/><a:cs typeface="+mj-cs"/></a:defRPr></a:lvl1pPr></p:titleStyle>'
            f'<p:bodyStyle>{lvls}</p:bodyStyle><p:otherStyle><a:defPPr><a:defRPr lang="en-US"/></a:defPPr></p:otherStyle>'
            "</p:txStyles></p:sldMaster>")


def _layout(kind: int) -> str:
    if kind == 1:
        sp = (_ph(2, "Title 1", '<p:ph type="ctrTitle"/>', _xfrm(1524000, 1122363, 9144000, 2387600), anchor="b")
              + _ph(3, "Subtitle 2", '<p:ph type="subTitle" idx="1"/>', _xfrm(1524000, 3602038, 9144000, 1655762)))
        return (HEAD + f'<p:sldLayout {NS} type="title" preserve="1"><p:cSld name="Title Slide"><p:spTree>{GRP}{sp}'
                "</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>")
    sp = _ph(2, "Title 1", '<p:ph type="title"/>') + _ph(3, "Content Placeholder 2", '<p:ph idx="1"/>')
    return (HEAD + f'<p:sldLayout {NS} type="obj" preserve="1"><p:cSld name="Title and Content"><p:spTree>{GRP}{sp}'
            "</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>")


THEME = (HEAD + f'<a:theme xmlns:a="{A}" name="baabaa"><a:themeElements><a:clrScheme name="baabaa">'
         '<a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1><a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>'
         '<a:dk2><a:srgbClr val="1F3864"/></a:dk2><a:lt2><a:srgbClr val="E7E6E6"/></a:lt2>'
         + "".join(f'<a:accent{i}><a:srgbClr val="{c}"/></a:accent{i}>' for i, c in
                   enumerate(["2F7D6D", "C0843E", "5A6FB0", "B5563C", "7A5C99", "8A7A2C"], 1))
         + '<a:hlink><a:srgbClr val="0563C1"/></a:hlink><a:folHlink><a:srgbClr val="954F72"/></a:folHlink></a:clrScheme>'
         '<a:fontScheme name="baabaa"><a:majorFont><a:latin typeface="Calibri Light"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont>'
         '<a:minorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont></a:fontScheme>'
         '<a:fmtScheme name="baabaa"><a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
         '<a:solidFill><a:schemeClr val="phClr"><a:tint val="50000"/></a:schemeClr></a:solidFill>'
         '<a:solidFill><a:schemeClr val="phClr"><a:shade val="80000"/></a:schemeClr></a:solidFill></a:fillStyleLst>'
         '<a:lnStyleLst>' + "".join(f'<a:ln w="{w}" cap="flat" cmpd="sng" algn="ctr"><a:solidFill><a:schemeClr val="phClr"/>'
                                    f'</a:solidFill><a:prstDash val="solid"/></a:ln>' for w in (6350, 12700, 19050))
         + '</a:lnStyleLst><a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle>'
         '<a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst><a:bgFillStyleLst>'
         '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"><a:tint val="95000"/></a:schemeClr>'
         '</a:solidFill><a:solidFill><a:schemeClr val="phClr"><a:shade val="90000"/></a:schemeClr></a:solidFill></a:bgFillStyleLst>'
         '</a:fmtScheme></a:themeElements><a:objectDefaults/><a:extraClrSchemeLst/></a:theme>')


def _rels(items: list[tuple[str, str, str]]) -> str:
    body = "".join(f'<Relationship Id="{i}" Type="{t}" Target="{escape(tg, {chr(34): "&quot;"})}"'
                   + (' TargetMode="External"' if t.endswith("/hyperlink") else "") + "/>" for i, t, tg in items)
    return HEAD + f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{body}</Relationships>'


def write(markdown: str, title: str | None = None) -> bytes:
    deck, first_title = _slides(md.parse(markdown))
    n = len(deck.slides)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        pml = "application/vnd.openxmlformats-officedocument.presentationml"
        z.writestr("[Content_Types].xml", HEAD +
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   f'<Override PartName="/ppt/presentation.xml" ContentType="{pml}.presentation.main+xml"/>'
                   f'<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="{pml}.slideMaster+xml"/>'
                   f'<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="{pml}.slideLayout+xml"/>'
                   f'<Override PartName="/ppt/slideLayouts/slideLayout2.xml" ContentType="{pml}.slideLayout+xml"/>'
                   + "".join(f'<Override PartName="/ppt/slides/slide{i}.xml" ContentType="{pml}.slide+xml"/>' for i in range(1, n + 1))
                   + '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>'
                   f'<Override PartName="/ppt/presProps.xml" ContentType="{pml}.presProps+xml"/>'
                   f'<Override PartName="/ppt/viewProps.xml" ContentType="{pml}.viewProps+xml"/>'
                   f'<Override PartName="/ppt/tableStyles.xml" ContentType="{pml}.tableStyles+xml"/>'
                   '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
                   '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
                   "</Types>")
        z.writestr("_rels/.rels", _rels([
            ("rId1", f"{REL}/officeDocument", "ppt/presentation.xml"),
            ("rId2", "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties", "docProps/core.xml"),
            ("rId3", f"{REL}/extended-properties", "docProps/app.xml")]))
        sld_ids = "".join(f'<p:sldId id="{255 + i}" r:id="rId{i + 1}"/>' for i in range(1, n + 1))
        z.writestr("ppt/presentation.xml", HEAD +
                   f'<p:presentation {NS} saveSubsetFonts="1"><p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/>'
                   f'</p:sldMasterIdLst><p:sldIdLst>{sld_ids}</p:sldIdLst><p:sldSz cx="{SW}" cy="{SH}"/>'
                   '<p:notesSz cx="6858000" cy="9144000"/></p:presentation>')
        items = [("rId1", f"{REL}/slideMaster", "slideMasters/slideMaster1.xml")]
        items += [(f"rId{i + 1}", f"{REL}/slide", f"slides/slide{i}.xml") for i in range(1, n + 1)]
        items += [(f"rId{n + 2}", f"{REL}/theme", "theme/theme1.xml"), (f"rId{n + 3}", f"{REL}/presProps", "presProps.xml"),
                  (f"rId{n + 4}", f"{REL}/viewProps", "viewProps.xml"), (f"rId{n + 5}", f"{REL}/tableStyles", "tableStyles.xml")]
        z.writestr("ppt/_rels/presentation.xml.rels", _rels(items))
        z.writestr("ppt/slideMasters/slideMaster1.xml", _master())
        z.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", _rels([
            ("rId1", f"{REL}/slideLayout", "../slideLayouts/slideLayout1.xml"),
            ("rId2", f"{REL}/slideLayout", "../slideLayouts/slideLayout2.xml"),
            ("rId3", f"{REL}/theme", "../theme/theme1.xml")]))
        for k in (1, 2):
            z.writestr(f"ppt/slideLayouts/slideLayout{k}.xml", _layout(k))
            z.writestr(f"ppt/slideLayouts/_rels/slideLayout{k}.xml.rels",
                       _rels([("rId1", f"{REL}/slideMaster", "../slideMasters/slideMaster1.xml")]))
        for i, s in enumerate(deck.slides, 1):
            z.writestr(f"ppt/slides/slide{i}.xml", HEAD + f'<p:sld {NS}><p:cSld><p:spTree>{GRP}{s["xml"]}</p:spTree></p:cSld>'
                       '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>')
            rel = [("rId1", f"{REL}/slideLayout", f"../slideLayouts/slideLayout{s['layout']}.xml")]
            rel += [(f"rIdH{j}", f"{REL}/hyperlink", url) for j, url in enumerate(s["links"], 1)]
            z.writestr(f"ppt/slides/_rels/slide{i}.xml.rels", _rels(rel))
        z.writestr("ppt/theme/theme1.xml", THEME)
        z.writestr("ppt/presProps.xml", HEAD + f"<p:presentationPr {NS}/>")
        z.writestr("ppt/viewProps.xml", HEAD + f'<p:viewPr {NS}><p:normalViewPr><p:restoredLeft sz="15620"/>'
                   '<p:restoredTop sz="94660"/></p:normalViewPr><p:gridSpacing cx="76200" cy="76200"/></p:viewPr>')
        z.writestr("ppt/tableStyles.xml", HEAD + f'<a:tblStyleLst xmlns:a="{A}" def="{{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}}"/>')
        z.writestr("docProps/core.xml", core_xml(title or first_title))
        z.writestr("docProps/app.xml", app_xml())
    return buf.getvalue()
