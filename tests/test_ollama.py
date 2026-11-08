"""Ollama provider: health check, text extraction before the model, request shape, JSON retry,
unreachable server, Claude fallback, truncation, Ask, and a sync end to end."""
from __future__ import annotations

import io
import json

import pytest

from app import ask, config, extract, llm, ollama, settings, sync, textract
from app.sources import FileContent, FileMeta

from conftest import jpeg_bytes, wait_sync
from fake_ollama import CANNED, FakeOllama, install_fake_ollama, pdf_with_text

INVOICE_TEXT = ("VISTA ROOFING LIMITED, Unit 77 Park West, Dublin 12. INVOICE INV-2026-014 Date 2026-03-04 Due 2026-04-03. "
                "Bill to John Murphy, 12 Oak Drive. Re-roof rear extension. Net 4200.00 VAT 567.00 TOTAL 4767.00. Unpaid.")
RECEIPT_TEXT = "CHADWICKS Cork 14/02/2026 Roof battens 86.00 Slate nails 12.50 Subtotal 98.50 VAT 22.66 TOTAL 121.16 VISA PAID"


def _meta(name: str, mime: str) -> FileMeta:
    return FileMeta("local", 1, f"x:{name}", name, mime, "Docs", "", 100, "2026-03-04T00:00:00+00:00")


def test_health_reports_version_models_and_missing_model(monkeypatch):
    fake = FakeOllama(models=("qwen2.5:3b", "llama3.2:1b"))
    install_fake_ollama(monkeypatch, fake)
    fake.answers.append("OK")
    h = llm.health()
    assert h["ok"] and h["version"] == "0.40.14" and h["models"] == ["qwen2.5:3b", "llama3.2:1b"] and h["probe"] == "OK"
    assert fake.requests[-1]["options"]["num_predict"] == 5  # the probe is tiny
    monkeypatch.setattr(config, "OLLAMA_MODEL", "mistral:7b")
    h = ollama.health()
    assert h["ok"] is False and "ollama pull mistral:7b" in h["message"] and "not installed" in h["message"]
    fake.down = True
    h = ollama.health()
    assert h["ok"] is False and "not reachable" in h["message"] and config.OLLAMA_BASE_URL in h["message"]


def test_text_invoice_is_classified_with_the_required_request_shape(monkeypatch, fresh_db):
    fake = FakeOllama()
    install_fake_ollama(monkeypatch, fake)
    settings.set_many({"company_name": "Vista Roofing Limited"})
    fields, model = extract.extract(_meta("INV-2026-014.txt", "text/plain"), FileContent(mime="text/plain", text=INVOICE_TEXT))
    assert model == "ollama/qwen2.5:3b"
    assert fields["doc_type"] == "sales_invoice" and fields["direction"] == "money_in" and fields["treatment"] == "revenue"
    assert fields["total"] == 4767.0 and fields["net_amount"] == 4200.0 and fields["counterparty"] == "John Murphy"
    assert fields["counterparty_role"] == "customer" and fields["category"] == "roof_repair" and fields["confidence"] == 0.9
    assert fields["summary"].startswith("Invoice issued") and fields["flags"] == ["text_only_model"] and fields["line_items"] == []
    assert fields["raw"]["attempts"] == 1
    req = fake.requests[-1]
    assert req["model"] == "qwen2.5:3b" and req["stream"] is False and req["keep_alive"] == "30m"
    assert req["options"]["temperature"] == 0 and req["options"]["num_ctx"] >= 8192
    assert req["format"]["type"] == "object" and req["format"]["properties"]["category"]["enum"] == extract.DOC_TYPES
    system, user = req["messages"][0]["content"], req["messages"][1]["content"]
    assert req["messages"][0]["role"] == "system" and "sales_invoice" in system and "Vista Roofing Limited" in system
    assert "<document_text>" in user and INVOICE_TEXT in user and "INV-2026-014.txt" in user
    assert len(system) < 3500  # the small-model prompt stays short


def test_small_model_numbers_and_enums_are_normalised(monkeypatch, fresh_db):
    """Strings like "€121.16", capitalised enums and a missing confidence are tolerated, not retried."""
    fake = FakeOllama()
    install_fake_ollama(monkeypatch, fake)
    fake.answers.append(json.dumps({**CANNED["receipt"], "category": "Receipt", "direction": "Money Out", "payment_status": "PAID",
                                    "confidence": "high", "cost_category": "Materials roofing"}))
    fields, _ = extract.extract(_meta("till.txt", "text/plain"), FileContent(mime="text/plain", text=RECEIPT_TEXT))
    assert fields["doc_type"] == "receipt" and fields["direction"] == "money_out" and fields["treatment"] == "expense"
    assert fields["total"] == 121.16 and fields["subtotal"] == 98.5 and fields["vat_amount"] == 22.66 and fields["amount_paid"] == 121.16
    assert fields["payment_status"] == "paid" and fields["category"] == "materials_roofing" and fields["confidence"] == 0.5
    assert fields["counterparty_role"] == "supplier" and len(fake.requests) == 1


def test_pdf_excel_and_word_are_turned_into_text_before_the_model(monkeypatch, fresh_db):
    fake = FakeOllama()
    install_fake_ollama(monkeypatch, fake)
    # PDF: the text layer goes to the model, never the bytes
    pdf = pdf_with_text("INVOICE INV-77 Chadwicks total 123.00")
    fields, _ = extract.extract(_meta("inv77.pdf", "application/pdf"), FileContent(mime="application/pdf", data=pdf))
    user = fake.requests[-1]["messages"][1]["content"]
    assert "INVOICE INV-77 Chadwicks total 123.00" in user and "%PDF" not in user and "JVBER" not in user
    assert fields["doc_type"] == "sales_invoice"
    # Excel: rows joined with " | "
    import openpyxl
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Statement"
    ws.append(["Date", "Description", "Amount"]); ws.append(["2026-03-01", "AIB statement line", 100.5])
    buf = io.BytesIO(); wb.save(buf)
    xlsx_mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    fields, _ = extract.extract(_meta("march.xlsx", xlsx_mime), FileContent(mime=xlsx_mime, data=buf.getvalue()))
    user = fake.requests[-1]["messages"][1]["content"]
    assert "## Sheet: Statement" in user and "2026-03-01 | AIB statement line | 100.5" in user and fields["doc_type"] == "bank_statement"
    # Word
    import docx
    d = docx.Document(); d.add_paragraph("Receipt from Screwfix total 45.00 paid"); buf = io.BytesIO(); d.save(buf)
    docx_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    fields, _ = extract.extract(_meta("r.docx", docx_mime), FileContent(mime=docx_mime, data=buf.getvalue()))
    assert "Receipt from Screwfix" in fake.requests[-1]["messages"][1]["content"] and fields["doc_type"] == "receipt"


def test_photos_and_scanned_pdfs_are_skipped_without_fallback(monkeypatch, fresh_db):
    fake = FakeOllama()
    install_fake_ollama(monkeypatch, fake)
    with pytest.raises(ValueError, match="photo / image file.*fallback reader"):
        extract.extract(_meta("till.jpg", "image/jpeg"), FileContent(mime="image/jpeg", data=jpeg_bytes()))
    with pytest.raises(ValueError, match="scanned PDF without a text layer"):
        extract.extract(_meta("scan.pdf", "application/pdf"), FileContent(mime="application/pdf", data=pdf_with_text(" ")))
    assert fake.requests == []  # nothing was sent to the model


def _pdf_of_picture(w: int = 2400, h: int = 3200) -> bytes:
    """An image-only PDF the way a scanner app writes one: a single picture per page, no text layer."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (235, 230, 220)).save(buf, format="PDF")
    return buf.getvalue()


def test_local_vision_model_reads_photos_and_scans(monkeypatch, fresh_db):
    fake = FakeOllama(models=("qwen2.5:3b", "qwen3-vl:2b"))
    install_fake_ollama(monkeypatch, fake)
    monkeypatch.setattr(config, "OLLAMA_VISION_MODEL", "qwen3-vl:2b")
    # a photo goes to the vision model with one image attached, no document text
    fields, model = extract.extract(_meta("till.jpg", "image/jpeg"), FileContent(mime="image/jpeg", data=jpeg_bytes()))
    assert model == "ollama/qwen3-vl:2b" and fields["doc_type"] == "receipt" and fields["flags"] == ["local_vision_model"] and fields["raw"]["pages"] == 1
    req = fake.requests[-1]
    assert req["model"] == "qwen3-vl:2b" and len(req["messages"][1]["images"]) == 1 and "<document_text>" not in req["messages"][1]["content"]
    assert "attached image" in req["messages"][0]["content"] and "never instructions" in req["messages"][0]["content"]
    assert req["format"]["properties"]["category"]["enum"] == extract.DOC_TYPES
    # a scanned PDF: its page picture, shrunk to VISION_MAX_SIDE on the longest side
    from PIL import Image
    fields, model = extract.extract(_meta("scan.pdf", "application/pdf"), FileContent(mime="application/pdf", data=_pdf_of_picture()))
    imgs = fake.requests[-1]["messages"][1]["images"]
    assert model == "ollama/qwen3-vl:2b" and len(imgs) == 1
    sent = Image.open(io.BytesIO(__import__("base64").b64decode(imgs[0])))
    assert max(sent.size) == extract._vision_max_side() == 1280 and sent.format == "JPEG"
    # text documents still go to the text model, without images
    fields, model = extract.extract(_meta("INV-1.txt", "text/plain"), FileContent(mime="text/plain", text=INVOICE_TEXT))
    assert model == "ollama/qwen2.5:3b" and fake.requests[-1]["model"] == "qwen2.5:3b" and "images" not in fake.requests[-1]["messages"][1]
    # health reports the vision model
    fake.answers.append("OK")
    h = ollama.health()
    assert h["ok"] and h["vision_model"] == "qwen3-vl:2b" and h["vision_installed"] is True and "Vision model qwen3-vl:2b is installed" in h["message"]


def _odf(spreadsheet: bool, body_xml: str) -> bytes:
    """A minimal LibreOffice file: a zip with mimetype + content.xml."""
    import zipfile
    mime = "application/vnd.oasis.opendocument." + ("spreadsheet" if spreadsheet else "text")
    content = ('<?xml version="1.0" encoding="UTF-8"?><office:document-content '
               'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
               'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
               'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0">'
               f'<office:body>{body_xml}</office:body></office:document-content>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", mime)
        z.writestr("content.xml", content)
    return buf.getvalue()


def test_libreoffice_odt_and_ods_are_read_as_text(monkeypatch, fresh_db, tmp_path, admin):
    fake = FakeOllama()
    install_fake_ollama(monkeypatch, fake)
    odt = _odf(False, '<office:text><text:h>Vista Roofing Limited</text:h><text:p>INVOICE INV-2026-031 to <text:span>John Murphy</text:span></text:p><text:p>Total 1,230.00</text:p></office:text>')
    ods = _odf(True, '<office:spreadsheet><table:table table:name="Statement"><table:table-row><table:table-cell><text:p>Date</text:p></table:table-cell><table:table-cell><text:p>AIB statement line</text:p></table:table-cell></table:table-row>'
                     '<table:table-row><table:table-cell><text:p>2026-03-01</text:p></table:table-cell><table:table-cell><text:p>100.50</text:p></table:table-cell></table:table-row></table:table></office:spreadsheet>')
    from app import sources as src_mod
    assert textract.odf_to_text(odt, False) == "Vista Roofing Limited\nINVOICE INV-2026-031 to John Murphy\nTotal 1,230.00"
    assert textract.odf_to_text(ods, True) == "## Sheet: Statement\nDate | AIB statement line\n2026-03-01 | 100.50"
    assert src_mod.guess_mime("Vista Oct 27- 29 Incl.odt") == src_mod.ODT_MIME and src_mod.ODT_MIME in src_mod.SUPPORTED_MIMES
    fields, model = extract.extract(_meta("Vista Oct 27- 29 Incl.odt", src_mod.ODT_MIME), FileContent(mime=src_mod.ODT_MIME, data=odt))
    assert model == "ollama/qwen2.5:3b" and fields["doc_type"] == "sales_invoice" and "INV-2026-031" in fake.requests[-1]["messages"][1]["content"]
    fields, _ = extract.extract(_meta("march.ods", src_mod.ODS_MIME), FileContent(mime=src_mod.ODS_MIME, data=ods))
    assert fields["doc_type"] == "bank_statement"
    # a file skipped earlier as "unsupported file type" is re-read by the next sync once the type is supported
    folder = tmp_path / "Docs"
    folder.mkdir()
    (folder / "Vista Oct 14 - 16 Incl.odt").write_bytes(odt)
    source = admin.post("/api/admin/sources", json={"kind": "local", "name": "Docs", "local_folder": str(folder)}).json()
    admin.post(f"/api/admin/sources/{source['id']}/sync"); wait_sync()
    doc = admin.get("/api/documents?limit=10").json()[0]
    from app import db
    db.mark_skipped(doc["id"], "unsupported file type (application/vnd.oasis.opendocument.text)")  # how the old version left it
    admin.post(f"/api/admin/sources/{source['id']}/sync"); wait_sync()
    assert admin.get(f"/api/documents/{doc['id']}").json()["status"] == "done"


def test_skipped_photos_are_re_read_once_a_local_vision_model_is_configured(monkeypatch, fresh_db, tmp_path, admin):
    fake = FakeOllama(models=("qwen2.5:3b", "qwen3-vl:2b"))
    install_fake_ollama(monkeypatch, fake)
    folder = tmp_path / "Docs"
    folder.mkdir()
    (folder / "Stefan Baciu Invoice 96.jpg").write_bytes(jpeg_bytes())
    (folder / "INV-1.txt").write_text(INVOICE_TEXT, encoding="utf-8")
    source = admin.post("/api/admin/sources", json={"kind": "local", "name": "Docs", "local_folder": str(folder)}).json()
    admin.post(f"/api/admin/sources/{source['id']}/sync"); wait_sync()
    docs = {d["name"]: d for d in admin.get("/api/documents?limit=10").json()}
    assert docs["Stefan Baciu Invoice 96.jpg"]["status"] == "skipped"  # vision off: skipped with the reason
    monkeypatch.setattr(config, "OLLAMA_VISION_MODEL", "qwen3-vl:2b")  # the admin pulls the model and sets .env
    n = len(fake.requests)
    admin.post(f"/api/admin/sources/{source['id']}/sync"); wait_sync()
    docs = {d["name"]: d for d in admin.get("/api/documents?limit=10").json()}
    assert docs["Stefan Baciu Invoice 96.jpg"]["status"] == "done" and len(fake.requests) == n + 1  # only the photo was re-read
    assert admin.get(f"/api/documents/{docs['Stefan Baciu Invoice 96.jpg']['id']}").json()["model"] == "ollama/qwen3-vl:2b"


def test_vision_model_set_but_not_pulled(monkeypatch, fresh_db):
    fake = FakeOllama(models=("qwen2.5:3b",))
    install_fake_ollama(monkeypatch, fake)
    monkeypatch.setattr(config, "OLLAMA_VISION_MODEL", "qwen3-vl:2b")
    fake.answers.append("OK")
    h = ollama.health()
    assert h["ok"] is True and h["vision_installed"] is False and "ollama pull qwen3-vl:2b" in h["message"]  # text still works
    with pytest.raises(extract.ApiUnavailable, match="ollama pull qwen3-vl:2b"):
        extract.extract(_meta("till.jpg", "image/jpeg"), FileContent(mime="image/jpeg", data=jpeg_bytes()))
    assert isinstance(pytest.raises(ollama.ModelMissing, extract.extract, _meta("till.jpg", "image/jpeg"), FileContent(mime="image/jpeg", data=jpeg_bytes())).value, extract.ApiUnavailable)


def test_fallback_to_claude_for_vision_and_when_ollama_is_down(monkeypatch, fresh_db, fake_claude):
    fake = FakeOllama()
    install_fake_ollama(monkeypatch, fake)
    settings.set_value("llm_fallback", "1")
    # a photo goes to Claude (fake_claude's "default" canned answer is a money_out document)
    fields, model = extract.extract(_meta("till.jpg", "image/jpeg"), FileContent(mime="image/jpeg", data=jpeg_bytes()))
    assert model == "fake-model" and fields["treatment"] == "expense" and len(fake_claude.calls) == 1 and fake.requests == []
    assert fake_claude.calls[0][0]["type"] == "image"  # Claude received the photo itself, not text
    # Ollama asleep: text files go to Claude too, instead of stopping the run
    fake.down = True
    fields, model = extract.extract(_meta("INV-9.txt", "text/plain"), FileContent(mime="text/plain", text=INVOICE_TEXT))
    assert model == "fake-model" and fields["doc_type"] == "sales_invoice" and len(fake_claude.calls) == 2
    # without a Claude key the fallback is silently unavailable: the clear Ollama error comes through
    settings.set_value("anthropic_api_key", "")
    with pytest.raises(extract.ApiUnavailable, match="not reachable"):
        extract.extract(_meta("INV-9.txt", "text/plain"), FileContent(mime="text/plain", text=INVOICE_TEXT))


def test_unreachable_and_missing_model_stop_the_sync_with_one_message(monkeypatch, fresh_db):
    fake = FakeOllama()
    install_fake_ollama(monkeypatch, fake)
    fake.down = True
    with pytest.raises(ollama.Unreachable, match="Is the Ollama PC awake") as info:
        extract.extract(_meta("a.txt", "text/plain"), FileContent(mime="text/plain", text=INVOICE_TEXT))
    assert isinstance(info.value, extract.ApiUnavailable)
    fake.down = False
    monkeypatch.setattr(config, "OLLAMA_MODEL", "gemma:2b")
    with pytest.raises(ollama.ModelMissing, match="ollama pull gemma:2b"):
        extract.extract(_meta("a.txt", "text/plain"), FileContent(mime="text/plain", text=INVOICE_TEXT))


def test_a_slow_file_is_skipped_not_an_outage(monkeypatch, fresh_db):
    """ReadTimeout while the server still answers a probe = this file is too slow for the model: a
    ValueError (skipped), never Unreachable (which would stop the run and retry it every sync)."""
    fake = FakeOllama(models=("qwen2.5:3b", "qwen3-vl:2b"))
    install_fake_ollama(monkeypatch, fake)
    monkeypatch.setattr(config, "OLLAMA_VISION_MODEL", "qwen3-vl:2b")
    fake.slow_models.add("qwen3-vl:2b")
    with pytest.raises(ollama.TooSlow, match=r"reader too slow: Ollama model qwen3-vl:2b did not finish this file within 300 s") as info:
        extract.extract(_meta("handwritten.jpg", "image/jpeg"), FileContent(mime="image/jpeg", data=jpeg_bytes()))
    assert isinstance(info.value, ValueError) and not isinstance(info.value, extract.ApiUnavailable)
    assert fake.timeouts[-1] == config.OLLAMA_VISION_TIMEOUT == 300  # photos get the longer vision timeout
    # text documents keep the normal timeout and still work
    fields, _ = extract.extract(_meta("INV-1.txt", "text/plain"), FileContent(mime="text/plain", text=INVOICE_TEXT))
    assert fields["doc_type"] == "sales_invoice" and fake.timeouts[-1] == config.OLLAMA_TIMEOUT
    # the same timeout when nothing answers at all is a real outage
    fake.hang = True
    with pytest.raises(ollama.Unreachable, match="ReadTimeout"):
        extract.extract(_meta("INV-2.txt", "text/plain"), FileContent(mime="text/plain", text=INVOICE_TEXT))


def test_slow_photos_are_skipped_and_the_sync_carries_on(admin, fresh_db, monkeypatch, tmp_path):
    fake = FakeOllama(models=("qwen2.5:3b", "qwen3-vl:2b"))
    install_fake_ollama(monkeypatch, fake)
    settings.set_value("anthropic_api_key", "")
    monkeypatch.setattr(config, "OLLAMA_VISION_MODEL", "qwen3-vl:2b")
    fake.slow_models.add("qwen3-vl:2b")
    folder = tmp_path / "Docs"
    folder.mkdir()
    (folder / "handwritten.jpg").write_bytes(jpeg_bytes())
    (folder / "INV-1.txt").write_text(INVOICE_TEXT, encoding="utf-8")
    src = admin.post("/api/admin/sources", json={"kind": "local", "name": "Docs", "local_folder": str(folder)}).json()
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    docs = {d["name"]: d for d in admin.get("/api/documents?limit=100").json()}
    assert docs["INV-1.txt"]["status"] == "done"
    assert docs["handwritten.jpg"]["status"] == "skipped" and docs["handwritten.jpg"]["error"].startswith("reader too slow")
    st = sync.status()
    assert st["stop_reason"] is None and st["errors"] == 0  # the run was not aborted
    assert admin.get("/api/admin/sources").json()[0]["last_status"] == "ok"
    n = len(fake.requests)
    # the next sync does not try the slow photo again (a run must not lose minutes to it every time)
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    assert len(fake.requests) == n
    # the owner can still enter it by hand ...
    r = admin.patch(f"/api/documents/{docs['handwritten.jpg']['id']}", json={"doc_type": "receipt", "total": 40})
    assert r.status_code == 200 and r.json()["status"] == "done" and r.json()["model"] == "manual"


def test_slow_photo_goes_to_the_fallback_reader(monkeypatch, fresh_db, fake_claude):
    fake = FakeOllama(models=("qwen2.5:3b", "qwen3-vl:2b"))
    install_fake_ollama(monkeypatch, fake)
    monkeypatch.setattr(config, "OLLAMA_VISION_MODEL", "qwen3-vl:2b")
    settings.set_value("llm_fallback", "claude")
    fake.slow_models.add("qwen3-vl:2b")
    fields, model = extract.extract(_meta("handwritten.jpg", "image/jpeg"), FileContent(mime="image/jpeg", data=jpeg_bytes()))
    assert model == "fake-model" and len(fake_claude.calls) == 1


def test_a_failing_file_is_retried_three_times_then_left_alone(admin, fresh_db, monkeypatch, tmp_path, fake_claude):
    settings.set_value("llm_provider", "claude")
    folder = tmp_path / "Docs"
    folder.mkdir()
    (folder / "INV-9 Murphy.pdf").write_bytes(b"%PDF-1.4 invoice")
    (folder / "INV-1.txt").write_text(INVOICE_TEXT, encoding="utf-8")
    fake_claude.fail_for.add("INV-9 Murphy.pdf")
    src = admin.post("/api/admin/sources", json={"kind": "local", "name": "Docs", "local_folder": str(folder)}).json()
    for i in range(1, 6):
        admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
        d = {x["name"]: x for x in admin.get("/api/documents?limit=100").json()}["INV-9 Murphy.pdf"]
        assert d["status"] == "error"
        if i >= 3:
            assert "gave up after 3 attempts" in d["error"]
    bad_calls = [c for c in fake_claude.calls if "INV-9" in json.dumps(c, default=str)]
    assert len(bad_calls) == 3  # attempts 1-3, then the sync left it alone (runs 4 and 5 did not call the model)
    # Re-read (explicit) and a hand entry still work on it
    fake_claude.fail_for.clear()
    r = admin.post(f"/api/documents/{d['id']}/reprocess")
    assert r.status_code == 200 and r.json()["status"] == "done" and r.json()["attempts"] == 0


def test_rows_left_processing_by_a_restart_go_back_to_pending(fresh_db):
    from app import db
    doc_id = db.upsert_file_metadata({"source": "local", "source_ref": 1, "source_id": "x:1", "name": "a.pdf", "mime": "application/pdf",
                                      "folder": "", "web_link": "", "size": 1, "modified_at": "2026-03-04T00:00:00+00:00"})
    db.set_status(doc_id, "processing")
    assert db.reset_stuck_processing() == 1 and db.get_document(doc_id)["status"] == "pending"
    assert db.reset_stuck_processing() == 0


def test_malformed_json_is_retried_at_most_twice(monkeypatch, fresh_db):
    fake = FakeOllama()
    install_fake_ollama(monkeypatch, fake)
    fake.answers += ["Sure! Here is the JSON: {not json", "```json\n" + json.dumps({**CANNED["sales"], "category": "invoice"}) + "\n```",
                     json.dumps(CANNED["sales"])]
    fields, _ = extract.extract(_meta("INV-1.txt", "text/plain"), FileContent(mime="text/plain", text=INVOICE_TEXT))
    assert fields["doc_type"] == "sales_invoice" and fields["raw"]["attempts"] == 3 and len(fake.requests) == 3
    # the retry carries the bad answer and a correction, so the model sees what was wrong
    third = fake.requests[2]["messages"]
    assert third[-2]["role"] == "assistant" and third[-1]["role"] == "user" and "category must be one of" in third[-1]["content"]
    fake.answers += ["nope", "still nope", "{}"]
    with pytest.raises(RuntimeError, match="did not return a valid answer after 3 attempts"):
        extract.extract(_meta("INV-2.txt", "text/plain"), FileContent(mime="text/plain", text=INVOICE_TEXT))
    assert len(fake.requests) == 6


def test_long_documents_are_cut_to_the_context_budget(monkeypatch, fresh_db):
    fake = FakeOllama()
    install_fake_ollama(monkeypatch, fake)
    long = ("line of a very long invoice " * 20 + "\n") * 400 + "TOTAL 999.00 at the very end"
    assert len(long) > extract.small_text_budget()
    fields, _ = extract.extract(_meta("INV-long.txt", "text/plain"), FileContent(mime="text/plain", text=long))
    user = fake.requests[-1]["messages"][1]["content"]
    assert len(user) < extract.small_text_budget() + 500 and "omitted to fit" in user and "TOTAL 999.00 at the very end" in user
    assert "text_truncated" in fields["flags"]
    cut, truncated = textract.fit("abc", 10)
    assert cut == "abc" and truncated is False


def test_ask_uses_ollama_and_fits_the_rows_to_its_context(admin, fresh_db, monkeypatch):
    fake = FakeOllama()
    install_fake_ollama(monkeypatch, fake)
    admin.post("/api/admin/demo/load")
    fake.answers.append("Revenue in the period was 12,345 EUR across 10 invoices (ids 1, 2, 3).")
    r = admin.post("/api/ask", json={"question": "What was the revenue?"})
    assert r.status_code == 200 and r.json()["answer"].startswith("Revenue in the period")
    req = fake.requests[-1]
    assert req["options"]["temperature"] == 0 and "format" not in req and req["messages"][0]["role"] == "system"
    user = req["messages"][1]["content"]
    assert "<ledger_summary>" in user and "Question: What was the revenue?" in user
    assert len(user) <= (max(config.OLLAMA_NUM_CTX, 8192) - ask.ASK_RESERVE_TOKENS) * extract.CHARS_PER_TOKEN + 2000


def test_sync_end_to_end_with_ollama_and_no_claude_key(admin, fresh_db, monkeypatch, tmp_path):
    fake = FakeOllama()
    install_fake_ollama(monkeypatch, fake)
    folder = tmp_path / "Docs"
    folder.mkdir()
    (folder / "INV-2026-014.txt").write_text(INVOICE_TEXT, encoding="utf-8")
    (folder / "chadwicks.pdf").write_bytes(pdf_with_text("Chadwicks till receipt total 121.16 PAID"))
    (folder / "photo.jpg").write_bytes(jpeg_bytes())
    cfg = admin.get("/api/config").json()
    assert cfg["llm_provider"] == "ollama" and cfg["llm_ready"] is True and cfg["has_api_key"] is False and cfg["llm_model"] == "qwen2.5:3b"
    s = admin.get("/api/admin/overview").json()["settings"]
    assert s["llm_ready"] is True and s["llm_url"] == config.OLLAMA_BASE_URL
    src = admin.post("/api/admin/sources", json={"kind": "local", "name": "Docs", "local_folder": str(folder)}).json()
    assert admin.post(f"/api/admin/sources/{src['id']}/sync").status_code == 200  # no Claude key needed
    wait_sync()
    docs = {d["name"]: d for d in admin.get("/api/documents?limit=100").json()}
    assert docs["INV-2026-014.txt"]["status"] == "done"
    assert admin.get(f"/api/documents/{docs['INV-2026-014.txt']['id']}").json()["model"] == "ollama/qwen2.5:3b"
    assert docs["chadwicks.pdf"]["status"] == "done" and docs["chadwicks.pdf"]["doc_type"] == "receipt"
    assert docs["photo.jpg"]["status"] == "skipped" and "photo" in docs["photo.jpg"]["error"] and "fallback reader" in docs["photo.jpg"]["error"]
    assert sync.status()["errors"] == 0
    # the Admin "Check Ollama" button
    fake.answers.append("OK")
    r = admin.post("/api/admin/llm-check").json()
    assert r["ok"] and r["provider"] == "ollama" and r["models"] == ["qwen2.5:3b"]
    # Ollama goes to sleep: the next run stops with one clear message, no error rows
    fake.down = True
    (folder / "INV-2.txt").write_text(INVOICE_TEXT, encoding="utf-8")
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    assert "not reachable" in sync.status()["message"] and admin.get("/api/admin/sources").json()[0]["last_status"] == "failed"
    assert all(d["status"] != "error" for d in admin.get("/api/documents?limit=100").json())


def test_skipped_photos_are_read_by_the_next_sync_once_a_vision_reader_exists(admin, fresh_db, monkeypatch, tmp_path, fake_claude):
    fake = FakeOllama()
    install_fake_ollama(monkeypatch, fake)
    settings.set_value("anthropic_api_key", "")  # Ollama only, no fallback yet
    folder = tmp_path / "Docs"
    folder.mkdir()
    (folder / "INV-1.txt").write_text(INVOICE_TEXT, encoding="utf-8")
    (folder / "photo.jpg").write_bytes(jpeg_bytes())
    src = admin.post("/api/admin/sources", json={"kind": "local", "name": "Docs", "local_folder": str(folder)}).json()
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    docs = {d["name"]: d for d in admin.get("/api/documents?limit=100").json()}
    assert docs["photo.jpg"]["status"] == "skipped" and docs["photo.jpg"]["error"].startswith("needs a vision reader")
    # a plain sync changes nothing while no vision reader exists
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    assert {d["name"]: d["status"] for d in admin.get("/api/documents?limit=100").json()}["photo.jpg"] == "skipped" and len(fake.requests) == 1
    # the admin names Claude as the fallback: the next ordinary sync reads just the photo
    from conftest import FAKE_KEY
    settings.set_many({"anthropic_api_key": FAKE_KEY, "llm_fallback": "claude"})
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    docs = {d["name"]: d for d in admin.get("/api/documents?limit=100").json()}
    assert docs["photo.jpg"]["status"] == "done" and len(fake_claude.calls) == 1 and len(fake.requests) == 1  # the text file was not re-read
    assert admin.get(f"/api/documents/{docs['photo.jpg']['id']}").json()["model"] == "fake-model"


def test_a_skipped_handwritten_file_can_be_entered_by_hand(admin, fresh_db, monkeypatch, tmp_path):
    """A photo Ollama cannot read: the owner types the figures in. It becomes a done, hand-entered
    document that counts in the dashboard and is left alone by every later sync."""
    fake = FakeOllama()
    install_fake_ollama(monkeypatch, fake)
    settings.set_value("anthropic_api_key", "")
    folder = tmp_path / "Docs"
    folder.mkdir()
    (folder / "handwritten receipt.jpg").write_bytes(jpeg_bytes())
    (folder / "site note.jpg").write_bytes(jpeg_bytes())
    src = admin.post("/api/admin/sources", json={"kind": "local", "name": "Docs", "local_folder": str(folder)}).json()
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    docs = {d["name"]: d for d in admin.get("/api/documents?status=skipped").json()}
    assert set(docs) == {"handwritten receipt.jpg", "site note.jpg"}
    rid = docs["handwritten receipt.jpg"]["id"]
    before = admin.get("/api/summary").json()["totals"]

    # the type is required: it decides how the figures are counted
    r = admin.patch(f"/api/documents/{rid}", json={"total": 121.16})
    assert r.status_code == 400 and "document type" in r.json()["detail"]
    # the usual direction / currency / payment status are filled in when left blank
    r = admin.patch(f"/api/documents/{rid}", json={"doc_type": "receipt", "counterparty": "Chadwicks", "issue_date": "2026-02-14", "total": 121.16, "vat_amount": 22.66})
    d = r.json()
    assert r.status_code == 200 and d["status"] == "done" and d["overridden"] == 1 and d["error"] is None
    assert d["model"] == "manual" and d["direction"] == "money_out" and d["treatment"] == "expense" and d["currency"] == "EUR"
    assert d["payment_status"] == "unknown" and d["subtotal"] == 98.5 and d["net_amount"] == 98.5 and d["confidence"] == 1.0 and d["flags"] == []
    assert admin.get("/api/summary").json()["totals"]["expenses"] == pytest.approx(before["expenses"] + 98.5)
    assert sync.status()["read_by"] == {"manual": 1}
    listed = {x["name"]: x for x in admin.get("/api/documents").json()}
    assert listed["handwritten receipt.jpg"]["model"] == "manual" and listed["handwritten receipt.jpg"]["status"] == "done"

    # a site photo is "not a financial document": done, counts nothing, no longer skipped
    nid = docs["site note.jpg"]["id"]
    d = admin.patch(f"/api/documents/{nid}", json={"doc_type": "other", "direction": "none", "payment_status": "not_applicable"}).json()
    assert d["status"] == "done" and d["treatment"] == "none" and d["net_amount"] is None
    assert admin.get("/api/documents?status=skipped").json() == []

    # later syncs, even a full re-read, leave hand-entered figures alone
    admin.post(f"/api/admin/sources/{src['id']}/sync?force=1"); wait_sync()
    d = admin.get(f"/api/documents/{rid}").json()
    assert d["status"] == "done" and d["total"] == 121.16 and d["model"] == "manual" and fake.requests == []
    # a correction of a document a model read keeps the model's name (it is an edit, not a hand entry)
    (folder / "INV-1.txt").write_text(INVOICE_TEXT, encoding="utf-8")
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    inv = {x["name"]: x for x in admin.get("/api/documents").json()}["INV-1.txt"]
    d = admin.patch(f"/api/documents/{inv['id']}", json={"project": "Oak Drive"}).json()
    assert d["model"] == inv["model"] != "manual" and d["project"] == "Oak Drive" and d["overridden"] == 1


def test_claude_provider_still_works_and_needs_a_key(admin, fresh_db, monkeypatch, fake_claude):
    settings.set_value("llm_provider", "claude")
    cfg = admin.get("/api/config").json()
    assert cfg["llm_provider"] == "claude" and cfg["llm_ready"] is True
    fields, model = extract.extract(_meta("INV-1.txt", "text/plain"), FileContent(mime="text/plain", text=INVOICE_TEXT))
    assert model == "fake-model" and fields["doc_type"] == "sales_invoice"
    settings.set_value("anthropic_api_key", "")
    assert admin.get("/api/config").json()["llm_ready"] is False
    assert admin.post("/api/sync").status_code == 400


def test_provider_can_be_switched_in_admin_and_status_follows(admin, fresh_db, monkeypatch, fake_claude):
    fake = FakeOllama()
    install_fake_ollama(monkeypatch, fake)
    assert admin.put("/api/admin/settings", json={"llm_provider": "gpt"}).status_code == 400
    r = admin.put("/api/admin/settings", json={"llm_provider": "claude", "llm_fallback": True}).json()
    assert r["llm_provider"] == "claude" and r["llm_fallback"] == "claude" and settings.llm_provider() == "claude"
    fields, model = extract.extract(_meta("INV-1.txt", "text/plain"), FileContent(mime="text/plain", text=INVOICE_TEXT))
    assert model == "fake-model" and fake.requests == []  # Claude read it
    st = llm.refresh_status()
    assert st["state"] == "ready" and st["provider"] == "claude" and st["label"] == "Claude ready"
    r = admin.put("/api/admin/settings", json={"llm_provider": "ollama"}).json()
    assert r["llm_provider"] == "ollama" and r["llm_fallback_usable"] is True  # fallback claude + key saved
    assert [p["id"] for p in r["providers"]] == ["ollama", "claude", "gemini"] and r["providers"][2]["ready"] is False
    fields, model = extract.extract(_meta("INV-2.txt", "text/plain"), FileContent(mime="text/plain", text=INVOICE_TEXT))
    assert model == "ollama/qwen2.5:3b" and len(fake.requests) == 1


def test_live_status_connected_unreachable_and_model_missing(admin, fresh_db, monkeypatch):
    fake = FakeOllama()
    install_fake_ollama(monkeypatch, fake)
    st = llm.refresh_status()
    assert st["state"] == "connected" and st["ok"] and st["version"] == "0.40.14" and st["checked_at"] and st["probe"] is None
    assert fake.requests == []  # the periodic check never calls the model
    v = admin.get("/api/version").json()
    assert v["llm"]["state"] == "connected" and v["llm"]["label"] == "Ollama connected" and v["llm"]["model"] == "qwen2.5:3b"
    assert admin.get("/api/config").json()["llm_status"]["state"] == "connected"
    fake.down = True
    st = llm.refresh_status()
    assert st["state"] == "unreachable" and st["ok"] is False and "Is the Ollama PC awake" in st["message"]
    assert admin.get("/api/sync/status").json()["llm"]["label"] == "Ollama unreachable"
    fake.down = False
    monkeypatch.setattr(config, "OLLAMA_MODEL", "phi3:mini")
    st = llm.refresh_status()
    assert st["state"] == "model_missing" and "ollama pull phi3:mini" in st["message"]
    # the Admin "Check now" button: same check plus a probe, and it updates the stored status
    monkeypatch.setattr(config, "OLLAMA_MODEL", "qwen2.5:3b")
    fake.answers.append("OK")
    r = admin.post("/api/admin/llm-check").json()
    assert r["state"] == "connected" and r["probe"] == "OK" and llm.status()["state"] == "connected"


def test_read_by_counts_show_which_model_produced_the_figures(admin, fresh_db, monkeypatch, tmp_path):
    fake = FakeOllama()
    install_fake_ollama(monkeypatch, fake)
    folder = tmp_path / "Docs"
    folder.mkdir()
    for i in range(3):
        (folder / f"INV-{i}.txt").write_text(INVOICE_TEXT, encoding="utf-8")
    src = admin.post("/api/admin/sources", json={"kind": "local", "name": "Docs", "local_folder": str(folder)}).json()
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    st = admin.get("/api/sync/status").json()
    assert st["read_by"] == {"ollama/qwen2.5:3b": 3}
    assert admin.get("/api/summary").json()["totals"]["revenue"] == 3 * 4200.0  # the figures, not the files, are in the ledger
