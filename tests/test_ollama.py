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
    with pytest.raises(ValueError, match="photo / image file.*LLM_FALLBACK"):
        extract.extract(_meta("till.jpg", "image/jpeg"), FileContent(mime="image/jpeg", data=jpeg_bytes()))
    with pytest.raises(ValueError, match="scanned PDF without a text layer"):
        extract.extract(_meta("scan.pdf", "application/pdf"), FileContent(mime="application/pdf", data=pdf_with_text(" ")))
    assert fake.requests == []  # nothing was sent to the model


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
    assert docs["photo.jpg"]["status"] == "skipped" and "photo" in docs["photo.jpg"]["error"] and "LLM_FALLBACK" in docs["photo.jpg"]["error"]
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
    assert r["llm_provider"] == "claude" and r["llm_fallback"] is True and settings.llm_provider() == "claude"
    fields, model = extract.extract(_meta("INV-1.txt", "text/plain"), FileContent(mime="text/plain", text=INVOICE_TEXT))
    assert model == "fake-model" and fake.requests == []  # Claude read it
    st = llm.refresh_status()
    assert st["state"] == "ready" and st["provider"] == "claude" and st["label"] == "Claude ready"
    r = admin.put("/api/admin/settings", json={"llm_provider": "ollama"}).json()
    assert r["llm_provider"] == "ollama" and r["llm_fallback_usable"] is True  # fallback on + key saved
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
