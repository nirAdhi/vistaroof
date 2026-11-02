"""Two-way sync: uploads from the app into Drive / folders, the Drive change watcher, deletions."""
from __future__ import annotations

import json

from app import db, settings, sources, sync

from conftest import FakeDrive, connect_fake_drive, install_fake_drive, jpeg_bytes, wait_sync

GOOGLE_CLIENT = {"installed": {"client_id": "id.apps.googleusercontent.com", "client_secret": "s", "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"}}


def _files(*items):
    return [("files", (name, data, mime)) for name, data, mime in items]


# ------------------------------------------------------------------ uploads


def test_upload_to_local_folder_is_stored_and_read(admin, fresh_db, fake_claude, tmp_path):
    folder = tmp_path / "Accounts"; folder.mkdir()
    src = admin.post("/api/admin/sources", json={"kind": "local", "name": "Office", "local_folder": str(folder)}).json()
    targets = admin.get("/api/upload/targets").json()
    assert targets == [{"id": src["id"], "name": "Office", "kind": "local", "can_upload": True, "folder": str(folder.resolve()), "reason": None}]

    r = admin.post("/api/upload", data={"source_id": src["id"]}, files=_files(
        ("INV-77 Murphy.pdf", b"%PDF-1.4 invoice", "application/pdf"),
        ("till.jpg", jpeg_bytes(), "image/jpeg"),
        ("virus.exe", b"MZ", "application/octet-stream"),
        ("empty.pdf", b"", "application/pdf"),
    ))
    assert r.status_code == 200, r.text
    by_name = {x["name"]: x for x in r.json()["results"]}
    assert by_name["INV-77 Murphy.pdf"]["ok"] and by_name["INV-77 Murphy.pdf"]["status"] == "done" and by_name["INV-77 Murphy.pdf"]["doc_type"] == "sales_invoice"
    assert by_name["till.jpg"]["status"] == "done" and by_name["till.jpg"]["total"] == 1230.0
    assert by_name["virus.exe"]["ok"] is False and "unsupported" in by_name["virus.exe"]["error"]
    assert by_name["empty.pdf"]["ok"] is False
    # files landed in the folder, and the ledger knows they came from the app
    assert (folder / "Ledger Uploads" / "INV-77 Murphy.pdf").read_bytes() == b"%PDF-1.4 invoice"
    docs = {d["name"]: d for d in admin.get("/api/documents").json()}
    assert docs["INV-77 Murphy.pdf"]["uploaded_by"] == "app" and docs["INV-77 Murphy.pdf"]["folder"] == "Office/Ledger Uploads"
    # uploading the same name again does not overwrite
    admin.post("/api/upload", data={"source_id": src["id"]}, files=_files(("till.jpg", jpeg_bytes(), "image/jpeg")))
    assert (folder / "Ledger Uploads" / "till (1).jpg").exists()
    # a later folder sync sees the uploaded files as unchanged
    n = len(fake_claude.calls)
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    assert len(fake_claude.calls) == n and sync.status()["errors"] == 0


def test_upload_without_api_key_is_stored_as_pending(admin, fresh_db, tmp_path):
    settings.set_value("anthropic_api_key", "")
    folder = tmp_path / "Docs"; folder.mkdir()
    src = admin.post("/api/admin/sources", json={"kind": "local", "name": "Docs", "local_folder": str(folder)}).json()
    r = admin.post("/api/upload", data={"source_id": src["id"]}, files=_files(("receipt.png", jpeg_bytes(), "image/png")))
    assert r.json()["results"][0]["status"] == "pending"
    assert (folder / "Ledger Uploads" / "receipt.png").exists()


def test_upload_to_google_drive(admin, fresh_db, fake_claude, monkeypatch):
    admin.post("/api/admin/google-client", json={"json_text": json.dumps(GOOGLE_CLIENT)})
    src = admin.post("/api/admin/sources", json={"kind": "drive", "name": "Cormac Drive"}).json()
    drive = FakeDrive(files=[], folders=[])
    install_fake_drive(monkeypatch, drive)

    # connected without the drive.file scope (old token) -> uploads refused with a clear message
    connect_fake_drive(src["id"], with_upload_scope=False)
    assert admin.get("/api/upload/targets").json()[0]["can_upload"] is False
    r = admin.post("/api/upload", data={"source_id": src["id"]}, files=_files(("a.pdf", b"%PDF-1.4", "application/pdf")))
    assert r.status_code == 400 and "reconnect" in r.json()["detail"]

    # reconnect with the full scopes -> upload creates "Ledger Uploads" at the root and the file inside it
    connect_fake_drive(src["id"], with_upload_scope=True)
    r = admin.post("/api/upload", data={"source_id": src["id"]}, files=_files(("INV-5.pdf", b"%PDF-1.4 inv", "application/pdf")))
    assert r.status_code == 200, r.text
    res = r.json()["results"][0]
    assert res["ok"] and res["status"] == "done" and res["web_link"].startswith("https://drive.google.com/")
    assert drive.created[0]["name"] == "INV-5.pdf" and drive.created[0]["data"] == b"%PDF-1.4 inv"
    assert drive.created[0]["parents"] == ["folder-1"] and drive._folders[0]["name"] == sources.UPLOAD_FOLDER_NAME
    doc = admin.get("/api/documents").json()[0]
    assert doc["source_id"] == "up-1" and doc["uploaded_by"] == "app" and doc["folder"] == "Cormac Drive/Ledger Uploads"

    # a source limited to a folder uploads straight into that folder
    src2 = admin.post("/api/admin/sources", json={"kind": "drive", "name": "Accounts only", "drive_folder_id": "ACC"}).json()
    connect_fake_drive(src2["id"])
    admin.post("/api/upload", data={"source_id": src2["id"]}, files=_files(("b.pdf", b"%PDF-1.4 b", "application/pdf")))
    assert drive.created[-1]["parents"] == ["ACC"]


# ------------------------------------------------------------------ watcher (Drive -> app)


def test_watcher_picks_up_new_and_deleted_drive_files(admin, fresh_db, fake_claude, monkeypatch):
    admin.post("/api/admin/google-client", json={"json_text": json.dumps(GOOGLE_CLIENT)})
    src = admin.post("/api/admin/sources", json={"kind": "drive", "name": "Live Drive"}).json()
    drive = FakeDrive(files=[{"id": "f1", "name": "INV-1.pdf", "mimeType": "application/pdf", "modifiedTime": "2026-03-01T00:00:00Z", "parents": ["root"]}])
    install_fake_drive(monkeypatch, drive)
    connect_fake_drive(src["id"])
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    assert len(admin.get("/api/documents").json()) == 1

    # first watcher pass only establishes the cursor
    r = admin.post("/api/admin/watch-now").json()
    assert r["changed_sources"] == [] and db.get_source(src["id"])["drive_page_token"] == "100"
    # nothing changed -> no sync
    assert admin.post("/api/admin/watch-now").json()["changed_sources"] == []

    # a file is uploaded in Google Drive (by someone, from anywhere) -> next pass triggers a sync of that source only
    drive.add_file({"id": "f2", "name": "Chadwicks bill.pdf", "mimeType": "application/pdf", "modifiedTime": "2026-03-02T00:00:00Z", "parents": ["root"]})
    r = admin.post("/api/admin/watch-now").json()
    assert r["changed_sources"] == [src["id"]] and r["started"] is True
    wait_sync()
    docs = {d["name"]: d for d in admin.get("/api/documents").json()}
    assert docs["Chadwicks bill.pdf"]["status"] == "done" and docs["Chadwicks bill.pdf"]["treatment"] == "expense"
    assert admin.get("/api/admin/runs").json()[0]["trigger"] == "watch"
    version_before = admin.get("/api/version").json()["version"]

    # the file is deleted in Drive -> it disappears from the ledger and the totals
    drive.remove_file("f2")
    assert admin.post("/api/admin/watch-now").json()["changed_sources"] == [src["id"]]
    wait_sync()
    names = [d["name"] for d in admin.get("/api/documents").json()]
    assert "Chadwicks bill.pdf" not in names and "INV-1.pdf" in names
    removed = admin.get("/api/documents?status=removed").json()
    assert len(removed) == 1 and removed[0]["name"] == "Chadwicks bill.pdf"
    assert admin.get("/api/summary").json()["totals"]["expenses"] == 0
    assert admin.get("/api/admin/runs").json()[0]["removed"] == 1
    assert admin.get("/api/version").json()["version"] != version_before

    # ...and comes back if restored
    drive.add_file({"id": "f2", "name": "Chadwicks bill.pdf", "mimeType": "application/pdf", "modifiedTime": "2026-03-02T00:00:00Z", "parents": ["root"]})
    admin.post("/api/admin/watch-now"); wait_sync()
    assert admin.get("/api/summary").json()["totals"]["expenses"] == 1000


def test_check_for_changes_handles_disconnected_and_local_sources(admin, fresh_db, fake_claude, tmp_path, monkeypatch):
    folder = tmp_path / "F"; folder.mkdir()
    local = admin.post("/api/admin/sources", json={"kind": "local", "name": "Local", "local_folder": str(folder)}).json()
    admin.post("/api/admin/google-client", json={"json_text": json.dumps(GOOGLE_CLIENT)})
    unconnected = admin.post("/api/admin/sources", json={"kind": "drive", "name": "Not yet"}).json()
    admin.patch(f"/api/admin/sources/{local['id']}", json={"enabled": False})
    assert sync.check_for_changes() == []  # disabled local, unconnected drive
    admin.patch(f"/api/admin/sources/{local['id']}", json={"enabled": True})
    assert sync.check_for_changes() == [local["id"]]  # local folders are always re-scanned
    assert db.get_source(unconnected["id"])["last_change_check"] is None


def test_sync_requests_while_busy_are_queued(admin, fresh_db, fake_claude, tmp_path):
    folder = tmp_path / "Q"; folder.mkdir()
    (folder / "INV-1.pdf").write_bytes(b"%PDF-1.4")
    src = admin.post("/api/admin/sources", json={"kind": "local", "name": "Q", "local_folder": str(folder)}).json()
    import threading
    gate = threading.Event()
    original = fake_claude.__call__

    def slow(blocks):
        gate.wait(5)
        return original(blocks)

    fake_claude.__class__.__call__ = lambda self, blocks: slow(blocks)
    try:
        assert sync.start([src["id"]]) is True
        assert sync.start([src["id"]], force=True) is False  # busy -> queued
        assert sync._rerun == {src["id"]: True}
        gate.set()
        wait_sync()
        import time
        time.sleep(0.3); wait_sync()
        runs = admin.get("/api/admin/runs").json()
        assert len(runs) == 2 and runs[0]["trigger"] == "queued" and runs[0]["force"] == 1
    finally:
        fake_claude.__class__.__call__ = original.__func__ if hasattr(original, "__func__") else fake_claude.__class__.__call__
