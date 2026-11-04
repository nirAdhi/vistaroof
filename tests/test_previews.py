"""Previews of the actual files, image normalisation (HEIC / huge photos) and the companies directory."""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from app import images, sync

from conftest import wait_sync


def _png(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 120, 40)).save(buf, format="PNG")
    return buf.getvalue()


def _heic(w: int, h: int) -> bytes:
    import pillow_heif

    pillow_heif.register_heif_opener()
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 90, 200)).save(buf, format="HEIF")
    return buf.getvalue()


def test_normalize_passes_small_jpeg_and_png_through():
    data = _png(300, 200)
    out, mime = images.normalize(data, "image/png")
    assert out == data and mime == "image/png"


def test_normalize_converts_heic_and_downscales_huge_photos():
    out, mime = images.normalize(_heic(640, 480), "image/heic")
    assert mime == "image/jpeg" and Image.open(io.BytesIO(out)).size == (640, 480)
    out, mime = images.normalize(_png(9000, 300), "image/png")
    img = Image.open(io.BytesIO(out))
    assert mime == "image/jpeg" and max(img.size) <= images.MAX_SIDE and len(out) <= images.MAX_BYTES


def test_preview_endpoint_serves_the_real_file(admin, fresh_db, fake_claude, tmp_path):
    folder: Path = tmp_path / "Docs"
    folder.mkdir()
    (folder / "INV-1.pdf").write_bytes(b"%PDF-1.4 the real invoice")
    (folder / "till receipt.heic").write_bytes(_heic(320, 240))
    (folder / "note.txt").write_text("handwritten note typed up", encoding="utf-8")
    import docx
    d = docx.Document(); d.add_paragraph("Word bill from Chadwicks"); d.save(folder / "bill.docx")

    src = admin.post("/api/admin/sources", json={"kind": "local", "name": "Docs", "local_folder": str(folder)}).json()
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    docs = {d["name"]: d for d in admin.get("/api/documents").json()}
    assert docs["till receipt.heic"]["status"] == "done"  # HEIC photos are read, not skipped
    # the model received the HEIC as a JPEG image block
    heic_call = next(c for c in fake_claude.calls if "till receipt.heic" in c[-1]["text"])
    assert heic_call[0]["type"] == "image" and heic_call[0]["source"]["media_type"] == "image/jpeg"

    r = admin.get(f"/api/documents/{docs['INV-1.pdf']['id']}/preview")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content == b"%PDF-1.4 the real invoice"
    assert 'inline; filename="INV-1.pdf"' == r.headers["content-disposition"]
    r = admin.get(f"/api/documents/{docs['till receipt.heic']['id']}/preview")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and Image.open(io.BytesIO(r.content)).size == (320, 240)
    r = admin.get(f"/api/documents/{docs['till receipt.heic']['id']}/preview?embed=1")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html") and "<img" in r.text
    r = admin.get(f"/api/documents/{docs['INV-1.pdf']['id']}/preview?embed=1")
    assert r.headers["content-type"] == "application/pdf"  # PDFs are shown by the browser directly
    r = admin.get(f"/api/documents/{docs['note.txt']['id']}/preview")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain") and "handwritten" in r.text
    r = admin.get(f"/api/documents/{docs['bill.docx']['id']}/preview")
    assert r.status_code == 200 and "Word bill" in r.text

    assert admin.get("/api/documents/999999/preview").status_code == 404

    # Default: nothing is kept on the server, every preview is fetched from the source again
    from app import previews

    assert previews.stats() == {"enabled": False, "files": 0, "bytes": 0}, previews.stats()
    (folder / "INV-1.pdf").rename(folder / "INV-1.moved")
    assert admin.get(f"/api/documents/{docs['INV-1.pdf']['id']}/preview").status_code in (404, 502)  # file gone at the source
    (folder / "INV-1.moved").rename(folder / "INV-1.pdf")

    # Opt in: copies are kept, so the preview survives the source going away; turning it off deletes them
    assert admin.put("/api/admin/settings", json={"cache_previews": True}).json()["cache_previews"] == "1"
    assert admin.get(f"/api/documents/{docs['INV-1.pdf']['id']}/preview").status_code == 200
    assert previews.stats()["enabled"] and previews.stats()["files"] == 1
    (folder / "INV-1.pdf").unlink()
    assert admin.get(f"/api/documents/{docs['INV-1.pdf']['id']}/preview").content == b"%PDF-1.4 the real invoice"
    r = admin.post("/api/admin/previews/clear").json()
    assert r["removed"] == 1 and r["previews"]["files"] == 0
    assert admin.get(f"/api/documents/{docs['INV-1.pdf']['id']}/preview").status_code in (404, 502)  # gone from both places now
    admin.put("/api/admin/settings", json={"cache_previews": False})
    assert admin.get("/api/admin/overview").json()["previews"] == {"enabled": False, "files": 0, "bytes": 0}


def test_preview_for_demo_documents_renders_sample_pages(admin, fresh_db):
    admin.post("/api/admin/demo/load")
    docs = admin.get("/api/documents?limit=5000").json()
    for kind in ("sales_invoice", "receipt", "payslip", "bank_statement", "quote_estimate", "insurance"):
        doc = next(d for d in docs if d["doc_type"] == kind)
        r = admin.get(f"/api/documents/{doc['id']}/preview")
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg", kind
        img = Image.open(io.BytesIO(r.content))
        assert img.size[0] > 400 and img.size[1] > 400
    # a handwritten private-client receipt renders too
    priv = [d for d in docs if d["doc_type"] == "sales_invoice" and (d["counterparty"] or "").startswith("Private client")]
    assert priv and admin.get(f"/api/documents/{priv[0]['id']}/preview").status_code == 200


def test_companies_directory(admin, fresh_db):
    admin.post("/api/admin/demo/load")
    cos = admin.get("/api/companies").json()
    by_name = {c["name"]: c for c in cos}
    assert "Chadwicks" in by_name and by_name["Chadwicks"]["role"] == "supplier" and by_name["Chadwicks"]["expenses"] > 0
    cust = next(c for c in cos if c["role"] == "customer")
    assert cust["revenue"] > 0 and cust["docs"] > 0 and cust["first_date"] <= cust["last_date"]
    assert any(c["outstanding"] > 0 for c in cos)
    # the company's documents can be listed and each one has a counterparty match
    docs = admin.get(f"/api/documents?counterparty=Chadwicks&limit=5000").json()
    assert docs and all(d["counterparty"] == "Chadwicks" for d in docs)
