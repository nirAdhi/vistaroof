"""Stopping a running sync: what was read stays, the rest is picked up by the next sync."""
from __future__ import annotations

import time

from app import extract, sync

from conftest import wait_sync


def test_api_failure_stops_the_run_instead_of_failing_every_file(admin, fresh_db, fake_claude, tmp_path, monkeypatch):
    """No credit / bad key: one clear message, the run stops, touched files go back to pending (no error rows)."""
    folder = tmp_path / "Docs"
    folder.mkdir()
    for i in range(12):
        (folder / f"INV-{i}.pdf").write_bytes(b"%PDF-1.4 invoice " + str(i).encode())
    real = sync.extract.extract

    def broke(meta, content):
        raise extract.ApiUnavailable("Claude API: the Anthropic account has no credit left. Add credit under Plans & Billing.")

    monkeypatch.setattr(sync.extract, "extract", broke)
    admin.put("/api/admin/settings", json={"concurrency": 2})
    src = admin.post("/api/admin/sources", json={"kind": "local", "name": "Docs", "local_folder": str(folder)}).json()
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    st = sync.status()
    assert st["message"].startswith("Sync stopped: Claude API") and 1 <= st["errors"] <= 2, st
    run = admin.get("/api/admin/runs").json()[0]
    assert run["status"] == "stopped" and "no credit" in run["message"]
    s = admin.get("/api/admin/sources").json()[0]
    assert s["last_status"] == "failed" and "no credit" in s["last_error"]
    docs = admin.get("/api/documents").json()
    assert docs and all(d["status"] == "pending" and not d["error"] for d in docs)  # nothing recorded as a file error
    # credit added: the next sync reads everything
    monkeypatch.setattr(sync.extract, "extract", real)
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    docs = admin.get("/api/documents").json()
    assert len(docs) == 12 and all(d["status"] == "done" for d in docs)
    assert admin.get("/api/admin/sources").json()[0]["last_status"] == "ok"


def test_stop_running_sync(admin, fresh_db, fake_claude, tmp_path, monkeypatch):
    folder = tmp_path / "Docs"
    folder.mkdir()
    for i in range(12):
        (folder / f"INV-{i}.pdf").write_bytes(b"%PDF-1.4 invoice " + str(i).encode())
    real = sync.extract.extract

    def slow(meta, content):
        time.sleep(0.3)
        return real(meta, content)

    monkeypatch.setattr(sync.extract, "extract", slow)
    admin.put("/api/admin/settings", json={"concurrency": 2})  # so most files are still queued when Stop is pressed
    assert admin.post("/api/admin/sync-stop").json()["stopped"] is False  # nothing running

    src = admin.post("/api/admin/sources", json={"kind": "local", "name": "Docs", "local_folder": str(folder)}).json()
    admin.post(f"/api/admin/sources/{src['id']}/sync")
    time.sleep(0.5)
    assert admin.delete(f"/api/admin/sources/{src['id']}").status_code == 409  # cannot remove mid-run, must stop first
    r = admin.post("/api/admin/sync-stop").json()
    assert r["stopped"] is True and r["status"]["stop_requested"] is True
    wait_sync()
    st = sync.status()
    assert st["message"] == "Sync stopped" and 0 < st["processed"] < 12, st
    run = admin.get("/api/admin/runs").json()[0]
    assert run["status"] == "stopped" and run["processed"] == st["processed"]
    assert admin.get("/api/admin/sources").json()[0]["last_status"] == "stopped"
    assert all(d["status"] == "done" for d in admin.get("/api/documents").json())  # nothing half-done

    # the next sync reads only what is left, and the source can be removed once idle
    monkeypatch.setattr(sync.extract, "extract", real)
    n = len(fake_claude.calls)
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    docs = admin.get("/api/documents").json()
    assert len(docs) == 12 and all(d["status"] == "done" for d in docs)
    assert len(fake_claude.calls) == n + 12 - st["processed"]
    assert admin.delete(f"/api/admin/sources/{src['id']}").status_code == 200
