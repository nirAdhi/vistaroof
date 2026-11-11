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
import re

from . import db
from .sources import FileContent, FileMeta

log = logging.getLogger(__name__)

MAX_PDF_PAGES = 60  # beyond this the text is cut anyway by the context budget


class NeedsVision(ValueError):
    """The file has no extractable text (photo, scanned PDF); only a vision model can read it."""


_ODF_TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
_ODF_TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"


def _odf_root(data: bytes):
    import zipfile
    from xml.etree.ElementTree import ParseError

    from defusedxml import ElementTree as ET  # the XML comes from an untrusted file: no entity expansion / external entities

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return ET.fromstring(z.read("content.xml"))
    except (zipfile.BadZipFile, KeyError, ParseError, ValueError) as exc:  # defusedxml raises ValueError subclasses for forbidden constructs
        raise ValueError(f"could not read OpenDocument file ({type(exc).__name__})") from exc


def _odf_sheets(data: bytes) -> list[tuple[str, list[str]]]:
    """(sheet name, non-empty rows) of a .ods file."""
    out = []
    for table in _odf_root(data).iter(f"{_ODF_TABLE}table"):
        rows = []
        for row in table.iter(f"{_ODF_TABLE}table-row"):
            line = _cells(tuple("".join(c.itertext()) for c in row if c.tag == f"{_ODF_TABLE}table-cell"))
            if line:
                rows.append(line)
        out.append((table.get(f"{_ODF_TABLE}name", ""), rows))
    return out


def odf_to_text(data: bytes, spreadsheet: bool) -> str:
    """OpenDocument (.odt / .ods, LibreOffice) → text. The file is a zip; ``content.xml`` holds the body.
    Writer: paragraphs and headings in order. Calc: one line per row, cells joined with " | "."""
    if spreadsheet:
        return "\n".join(f"## Sheet: {name}\n" + "\n".join(rows) for name, rows in _odf_sheets(data) if rows)
    parts: list[str] = []
    for el in _odf_root(data).iter():
        if el.tag in (f"{_ODF_TEXT}p", f"{_ODF_TEXT}h"):
            t = "".join(el.itertext()).strip()
            if t:
                parts.append(t)
    return "\n".join(parts)


ODS_MIME = "application/vnd.oasis.opendocument.spreadsheet"


def is_spreadsheet(mime: str) -> bool:
    return mime.endswith("spreadsheetml.sheet") or mime == ODS_MIME


def workbook_sheets(content: FileContent) -> list[tuple[str, str]]:
    """(sheet name, text) for every non-empty sheet of an .xlsx / .ods workbook, rows joined with " | "."""
    if content.data is None:
        raise ValueError("office document has no content")
    sheets: list[tuple[str, str]] = []
    if content.mime == ODS_MIME:
        for name, rows in _odf_sheets(content.data):
            if rows:
                sheets.append((name, "\n".join(rows)))
        return sheets
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(content.data), read_only=True, data_only=True)
    for ws in wb.worksheets:
        rows = [_cells(row) for row in ws.iter_rows(values_only=True)]
        rows = [r for r in rows if r]
        if rows:  # an empty sheet adds nothing
            sheets.append((ws.title, "\n".join(rows)))
    return sheets


def office_to_text(content: FileContent) -> str | None:
    """Convert .docx / .xlsx / .odt / .ods bytes into plain text. None when the mime is not an Office type."""
    if content.data is None:
        raise ValueError("office document has no content")
    if content.mime == "application/vnd.oasis.opendocument.text":
        return odf_to_text(content.data, spreadsheet=False)
    if is_spreadsheet(content.mime):
        return "\n".join(f"## Sheet: {name}\n{text}" for name, text in workbook_sheets(content))
    if content.mime.endswith("wordprocessingml.document"):
        import docx  # python-docx

        d = docx.Document(io.BytesIO(content.data))
        parts = [p.text for p in d.paragraphs if p.text.strip()]
        for table in d.tables:
            for row in table.rows:
                parts.append(" | ".join(c.text.strip() for c in row.cells))
        return "\n".join(parts)
    return None


# ---------------------------------------------------------------- workbooks with one record per sheet

SHEET_SEP = db.SHEET_SEP  # a sheet document's source_id = "<file id>#sheet:<sheet name>"
MIN_TEMPLATE_SHEETS = 3  # this many sheets starting with the same line = one record per sheet (a job summary workbook)


def split_ref(source_id: str) -> tuple[str, str | None]:
    """(file id, sheet name or None) of a document's source_id."""
    base, sep, sheet = source_id.partition(SHEET_SEP)
    return (base, sheet) if sep else (source_id, None)


def sheet_ref(source_id: str, sheet: str) -> str:
    return f"{source_id}{SHEET_SEP}{sheet}"


def should_split(sheets: list[tuple[str, str]]) -> bool:
    """Read a workbook sheet by sheet when its sheets are separate records: several sheets built from the same
    template (same first line, e.g. "JOB SUMMARY" on every job's sheet), or more text than one document can be.
    A two-sheet statement (data + summary) or an invoice with a "Rates" sheet stays one document."""
    if len(sheets) < 2:
        return False
    if sum(len(t) for _, t in sheets) > MAX_TEXT_CHARS:
        return True
    first_lines: dict[str, int] = {}
    for _, text in sheets:
        line = text.split("\n", 1)[0].strip().lower()
        first_lines[line] = first_lines.get(line, 0) + 1
    return max(first_lines.values()) >= MIN_TEMPLATE_SHEETS


def fetch_part(source, meta: FileMeta) -> FileContent:
    """The file behind a document, or for a sheet document (source_id "<file>#sheet:<name>") just that sheet as text."""
    base, sheet = split_ref(meta.source_id)
    if sheet is None:
        return source.fetch(meta)
    from dataclasses import replace

    content = source.fetch(replace(meta, source_id=base))
    for name, text in workbook_sheets(content):
        if name == sheet:
            return FileContent(mime="text/plain", text=text)
    raise ValueError(f"sheet {sheet!r} is no longer in the workbook")


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


def pdf_pages(data: bytes) -> list[str]:
    """Text layer of a PDF, one string per page. Raises NeedsVision when there is none (scanned / image-only PDF)."""
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
    if sum(len(p.strip()) for p in pages) < 20:
        raise NeedsVision("scanned PDF without a text layer")
    return pages


def pdf_to_text(data: bytes) -> str:
    """Text layer of a PDF. Raises NeedsVision when there is none (scanned / image-only PDF)."""
    return "\n\n".join(p.strip() for p in pdf_pages(data) if p and p.strip())


_MONEY = re.compile(r"(?:€|EUR|£|\$)\s?\d|\b\d{1,3}(?:[,.]\d{3})*[.,]\d{2}\b")
_PAGE_WORDS = ("invoice", "total", "vat", "amount", "due", "balance", "receipt", "subtotal", "net", "credit note",
               "statement", "qty", "quantity", "payslip", "remittance", "date", "account")


def page_score(text: str) -> float:
    """How much a page looks like the document itself (amounts, totals, dates) rather than its terms and
    conditions (walls of prose with hardly a number). Used to decide which pages a small model sees."""
    low = text.lower()
    money = len(_MONEY.findall(text))
    words = sum(low.count(w) for w in _PAGE_WORDS)
    digits = sum(ch.isdigit() for ch in text) / max(1, len(text))
    score = money * 3 + words * 2 + digits * 200
    if len(text) > 2000 and digits < 0.02:  # a long page with almost no numbers: terms, conditions, legal notes
        score -= 40
    return score


def pick_pages(pages: list[str], max_chars: int) -> tuple[str, bool]:
    """The pages of a PDF a small model should see within ``max_chars``: a supplier invoice often carries two
    pages of terms before the invoice page, and a 3B model on a small GPU reads every character slowly. The
    best-scoring page is always kept; more pages are added in score order while they fit; the result is in
    page order with page markers, and the omitted pages are named. Second value: True when anything was cut."""
    numbered = [(i, p.strip()) for i, p in enumerate(pages) if p and p.strip()]
    if not numbered:
        return "", False
    if sum(len(p) + 2 for _, p in numbered) <= max_chars:
        return "\n\n".join(p for _, p in numbered), False
    ranked = sorted(numbered, key=lambda ip: page_score(ip[1]), reverse=True)
    chosen, used = [], 0
    for i, p in ranked:
        cost = len(p) + 40
        if chosen and used + cost > max_chars:
            continue
        chosen.append(i)
        used += cost
    chosen.sort()
    parts = [f"[page {i + 1} of {len(pages)}]\n{pages[i].strip()}" for i in chosen]
    dropped = [str(i + 1) for i, _ in numbered if i not in chosen]
    text = "\n\n".join(parts)
    if dropped:
        text += f"\n\n[page(s) {', '.join(dropped)} omitted: terms or notes without the document's amounts]"
    text, _ = fit(text, max_chars)  # the single best page may itself be longer than the budget
    log.debug("kept PDF pages %s of %d for the small model", [i + 1 for i in chosen], len(pages))
    return text, True


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
