"""Backups: application zip, full backup with the source files, downloads, deletion, retention, schedule, name safety."""
from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path

from app import backup, settings, sync

from conftest import wait_sync


def _ledger_with_files(admin, tmp_path):
    folder = tmp_path / "Docs"
    (folder / "2026").mkdir(parents=True)
    (folder / "INV-1.pdf").write_bytes(b"%PDF-1.4 invoice one")
    (folder / "2026" / "receipt.txt").write_text("Chadwicks receipt total 12.50", encoding="utf-8")
    src = admin.post("/api/admin/sources", json={"kind": "local", "name": "Docs", "local_folder": str(folder)}).json()
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    dest = tmp_path / "usb"
    dest.mkdir()
    settings.set_value("backup_dir", str(dest))
    return folder, dest


def test_application_backup_zip(admin, fresh_db, fake_claude, tmp_path):
    _, dest = _ledger_with_files(admin, tmp_path)
    r = admin.post("/api/admin/backups/app").json()
    assert r["ok"] and r["item"]["kind"] == "app" and r["item"]["name"].startswith("ledger-app-") and r["item"]["name"].endswith(".zip")
    z = zipfile.ZipFile(dest / r["item"]["name"])
    assert sorted(z.namelist()) == ["documents.csv", "ledger.db", "manifest.json", "summary.json"]
    assert z.read("ledger.db").startswith(b"SQLite format 3")
    csv_text = z.read("documents.csv").decode()
    assert "INV-1.pdf" in csv_text and "receipt.txt" in csv_text and csv_text.startswith("id,issue_date,due_date,doc_type")
    assert "totals" in json.loads(z.read("summary.json")) and json.loads(z.read("manifest.json"))["kind"] == "app"
    listing = admin.get("/api/admin/backups").json()
    assert listing["dir"]["path"] == str(dest) and listing["dir"]["writable"] is True and listing["items"][0]["name"] == r["item"]["name"]
    assert listing["job"]["running"] is False and listing["schedule"]["every_hours"] == 0
    # download through the browser: the same zip
    d = admin.get(f"/api/admin/backups/download?name={r['item']['name']}")
    assert d.status_code == 200 and d.headers["content-type"] == "application/zip" and zipfile.ZipFile(io.BytesIO(d.content)).namelist()


def test_full_backup_mirrors_the_source_files(admin, fresh_db, fake_claude, tmp_path):
    folder, dest = _ledger_with_files(admin, tmp_path)
    r = admin.post("/api/admin/backups/full")
    assert r.status_code == 200, r.text
    for _ in range(200):
        job = admin.get("/api/admin/backups").json()["job"]
        if not job["running"]:
            break
        time.sleep(0.05)
    assert job["phase"] == "done" and job["done"] == 2 and job["error_count"] == 0, job
    root = dest / job["last_name"]
    assert (root / "files" / "Docs" / "INV-1.pdf").read_bytes() == b"%PDF-1.4 invoice one"
    assert (root / "files" / "Docs" / "2026" / "receipt.txt").read_text(encoding="utf-8") == "Chadwicks receipt total 12.50"
    app_zips = list((root / "app").glob("ledger-app-*.zip"))
    assert len(app_zips) == 1 and zipfile.ZipFile(app_zips[0]).read("ledger.db").startswith(b"SQLite format 3")
    man = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert man["kind"] == "full" and man["files_written"] == 2 and man["files_skipped"] == 0 and {f["name"] for f in man["files"]} == {"INV-1.pdf", "receipt.txt"}
    # the source folder is untouched
    assert sorted(p.name for p in folder.rglob("*") if p.is_file()) == ["INV-1.pdf", "receipt.txt"]
    # the folder downloads as one zip
    d = admin.get(f"/api/admin/backups/download?name={job['last_name']}")
    names = zipfile.ZipFile(io.BytesIO(d.content)).namelist()
    assert f"{job['last_name']}/files/Docs/INV-1.pdf" in names and f"{job['last_name']}/manifest.json" in names
    items = admin.get("/api/admin/backups").json()["items"]
    assert items[0]["kind"] == "full" and items[0]["complete"] is True and items[0]["size"] > 100
    # a file that vanished from the source is reported, not fatal
    (folder / "INV-1.pdf").unlink()
    admin.post("/api/admin/backups/full")
    for _ in range(200):
        job = admin.get("/api/admin/backups").json()["job"]
        if not job["running"]:
            break
        time.sleep(0.05)
    assert job["phase"] == "done" and job["error_count"] == 1 and "INV-1.pdf" in job["errors"][0]


def test_backup_names_folder_and_deletion_are_guarded(admin, fresh_db, tmp_path):
    assert admin.put("/api/admin/settings", json={"backup_dir": str(tmp_path / "nope")}).status_code == 400
    f = tmp_path / "file.txt"
    f.write_text("x", encoding="utf-8")
    assert admin.put("/api/admin/settings", json={"backup_dir": str(f)}).status_code == 400
    dest = tmp_path / "usb"
    dest.mkdir()
    r = admin.put("/api/admin/settings", json={"backup_dir": str(dest), "backup_every_hours": 6, "backup_keep": 2}).json()
    assert Path(r["backup_dir"]) == dest.resolve() and r["backup_every_hours"] == "6" and r["backup_keep"] == "2"
    (dest / "secret.txt").write_text("not ours", encoding="utf-8")
    for bad in ("../../etc/passwd", "secret.txt", "ledger-app-2026.zip", "ledger-full-20260101-000000/../x"):
        assert admin.get(f"/api/admin/backups/download?name={bad}").status_code in (400, 404), bad
        assert admin.delete(f"/api/admin/backups/{bad}").status_code in (400, 404, 405), bad
    assert admin.get("/api/admin/backups/download?name=ledger-app-20260101-000000.zip").status_code == 404
    assert (dest / "secret.txt").exists() and admin.get("/api/admin/backups").json()["items"] == []  # foreign files are never listed or touched
    # retention keeps the newest two application backups
    for _ in range(3):
        admin.post("/api/admin/backups/app")
        time.sleep(1.1)  # distinct stamps
    items = admin.get("/api/admin/backups").json()["items"]
    assert len(items) == 2 and all(i["kind"] == "app" for i in items) and (dest / "secret.txt").exists()
    name = items[0]["name"]
    assert admin.delete(f"/api/admin/backups/{name}").status_code == 200 and not (dest / name).exists()
    assert admin.put("/api/admin/settings", json={"backup_dir": ""}).json()["backup_dir"] == ""  # back to the default folder


def test_scheduled_application_backup(admin, fresh_db, tmp_path):
    dest = tmp_path / "usb"
    dest.mkdir()
    settings.set_many({"backup_dir": str(dest), "backup_every_hours": "0"})
    assert backup.due() is False and backup.run_scheduled() is None
    settings.set_value("backup_every_hours", "1")
    assert backup.due() is True  # nothing there yet
    item = backup.run_scheduled()
    assert item and (dest / item["name"]).exists()
    assert backup.due() is False and backup.run_scheduled() is None  # just written
    assert backup.due(now=time.time() + 3601) is True  # an hour later it is due again


def test_full_backup_waits_for_sync_and_can_be_stopped(admin, fresh_db, fake_claude, tmp_path, monkeypatch):
    folder = tmp_path / "Docs"
    folder.mkdir()
    for i in range(12):
        (folder / f"INV-{i}.pdf").write_bytes(b"%PDF-1.4 " + str(i).encode())
    src = admin.post("/api/admin/sources", json={"kind": "local", "name": "Docs", "local_folder": str(folder)}).json()
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    dest = tmp_path / "usb"
    dest.mkdir()
    settings.set_value("backup_dir", str(dest))
    # a slow source: every fetch sleeps, so Stop lands mid-way
    from app import sources
    real_fetch = sources.LocalSource.fetch

    def slow(self, meta):
        time.sleep(0.2)
        return real_fetch(self, meta)

    monkeypatch.setattr(sources.LocalSource, "fetch", slow)
    assert admin.post("/api/admin/backups/full").status_code == 200
    time.sleep(0.5)
    assert admin.post("/api/admin/backups/full").status_code == 409  # one at a time
    assert admin.post("/api/admin/backups/stop").json()["stopped"] is True
    for _ in range(100):
        job = admin.get("/api/admin/backups").json()["job"]
        if not job["running"]:
            break
        time.sleep(0.05)
    assert job["phase"] == "stopped" and 0 < job["done"] < 12 and not list(dest.glob("ledger-full-*"))  # partial folder removed
    # not while a sync is running
    monkeypatch.setattr(sync, "status", lambda: {"running": True})
    assert admin.post("/api/admin/backups/full").status_code == 409
