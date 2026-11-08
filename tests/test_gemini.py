"""Gemini provider: schema conversion, key test, request shape (inline PDF / image, header key, JSON
schema), fatal errors stopping the run, retries, fallbacks between all three readers, Ask, secrets."""
from __future__ import annotations

import json

import pytest

from app import ask, config, crypto, db, extract, gemini, llm, settings, sync
from app.sources import FileContent, FileMeta

from conftest import jpeg_bytes, wait_sync
from fake_gemini import FULL, KEY, FakeGemini, install_fake_gemini
from fake_ollama import FakeOllama, install_fake_ollama, pdf_with_text

INVOICE_TEXT = "VISTA ROOFING LIMITED INVOICE INV-2026-014 Bill to John Murphy Net 4200.00 VAT 567.00 TOTAL 4767.00 Unpaid."


def _meta(name: str, mime: str) -> FileMeta:
    return FileMeta("local", 1, f"x:{name}", name, mime, "Docs", "", 100, "2026-03-04T00:00:00+00:00")


def _walk(node, found):
    if isinstance(node, dict):
        found |= set(node.keys())
        for v in node.values():
            _walk(v, found)
    elif isinstance(node, list):
        for v in node:
            _walk(v, found)


def test_schema_is_converted_to_gemini_form():
    g = gemini.to_gemini_schema(extract.OUTPUT_SCHEMA)
    p = g["properties"]
    assert g["type"] == "OBJECT" and p["doc_type"] == {"type": "STRING", "enum": extract.DOC_TYPES}
    assert p["counterparty_role"] == {"type": "STRING", "enum": list(extract.ROLES), "nullable": True}
    assert p["subtotal"] == {"type": "NUMBER", "nullable": True, "description": "Net amount before VAT"}
    assert p["line_items"]["type"] == "ARRAY" and p["line_items"]["items"]["type"] == "OBJECT" and p["line_items"]["items"]["required"]
    keys: set = set()
    _walk(g, keys)
    assert not ({"anyOf", "additionalProperties"} & keys)  # nothing Gemini rejects


def test_key_test_lists_models_and_rejects_bad_keys(admin, fresh_db, monkeypatch):
    fake = FakeGemini()
    install_fake_gemini(monkeypatch, fake, as_provider=False)
    fake.answers.append("OK")
    r = admin.post("/api/admin/test-key", json={"provider": "gemini", "key": KEY, "model": "gemini-2.5-flash"}).json()
    assert r["ok"] and "answers" in r["message"] and r["models"] == ["gemini-2.5-flash", "gemini-2.5-pro"] and r["probe"] == "OK"
    r = admin.post("/api/admin/test-key", json={"provider": "gemini", "key": KEY, "model": "gemini-9"}).json()
    assert r["ok"] is False and "not available" in r["message"]
    r = admin.post("/api/admin/test-key", json={"provider": "gemini", "key": "AIza-wrong-key-" + "w" * 20}).json()
    assert r["ok"] is False and "rejected the key" in r["message"]
    assert admin.post("/api/admin/test-key", json={"provider": "gemini", "key": KEY, "model": "../../x"}).json()["ok"] is False
    assert admin.post("/api/admin/test-key", json={"provider": "gemini", "key": "short"}).json()["message"] == "No key to test."


def test_pdf_and_photo_go_inline_with_the_schema_and_header_key(monkeypatch, fresh_db):
    fake = FakeGemini()
    install_fake_gemini(monkeypatch, fake)
    settings.set_many({"company_name": "Vista Roofing Limited"})
    fields, model = extract.extract(_meta("till.jpg", "image/jpeg"), FileContent(mime="image/jpeg", data=jpeg_bytes()))
    assert model == "gemini/gemini-2.5-flash" and fields["doc_type"] == "receipt" and fields["treatment"] == "expense" and fields["total"] == 121.16
    req = fake.requests[-1]
    parts = req["contents"][0]["parts"]
    assert parts[0]["inlineData"]["mimeType"] == "image/jpeg" and len(parts[0]["inlineData"]["data"]) > 100 and "till.jpg" in parts[1]["text"]
    assert "Vista Roofing Limited" in req["systemInstruction"]["parts"][0]["text"]
    gen = req["generationConfig"]
    assert gen["temperature"] == 0 and gen["responseMimeType"] == "application/json" and gen["responseSchema"]["type"] == "OBJECT"
    fields, _ = extract.extract(_meta("inv.pdf", "application/pdf"), FileContent(mime="application/pdf", data=pdf_with_text("INVOICE INV-1")))
    assert fake.requests[-1]["contents"][0]["parts"][0]["inlineData"]["mimeType"] == "application/pdf" and fields["doc_type"] == "sales_invoice"
    assert fields["line_items"] == FULL["sales"]["line_items"] and fields["counterparty_role"] == "customer"
    fields, _ = extract.extract(_meta("bank statement.txt", "text/plain"), FileContent(mime="text/plain", text="AIB statement March"))
    assert fields["doc_type"] == "bank_statement" and fields["treatment"] == "none"


def test_odd_answers_are_normalised_not_stored(monkeypatch, fresh_db):
    fake = FakeGemini()
    install_fake_gemini(monkeypatch, fake)
    fake.answers.append("```json\n" + json.dumps({**FULL["sales"], "doc_type": "Sales Invoice", "payment_status": "NOT PAID", "category": "roofing",
                                                     "counterparty_role": "client", "flags": ["x" * 200, None], "summary": "s" * 900,
                                                     "line_items": ["junk", {"description": "ok"}]}) + "\n```")
    fields, _ = extract.extract(_meta("INV-1.txt", "text/plain"), FileContent(mime="text/plain", text=INVOICE_TEXT))
    assert fields["doc_type"] == "sales_invoice" and fields["payment_status"] == "unknown" and fields["category"] is None
    assert fields["counterparty_role"] is None and fields["flags"] == ["x" * 60] and len(fields["summary"]) == 500
    assert fields["line_items"] == [{"description": "ok"}]


def test_key_rejected_quota_and_missing_model_stop_the_run(admin, fresh_db, monkeypatch, tmp_path):
    fake = FakeGemini()
    install_fake_gemini(monkeypatch, fake)
    folder = tmp_path / "Docs"
    folder.mkdir()
    for i in range(4):
        (folder / f"INV-{i}.txt").write_text(INVOICE_TEXT, encoding="utf-8")
    src = admin.post("/api/admin/sources", json={"kind": "local", "name": "Docs", "local_folder": str(folder)}).json()
    fake.quota = True
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    st = sync.status()
    assert "quota" in st["message"].lower() and st["processed"] == 0
    assert all(d["status"] == "pending" for d in admin.get("/api/documents?limit=100").json())  # no error rows
    fake.quota = False
    settings.set_value("gemini_api_key", "AIza-revoked-key-" + "r" * 20)
    with pytest.raises(gemini.KeyRejected, match="rejected the key"):
        extract.extract(_meta("a.txt", "text/plain"), FileContent(mime="text/plain", text=INVOICE_TEXT))
    assert llm.health()["state"] == "key_invalid"
    settings.set_many({"gemini_api_key": KEY, "gemini_model": "gemini-9-ultra"})
    with pytest.raises(gemini.ModelMissing, match="set the model to 'auto'|Set the model to 'auto'"):
        extract.extract(_meta("a.txt", "text/plain"), FileContent(mime="text/plain", text=INVOICE_TEXT))
    fake.down = True
    with pytest.raises(extract.ApiUnavailable, match="not reachable"):
        extract.extract(_meta("a.txt", "text/plain"), FileContent(mime="text/plain", text=INVOICE_TEXT))


def test_transient_503_is_retried(monkeypatch, fresh_db):
    fake = FakeGemini()
    install_fake_gemini(monkeypatch, fake)
    fake.flaky_503 = 2
    fields, _ = extract.extract(_meta("INV-1.txt", "text/plain"), FileContent(mime="text/plain", text=INVOICE_TEXT))
    assert fields["doc_type"] == "sales_invoice" and len(fake.requests) == 1


def test_fallbacks_between_the_three_readers(monkeypatch, fresh_db, fake_claude):
    """Ollama → Gemini for a photo; Gemini → Claude when Gemini's quota is gone; Claude → Gemini when Claude has no credit."""
    ol, ge = FakeOllama(), FakeGemini()
    install_fake_ollama(monkeypatch, ol)
    install_fake_gemini(monkeypatch, ge, as_provider=False)
    settings.set_value("llm_fallback", "gemini")
    fields, model = extract.extract(_meta("till.jpg", "image/jpeg"), FileContent(mime="image/jpeg", data=jpeg_bytes()))
    assert model == "gemini/gemini-2.5-flash" and fields["doc_type"] == "receipt" and ol.requests == []
    # Gemini primary, quota gone → Claude
    settings.set_many({"llm_provider": "gemini", "llm_fallback": "claude"})
    ge.quota = True
    fields, model = extract.extract(_meta("INV-1.txt", "text/plain"), FileContent(mime="text/plain", text=INVOICE_TEXT))
    assert model == "fake-model" and fields["doc_type"] == "sales_invoice" and len(fake_claude.calls) == 1
    # Claude primary, no credit → Gemini
    ge.quota = False
    settings.set_many({"llm_provider": "claude", "llm_fallback": "gemini"})

    def broke(blocks):
        raise extract.ApiUnavailable("Claude API: the Anthropic account has no credit left")

    monkeypatch.setattr(extract, "_call_model", broke)
    fields, model = extract.extract(_meta("INV-2.txt", "text/plain"), FileContent(mime="text/plain", text=INVOICE_TEXT))
    assert model == "gemini/gemini-2.5-flash" and len(ge.requests) == 2
    # a fallback with no key is ignored: the primary error comes through
    settings.set_value("gemini_api_key", "")
    with pytest.raises(extract.ApiUnavailable, match="no credit"):
        extract.extract(_meta("INV-3.txt", "text/plain"), FileContent(mime="text/plain", text=INVOICE_TEXT))


def test_ask_with_gemini_and_status_badge(admin, fresh_db, monkeypatch):
    fake = FakeGemini()
    install_fake_gemini(monkeypatch, fake)
    admin.post("/api/admin/demo/load")
    fake.answers.append("Revenue was 10,000 EUR (documents 1, 2, 3).")
    r = admin.post("/api/ask", json={"question": "Revenue?"})
    assert r.status_code == 200 and r.json()["answer"].startswith("Revenue was")
    req = fake.requests[-1]
    assert "responseSchema" not in req["generationConfig"] and "Question: Revenue?" in req["contents"][0]["parts"][0]["text"]
    st = llm.refresh_status()
    assert st["state"] == "ready" and st["label"] == "Gemini ready" and st["provider"] == "gemini" and fake.requests[-1] is req  # no paid call
    cfg = admin.get("/api/config").json()
    assert cfg["llm_provider"] == "gemini" and cfg["llm_ready"] is True and cfg["llm_model"] == "gemini-2.5-flash"
    settings.set_value("gemini_api_key", "")
    assert admin.get("/api/config").json()["llm_ready"] is False and llm.refresh_status()["state"] == "no_key"
    assert admin.post("/api/sync").status_code == 400


def test_model_auto_picks_the_newest_flash_model_the_key_can_use(admin, fresh_db, monkeypatch):
    """Google retires model names for new accounts ("gemini-2.5-flash is no longer available to new users…"):
    with the model set to auto the app asks the key which models it has and takes the newest plain Flash."""
    assert gemini.pick_model(["embedding-001", "gemini-2.5-flash", "gemini-3.8-flash-lite", "gemini-3.8-flash-preview-0611", "gemini-3.8-flash", "gemini-2.5-pro"]) == "gemini-3.8-flash"
    assert gemini.pick_model(["gemini-2.5-pro", "gemini-2.5-pro-exp"]) == "gemini-2.5-pro"
    assert gemini.pick_model(["embedding-001"]) is None
    fake = FakeGemini(models=("gemini-3.8-flash", "gemini-3.8-flash-lite", "gemini-2.5-pro", "embedding-001"))
    install_fake_gemini(monkeypatch, fake)
    settings.set_value("gemini_model", "auto")
    r = admin.post("/api/admin/test-key", json={"provider": "gemini", "key": KEY, "model": "auto"}).json()
    assert r["ok"] and r["model"] == "gemini-3.8-flash" and "chosen automatically" in r["message"]
    fields, model = extract.extract(_meta("INV-1.txt", "text/plain"), FileContent(mime="text/plain", text=INVOICE_TEXT))
    assert model == "gemini/gemini-3.8-flash" and fake.requests[-1]["_model"] == "gemini-3.8-flash"
    n_before = len([1 for _ in fake.requests])
    extract.extract(_meta("INV-2.txt", "text/plain"), FileContent(mime="text/plain", text=INVOICE_TEXT))  # the model list is cached, not fetched again
    assert len(fake.requests) == n_before + 1
    # the retired name still gives a clear message pointing at auto
    settings.set_value("gemini_model", "gemini-2.5-flash")
    with pytest.raises(gemini.ModelMissing, match="auto"):
        extract.extract(_meta("INV-3.txt", "text/plain"), FileContent(mime="text/plain", text=INVOICE_TEXT))
    r = admin.post("/api/admin/test-key", json={"provider": "gemini", "key": KEY, "model": "gemini-2.5-flash"}).json()
    assert r["ok"] is False and "Use gemini-3.8-flash or set the model to auto" in r["message"]
    assert llm.health()["model"] == "gemini-2.5-flash" and llm.health()["state"] == "key_invalid"
    settings.set_value("gemini_model", "auto")
    h = llm.health()
    assert h["state"] == "ready" and h["model"] == "gemini-3.8-flash"


def test_a_broken_fallback_reader_fails_the_file_not_the_run(admin, fresh_db, monkeypatch, tmp_path):
    """Ollama reads text; Gemini is the fallback for photos but its model name is retired: the photo fails, the run goes on."""
    ol, ge = FakeOllama(), FakeGemini(models=("gemini-3.8-flash",))
    install_fake_ollama(monkeypatch, ol)
    install_fake_gemini(monkeypatch, ge, as_provider=False)
    settings.set_many({"llm_fallback": "gemini", "gemini_model": "gemini-2.5-flash"})  # retired name
    folder = tmp_path / "Docs"
    folder.mkdir()
    (folder / "till.jpg").write_bytes(jpeg_bytes())
    for i in range(3):
        (folder / f"INV-{i}.txt").write_text(INVOICE_TEXT, encoding="utf-8")
    src = admin.post("/api/admin/sources", json={"kind": "local", "name": "Docs", "local_folder": str(folder)}).json()
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    st = sync.status()
    assert st["message"] == "Sync complete" and st["processed"] == 3 and st["errors"] == 1
    docs = {d["name"]: d for d in admin.get("/api/documents?limit=100").json()}
    assert docs["till.jpg"]["status"] == "error" and "fallback reader Gemini" in docs["till.jpg"]["error"] and "auto" in docs["till.jpg"]["error"]
    assert all(docs[f"INV-{i}.txt"]["status"] == "done" for i in range(3))
    assert admin.get("/api/admin/sources").json()[0]["last_status"] == "errors"


def test_a_skipped_photo_can_be_re_read_with_another_reader(admin, fresh_db, monkeypatch, tmp_path):
    """Ollama is the reader and skips a photo; the owner sends just that file to Gemini from the drawer."""
    ol, ge = FakeOllama(), FakeGemini()
    install_fake_ollama(monkeypatch, ol)
    install_fake_gemini(monkeypatch, ge, as_provider=False)  # Gemini has a key but is neither reader nor fallback
    folder = tmp_path / "Docs"
    folder.mkdir()
    (folder / "till.jpg").write_bytes(jpeg_bytes())
    (folder / "INV-1.txt").write_text(INVOICE_TEXT, encoding="utf-8")
    src = admin.post("/api/admin/sources", json={"kind": "local", "name": "Docs", "local_folder": str(folder)}).json()
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    docs = {d["name"]: d for d in admin.get("/api/documents?limit=100").json()}
    assert docs["till.jpg"]["status"] == "skipped" and docs["INV-1.txt"]["status"] == "done"
    photo, inv = docs["till.jpg"]["id"], docs["INV-1.txt"]["id"]
    # the readers the drawer may offer
    assert [p["id"] for p in admin.get("/api/config").json()["providers"] if p["ready"]] == ["ollama", "gemini"]
    # same reader again: still cannot, with a clear reason
    r = admin.post(f"/api/documents/{photo}/reprocess?provider=ollama")
    assert r.status_code == 400 and "reads text only" in r.json()["detail"]
    # a reader without a key, or an unknown one
    assert admin.post(f"/api/documents/{photo}/reprocess?provider=claude").status_code == 400
    assert admin.post(f"/api/documents/{photo}/reprocess?provider=gpt").status_code == 400
    # Gemini reads the photo; the ledger keeps which model did it
    r = admin.post(f"/api/documents/{photo}/reprocess?provider=gemini")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "done" and r.json()["doc_type"] == "receipt" and r.json()["model"] == "gemini/gemini-2.5-flash"
    assert ge.requests[-1]["contents"][0]["parts"][0]["inlineData"]["mimeType"] == "image/jpeg"
    # the text invoice can also be re-read with Gemini on demand; the main reader stays Ollama
    r = admin.post(f"/api/documents/{inv}/reprocess?provider=gemini")
    assert r.status_code == 200 and r.json()["model"] == "gemini/gemini-2.5-flash" and settings.llm_provider() == "ollama"
    assert admin.get("/api/sync/status").json()["read_by"] == {"gemini/gemini-2.5-flash": 2}
    # without a provider the current reader is used
    r = admin.post(f"/api/documents/{inv}/reprocess")
    assert r.status_code == 200 and r.json()["model"] == "ollama/qwen2.5:3b"


def test_gemini_key_is_validated_masked_and_encrypted(admin, fresh_db, monkeypatch):
    assert admin.put("/api/admin/settings", json={"gemini_api_key": "short"}).status_code == 400
    assert admin.put("/api/admin/settings", json={"gemini_model": "../etc"}).status_code == 400
    assert admin.put("/api/admin/settings", json={"llm_fallback": "gpt4"}).status_code == 400
    monkeypatch.setenv("APP_SECRET_KEY", "a-long-random-operator-secret")
    crypto._reset_for_tests()
    try:
        r = admin.put("/api/admin/settings", json={"gemini_api_key": KEY, "gemini_model": "gemini-2.5-pro", "llm_fallback": "none"}).json()
        assert r["has_gemini_key"] is True and r["gemini_api_key_masked"].startswith("AIzaSyT") and KEY not in json.dumps(r)
        assert r["gemini_model"] == "gemini-2.5-pro" and r["llm_fallback"] == ""
        with db.connect() as conn:
            raw = conn.execute("SELECT value FROM settings WHERE key='gemini_api_key'").fetchone()["value"]
        assert raw.startswith("enc:") and "kkkk" not in raw and settings.get("gemini_api_key") == KEY
        assert KEY not in json.dumps(admin.get("/api/admin/overview").json()) and KEY not in json.dumps(admin.get("/api/config").json())
    finally:
        monkeypatch.delenv("APP_SECRET_KEY", raising=False)
        crypto._reset_for_tests()
        settings.set_many({"gemini_api_key": ""})
