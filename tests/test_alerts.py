"""Email alerts (reader down / back, sync stopped) and the dashboard staying live while the reader is down."""
from __future__ import annotations

import json

import pytest

from app import config, crypto, db, llm, notify, settings, sync
from app.sources import FileContent, FileMeta

from conftest import wait_sync
from fake_ollama import FakeOllama, install_fake_ollama

SMTP = {"smtp_host": "mail.test", "smtp_port": 587, "smtp_security": "starttls", "smtp_user": "ledger@test.ie",
        "smtp_password": "app-password-123", "smtp_from": "ledger@test.ie", "alert_to": "suraj@clinitech.ie, cormac@vistaroofing.ie", "alerts_enabled": True}


@pytest.fixture()
def outbox(monkeypatch):
    sent: list[tuple[str, str, str, dict]] = []
    monkeypatch.setattr(notify, "DELIVER_IN_THREAD", False)
    monkeypatch.setattr(notify, "_deliver", lambda msg, cfg: sent.append((msg["Subject"], msg["To"], msg.get_content(), cfg)))
    notify._last_sent.clear()
    return sent


def test_alert_settings_are_validated_saved_and_the_password_hidden(admin, fresh_db, outbox):
    assert admin.put("/api/admin/settings", json={"alert_to": "not-an-email"}).status_code == 400
    assert admin.put("/api/admin/settings", json={"smtp_security": "tls13"}).status_code == 400
    assert admin.put("/api/admin/settings", json={"smtp_from": "nope"}).status_code == 400
    assert admin.put("/api/admin/settings", json={"smtp_port": 70000}).status_code == 422
    r = admin.put("/api/admin/settings", json=SMTP).json()
    assert r["smtp_host"] == "mail.test" and r["alert_to"] == "suraj@clinitech.ie, cormac@vistaroofing.ie" and r["has_smtp_password"] is True
    assert "app-password-123" not in json.dumps(r) and "smtp_password" not in r
    assert "app-password-123" not in json.dumps(admin.get("/api/admin/overview").json())
    a = admin.get("/api/admin/overview").json()["alerts"]
    assert a["configured"] is True and a["enabled"] is True and a["recipients"] == ["suraj@clinitech.ie", "cormac@vistaroofing.ie"]
    # test email
    resp = admin.post("/api/admin/alerts/test")
    assert resp.status_code == 200, resp.text
    r = resp.json()
    assert r["ok"] and len(outbox) == 1 and outbox[0][0] == "[Roofing Ledger] Test alert" and outbox[0][1] == "suraj@clinitech.ie, cormac@vistaroofing.ie"
    assert outbox[0][3]["password"] == "app-password-123" and outbox[0][3]["security"] == "starttls"
    # unconfigured → clear 400
    settings.set_value("smtp_host", "")
    assert admin.post("/api/admin/alerts/test").status_code == 400


def test_smtp_password_is_encrypted_at_rest(admin, fresh_db, monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "a-long-random-operator-secret")
    crypto._reset_for_tests()
    try:
        admin.put("/api/admin/settings", json={"smtp_password": "very-secret-app-pw"})
        with db.connect() as conn:
            raw = conn.execute("SELECT value FROM settings WHERE key='smtp_password'").fetchone()["value"]
        assert raw.startswith("enc:") and "very-secret" not in raw and settings.get("smtp_password") == "very-secret-app-pw"
    finally:
        monkeypatch.delenv("APP_SECRET_KEY", raising=False)
        crypto._reset_for_tests()
        settings.set_value("smtp_password", "")


def test_reader_down_and_back_sends_one_email_each(admin, fresh_db, monkeypatch, outbox):
    fake = FakeOllama()
    install_fake_ollama(monkeypatch, fake)
    settings.set_many({k: ("1" if v is True else v) for k, v in SMTP.items()})
    llm._status.update(state="unknown")
    assert llm.refresh_status()["state"] == "connected" and outbox == []  # start-up: connected, nothing to say
    fake.down = True
    assert llm.refresh_status()["state"] == "unreachable"
    assert len(outbox) == 1
    subject, to, body, _ = outbox[0]
    assert subject == "[Roofing Ledger] Ollama unreachable" and "Is the Ollama PC awake" in body and "dashboard and every figure already read keep working" in body
    assert "switched on, awake" in body
    llm.refresh_status(); llm.refresh_status()
    assert len(outbox) == 1  # still down: no repeat (cooldown)
    fake.down = False
    assert llm.refresh_status()["state"] == "connected" and len(outbox) == 2 and outbox[1][0] == "[Roofing Ledger] Ollama connected again"
    # alerts off: silence
    settings.set_value("alerts_enabled", "0")
    fake.down = True
    llm.refresh_status()
    assert len(outbox) == 2
    # missing model is an alert too
    settings.set_value("alerts_enabled", "1")
    fake.down = False
    monkeypatch.setattr(config, "OLLAMA_MODEL", "phi3:mini")
    assert llm.refresh_status()["state"] == "model_missing" and outbox[-1][0] == "[Roofing Ledger] Ollama model not installed"


def test_sync_stopped_by_reader_failure_sends_an_email(admin, fresh_db, monkeypatch, outbox, tmp_path):
    fake = FakeOllama()
    install_fake_ollama(monkeypatch, fake)
    settings.set_many({k: ("1" if v is True else v) for k, v in SMTP.items()})
    folder = tmp_path / "Docs"
    folder.mkdir()
    (folder / "INV-1.txt").write_text("INVOICE INV-1 total 100", encoding="utf-8")
    src = admin.post("/api/admin/sources", json={"kind": "local", "name": "Docs", "local_folder": str(folder)}).json()
    fake.down = True
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    subjects = [s for s, *_ in outbox]
    assert "[Roofing Ledger] Sync stopped: reader unavailable" in subjects and "not reachable" in outbox[-1][2]
    n = len(outbox)
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    assert len(outbox) == n  # the same event is not repeated within the cooldown


def test_dashboard_stays_live_while_the_reader_is_down(admin, fresh_db, monkeypatch, tmp_path, fake_claude):
    """Every figure already read is served from the database; a dead reader changes nothing on the dashboard."""
    folder = tmp_path / "Docs"
    folder.mkdir()
    (folder / "INV-1.pdf").write_bytes(b"%PDF-1.4 invoice one")
    src = admin.post("/api/admin/sources", json={"kind": "local", "name": "Docs", "local_folder": str(folder)}).json()
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    before = admin.get("/api/summary").json()["totals"]
    assert before["documents"] == 1 and before["revenue"] > 0
    # now the reader is Ollama and its PC is off
    fake = FakeOllama()
    install_fake_ollama(monkeypatch, fake)
    settings.set_value("anthropic_api_key", "")
    fake.down = True
    assert llm.refresh_status()["state"] == "unreachable"
    for path in ("/api/summary", "/api/documents", "/api/companies", "/api/config", "/api/version", "/api/sync/status", "/api/meta"):
        assert admin.get(path).status_code == 200, path
    assert admin.get("/api/summary").json()["totals"] == before
    doc = admin.get("/api/documents").json()[0]
    assert admin.get(f"/api/documents/{doc['id']}/preview").status_code == 200  # the file itself still opens (from the source)
    assert admin.get(f"/api/documents/{doc['id']}").json()["status"] == "done"
    r = admin.patch(f"/api/documents/{doc['id']}", json={"project": "Oak Drive"})  # hand edits still work
    assert r.status_code == 200 and r.json()["project"] == "Oak Drive"
    assert admin.get("/api/config").json()["llm_status"]["state"] == "unreachable"
    # a new file is not read until the reader is back; it waits as pending, nothing is lost
    (folder / "INV-2.txt").write_text("INVOICE INV-2 total 200", encoding="utf-8")
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    names = {d["name"]: d["status"] for d in admin.get("/api/documents?limit=100").json()}
    assert names["INV-1.pdf"] == "done" and names["INV-2.txt"] == "pending"
    assert admin.get("/api/summary").json()["totals"] == before
    fake.down = False
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    assert {d["name"]: d["status"] for d in admin.get("/api/documents?limit=100").json()}["INV-2.txt"] == "done"
