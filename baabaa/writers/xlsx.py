"""Excel workbooks (.xlsx) written as SpreadsheetML with zipfile. No packages.

Input, whichever is given: CSV text (one sheet), Markdown with tables (one sheet per table, named after
the heading above it), or JSON {"sheets": [{"name", "rows": [[...], ...]}]}. Numbers become numbers,
cells starting with "=" become formulas, the first row is a bold, frozen header with a filter.
"""

import csv
import io
import json
import re
import zipfile
from xml.sax.saxutils import escape

from . import md
from .docx import app_xml, core_xml

NUM = re.compile(r"^[-+]?(\d{1,3}(,\d{3})+|\d+)?(\.\d+)?([eE][-+]?\d+)?$")
PCT = re.compile(r"^[-+]?\d+(\.\d+)?%$")
CUR = re.compile(r"^[-+]?[$€£¥]\s?\d[\d,]*(\.\d+)?$")


def col_name(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell(ref: str, value, style: int = 0) -> str:
    st = f' s="{style}"' if style else ""
    if value is None or value == "":
        return f'<c r="{ref}"{st}/>' if style else ""
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"{st}><v>{int(value)}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"{st}><v>{value!r}</v></c>'
    text = str(value)
    t = text.strip()
    if t.startswith("=") and len(t) > 1:
        return f'<c r="{ref}"{st}><f>{escape(t[1:])}</f></c>'
    if t and NUM.match(t) and any(ch.isdigit() for ch in t):
        num = float(t.replace(",", ""))
        return f'<c r="{ref}"{st}><v>{int(num) if num.is_integer() and abs(num) < 1e15 else num!r}</v></c>'
    if PCT.match(t):
        return f'<c r="{ref}" s="{3}"><v>{float(t[:-1]) / 100!r}</v></c>'
    if CUR.match(t):
        return f'<c r="{ref}" s="{4}"><v>{float(re.sub(r"[^0-9.+-]", "", t))!r}</v></c>'
    if t.lower() in ("true", "false"):
        return f'<c r="{ref}" t="b"{st}><v>{int(t.lower() == "true")}</v></c>'
    return f'<c r="{ref}" t="inlineStr"{st}><is><t xml:space="preserve">{escape(text)}</t></is></c>'


def _sheet(rows: list[list]) -> str:
    rows = [list(r) for r in rows if r is not None]
    ncols = max((len(r) for r in rows), default=1)
    widths = [8.0] * ncols
    body = []
    for i, r in enumerate(rows):
        cells = []
        for j, v in enumerate(r):
            widths[j] = max(widths[j], min(60.0, len(str(v if v is not None else "")) * 1.1 + 2))
            cells.append(_cell(f"{col_name(j)}{i + 1}", v, 1 if i == 0 and len(rows) > 1 else 0))
        body.append(f'<row r="{i + 1}">{"".join(cells)}</row>')
    last = f"{col_name(ncols - 1)}{max(1, len(rows))}"
    cols = "".join(f'<col min="{j + 1}" max="{j + 1}" width="{w:.1f}" customWidth="1"/>' for j, w in enumerate(widths))
    header = len(rows) > 1
    views = ('<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
             '</sheetView></sheetViews>' if header else '<sheetViews><sheetView workbookViewId="0"/></sheetViews>')
    auto = f'<autoFilter ref="A1:{last}"/>' if header else ""
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<dimension ref="A1:{last}"/>{views}<sheetFormatPr defaultRowHeight="15"/><cols>{cols}</cols>'
            f'<sheetData>{"".join(body)}</sheetData>{auto}'
            '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/></worksheet>')


STYLES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
          '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
          '<numFmts count="1"><numFmt numFmtId="164" formatCode="#,##0.00"/></numFmts>'
          '<fonts count="2"><font><sz val="11"/><name val="Calibri"/><family val="2"/></font>'
          '<font><b/><sz val="11"/><name val="Calibri"/><family val="2"/></font></fonts>'
          '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
          '<fill><patternFill patternType="solid"><fgColor rgb="FFE7E6E6"/><bgColor indexed="64"/></patternFill></fill></fills>'
          '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
          '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
          '<cellXfs count="5"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
          '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
          '<xf numFmtId="14" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
          '<xf numFmtId="10" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
          '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>'
          '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')


def sheets_from(content: str) -> list[tuple[str, list[list]]]:
    """[(name, rows)] from JSON, Markdown tables or CSV text."""
    s = (content or "").strip()
    if s.startswith("{") or s.startswith("["):
        try:
            data = json.loads(s)
            if isinstance(data, list):
                data = {"sheets": [{"name": "Sheet1", "rows": data}]}
            out = []
            for i, sh in enumerate(data.get("sheets") or []):
                rows = sh.get("rows") or []
                if rows and isinstance(rows[0], dict):  # a list of records
                    keys = list(dict.fromkeys(k for r in rows for k in r))
                    rows = [keys] + [[r.get(k) for k in keys] for r in rows]
                out.append((str(sh.get("name") or f"Sheet{i + 1}"), rows))
            if out:
                return out
        except ValueError:
            pass
    blocks = md.parse(s)
    tables, heading = [], None
    for b in blocks:
        if b["t"] == "h":
            heading = md.plain(b["runs"])
        elif b["t"] == "table":
            tables.append((heading or f"Sheet{len(tables) + 1}", [b["raw_head"]] + b["raw_rows"]))
            heading = None
    if tables:
        return [(n, [[_strip_md(c) for c in r] for r in rows]) for n, rows in tables]
    dialect = csv.excel_tab if s.count("\t") > s.count(",") else csv.excel
    return [("Sheet1", list(csv.reader(io.StringIO(s), dialect)))]


def _strip_md(c: str) -> str:
    return md.plain(md.inline(c))


def _sheet_name(name: str, used: set) -> str:
    base = re.sub(r"[\[\]:*?/\\]", " ", name).strip()[:31] or "Sheet"
    n, k = base, 2
    while n.lower() in used:
        suffix = f" ({k})"
        n, k = base[:31 - len(suffix)] + suffix, k + 1
    used.add(n.lower())
    return n


def write(content: str, title: str | None = None) -> bytes:
    sheets = sheets_from(content) or [("Sheet1", [[]])]
    used: set = set()
    names = [_sheet_name(n, used) for n, _ in sheets]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        overrides = "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
                            f'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                            for i in range(1, len(sheets) + 1))
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   + overrides +
                   '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
                   '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
                   "</Types>")
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                   '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
                   '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
                   "</Relationships>")
        sheet_list = "".join(f'<sheet name="{escape(n, {chr(34): "&quot;"})}" sheetId="{i}" r:id="rId{i}"/>'
                             for i, n in enumerate(names, 1))
        defined = "".join(
            f'<definedName name="_xlnm._FilterDatabase" localSheetId="{i}" hidden="1">\'{n.replace(chr(39), chr(39) * 2)}\'!$A$1:${col_name(max(len(r) for r in rows) - 1)}${len(rows)}</definedName>'
            for i, (n, (_, rows)) in enumerate(zip(names, sheets)) if len(rows) > 1 and any(rows))
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                   f'<bookViews><workbookView/></bookViews><sheets>{sheet_list}</sheets>'
                   + (f"<definedNames>{defined}</definedNames>" if defined else "") + "</workbook>")
        rels = "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                       f'Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(sheets) + 1))
        rels += (f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
                 'Target="styles.xml"/>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                   f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>')
        z.writestr("xl/styles.xml", STYLES)
        for i, (_, rows) in enumerate(sheets, 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _sheet(rows or [[]]))
        z.writestr("docProps/core.xml", core_xml(title or ""))
        z.writestr("docProps/app.xml", app_xml())
    return buf.getvalue()
