"""Normalise photos (HEIC from phones, huge scans, TIFF/BMP) into something Claude and browsers accept."""
from __future__ import annotations

import io

MAX_SIDE = 4000           # px; Claude accepts up to 8000 but 4000 keeps receipts readable and cheap
MAX_BYTES = 4_500_000     # Claude's per-image limit is 5 MB
PASSTHROUGH = {"image/png", "image/jpeg", "image/gif", "image/webp"}
CONVERT = {"image/heic", "image/heif", "image/tiff", "image/bmp", "image/x-ms-bmp"}


def _open(data: bytes):
    from PIL import Image

    try:
        import pillow_heif  # noqa: F401  (registers HEIC/HEIF support when installed)
        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


def normalize(data: bytes, mime: str) -> tuple[bytes, str]:
    """Return (bytes, mime) that is a JPEG/PNG/GIF/WebP under the size limit, re-encoding only when needed."""
    from PIL import ImageOps

    img = _open(data)
    if mime in PASSTHROUGH and len(data) <= MAX_BYTES and max(img.size) <= MAX_SIDE:
        return data, mime
    img = ImageOps.exif_transpose(img)  # phones store rotation in EXIF; apply it so the receipt is upright
    if max(img.size) > MAX_SIDE:
        img.thumbnail((MAX_SIDE, MAX_SIDE))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    for quality in (88, 80, 70, 60):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        if buf.tell() <= MAX_BYTES:
            break
    return buf.getvalue(), "image/jpeg"
