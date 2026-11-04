"""End-to-end tests for the owner API, admin API, sources, OAuth callback and sync pipeline."""
from __future__ import annotations

import json

import pytest
from pathlib import Path
from types import SimpleNamespace

from app import db, extract, settings, sources, sync

from conftest import FAKE_KEY, FakeDrive, install_fake_drive, wait_sync

GOOGLE_CLIENT = {
    "installed": {
        "client_id": "123-abc.apps.googleusercontent.com", "project_id": "ledger-test",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token",
        "client_secret": "shhh", "redirect_uris": ["http://localhost"],
    }
}


# ====================================================================== admin auth


def test_admin_page_served(client):
    assert client.get("/admin").status_code == 200 and "Ledger Admin" in client.get("/admin").text


# ====================================================================== settings


def test_settings_roundtrip_and_key_masking(admin, fresh_db):
    r = admin.put("/api/admin/settings", json={"company_name": "Cormac Roofing Ltd", "base_currency": "eur", "extract_effort": "high",
                                               "concurrency": 10, "auto_sync_minutes": 15, "anthropic_api_key": FAKE_KEY})
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["company_name"] == "Cormac Roofing Ltd" and s["base_currency"] == "EUR" and s["extract_effort"] == "high"
    assert s["concurrency"] == "10"
    assert s["has_api_key"] is True and s["anthropic_api_key_masked"].startswith("sk-ant-") and "x" * 20 not in s["anthropic_api_key_masked"]
    assert "anthropic_api_key" not in s
    assert admin.put("/api/admin/settings", json={"anthropic_api_key": "sk-ant-..."}).status_code == 400
    assert admin.put("/api/admin/settings", json={"extract_effort": "ultra"}).status_code == 400
    # the extraction prompt picks the new company name up without a restart
    assert "Cormac Roofing Ltd" in extract.build_system_prompt()
    admin.put("/api/admin/settings", json={"company_address": "Unit 77\nPark West Enterprise Centre\nDublin 12", "company_vat": "IE1234567X"})
    prompt = extract.build_system_prompt()
    assert "Unit 77 Park West Enterprise Centre Dublin 12" in prompt and "IE1234567X" in prompt
    # owner config reflects settings
    cfg = admin.get("/api/config").json()
    assert cfg["company"] == "Cormac Roofing Ltd" and cfg["has_api_key"] is True and cfg["auto_sync_minutes"] == 15
    # clearing the key
    admin.put("/api/admin/settings", json={"anthropic_api_key": ""})
    assert admin.get("/api/config").json()["has_api_key"] is False
    assert admin.post("/api/sync").status_code == 400  # owner cannot sync without a key


def test_test_key_endpoint(admin, monkeypatch):
    class FakeModels:
        def retrieve(self, model):
            if model == "claude-opus-5-5":
                return SimpleNamespace(display_name="Claude Opus 5.5")
            import anthropic
            import httpx2
            resp = httpx2.Response(404, request=httpx2.Request("GET", "https://api.anthropic.com/v1/models/x"))
            raise anthropic.NotFoundError(message="nope", response=resp, body=None)

    monkeypatch.setattr(extract.anthropic, "Anthropic", lambda **kw: SimpleNamespace(models=FakeModels()))
    assert admin.post("/api/admin/test-key", json={"key": "short"}).json()["ok"] is False
    r = admin.post("/api/admin/test-key", json={"key": FAKE_KEY, "model": "claude-opus-5-5"}).json()
    assert r["ok"] is True and "Opus" in r["message"]
    r = admin.post("/api/admin/test-key", json={"key": FAKE_KEY, "model": "claude-nope"}).json()
    assert r["ok"] is False and "not found" in r["message"]


def test_google_client_upload(admin, fresh_db):
    assert admin.post("/api/admin/google-client", json={"json_text": "not json"}).status_code == 400
    assert admin.post("/api/admin/google-client", json={"json_text": json.dumps({"foo": 1})}).status_code == 400
    r = admin.post("/api/admin/google-client", json={"json_text": json.dumps(GOOGLE_CLIENT)})
    assert r.status_code == 200 and r.json()["has_google_client"] is True and r.json()["google_client_type"] == "installed"
    assert admin.delete("/api/admin/google-client").json()["has_google_client"] is False


# ====================================================================== local folder source, end to end


def _jpeg() -> bytes:
    import io
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (120, 80), (240, 240, 240)).save(buf, format="JPEG")
    return buf.getvalue()


def _make_folder(tmp_path: Path) -> Path:
    import docx
    import openpyxl

    (tmp_path / "Accounts" / "2026").mkdir(parents=True)
    (tmp_path / "Accounts" / "2026" / "INV-9 Murphy.pdf").write_bytes(b"%PDF-1.4 fake invoice")
    (tmp_path / "Accounts" / "2026" / "Chadwicks bill.txt").write_text("Chadwicks invoice total 1230", encoding="utf-8")
    (tmp_path / "Accounts" / "AIB statement March.jpg").write_bytes(_jpeg())
    d = docx.Document(); d.add_paragraph("Supplier invoice from Chadwicks"); d.save(tmp_path / "Accounts" / "bill.docx")
    wb = openpyxl.Workbook(); wb.active.append(["Item", "Amount"]); wb.save(tmp_path / "Accounts" / "sheet.xlsx")
    (tmp_path / "Accounts" / "notes.zip").write_bytes(b"PK unsupported")
    return tmp_path / "Accounts"


def test_local_source_sync_pipeline(admin, fresh_db, fake_claude, tmp_path):
    folder = _make_folder(tmp_path)
    assert admin.post("/api/admin/sources", json={"kind": "local", "name": "Office", "local_folder": str(tmp_path / "missing")}).status_code == 400
    src = admin.post("/api/admin/sources", json={"kind": "local", "name": "Office", "local_folder": str(folder)}).json()
    assert src["kind"] == "local" and src["last_status"] == "ready"

    r = admin.post(f"/api/admin/sources/{src['id']}/sync")
    assert r.status_code == 200 and r.json()["started"] is True
    wait_sync()

    st = sync.status()
    assert st["phase"] == "done" and st["total"] == 6 and st["processed"] == 5 and st["skipped"] == 1 and st["errors"] == 0
    docs = admin.get("/api/documents").json()
    by_name = {d["name"]: d for d in docs}
    assert by_name["INV-9 Murphy.pdf"]["treatment"] == "revenue" and by_name["INV-9 Murphy.pdf"]["net_amount"] == 5000
    assert by_name["Chadwicks bill.txt"]["treatment"] == "expense" and by_name["Chadwicks bill.txt"]["project"] == "Oak Drive"
    assert by_name["AIB statement March.jpg"]["treatment"] == "none"
    assert by_name["notes.zip"]["status"] == "skipped" and "unsupported" in by_name["notes.zip"]["error"]

    # a corrupt photo is skipped with a reason rather than sent to the model or treated as a failure
    (folder / "broken.jpg").write_bytes(b"\xff\xd8\xff not really a jpeg")
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    broken = next(d for d in admin.get("/api/documents").json() if d["name"] == "broken.jpg")
    assert broken["status"] == "skipped" and "could not read image" in broken["error"]
    (folder / "broken.jpg").unlink()
    with db.connect() as conn:
        conn.execute("DELETE FROM documents WHERE name='broken.jpg'")
    assert by_name["bill.docx"]["source_name"] == "Office" and by_name["bill.docx"]["folder"] == "Office"
    assert by_name["Chadwicks bill.txt"]["folder"] == "Office/2026"

    # the right content block types reached the model
    kinds = {b["type"] for call in fake_claude.calls for b in call[:-1]}
    assert kinds == {"document", "text", "image"}

    # summary reflects the ledger
    s = admin.get("/api/summary").json()["totals"]
    assert s["revenue"] == 5000 and s["expenses"] == 3000 and s["profit"] == 2000  # three money_out docs at 1000 net
    assert s["receivables"] == 5675 and s["payables"] == 3690
    assert admin.get("/api/summary").json()["projects"][0]["name"] == "Oak Drive"

    # second sync: nothing changed → everything skipped, no model calls
    n_calls = len(fake_claude.calls)
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    assert len(fake_claude.calls) == n_calls and sync.status()["skipped"] == 6

    # edit a file → only that one is re-read
    (folder / "2026" / "Chadwicks bill.txt").write_text("Chadwicks invoice total 1230 (updated)", encoding="utf-8")
    import os, time
    os.utime(folder / "2026" / "Chadwicks bill.txt", (time.time() + 5, time.time() + 5))
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    assert len(fake_claude.calls) == n_calls + 1 and sync.status()["processed"] == 1

    # force re-read reads all supported files again
    admin.post(f"/api/admin/sources/{src['id']}/sync?force=1"); wait_sync()
    assert sync.status()["processed"] == 5

    # a failing file is recorded as an error, not fatal
    fake_claude.fail_for.add("INV-9 Murphy.pdf")
    admin.post(f"/api/admin/sources/{src['id']}/sync?force=1"); wait_sync()
    st = sync.status()
    assert st["errors"] == 1 and st["recent_errors"][0]["file"] == "INV-9 Murphy.pdf"
    bad = [d for d in admin.get("/api/documents?status=error").json()]
    assert len(bad) == 1 and "simulated" in bad[0]["error"]
    srcs = admin.get("/api/admin/sources").json()
    assert srcs[0]["last_status"] == "errors" and srcs[0]["error_count"] == 1 and srcs[0]["done_count"] == 4
    fake_claude.fail_for.clear()

    # reprocess the failed one from the owner dashboard
    r = admin.post(f"/api/documents/{bad[0]['id']}/reprocess")
    assert r.status_code == 200 and r.json()["status"] == "done"

    # owner override wins and survives a sync
    doc = by_name["Chadwicks bill.txt"]
    r = admin.patch(f"/api/documents/{doc['id']}", json={"payment_status": "paid", "amount_paid": 1230, "doc_type": "receipt"})
    assert r.status_code == 200 and r.json()["overridden"] == 1 and r.json()["treatment"] == "expense"
    admin.post(f"/api/admin/sources/{src['id']}/sync?force=1"); wait_sync()
    assert admin.get(f"/api/documents/{doc['id']}").json()["payment_status"] == "paid"

    # disable → not synced; remove → documents gone
    admin.patch(f"/api/admin/sources/{src['id']}", json={"enabled": False})
    admin.post("/api/admin/sync-all"); wait_sync()
    assert sync.status()["total"] == 0
    r = admin.delete(f"/api/admin/sources/{src['id']}")
    assert r.json()["documents_removed"] == 6 and admin.get("/api/documents").json() == []


# ====================================================================== Google Drive: OAuth + sync


def test_drive_oauth_connect_and_auto_sync(admin, fresh_db, fake_claude, monkeypatch):
    # cannot add a Drive before the OAuth client exists
    assert admin.post("/api/admin/sources", json={"kind": "drive", "name": "Cormac's Drive"}).status_code == 400
    admin.post("/api/admin/google-client", json={"json_text": json.dumps(GOOGLE_CLIENT)})
    src = admin.post("/api/admin/sources", json={"kind": "drive", "name": "Cormac's Drive",
                                                 "drive_folder_id": "https://drive.google.com/drive/folders/FOLDER123?usp=sharing"}).json()
    assert src["drive_folder_id"] == "FOLDER123" and src["last_status"] == "not_connected"
    assert admin.post(f"/api/admin/sources/{src['id']}/sync").status_code == 400  # not connected yet

    # Step 1: Connect → real google-auth-oauthlib builds the consent URL from the stored client
    r = admin.get(f"/admin/sources/{src['id']}/connect", follow_redirects=False)
    assert r.status_code == 303
    url = r.headers["location"]
    assert url.startswith("https://accounts.google.com/o/oauth2/auth")
    assert "client_id=123-abc.apps.googleusercontent.com" in url and "drive.readonly" in url
    assert "redirect_uri=http%3A%2F%2Ftestserver%2Fadmin%2Foauth%2Fcallback" in url
    state = url.split("state=", 1)[1].split("&", 1)[0]

    # Step 2: Google redirects back. We stand in for Google's token endpoint + Drive.
    drive = FakeDrive(
        files=[
            {"id": "f1", "name": "INV-9 Murphy.pdf", "mimeType": "application/pdf", "modifiedTime": "2026-03-10T10:00:00Z", "size": "1200", "parents": ["FOLDER123"], "webViewLink": "https://drive.google.com/file/d/f1/view"},
            {"id": "f2", "name": "Chadwicks bill", "mimeType": "application/vnd.google-apps.document", "modifiedTime": "2026-03-04T10:00:00Z", "parents": ["SUB1"]},
            {"id": "f3", "name": "AIB statement", "mimeType": "application/vnd.google-apps.spreadsheet", "modifiedTime": "2026-03-31T10:00:00Z", "parents": ["SUB1"]},
            {"id": "f4", "name": "photo.heic", "mimeType": "image/heic", "modifiedTime": "2026-03-31T10:00:00Z", "parents": ["FOLDER123"]},
        ],
        folders=[{"id": "FOLDER123", "name": "Accounts", "parents": []}, {"id": "SUB1", "name": "Bills", "parents": ["FOLDER123"]}],
        email="cormac@example.com",
    )
    drive.contents["f3"] = b"date,amount\n2026-03-01,100\n"
    install_fake_drive(monkeypatch, drive)
    monkeypatch.setattr(sources, "exchange_code", lambda redirect_uri, code: ('{"token": "t", "refresh_token": "r"}', "cormac@example.com"))

    # wrong / expired state is rejected
    r = admin.get("/admin/oauth/callback?state=bogus&code=abc", follow_redirects=False)
    assert "expired" in r.headers["location"]
    r = admin.get(f"/admin/oauth/callback?state={state}&code=4/real-code", follow_redirects=False)
    assert r.status_code == 303 and "Connected%20cormac%40example.com" in r.headers["location"] and "Sync%20started" in r.headers["location"]
    wait_sync()

    srcs = admin.get("/api/admin/sources").json()
    assert srcs[0]["connected"] is True and srcs[0]["account_email"] == "cormac@example.com" and "token_json" not in srcs[0]
    assert srcs[0]["last_status"] == "ok" and srcs[0]["done_count"] == 3 and srcs[0]["doc_count"] == 4
    docs = {d["name"]: d for d in admin.get("/api/documents").json()}
    assert docs["INV-9 Murphy.pdf"]["treatment"] == "revenue" and docs["INV-9 Murphy.pdf"]["folder"] == "Cormac's Drive/Accounts"
    assert docs["Chadwicks bill"]["folder"] == "Cormac's Drive/Accounts/Bills" and docs["Chadwicks bill"]["status"] == "done"
    assert docs["AIB statement"]["treatment"] == "none"
    assert docs["photo.heic"]["status"] == "skipped"
    assert docs["INV-9 Murphy.pdf"]["web_link"].startswith("https://drive.google.com/")
    # exported Google Doc arrived as a PDF document block; exported Sheet as CSV text
    blocks_by_name = {c[-1]["text"].split("File name: ", 1)[1].split("\n", 1)[0]: c[0] for c in fake_claude.calls}
    assert blocks_by_name["Chadwicks bill"]["type"] == "document"
    assert blocks_by_name["AIB statement"]["type"] == "text" and "date,amount" in blocks_by_name["AIB statement"]["text"]

    # Cancelled sign-in is reported, not fatal
    r = admin.get(f"/admin/sources/{src['id']}/connect", follow_redirects=False)
    state2 = r.headers["location"].split("state=", 1)[1].split("&", 1)[0]
    r = admin.get(f"/admin/oauth/callback?state={state2}&error=access_denied", follow_redirects=False)
    assert "cancelled" in r.headers["location"]

    # Disconnect clears the token
    admin.post(f"/api/admin/sources/{src['id']}/disconnect")
    assert admin.get("/api/admin/sources").json()[0]["connected"] is False


def test_two_drives_same_file_id_do_not_collide(admin, fresh_db, fake_claude, monkeypatch):
    admin.post("/api/admin/google-client", json={"json_text": json.dumps(GOOGLE_CLIENT)})
    a = admin.post("/api/admin/sources", json={"kind": "drive", "name": "Drive A"}).json()
    b = admin.post("/api/admin/sources", json={"kind": "drive", "name": "Drive B"}).json()
    for s in (a, b):
        db.update_source(s["id"], token_json='{"token":"t"}', account_email=f"{s['name']}@x.com", last_status="connected")
    shared = {"id": "same-id", "name": "INV-9 shared.pdf", "mimeType": "application/pdf", "modifiedTime": "2026-03-10T10:00:00Z", "parents": ["root"]}
    install_fake_drive(monkeypatch, FakeDrive(files=[shared]))
    admin.post("/api/admin/sync-all"); wait_sync()
    docs = admin.get("/api/documents").json()
    assert len(docs) == 2 and {d["source_name"] for d in docs} == {"Drive A", "Drive B"}
    assert sync.status()["total"] == 2 and admin.get("/api/admin/runs").json()[0]["source_name"] in ("Drive A", "Drive B")
    # per-source filter on the owner API
    assert len(admin.get(f"/api/documents?source_ref={a['id']}").json()) == 1


def test_drive_not_connected_is_reported_cleanly(admin, fresh_db, fake_claude):
    admin.post("/api/admin/google-client", json={"json_text": json.dumps(GOOGLE_CLIENT)})
    src = admin.post("/api/admin/sources", json={"kind": "drive", "name": "Unconnected"}).json()
    sync.start([src["id"]], wait=True)
    s = admin.get("/api/admin/sources").json()[0]
    assert s["last_status"] == "failed" and "not connected" in s["last_error"]
    assert admin.get("/api/admin/runs").json()[0]["status"] == "failed"


# ====================================================================== extraction call shape & ask


def test_call_model_uses_structured_output_and_fallback(monkeypatch, fresh_db):
    settings.set_many({"anthropic_api_key": FAKE_KEY, "extract_model": "claude-opus-5-5", "extract_effort": "low"})
    seen = {}

    class FakeMessages:
        def create(self, **kw):
            seen.update(kw)
            return SimpleNamespace(stop_reason="end_turn", model="claude-opus-5-5",
                                   content=[SimpleNamespace(type="text", text=json.dumps({"doc_type": "receipt", "direction": "money_out", "total": 12.5, "payment_status": "paid"}))])

    fake_client = SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages()), messages=FakeMessages())
    monkeypatch.setattr(extract, "client", lambda: fake_client)
    meta = sources.FileMeta("local", 1, "x", "till.jpg", "image/jpeg", "Office", "", 10, "2026-01-01T00:00:00+00:00")
    fields, model = extract.extract(meta, sources.FileContent(mime="image/jpeg", data=_jpeg()))
    assert seen["model"] == "claude-opus-5-5" and seen["betas"] == ["server-side-fallback-2026-07-01"] and seen["fallbacks"] == "default"
    assert seen["output_config"]["format"]["type"] == "json_schema" and seen["output_config"]["effort"] == "low"
    # the API refuses an enum declared next to a type list (["string", "null"]); nullable enums must use anyOf
    for name, prop in seen["output_config"]["format"]["schema"]["properties"].items():
        assert not (isinstance(prop.get("type"), list) and "enum" in prop), f"{name}: nullable enum must be anyOf"
    role = seen["output_config"]["format"]["schema"]["properties"]["counterparty_role"]
    assert {"type": "null"} in role["anyOf"] and "customer" in role["anyOf"][0]["enum"]
    assert seen["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert seen["messages"][0]["content"][0]["type"] == "image"
    assert fields["treatment"] == "expense" and fields["net_amount"] == 12.5 and fields["currency"] == "EUR"


def test_call_model_turns_billing_and_key_errors_into_api_unavailable(monkeypatch, fresh_db):
    """A 'credit balance too low' 400 or a 401 must stop the sync (ApiUnavailable), other 400s stay per-file errors."""
    import anthropic
    try:
        import httpx2 as httpx  # anthropic 1.x
    except ImportError:  # pragma: no cover
        import httpx

    settings.set_many({"anthropic_api_key": FAKE_KEY, "extract_model": "claude-opus-5-5"})

    def err(cls, status, message):
        resp = httpx.Response(status, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
        return cls(message, response=resp, body={"error": {"type": "invalid_request_error", "message": message}})

    class FailingMessages:
        def __init__(self, exc): self.exc = exc
        def create(self, **kw): raise self.exc

    def fake_client_raising(exc):
        return SimpleNamespace(beta=SimpleNamespace(messages=FailingMessages(exc)), messages=FailingMessages(exc))

    blocks = [{"type": "text", "text": "x"}]
    for exc in (err(anthropic.BadRequestError, 400, "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits."),
                err(anthropic.AuthenticationError, 401, "invalid x-api-key")):
        monkeypatch.setattr(extract, "client", lambda exc=exc: fake_client_raising(exc))
        with pytest.raises(extract.ApiUnavailable) as info:
            extract._call_model(blocks)
        assert "credit" in str(info.value) or "key" in str(info.value)
    monkeypatch.setattr(extract, "client", lambda: fake_client_raising(err(anthropic.BadRequestError, 400, "messages: text content blocks must be non-empty")))
    with pytest.raises(anthropic.BadRequestError):
        extract._call_model(blocks)


def test_ask_endpoint(admin, fresh_db, monkeypatch):
    settings.set_value("anthropic_api_key", FAKE_KEY)
    admin.post("/api/admin/demo/load")

    class FakeMessages:
        def create(self, **kw):
            assert "<ledger_summary>" in kw["messages"][0]["content"] and "Question: Who owes us?" in kw["messages"][0]["content"]
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Kildare County Council owes €4,322.")])

    monkeypatch.setattr(extract, "client", lambda: SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages()), messages=FakeMessages()))
    r = admin.post("/api/ask", json={"question": "Who owes us?"})
    assert r.status_code == 200 and "Kildare" in r.json()["answer"]
    assert admin.get("/api/config").json()["demo_loaded"] is True
    admin.post("/api/admin/demo/clear")
    assert admin.get("/api/config").json()["demo_loaded"] is False


def test_owner_pages_and_clear(admin, fresh_db):
    assert "Roofing Ledger" in admin.get("/").text
    assert admin.get("/static/app.js").status_code == 200 and admin.get("/static/admin.js").status_code == 200
    admin.post("/api/admin/demo/load")
    assert admin.get("/api/config").json()["counts"]["done"] > 0
    admin.post("/api/admin/clear-documents")
    assert admin.get("/api/config").json()["counts"] == {}
