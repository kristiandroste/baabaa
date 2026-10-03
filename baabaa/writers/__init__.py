"""Files the assistant creates: Word, Excel, PowerPoint and PDF from Markdown (or CSV/JSON for
spreadsheets), and plain text formats as given. Everything is written with the standard library."""

import os

from . import docx, pdf, pptx, xlsx

DOCUMENTS = {".docx": docx.write, ".xlsx": xlsx.write, ".pptx": pptx.write, ".pdf": pdf.write}
TEXT = {".md", ".markdown", ".txt", ".csv", ".tsv", ".json", ".jsonl", ".html", ".htm", ".css", ".js", ".mjs", ".ts",
        ".py", ".r", ".sql", ".svg", ".xml", ".yaml", ".yml", ".toml", ".ini", ".sh", ".tex", ".bib", ".ics", ".vcf"}


class WriterError(Exception):
    pass


def create(path: str, content: str, title: str | None = None) -> dict:
    """Write `content` to `path` in the format its extension names. Returns {'path', 'bytes', 'kind'}."""
    ext = os.path.splitext(path)[1].lower()
    if ext in DOCUMENTS:
        try:
            data = DOCUMENTS[ext](content or "", title)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise WriterError(f"could not write the {ext} file: {exc}") from exc
        kind = ext[1:]
    elif ext in TEXT or not ext:
        data = (content or "").encode("utf-8")
        kind = "text"
    else:
        raise WriterError(f"{ext} files cannot be created; use .docx, .xlsx, .pptx, .pdf or a text format")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return {"path": path, "bytes": len(data), "kind": kind}
