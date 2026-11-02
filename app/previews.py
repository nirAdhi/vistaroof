"""Cached, browser-viewable copies of source files so the owner can see the actual invoice.

``get(row)`` returns ``(path, mime)`` of a file the browser can display: PDFs as-is, photos
normalised to JPEG/PNG (HEIC converted, huge scans downscaled), text/CSV as text, Word/Excel as
extracted text, demo documents as generated sample pages. Copies live in ``<DATA_DIR>/previews`` and
are named ``<id>-<hash of modified_at>`` so a changed source file gets a fresh copy. The cache can
be deleted at any time; it is rebuilt on demand.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from . import config, db, images
from .sources import FileContent, FileMeta, open_source

PREVIEW_DIR = config.DATA_DIR / "previews"
EXT = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp", "text/plain": "txt"}


def _cache_name(row: dict[str, Any]) -> str:
    # Cache-busting file name only (not a security hash): changes whenever the source file changes.
    stamp = hashlib.sha256(f"{row['id']}:{row.get('modified_at')}".encode(), usedforsecurity=False).hexdigest()[:12]
    return f"{row['id']}-{stamp}"


def get(row: dict[str, Any]) -> tuple[Path, str]:
    """Return (path, mime) of a viewable copy, building and caching it on first use."""
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    base = _cache_name(row)
    if row["source"] == "demo":
        from . import render_demo, settings

        path = PREVIEW_DIR / f"demo-{base}.jpg"
        if not path.exists():
            path.write_bytes(render_demo.render(row, settings.get("company_name"), settings.get("company_address")))
        return path, "image/jpeg"
    if not row.get("source_ref"):
        raise FileNotFoundError("This document has no file behind it.")
    for p in PREVIEW_DIR.glob(f"{base}.*"):
        mime = next((m for m, e in EXT.items() if e == p.suffix[1:]), "application/octet-stream")
        return p, mime

    src = db.get_source(row["source_ref"])
    if src is None:
        raise FileNotFoundError("The source for this document has been removed.")
    source = open_source(src)
    meta = FileMeta(source=row["source"], source_ref=row["source_ref"], source_id=row["source_id"], name=row["name"], mime=row["mime"],
                    folder=row["folder"], web_link=row["web_link"], size=row["size"], modified_at=row["modified_at"])
    content = source.fetch(meta)
    data, mime = to_viewable(content)
    path = PREVIEW_DIR / f"{base}.{EXT[mime]}"
    path.write_bytes(data)
    return path, mime


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
