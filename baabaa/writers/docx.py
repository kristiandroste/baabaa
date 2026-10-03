"""Word documents (.docx) from Markdown, written as WordprocessingML with zipfile. No packages.

Headings, paragraphs with bold/italic/code/strikethrough and links, numbered and bulleted lists (real
Word lists, nested), tables with a header row, code blocks, quotes, rules and page breaks.
"""

import io
import time
import zipfile
from xml.sax.saxutils import escape

from . import md

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
FONT, MONO = "Calibri", "Consolas"


def _t(text: str) -> str:
    return f'<w:t xml:space="preserve">{escape(text)}</w:t>'


def _run(text: str, st: dict) -> str:
    props = []
    if st.get("link"):
        props.append('<w:rStyle w:val="Hyperlink"/>')
    if st.get("code"):
        props.append(f'<w:rFonts w:ascii="{MONO}" w:hAnsi="{MONO}" w:cs="{MONO}"/>')
    if st.get("b"):
        props.append("<w:b/><w:bCs/>")
    if st.get("i"):
        props.append("<w:i/><w:iCs/>")
    if st.get("s"):
        props.append("<w:strike/>")
    if st.get("code"):
        props.append('<w:shd w:val="clear" w:color="auto" w:fill="F2F2F2"/>')
    rpr = f"<w:rPr>{''.join(props)}</w:rPr>" if props else ""
    parts = text.split("\n")
    body = "<w:br/>".join(_t(p) for p in parts)
    return f"<w:r>{rpr}{body}</w:r>"


class _Doc:
    def __init__(self):
        self.links: list[str] = []
        self.nums: list[bool] = []   # one entry per list instance: ordered?

    def runs(self, runs) -> str:
        out = []
        for text, st in runs:
            if st.get("link"):
                self.links.append(st["link"])
                rid = f"rIdL{len(self.links)}"
                out.append(f'<w:hyperlink r:id="{rid}" w:history="1">{_run(text, st)}</w:hyperlink>')
            else:
                out.append(_run(text, st))
        return "".join(out)

    def para(self, runs, style: str | None = None, num: tuple[int, int] | None = None, jc: str | None = None,
             page_break_before: bool = False) -> str:
        ppr = []
        if style:
            ppr.append(f'<w:pStyle w:val="{style}"/>')
        if page_break_before:
            ppr.append("<w:pageBreakBefore/>")
        if num:
            ppr.append(f'<w:numPr><w:ilvl w:val="{num[1]}"/><w:numId w:val="{num[0]}"/></w:numPr>')
        if jc:
            ppr.append(f'<w:jc w:val="{jc}"/>')
        p = f"<w:pPr>{''.join(ppr)}</w:pPr>" if ppr else ""
        return f"<w:p>{p}{self.runs(runs)}</w:p>"

    def table(self, b) -> str:
        n = max(1, len(b["head"]))
        width = 9000 // n
        grid = "".join(f'<w:gridCol w:w="{width}"/>' for _ in range(n))
        jcmap = {"left": None, "right": "right", "center": "center"}

        def cell(runs, header, j):
            runs = [(t, {**s, "b": True}) if header else (t, s) for t, s in runs] or [("", {})]
            shade = '<w:shd w:val="clear" w:color="auto" w:fill="E7E6E6"/>' if header else ""
            return (f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/>{shade}</w:tcPr>'
                    f'{self.para(runs, "TableText", jc=jcmap.get(b["align"][j]))}</w:tc>')
        rows = ['<w:tr><w:trPr><w:tblHeader/></w:trPr>' + "".join(cell(c, True, j) for j, c in enumerate(b["head"])) + "</w:tr>"]
        for r in b["rows"]:
            rows.append("<w:tr>" + "".join(cell(c, False, j) for j, c in enumerate(r)) + "</w:tr>")
        return (f'<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="0" w:type="auto"/>'
                f'<w:tblLook w:val="04A0" w:firstRow="1" w:lastRow="0" w:firstColumn="0" w:lastColumn="0" w:noHBand="0" w:noVBand="1"/>'
                f"</w:tblPr><w:tblGrid>{grid}</w:tblGrid>{''.join(rows)}</w:tbl><w:p/>")

    def body(self, blocks, title: str | None) -> str:
        out = []
        if title:
            out.append(self.para([(title, {})], "Title"))
        list_id = {}
        pending_break = False
        for b in blocks:
            t = b["t"]
            if t == "break":
                pending_break = True
                continue
            pb = pending_break
            pending_break = False
            if t == "h":
                out.append(self.para(b["runs"], f"Heading{min(6, b['level'])}", page_break_before=pb))
            elif t == "p":
                out.append(self.para(b["runs"], page_break_before=pb))
            elif t == "li":
                key = (b["level"], b["ordered"])
                if b.get("start") or key not in list_id:
                    if b.get("start"):
                        list_id.clear()
                    self.nums.append(b["ordered"])
                    list_id[key] = len(self.nums)
                    # deeper levels of the same list share its numbering instance
                    for lv in range(9):
                        list_id.setdefault((lv, b["ordered"]), list_id[key])
                out.append(self.para(b["runs"], "ListParagraph", num=(list_id[key], b["level"]), page_break_before=pb))
            elif t == "table":
                out.append(self.table(b))
            elif t == "code":
                out.append(self.para([(b["text"], {})], "Code", page_break_before=pb))
            elif t == "quote":
                out.append(self.para(b["runs"], "Quote", page_break_before=pb))
            elif t == "hr":
                out.append('<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" w:color="BFBFBF"/></w:pBdr></w:pPr></w:p>')
        sect = ('<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" '
                'w:left="1440" w:header="708" w:footer="708" w:gutter="0"/></w:sectPr>')
        return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:document xmlns:w="{W_NS}" xmlns:r="{R_NS}">'
                f"<w:body>{''.join(out)}{sect}</w:body></w:document>")


def _styles() -> str:
    heads = "".join(
        f'<w:style w:type="paragraph" w:styleId="Heading{i}"><w:name w:val="heading {i}"/><w:basedOn w:val="Normal"/>'
        f'<w:next w:val="Normal"/><w:uiPriority w:val="9"/><w:qFormat/><w:pPr><w:keepNext/><w:keepLines/>'
        f'<w:spacing w:before="{360 - i * 30}" w:after="120"/><w:outlineLvl w:val="{i - 1}"/></w:pPr>'
        f'<w:rPr><w:b/><w:bCs/><w:color w:val="{["1F3864", "1F3864", "2F5496", "2F5496", "404040", "404040"][i - 1]}"/>'
        f'<w:sz w:val="{[36, 30, 26, 24, 22, 22][i - 1]}"/><w:szCs w:val="{[36, 30, 26, 24, 22, 22][i - 1]}"/></w:rPr></w:style>'
        for i in range(1, 7))
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:styles xmlns:w="{W_NS}">'
            f'<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="{FONT}" w:eastAsia="{FONT}" w:hAnsi="{FONT}" w:cs="{FONT}"/>'
            '<w:sz w:val="22"/><w:szCs w:val="22"/><w:lang w:val="en-US" w:eastAsia="en-US" w:bidi="ar-SA"/></w:rPr></w:rPrDefault>'
            '<w:pPrDefault><w:pPr><w:spacing w:after="160" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
            '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>'
            '<w:style w:type="character" w:default="1" w:styleId="DefaultParagraphFont"><w:name w:val="Default Paragraph Font"/>'
            '<w:uiPriority w:val="1"/><w:semiHidden/><w:unhideWhenUsed/></w:style>'
            '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/>'
            '<w:qFormat/><w:pPr><w:spacing w:after="240"/></w:pPr><w:rPr><w:sz w:val="52"/><w:szCs w:val="52"/></w:rPr></w:style>'
            + heads +
            '<w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="List Paragraph"/><w:basedOn w:val="Normal"/>'
            '<w:qFormat/><w:pPr><w:spacing w:after="60"/><w:contextualSpacing/></w:pPr></w:style>'
            '<w:style w:type="paragraph" w:styleId="Quote"><w:name w:val="Quote"/><w:basedOn w:val="Normal"/><w:qFormat/>'
            '<w:pPr><w:pBdr><w:left w:val="single" w:sz="18" w:space="8" w:color="BFBFBF"/></w:pBdr><w:ind w:left="360"/></w:pPr>'
            '<w:rPr><w:i/><w:iCs/><w:color w:val="595959"/></w:rPr></w:style>'
            '<w:style w:type="paragraph" w:styleId="Code"><w:name w:val="Code"/><w:basedOn w:val="Normal"/>'
            '<w:pPr><w:shd w:val="clear" w:color="auto" w:fill="F2F2F2"/><w:spacing w:after="160" w:line="240" w:lineRule="auto"/></w:pPr>'
            f'<w:rPr><w:rFonts w:ascii="{MONO}" w:hAnsi="{MONO}" w:cs="{MONO}"/><w:sz w:val="19"/><w:szCs w:val="19"/></w:rPr></w:style>'
            '<w:style w:type="paragraph" w:styleId="TableText"><w:name w:val="Table Text"/><w:basedOn w:val="Normal"/>'
            '<w:pPr><w:spacing w:before="40" w:after="40" w:line="240" w:lineRule="auto"/></w:pPr></w:style>'
            '<w:style w:type="character" w:styleId="Hyperlink"><w:name w:val="Hyperlink"/><w:basedOn w:val="DefaultParagraphFont"/>'
            '<w:uiPriority w:val="99"/><w:unhideWhenUsed/><w:rPr><w:color w:val="0563C1"/><w:u w:val="single"/></w:rPr></w:style>'
            '<w:style w:type="table" w:default="1" w:styleId="TableNormal"><w:name w:val="Normal Table"/><w:uiPriority w:val="99"/>'
            '<w:semiHidden/><w:unhideWhenUsed/><w:tblPr><w:tblInd w:w="0" w:type="dxa"/><w:tblCellMar><w:top w:w="0" w:type="dxa"/>'
            '<w:left w:w="108" w:type="dxa"/><w:bottom w:w="0" w:type="dxa"/><w:right w:w="108" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>'
            '<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:basedOn w:val="TableNormal"/><w:uiPriority w:val="39"/>'
            '<w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr><w:tblPr><w:tblBorders>'
            '<w:top w:val="single" w:sz="4" w:space="0" w:color="BFBFBF"/><w:left w:val="single" w:sz="4" w:space="0" w:color="BFBFBF"/>'
            '<w:bottom w:val="single" w:sz="4" w:space="0" w:color="BFBFBF"/><w:right w:val="single" w:sz="4" w:space="0" w:color="BFBFBF"/>'
            '<w:insideH w:val="single" w:sz="4" w:space="0" w:color="BFBFBF"/><w:insideV w:val="single" w:sz="4" w:space="0" w:color="BFBFBF"/>'
            '</w:tblBorders></w:tblPr></w:style></w:styles>')


def _numbering(nums: list[bool]) -> str:
    bullets = "•◦▪•◦▪•◦▪"
    fmts = ["decimal", "lowerLetter", "lowerRoman"] * 3

    def levels(ordered: bool) -> str:
        out = []
        for lv in range(9):
            ind = 720 * (lv + 1)
            if ordered:
                out.append(f'<w:lvl w:ilvl="{lv}"><w:start w:val="1"/><w:numFmt w:val="{fmts[lv]}"/>'
                           f'<w:lvlText w:val="%{lv + 1}."/><w:lvlJc w:val="left"/>'
                           f'<w:pPr><w:ind w:left="{ind}" w:hanging="360"/></w:pPr></w:lvl>')
            else:
                out.append(f'<w:lvl w:ilvl="{lv}"><w:start w:val="1"/><w:numFmt w:val="bullet"/>'
                           f'<w:lvlText w:val="{bullets[lv]}"/><w:lvlJc w:val="left"/>'
                           f'<w:pPr><w:ind w:left="{ind}" w:hanging="360"/></w:pPr></w:lvl>')
        return "".join(out)
    abstract = (f'<w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="hybridMultilevel"/>{levels(False)}</w:abstractNum>'
                f'<w:abstractNum w:abstractNumId="1"><w:multiLevelType w:val="hybridMultilevel"/>{levels(True)}</w:abstractNum>')
    nums_xml = "".join(
        f'<w:num w:numId="{i}"><w:abstractNumId w:val="{1 if ordered else 0}"/>'
        + ("".join(f'<w:lvlOverride w:ilvl="{lv}"><w:startOverride w:val="1"/></w:lvlOverride>' for lv in range(9)) if ordered else "")
        + "</w:num>" for i, ordered in enumerate(nums, 1))
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:numbering xmlns:w="{W_NS}">{abstract}{nums_xml}</w:numbering>'


def core_xml(title: str) -> str:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<cp:coreProperties '
            'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f"<dc:title>{escape(title or '')}</dc:title><dc:creator>baabaa</dc:creator>"
            f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
            f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified></cp:coreProperties>')


def app_xml(app: str = "baabaa") -> str:
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Properties '
            'xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
            'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
            f"<Application>{app}</Application></Properties>")


def write(markdown: str, title: str | None = None) -> bytes:
    blocks = md.parse(markdown)
    if title is None and blocks and blocks[0]["t"] == "h" and blocks[0]["level"] == 1:
        title_text = md.plain(blocks[0]["runs"])
    else:
        title_text = title or ""
    doc = _Doc()
    document = doc.body(blocks, title)
    rels = ['<Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>',
            '<Relationship Id="rIdNumbering" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>']
    for i, url in enumerate(doc.links, 1):
        rels.append(f'<Relationship Id="rIdL{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
                    f'Target="{escape(url, {chr(34): "&quot;"})}" TargetMode="External"/>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                   '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
                   '<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>'
                   '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
                   '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
                   "</Types>")
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                   '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
                   '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
                   "</Relationships>")
        z.writestr("word/_rels/document.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + "".join(rels) + "</Relationships>")
        z.writestr("word/document.xml", document)
        z.writestr("word/styles.xml", _styles())
        z.writestr("word/numbering.xml", _numbering(doc.nums or [False]))
        z.writestr("docProps/core.xml", core_xml(title_text))
        z.writestr("docProps/app.xml", app_xml())
    return buf.getvalue()
