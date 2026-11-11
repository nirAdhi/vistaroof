"""Browser-viewable copies of source files so the owner can see the actual invoice.

``get(row)`` returns ``(bytes, mime)`` the browser can display: PDFs as-is, photos normalised to
JPEG/PNG (HEIC converted, huge scans downscaled), text/CSV as text, Word/Excel as extracted text,
demo documents as generated sample pages.

Where the bytes come from
-------------------------
By default (setting ``cache_previews`` = 0) the file is fetched from its source (SharePoint,
OneDrive, Google Drive, folder) every time a preview is opened and nothing is written to disk: the
server then holds no document content at all, only the extracted figures in the database. With
``cache_previews`` = 1 a copy is kept under ``<DATA_DIR>/previews`` named ``<id>-<hash of
modified_at>`` (a changed source file gets a fresh copy); ``clear()`` deletes them all. Demo pages
are generated, not user data, and are always cached.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from . import config, db, images, settings, textract
from .sources import FileContent, FileMeta, open_source

PREVIEW_DIR = config.DATA_DIR / "previews"
EXT = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp", "text/plain": "txt"}


def enabled() -> bool:
    """True when copies of real documents may be kept on disk."""
    return settings.get_bool("cache_previews", False)


def _cache_name(row: dict[str, Any]) -> str:
    # Cache-busting file name only (not a security hash): changes whenever the source file changes.
    stamp = hashlib.sha256(f"{row['id']}:{row.get('modified_at')}".encode(), usedforsecurity=False).hexdigest()[:12]
    return f"{row['id']}-{stamp}"


def get(row: dict[str, Any]) -> tuple[bytes, str]:
    """Return (bytes, mime) of a viewable version of the document's file."""
    base = _cache_name(row)
    if row["source"] == "demo":
        from . import render_demo

        PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        path = PREVIEW_DIR / f"demo-{base}.jpg"
        if not path.exists():
            path.write_bytes(render_demo.render(row, settings.get("company_name"), settings.get("company_address")))
        return path.read_bytes(), "image/jpeg"
    if not row.get("source_ref"):
        raise FileNotFoundError("This document has no file behind it.")
    keep = enabled()
    if keep:
        for p in PREVIEW_DIR.glob(f"{base}.*"):
            mime = next((m for m, e in EXT.items() if e == p.suffix[1:]), "application/octet-stream")
            return p.read_bytes(), mime

    src = db.get_source(row["source_ref"])
    if src is None:
        raise FileNotFoundError("The source for this document has been removed.")
    source = open_source(src)
    meta = FileMeta(source=row["source"], source_ref=row["source_ref"], source_id=row["source_id"], name=row["name"], mime=row["mime"],
                    folder=row["folder"], web_link=row["web_link"], size=row["size"], modified_at=row["modified_at"])
    content = textract.fetch_part(source, meta)  # a sheet document previews as that sheet's text
    data, mime = to_viewable(content)
    if keep:
        PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        (PREVIEW_DIR / f"{base}.{EXT[mime]}").write_bytes(data)
    return data, mime


def to_viewable(content: FileContent) -> tuple[bytes, str]:
    mime = content.mime
    if content.text is not None:
        return content.text.encode("utf-8"), "text/plain"
    if mime == "application/pdf":
        return content.data or b"", "application/pdf"
    if mime.startswith("image/"):
        return images.normalize(content.data or b"", mime)
    if mime.endswith("wordprocessingml.document") or mime.endswith("spreadsheetml.sheet"):
        from .extract import _office_to_text

        return (_office_to_text(content) or "").encode("utf-8"), "text/plain"
    raise FileNotFoundError(f"No preview available for {mime}.")


def _cached_files() -> list[Path]:
    """Copies of real documents (demo pages excluded)."""
    if not PREVIEW_DIR.exists():
        return []
    return [p for p in PREVIEW_DIR.iterdir() if p.is_file() and not p.name.startswith("demo-")]


def stats() -> dict[str, Any]:
    files = _cached_files()
    return {"enabled": enabled(), "files": len(files), "bytes": sum(p.stat().st_size for p in files)}


def clear() -> int:
    """Delete every stored copy of a real document. Returns how many were removed."""
    n = 0
    for p in _cached_files():
        p.unlink(missing_ok=True)
        n += 1
    return n
