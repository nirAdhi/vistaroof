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


def office_to_text(content: FileContent) -> str | None:
    """Convert .docx / .xlsx bytes into plain text. None when the mime is not an Office type."""
    if content.data is None:
        raise ValueError("office document has no content")
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
            parts.append(f"## Sheet: {ws.title}")
            for row in ws.iter_rows(values_only=True):
                if any(v is not None for v in row):
                    parts.append(" | ".join("" if v is None else str(v) for v in row))
        return "\n".join(parts)
    return None


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
