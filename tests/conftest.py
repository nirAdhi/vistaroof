"""Test fixtures: isolated database, fake Claude, fake Google Drive, logged-in clients.

Nothing here touches the network: ``FakeClaude`` replaces ``extract._call_model`` and ``FakeDrive``
replaces the googleapiclient Drive resource via ``sources.build_drive_service``.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

_TMP = tempfile.mkdtemp(prefix="ledger-test-")
os.environ["DATA_DIR"] = _TMP
os.environ["DB_PATH"] = str(Path(_TMP) / "test.db")
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ.pop("APP_SECRET_KEY", None)
os.environ.pop("PUBLIC_BASE_URL", None)
os.environ["ADMIN_EMAIL"] = ""  # explicit empty: the developer's .env / .env.production admin login must not leak into the tests
os.environ["ADMIN_PASSWORD"] = ""
os.environ["LLM_PROVIDER"] = "claude"  # the suite fakes Claude; tests/test_ollama.py switches the provider itself
os.environ["LLM_FALLBACK"] = "0"
os.environ["OLLAMA_BASE_URL"] = "http://ollama.test:11434"  # never a real host: the Ollama tests route httpx through a fake
os.environ["GEMINI_BASE_URL"] = "https://gemini.test/v1beta"  # same for Gemini
os.environ["GEMINI_API_KEY"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import db, extract, main, settings, sources, sync  # noqa: E402

settings.set_many({"watch_interval_seconds": "0", "auto_sync_minutes": "0"})  # keep the background watcher quiet during tests

FAKE_KEY = "sk-ant-api03-" + "x" * 40
ADMIN_PW = "admin-secret-123"
OWNER_PW = "owner-secret-456"


def jpeg_bytes(w: int = 120, h: int = 80) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (240, 240, 240)).save(buf, format="JPEG")
    return buf.getvalue()


# ------------------------------------------------------------------ fake Claude

CANNED = {
    "default": {
        "doc_type": "supplier_invoice", "direction": "money_out", "counterparty": "Chadwicks", "counterparty_role": "supplier",
        "document_number": "CH-1", "issue_date": "2026-03-04", "due_date": "2026-04-03", "currency": "EUR", "subtotal": 1000.0,
        "vat_amount": 230.0, "total": 1230.0, "amount_paid": None, "payment_status": "unpaid", "category": "materials_roofing",
        "project": "Oak Drive", "line_items": [{"description": "Slates", "quantity": 100, "unit_price": 10, "amount": 1000}],
        "summary": "Bill from Chadwicks for slates.", "confidence": 0.95, "flags": [],
    },
    "sales": {
        "doc_type": "sales_invoice", "direction": "money_in", "counterparty": "Murphy Homes", "counterparty_role": "customer",
        "document_number": "INV-9", "issue_date": "2026-03-10", "due_date": "2026-04-09", "currency": "EUR", "subtotal": 5000.0,
        "vat_amount": 675.0, "total": 5675.0, "amount_paid": 0, "payment_status": "unpaid", "category": "roof_repair",
        "project": "Oak Drive", "line_items": [], "summary": "Invoice to Murphy Homes.", "confidence": 0.9, "flags": [],
    },
    "bank": {
        "doc_type": "bank_statement", "direction": "none", "counterparty": "AIB", "counterparty_role": "bank", "document_number": None,
        "issue_date": "2026-03-31", "due_date": None, "currency": None, "subtotal": None, "vat_amount": None, "total": None,
        "amount_paid": None, "payment_status": "not_applicable", "category": None, "project": None, "line_items": [],
        "summary": "Bank statement.", "confidence": 0.99, "flags": [],
    },
}


class FakeClaude:
    """Stands in for extract._call_model: picks a canned answer by file name and records what it saw."""

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []
        self.fail_for: set[str] = set()

    def __call__(self, blocks):
        self.calls.append(blocks)
        tail = blocks[-1]["text"]
        name = tail.split("File name: ", 1)[1].split("\n", 1)[0]
        if name in self.fail_for:
            raise RuntimeError("simulated API failure")
        key = "sales" if "INV" in name else "bank" if "statement" in name.lower() else "default"
        return json.loads(json.dumps(CANNED[key])), "fake-model"


# ------------------------------------------------------------------ fake Google Drive


class _Req:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeDrive:
    """Minimal stand-in for the googleapiclient Drive v3 resource used by DriveSource.
    Supports files().list/get/get_media/export_media/create, about().get and changes()."""

    def __init__(self, files: list[dict], folders: list[dict] | None = None, email: str = "owner@example.com") -> None:
        self._files = files
        self._folders = [{"mimeType": "application/vnd.google-apps.folder", **f} for f in (folders or [])]
        self._email = email
        self.contents: dict[str, bytes] = {}
        self.created: list[dict] = []
        self.pending_changes: list[dict] = []
        self.page = 100

    # -- helpers for tests
    def add_file(self, f: dict, data: bytes = b"%PDF-1.4 new") -> None:
        self._files.append(f)
        self.contents[f["id"]] = data
        self.pending_changes.append({"fileId": f["id"], "removed": False, "file": {"id": f["id"], "name": f["name"], "mimeType": f["mimeType"], "trashed": False}})

    def remove_file(self, file_id: str) -> None:
        self._files = [f for f in self._files if f["id"] != file_id]
        self.pending_changes.append({"fileId": file_id, "removed": True})

    def about(self):
        return SimpleNamespace(get=lambda fields: _Req({"user": {"emailAddress": self._email}}))

    def changes(self):
        drive = self

        class Changes:
            def getStartPageToken(self, supportsAllDrives=False):
                return _Req({"startPageToken": str(drive.page)})

            def list(self, pageToken, **kw):
                changes, drive.pending_changes = drive.pending_changes, []
                drive.page += 1
                return _Req({"changes": changes, "newStartPageToken": str(drive.page)})

        return Changes()

    def files(self):
        drive = self

        class Files:
            def list(self, q, fields, pageSize, pageToken, supportsAllDrives, includeItemsFromAllDrives):
                if "mimeType = 'application/vnd.google-apps.folder'" in q:
                    if "name = " in q:  # upload folder lookup
                        name = q.split("name = '", 1)[1].split("'", 1)[0]
                        return _Req({"files": [f for f in drive._folders if f["name"] == name]})
                    return _Req({"files": drive._folders})
                if "in parents" in q:
                    parent = q.split("'")[1]
                    items = [f for f in drive._files + drive._folders if parent in (f.get("parents") or [])]
                    return _Req({"files": items})
                return _Req({"files": drive._files})

            def get(self, fileId, fields, supportsAllDrives=False):
                for f in drive._folders:
                    if f["id"] == fileId:
                        return _Req({"name": f["name"]})
                return _Req({"name": "folder"})

            def get_media(self, fileId, supportsAllDrives=False):
                return _Req(drive.contents.get(fileId, b"%PDF-1.4 fake"))

            def export_media(self, fileId, mimeType):
                return _Req(drive.contents.get(fileId, b"exported"))

            def create(self, body, media_body=None, fields="", supportsAllDrives=False):
                if body.get("mimeType") == "application/vnd.google-apps.folder":
                    folder = {"id": f"folder-{len(drive._folders) + 1}", "name": body["name"], "parents": body.get("parents", []),
                              "mimeType": "application/vnd.google-apps.folder"}
                    drive._folders.append(folder)
                    return _Req({"id": folder["id"]})
                fid = f"up-{len(drive.created) + 1}"
                if media_body is not None:
                    media_body._fd.seek(0)  # MediaIoBaseUpload leaves the stream at the end after measuring its size
                    data = media_body._fd.read()
                else:
                    data = b""
                meta = {"id": fid, "name": body["name"], "mimeType": media_body.mimetype() if media_body is not None else "application/octet-stream",
                        "modifiedTime": "2026-05-01T10:00:00Z", "size": str(len(data)), "parents": body.get("parents", []),
                        "webViewLink": f"https://drive.google.com/file/d/{fid}/view"}
                drive.created.append({**meta, "data": data})
                drive._files.append(meta)
                drive.contents[fid] = data
                return _Req(meta)

        return Files()


def install_fake_drive(monkeypatch, drive: FakeDrive) -> None:
    monkeypatch.setattr(sources, "build_drive_service", lambda creds: drive)
    monkeypatch.setattr(sources, "credentials_for", lambda source: SimpleNamespace(valid=True))


def connect_fake_drive(source_id: int, with_upload_scope: bool = True) -> None:
    """Pretend the OAuth flow completed for this source."""
    scopes = [sources.SCOPE_READ] + ([sources.SCOPE_FILE] if with_upload_scope else [])
    sources.save_token(source_id, json.dumps({"token": "t", "refresh_token": "r", "scopes": scopes}))
    db.update_source(source_id, account_email="drive@example.com", last_status="connected")


def wait_sync() -> None:
    import time

    for _ in range(300):
        if not sync.status()["running"]:
            return
        time.sleep(0.05)
    raise AssertionError("sync did not finish")


# ------------------------------------------------------------------ fixtures


@pytest.fixture(scope="session")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    main._attempts.clear()
    sync._rerun.clear()


@pytest.fixture()
def fresh_db():
    with db.connect() as conn:
        conn.execute("DELETE FROM documents")
        conn.execute("DELETE FROM sources")
        conn.execute("DELETE FROM sync_runs")
        conn.execute("DELETE FROM users")
        conn.execute("DELETE FROM settings WHERE key IN ('anthropic_api_key', 'gemini_api_key', 'gemini_model', 'google_client_config', "
                     "'ms_client_config', 'llm_provider', 'llm_fallback', 'backup_dir', 'backup_every_hours', 'backup_keep')")
    settings.invalidate()


@pytest.fixture()
def fake_claude(monkeypatch):
    fake = FakeClaude()
    monkeypatch.setattr(extract, "_call_model", fake)
    settings.set_value("anthropic_api_key", FAKE_KEY)
    return fake


@pytest.fixture()
def admin(client):
    """Client logged in as admin (creates the admin password if needed)."""
    client.cookies.clear()
    if settings.get("admin_password_hash"):
        r = client.post("/api/admin/login", json={"password": ADMIN_PW})
    else:
        r = client.post("/api/admin/setup", json={"password": ADMIN_PW})
    assert r.status_code == 200, r.text
    return client


def make_user(admin_client, email: str = "cormac@example.ie", name: str = "Cormac", role: str = "user", password: str = OWNER_PW) -> dict:
    """Create a user account through the admin API and return its row."""
    r = admin_client.post("/api/admin/users", json={"email": email, "name": name, "role": role, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["user"]


def login_as(email: str, password: str = OWNER_PW) -> TestClient:
    """A fresh browser signed in through the dashboard login box."""
    c = TestClient(main.app)
    r = c.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return c
