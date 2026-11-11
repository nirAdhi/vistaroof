"""Plain-text extraction from documents, for text-only models (Ollama).

Claude reads PDFs and photos natively, so the Claude path sends the file itself. A local text model
such as qwen2.5:3b gets *text only*; this module produces it before the model is called:

* text / csv / markdown / Google Docs & Sheets exports: the text as-is
* PDF: the text layer via ``pypdf`` (page by page). A scanned PDF has no text layer → ``NeedsVision``
* .docx / .xlsx: paragraphs, table rows and sheet rows joined with " | "
* photos (jpeg/png/heic/…): no text to extract → ``NeedsVision``

``NeedsVision`` is a ``ValueError`` so the sync marks the file *skipped* (with the reason) rather than
*error*, unless the Claude fallback is enabled, in which case the file goes to Claude instead.

``fit(text, max_chars)`` keeps the head and the tail of a long document (totals are usually at the
end) so it fits the model's context window.
"""
from __future__ import annotations

import io
import logging

from .sources import FileContent, FileMeta

log = logging.getLogger(__name__)

MAX_PDF_PAGES = 60  # beyond this the text is cut anyway by the context budget


class NeedsVision(ValueError):
    """The file has no extractable text (photo, scanned PDF); only a vision model can read it."""


_ODF_TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
_ODF_TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"


def odf_to_text(data: bytes, spreadsheet: bool) -> str:
    """OpenDocument (.odt / .ods, LibreOffice) → text. The file is a zip; ``content.xml`` holds the body.
    Writer: paragraphs and headings in order. Calc: one line per row, cells joined with " | "."""
    import zipfile
    from xml.etree.ElementTree import ParseError

    from defusedxml import ElementTree as ET  # the XML comes from an untrusted file: no entity expansion / external entities

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            root = ET.fromstring(z.read("content.xml"))
    except (zipfile.BadZipFile, KeyError, ParseError, ValueError) as exc:  # defusedxml raises ValueError subclasses for forbidden constructs
        raise ValueError(f"could not read OpenDocument file ({type(exc).__name__})") from exc
    parts: list[str] = []
    if spreadsheet:
        for table in root.iter(f"{_ODF_TABLE}table"):
            parts.append(f"## Sheet: {table.get(f'{_ODF_TABLE}name', '')}")
            for row in table.iter(f"{_ODF_TABLE}table-row"):
                line = _cells(tuple("".join(c.itertext()) for c in row if c.tag == f"{_ODF_TABLE}table-cell"))
                if line:
                    parts.append(line)
    else:
        for el in root.iter():
            if el.tag in (f"{_ODF_TEXT}p", f"{_ODF_TEXT}h"):
                t = "".join(el.itertext()).strip()
                if t:
                    parts.append(t)
    return "\n".join(parts)


def office_to_text(content: FileContent) -> str | None:
    """Convert .docx / .xlsx / .odt / .ods bytes into plain text. None when the mime is not an Office type."""
    if content.data is None:
        raise ValueError("office document has no content")
    if content.mime == "application/vnd.oasis.opendocument.text":
        return odf_to_text(content.data, spreadsheet=False)
    if content.mime == "application/vnd.oasis.opendocument.spreadsheet":
        return odf_to_text(content.data, spreadsheet=True)
    if content.mime.endswith("wordprocessingml.document"):
        import docx  # python-docx

        d = docx.Document(io.BytesIO(content.data))
        parts = [p.text for p in d.paragraphs if p.text.strip()]
        for table in d.tables:
            for row in table.rows:
                parts.append(" | ".join(c.text.strip() for c in row.cells))
        return "\n".join(parts)
    if content.mime.endswith("spreadsheetml.sheet"):
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(content.data), read_only=True, data_only=True)
        parts = []
        for ws in wb.worksheets:
            rows = [_cells(row) for row in ws.iter_rows(values_only=True)]
            rows = [r for r in rows if r]
            if rows:  # an empty sheet adds nothing
                parts.append(f"## Sheet: {ws.title}")
                parts.extend(rows)
        return "\n".join(parts)
    return None


def _cells(row: tuple) -> str:
    """One sheet row as text. A workbook's used range often runs far to the right of the last value
    (formatting, stray cells), so every row would end in dozens of empty " | " columns and a modest
    job-costing workbook would run past the size limit: trailing empties are dropped."""
    vals = ["" if v is None else str(v).strip() for v in row]
    while vals and not vals[-1]:
        vals.pop()
    return " | ".join(vals) if any(vals) else ""


MAX_TEXT_CHARS = 300_000  # ~75k tokens: beyond this a file is not one document but a ledger of its own


def check_length(text: str) -> None:
    """ValueError (→ skipped with the reason) for text too long to read as a single document."""
    if len(text) <= MAX_TEXT_CHARS:
        return
    sheets = text.count("\n## Sheet: ") + (1 if text.startswith("## Sheet: ") else 0)
    what = f"a workbook of {sheets} sheets" if sheets > 1 else "the file"
    raise ValueError(
        f"too long to read as one document: {what} holds {len(text):,} characters of text (limit {MAX_TEXT_CHARS:,}). "
        "A workbook or export with many records is a ledger of its own, not an invoice — enter the figures you need by hand, "
        "or save the relevant sheet or page as its own file."
    )


def pdf_to_text(data: bytes) -> str:
    """Text layer of a PDF. Raises NeedsVision when there is none (scanned / image-only PDF)."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = []
        for i, page in enumerate(reader.pages):
            if i >= MAX_PDF_PAGES:
                pages.append(f"[... {len(reader.pages) - MAX_PDF_PAGES} more pages omitted ...]")
                break
            pages.append(page.extract_text() or "")
    except NeedsVision:
        raise
    except Exception as exc:
        raise ValueError(f"could not read PDF ({type(exc).__name__}: {str(exc)[:120]})") from exc
    text = "\n\n".join(p.strip() for p in pages if p and p.strip())
    if len(text.strip()) < 20:
        raise NeedsVision("scanned PDF without a text layer")
    return text


def pdf_page_images(data: bytes, max_pages: int = 2) -> list:
    """The page pictures of a scanned PDF (PIL images), first ``max_pages`` pages. Scanner and phone apps store
    each page as one embedded picture, so pypdf can hand them over without a renderer. ValueError when none."""
    from pypdf import PdfReader

    out = []
    try:
        reader = PdfReader(io.BytesIO(data))
        for page in reader.pages[:max_pages]:
            best = None
            for img in page.images:
                try:
                    pil = img.image
                except Exception:  # an undecodable embedded picture (unsupported filter): try the next one
                    continue
                if pil is not None and (best is None or pil.size[0] * pil.size[1] > best.size[0] * best.size[1]):
                    best = pil
            if best is not None:
                out.append(best)
    except Exception as exc:
        raise ValueError(f"could not read the pictures in the PDF ({type(exc).__name__}: {str(exc)[:120]})") from exc
    if not out:
        raise ValueError("the PDF has no text layer and no readable page pictures")
    return out


def document_text(meta: FileMeta, content: FileContent) -> str:
    """The document as plain text, whatever the file type. ValueError / NeedsVision when impossible."""
    mime = content.mime
    if content.text is not None:
        return content.text
    if mime == "application/pdf":
        return pdf_to_text(content.data or b"")
    if mime.startswith("image/"):
        raise NeedsVision("photo / image file")
    text = office_to_text(content)
    if text is None:
        raise ValueError(f"unsupported file type {mime}")
    if not text.strip():
        raise ValueError("no text found in office document")
    return text


def fit(text: str, max_chars: int) -> tuple[str, bool]:
    """Cut a long text to ``max_chars`` keeping 70% from the start and 30% from the end."""
    text = text.strip()
    if len(text) <= max_chars:
        return text, False
    head = int(max_chars * 0.7)
    tail = max_chars - head
    cut = text[:head].rstrip() + "\n\n[... middle of the document omitted to fit the model's context ...]\n\n" + text[-tail:].lstrip()
    log.debug("document text cut from %d to %d chars for the model", len(text), len(cut))
    return cut, True
