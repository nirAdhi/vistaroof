"""Microsoft 365 (SharePoint / OneDrive) source: registration, OAuth, listing, downloads, delta watcher, uploads."""
from __future__ import annotations

import json
import time

from app import db, microsoft, settings, sync

from conftest import jpeg_bytes, wait_sync
from fake_graph import FakeGraph, install_fake_graph, sharepoint_fixture

LINK = "https://vistaroofing.sharepoint.com/:f:/s/HRAccounts/EabcInvoicesLink"


def _register(admin, secret: str = "s3cret") -> None:
    r = admin.post("/api/admin/ms-client", json={"client_id": "11111111-2222-3333-4444-555555555555", "client_secret": secret, "tenant": "common"})
    assert r.status_code == 200, r.text


def _connect(admin, fake: FakeGraph, source_id: int) -> str:
    """Run the Connect → Microsoft → callback round trip against the fake. Returns the redirect location."""
    r = admin.get(f"/admin/sources/{source_id}/connect", follow_redirects=False)
    assert r.status_code == 303, r.text
    state = r.headers["location"].split("state=", 1)[1].split("&", 1)[0]
    r = admin.get(f"/admin/oauth/ms-callback?state={state}&code=good-code", follow_redirects=False)
    assert r.status_code == 303
    return r.headers["location"]


def _files(*items):
    return [("files", (name, data, mime)) for name, data, mime in items]


# ------------------------------------------------------------------ registration (settings)


def test_ms_registration_roundtrip(admin, fresh_db):
    assert admin.get("/api/admin/overview").json()["settings"]["has_ms_client"] is False
    assert admin.post("/api/admin/ms-client", json={"client_id": "bad client id!", "client_secret": "x"}).status_code == 400
    assert admin.post("/api/admin/ms-client", json={"client_id": "11111111-2222-3333-4444-555555555555", "tenant": "not a tenant"}).status_code == 400
    _register(admin)
    s = admin.get("/api/admin/overview").json()["settings"]
    assert s["has_ms_client"] and s["ms_client_id"].startswith("1111") and s["ms_tenant"] == "common" and s["ms_has_secret"] is True
    assert "s3cret" not in json.dumps(s)
    # blank secret keeps the saved one; "-" clears it; tenant can be a domain
    admin.post("/api/admin/ms-client", json={"client_id": "11111111-2222-3333-4444-555555555555", "client_secret": "", "tenant": "vistaroofing.ie"})
    cfg = microsoft.client_config()
    assert cfg["client_secret"] == "s3cret" and cfg["tenant"] == "vistaroofing.ie"
    admin.post("/api/admin/ms-client", json={"client_id": "11111111-2222-3333-4444-555555555555", "client_secret": "-"})
    assert microsoft.client_config()["client_secret"] == ""
    assert admin.delete("/api/admin/ms-client").json()["has_ms_client"] is False


def test_ms_source_validation(admin, fresh_db):
    assert admin.post("/api/admin/sources", json={"kind": "m365", "name": "HR"}).status_code == 400  # no registration yet
    _register(admin)
    assert admin.post("/api/admin/sources", json={"kind": "m365", "name": "HR", "ms_link": "not a link"}).status_code == 400
    assert admin.post("/api/admin/sources", json={"kind": "m365", "name": "HR", "ms_tenant": "bad tenant!"}).status_code == 400
    src = admin.post("/api/admin/sources", json={"kind": "m365", "name": "HR Invoices", "ms_link": LINK, "ms_tenant": "vistaroofing.ie", "ms_upload": False}).json()
    assert src["kind"] == "m365" and src["last_status"] == "not_connected" and src["ms_link"] == LINK and src["ms_tenant"] == "vistaroofing.ie" and src["ms_upload"] == 0
    assert src["connected"] is False if "connected" in src else True
    assert admin.post(f"/api/admin/sources/{src['id']}/sync").status_code == 400  # must connect first
    shared = admin.post("/api/admin/sources", json={"kind": "m365", "name": "Shared with me", "ms_link": " Shared "}).json()
    assert shared["ms_link"] == "shared"


# ------------------------------------------------------------------ OAuth + first sync


def test_ms_connect_resolves_link_and_syncs(admin, fresh_db, fake_claude, monkeypatch):
    fake = sharepoint_fixture()
    install_fake_graph(monkeypatch, fake)
    _register(admin)
    src = admin.post("/api/admin/sources", json={"kind": "m365", "name": "HR Invoices", "ms_link": LINK, "ms_tenant": "vistaroofing.ie"}).json()

    # the consent URL: right tenant, scopes, PKCE and redirect
    r = admin.get(f"/admin/sources/{src['id']}/connect", follow_redirects=False)
    url = r.headers["location"]
    assert url.startswith("https://login.microsoftonline.com/vistaroofing.ie/oauth2/v2.0/authorize?")
    assert "client_id=11111111-2222-3333-4444-555555555555" in url and "Files.ReadWrite.All" in url and "Sites.Read.All" in url
    assert "code_challenge_method=S256" in url and "redirect_uri=http%3A%2F%2Ftestserver%2Fadmin%2Foauth%2Fms-callback" in url
    state = url.split("state=", 1)[1].split("&", 1)[0]

    # bad state / cancelled sign-in are handled
    assert "expired" in admin.get("/admin/oauth/ms-callback?state=nope&code=x", follow_redirects=False).headers["location"]
    r2 = admin.get(f"/admin/sources/{src['id']}/connect", follow_redirects=False)
    st2 = r2.headers["location"].split("state=", 1)[1].split("&", 1)[0]
    loc = admin.get(f"/admin/oauth/ms-callback?state={st2}&error=access_denied&error_description=User+cancelled", follow_redirects=False).headers["location"]
    assert "cancelled" in loc

    # the real thing
    loc = admin.get(f"/admin/oauth/ms-callback?state={state}&code=good-code", follow_redirects=False).headers["location"]
    assert "Connected%20suraj%40prasanit.org" in loc and "Sync%20started" in loc
    tok = fake.token_calls[0]
    assert tok["tenant"] == "vistaroofing.ie" and tok["client_secret"] == "s3cret" and tok["code_verifier"]
    wait_sync()

    s = admin.get("/api/admin/sources").json()[0]
    assert s["connected"] and s["account_email"] == "suraj@prasanit.org" and "token_json" not in s and s["can_upload"] is True
    assert json.loads(s["ms_location"]) == {"mode": "folder", "drive_id": "drv1", "item_id": "inv", "label": "HRAccounts/Invoices",
                                            "web_url": "https://vistaroofing.sharepoint.com/sites/HRAccounts/Shared%20Documents/Invoices"}
    assert s["last_status"] == "ok" and s["done_count"] == 4 and s["doc_count"] == 5
    docs = {d["name"]: d for d in admin.get("/api/documents").json()}
    assert docs["INV-1001 Murphy.pdf"]["folder"] == "HR Invoices/HRAccounts/Invoices" and docs["INV-1001 Murphy.pdf"]["treatment"] == "revenue"
    assert docs["Chadwicks bill.pdf"]["folder"] == "HR Invoices/HRAccounts/Invoices/2026" and docs["Chadwicks bill.pdf"]["treatment"] == "expense"
    assert docs["till receipt.jpg"]["status"] == "done" and docs["AIB statement.txt"]["treatment"] == "none"
    assert docs["notes.one"]["status"] == "skipped" and docs["INV-1001 Murphy.pdf"]["source_id"] == "drv1:f1"
    assert docs["INV-1001 Murphy.pdf"]["web_link"].startswith("https://x.sharepoint.com/")
    # downloads followed the 302 to the download host WITHOUT the bearer token
    assert fake.download_headers and all("authorization" not in {k.lower() for k in h} for h in fake.download_headers)
    # preview serves the real bytes
    r = admin.get(f"/api/documents/{docs['INV-1001 Murphy.pdf']['id']}/preview")
    assert r.status_code == 200 and r.content == b"%PDF-1.4 inv 1001"


def test_ms_link_not_visible_to_account(admin, fresh_db, fake_claude, monkeypatch):
    fake = sharepoint_fixture()
    install_fake_graph(monkeypatch, fake)
    _register(admin)
    src = admin.post("/api/admin/sources", json={"kind": "m365", "name": "Wrong link", "ms_link": "https://vistaroofing.sharepoint.com/:f:/s/Other/NotShared"}).json()
    loc = _connect(admin, fake, src["id"])
    assert "could%20not%20be%20opened" in loc or "could not be opened" in loc
    s = admin.get("/api/admin/sources").json()[0]
    assert s["connected"] and s["last_status"] == "failed" and "not visible" in s["last_error"]
    # fixing the link on a connected source resolves it immediately
    r = admin.patch(f"/api/admin/sources/{src['id']}", json={"ms_link": LINK})
    assert r.status_code == 200 and json.loads(r.json()["ms_location"])["item_id"] == "inv" and r.json()["last_error"] is None
    assert admin.patch(f"/api/admin/sources/{src['id']}", json={"ms_link": "https://vistaroofing.sharepoint.com/:f:/s/Other/Still"}).status_code == 400


# ------------------------------------------------------------------ watcher (delta)


def test_ms_watcher_delta_add_and_delete(admin, fresh_db, fake_claude, monkeypatch):
    fake = sharepoint_fixture()
    install_fake_graph(monkeypatch, fake)
    _register(admin)
    src = admin.post("/api/admin/sources", json={"kind": "m365", "name": "HR", "ms_link": LINK}).json()
    _connect(admin, fake, src["id"]); wait_sync()
    n_calls = len(fake_claude.calls)

    r = admin.post("/api/admin/watch-now").json()
    assert r["changed_sources"] == []  # first pass seeds the cursor
    cursor = json.loads(db.get_source(src["id"])["drive_page_token"])
    assert list(cursor) == ["drv1:inv"] and "token=" in cursor["drv1:inv"]
    assert admin.post("/api/admin/watch-now").json()["changed_sources"] == []

    fake.add_file("drv1", "y2026", "f9", "Screwfix receipt.jpg", "image/jpeg", jpeg_bytes(), modified="2026-03-05T09:00:00Z")
    r = admin.post("/api/admin/watch-now").json()
    assert r["changed_sources"] == [src["id"]] and r["started"] is True
    wait_sync()
    assert len(fake_claude.calls) == n_calls + 1
    docs = {d["name"]: d for d in admin.get("/api/documents").json()}
    assert docs["Screwfix receipt.jpg"]["status"] == "done" and admin.get("/api/admin/runs").json()[0]["trigger"] == "watch"

    fake.remove_file("drv1", "f9")
    assert admin.post("/api/admin/watch-now").json()["changed_sources"] == [src["id"]]
    wait_sync()
    assert "Screwfix receipt.jpg" not in [d["name"] for d in admin.get("/api/documents").json()]
    assert admin.get("/api/admin/runs").json()[0]["removed"] == 1


# ------------------------------------------------------------------ uploads (app -> SharePoint)


def test_ms_upload_into_folder_source(admin, fresh_db, fake_claude, monkeypatch):
    fake = sharepoint_fixture()
    install_fake_graph(monkeypatch, fake)
    _register(admin)
    src = admin.post("/api/admin/sources", json={"kind": "m365", "name": "HR", "ms_link": LINK}).json()
    _connect(admin, fake, src["id"]); wait_sync()

    t = admin.get("/api/upload/targets").json()[0]
    assert t["can_upload"] is True and t["folder"] == "HRAccounts/Invoices" and t["kind"] == "m365"
    r = admin.post("/api/upload", data={"source_id": src["id"]}, files=_files(("INV-2001.pdf", b"%PDF-1.4 new invoice", "application/pdf")))
    assert r.status_code == 200, r.text
    res = r.json()["results"][0]
    assert res["ok"] and res["status"] == "done" and res["doc_type"] == "sales_invoice"
    assert fake.created[0] == {"kind": "file", "name": "INV-2001.pdf", "data": b"%PDF-1.4 new invoice", "parent": "inv", "drive": "drv1"}
    doc = admin.get(f"/api/documents/{res['doc_id']}").json()
    assert doc["uploaded_by"] == "app" and doc["source_id"] == "drv1:up-1" and doc["folder"] == "HR/(uploads)"

    # a big file goes through an upload session in 5 MiB chunks, without the bearer header
    big = b"%PDF-1.4 " + b"x" * (6 * 1024 * 1024)
    r = admin.post("/api/upload", data={"source_id": src["id"]}, files=_files(("big scan.pdf", big, "application/pdf")))
    assert r.status_code == 200 and r.json()["results"][0]["ok"], r.text
    sess = list(fake.sessions.values())[0]
    assert len(sess["ranges"]) == 2 and bytes(sess["buf"]) == big

    # a later sync sees the uploaded files as unchanged (no extra Claude calls)
    n = len(fake_claude.calls)
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    assert len(fake_claude.calls) == n

    # "Can view" only: Graph says 403 -> clear per-file error, not a crash
    fake.readonly = True
    r = admin.post("/api/upload", data={"source_id": src["id"]}, files=_files(("x.pdf", b"%PDF-1.4", "application/pdf")))
    assert r.status_code == 200 and r.json()["results"][0]["ok"] is False and "403" in r.json()["results"][0]["error"]


def test_ms_read_only_connection_and_shared_mode(admin, fresh_db, fake_claude, monkeypatch):
    fake = FakeGraph(granted_scope="User.Read Files.Read.All Sites.Read.All")  # user declined / admin didn't request write
    fake.add_drive("od1", "https://prasanit-my.sharepoint.com/personal/suraj")
    fake.add_file("od1", "root", "o1", "receipt.jpg", "image/jpeg", b"\xff\xd8 od")
    # something shared with the account from another organisation
    fake.add_drive("drv7", "https://vistaroofing.sharepoint.com/sites/VistaFiles/Shared Documents")
    fake.add_folder("drv7", "root", "proj", "Projects")
    fake.add_file("drv7", "proj", "p1", "Quote - Oak Drive.pdf", "application/pdf", b"%PDF-1.4 quote")
    fake.add_file("drv7", "root", "p2", "Loose invoice.pdf", "application/pdf", b"%PDF-1.4 loose")
    fake.shared_with_me = [
        {"id": "s1", "name": "Projects", "remoteItem": {**fake.items["drv7"]["proj"], "parentReference": {"driveId": "drv7"}}},
        {"id": "s2", "name": "Loose invoice.pdf", "remoteItem": {**fake.items["drv7"]["p2"], "parentReference": {"driveId": "drv7"}}},
    ]
    install_fake_graph(monkeypatch, fake)
    _register(admin)

    onedrive = admin.post("/api/admin/sources", json={"kind": "m365", "name": "My OneDrive", "ms_upload": False}).json()
    r = admin.get(f"/admin/sources/{onedrive['id']}/connect", follow_redirects=False)
    assert "Files.ReadWrite.All" not in r.headers["location"]  # read-only requested
    _connect(admin, fake, onedrive["id"]); wait_sync()
    s = admin.get("/api/admin/sources").json()[0]
    assert s["can_upload"] is False and json.loads(s["ms_location"])["mode"] == "onedrive"
    assert admin.get("/api/documents").json()[0]["folder"] == "My OneDrive/OneDrive"
    r = admin.post("/api/upload", data={"source_id": onedrive["id"]}, files=_files(("a.pdf", b"%PDF-1.4", "application/pdf")))
    assert r.status_code == 400 and "create-files permission" in r.json()["detail"]

    shared = admin.post("/api/admin/sources", json={"kind": "m365", "name": "Shared by Cormac", "ms_link": "shared", "ms_tenant": "vistaroofing.ie"}).json()
    _connect(admin, fake, shared["id"]); wait_sync()
    docs = {d["name"]: d for d in admin.get(f"/api/documents?source_ref={shared['id']}").json()}
    assert docs["Quote - Oak Drive.pdf"]["folder"] == "Shared by Cormac/Shared/Projects" and docs["Loose invoice.pdf"]["folder"] == "Shared by Cormac/Shared"
    targets = {t["name"]: t for t in admin.get("/api/upload/targets").json()}
    assert targets["Shared by Cormac"]["can_upload"] is False and "specific folder" in targets["Shared by Cormac"]["reason"]
    # watcher seeds one delta link per shared folder
    admin.post("/api/admin/watch-now")
    assert list(json.loads(db.get_source(shared["id"])["drive_page_token"])) == ["drv7:proj"]


def test_ms_upload_to_whole_onedrive_creates_ledger_uploads_folder(admin, fresh_db, fake_claude, monkeypatch):
    fake = FakeGraph()
    fake.add_drive("od1", "https://x-my.sharepoint.com/personal/me")
    install_fake_graph(monkeypatch, fake)
    _register(admin)
    src = admin.post("/api/admin/sources", json={"kind": "m365", "name": "OneDrive"}).json()
    _connect(admin, fake, src["id"]); wait_sync()
    r = admin.post("/api/upload", data={"source_id": src["id"]}, files=_files(("r.jpg", b"\xff\xd8 r", "image/jpeg")))
    assert r.status_code == 200 and r.json()["results"][0]["ok"], r.text
    assert fake.created[0] == {"kind": "folder", "name": "Ledger Uploads", "id": "folder-1"} and fake.created[1]["parent"] == "folder-1"
    # second upload reuses the folder and renames the duplicate
    admin.post("/api/upload", data={"source_id": src["id"]}, files=_files(("r.jpg", b"\xff\xd8 r2", "image/jpeg")))
    assert [c["name"] for c in fake.created if c["kind"] == "file"] == ["r.jpg", "r 1.jpg"]
    assert admin.get("/api/documents").json()[0]["folder"] == "OneDrive/Ledger Uploads"


# ------------------------------------------------------------------ token refresh


def test_ms_token_refresh_and_reconnect_message(admin, fresh_db, fake_claude, monkeypatch):
    fake = sharepoint_fixture()
    install_fake_graph(monkeypatch, fake)
    _register(admin)
    src = admin.post("/api/admin/sources", json={"kind": "m365", "name": "HR", "ms_link": LINK}).json()
    _connect(admin, fake, src["id"]); wait_sync()

    # expire the stored token: the next sync refreshes it transparently and saves the new one
    tok = microsoft.token_info(db.get_source(src["id"]))
    tok["expires_at"] = time.time() - 10
    microsoft.save_token(src["id"], tok)
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    assert any(c.get("grant_type") == "refresh_token" and c.get("refresh_token") == "rt1" for c in fake.token_calls)
    new = microsoft.token_info(db.get_source(src["id"]))
    assert new["access_token"] == "at2" and new["refresh_token"] == "rt2" and new["scope"] and new["expires_at"] > time.time()
    assert admin.get("/api/admin/sources").json()[0]["last_status"] == "ok"

    # refresh token revoked: the source reports it needs reconnecting instead of crashing
    tok = microsoft.token_info(db.get_source(src["id"]))
    tok["expires_at"] = time.time() - 10
    microsoft.save_token(src["id"], tok)
    fake.refresh_fails = True
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    s = admin.get("/api/admin/sources").json()[0]
    assert s["last_status"] == "failed" and "reconnected" in s["last_error"]
