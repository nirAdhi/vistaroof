"""Authentication, roles, sessions, rate limiting, CSRF, headers, encryption at rest, backup."""
from __future__ import annotations

import os
import time

from fastapi.testclient import TestClient

from app import crypto, db, main, settings, sources

from conftest import ADMIN_PW, OWNER_PW


def test_everything_requires_login(client, fresh_db):
    client.cookies.clear()
    for path in ("/api/config", "/api/summary", "/api/documents", "/api/companies", "/api/version", "/api/meta", "/api/sync/status"):
        assert client.get(path).status_code == 401, path
    assert client.post("/api/sync").status_code == 401
    assert client.post("/api/ask", json={"question": "x"}).status_code == 401
    assert client.get("/api/admin/overview").status_code == 401
    # pages and health stay reachable (the JS shows the login box)
    assert client.get("/").status_code == 200 and client.get("/admin").status_code == 200
    h = client.get("/healthz").json()
    assert h["ok"] is True and "version" in h


def test_admin_setup_login_and_password_rules(client, fresh_db):
    settings.set_many({"admin_password_hash": ""})
    client.cookies.clear()
    me = client.get("/api/admin/me").json()
    assert me["authenticated"] is False and me["needs_setup"] is True
    assert client.get("/api/auth/me").json()["needs_setup"] is True
    assert client.post("/api/admin/setup", json={"password": "short"}).status_code == 400
    assert client.post("/api/admin/setup", json={"password": ADMIN_PW}).status_code == 200
    assert client.get("/api/admin/me").json()["authenticated"] is True
    assert client.post("/api/admin/setup", json={"password": "another-pw-123"}).status_code == 400
    client.post("/api/admin/logout")
    assert client.get("/api/admin/me").json() == {"authenticated": False, "role": None, "needs_setup": False}
    assert client.post("/api/admin/login", json={"password": "wrong-password"}).status_code == 401
    assert client.post("/api/admin/login", json={"password": ADMIN_PW}).status_code == 200
    assert client.post("/api/admin/password", json={"current": "nope", "new": "new-password-1"}).status_code == 400
    assert client.post("/api/admin/password", json={"current": ADMIN_PW, "new": "short"}).status_code == 400
    assert client.post("/api/admin/password", json={"current": ADMIN_PW, "new": "new-password-1"}).status_code == 200
    assert client.get("/api/admin/me").json()["authenticated"] is True  # our own session was renewed
    client.post("/api/admin/logout")
    assert client.post("/api/admin/login", json={"password": "new-password-1"}).status_code == 200
    client.post("/api/admin/password", json={"current": "new-password-1", "new": ADMIN_PW})  # restore for other tests


def test_owner_role_sees_dashboard_but_not_admin(admin, fresh_db):
    assert admin.post("/api/admin/owner-password", json={"password": "short"}).status_code == 400
    assert admin.post("/api/admin/owner-password", json={"password": OWNER_PW}).json()["has_owner_password"] is True
    owner = TestClient(main.app)
    assert owner.get("/api/summary").status_code == 401
    r = owner.post("/api/auth/login", json={"password": OWNER_PW})
    assert r.status_code == 200 and r.json()["role"] == "owner"
    assert owner.get("/api/summary").status_code == 200
    assert owner.get("/api/config").json()["role"] == "owner"
    assert owner.get("/api/admin/overview").status_code == 403
    assert owner.put("/api/admin/settings", json={"company_name": "x"}).status_code == 403
    assert owner.get("/admin/sources/1/connect", follow_redirects=False).status_code == 303  # bounced to /admin
    # the shared login box also accepts the admin password and grants admin
    owner.post("/api/auth/logout")
    assert owner.post("/api/auth/login", json={"password": ADMIN_PW}).json()["role"] == "admin"
    assert owner.get("/api/admin/overview").status_code == 200
    # removing the owner password makes the owner password stop working
    admin.post("/api/admin/owner-password", json={"password": ""})
    other = TestClient(main.app)
    assert other.post("/api/auth/login", json={"password": OWNER_PW}).status_code == 401


def test_session_tokens_expire_and_cannot_be_forged():
    tok = settings.make_session_token("owner")
    assert settings.session_role(tok) == "owner"
    role, exp, nonce, sig = tok.split(".")
    assert settings.session_role(f"admin.{exp}.{nonce}.{sig}") is None  # role swap breaks the signature
    assert settings.session_role(f"{role}.{int(time.time()) - 10}.{nonce}.{sig}") is None  # expired + re-signed needed
    assert settings.session_role("garbage") is None and settings.session_role(None) is None


def test_login_rate_limit(client, fresh_db):
    client.cookies.clear()
    codes = [client.post("/api/auth/login", json={"password": "bad-password"}).status_code for _ in range(12)]
    assert codes[:10] == [401] * 10 and codes[10] == 429 and codes[11] == 429
    main._attempts.clear()
    assert client.post("/api/auth/login", json={"password": "bad-password"}).status_code == 401


def test_cross_site_requests_are_blocked(admin, fresh_db):
    r = admin.post("/api/sync", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = admin.post("/api/admin/demo/load", headers={"Origin": "http://testserver"})
    assert r.status_code == 200
    r = admin.get("/api/config", headers={"Origin": "https://evil.example"})  # GETs are not state-changing
    assert r.status_code == 200


def test_security_headers_present(client):
    r = client.get("/")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "SAMEORIGIN"
    assert client.get("/healthz").headers["referrer-policy"] == "same-origin"
    assert client.get("/api/auth/me").headers["cache-control"] == "no-store"
    assert "strict-transport-security" not in r.headers  # only on https


def test_api_docs_are_disabled(client):
    assert client.get("/docs").status_code == 404 and client.get("/openapi.json").status_code == 404


def test_secrets_encrypted_at_rest(admin, fresh_db, monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "a-long-random-operator-secret")
    crypto._reset_for_tests()
    try:
        assert crypto.is_enabled()
        admin.put("/api/admin/settings", json={"anthropic_api_key": "sk-ant-api03-" + "z" * 40})
        with db.connect() as conn:
            raw = conn.execute("SELECT value FROM settings WHERE key='anthropic_api_key'").fetchone()["value"]
        assert raw.startswith("enc:") and "zzzz" not in raw
        assert settings.get("anthropic_api_key") == "sk-ant-api03-" + "z" * 40
        assert admin.get("/api/admin/overview").json()["settings"]["secrets_encrypted"] is True
        # Google tokens too
        sid = db.create_source("drive", "D")
        sources.save_token(sid, '{"token":"abc","scopes":[]}')
        assert db.get_source(sid)["token_json"].startswith("enc:")
        assert sources.token_info(db.get_source(sid))["token"] == "abc"
        # a different key cannot read it
        monkeypatch.setenv("APP_SECRET_KEY", "another-key")
        crypto._reset_for_tests()
        try:
            sources.token_info(db.get_source(sid))
        except RuntimeError as exc:
            assert "does not match" in str(exc)
        else:
            raise AssertionError("expected decrypt failure")
    finally:
        monkeypatch.delenv("APP_SECRET_KEY", raising=False)
        crypto._reset_for_tests()
        settings.set_many({"anthropic_api_key": ""})


def test_backup_download(admin, fresh_db):
    r = admin.get("/api/admin/backup")
    assert r.status_code == 200 and r.content.startswith(b"SQLite format 3")
    assert "ledger-" in r.headers["content-disposition"]


def test_validation_limits(admin, fresh_db):
    assert admin.post("/api/ask", json={"question": "x" * 2001}).status_code == 422
    assert admin.post("/api/admin/sources", json={"kind": "drive", "name": "n" * 101}).status_code == 422
    assert admin.put("/api/admin/settings", json={"extract_model": "gpt-9"}).status_code == 400
    assert admin.put("/api/admin/settings", json={"concurrency": 99}).status_code == 422
    assert admin.patch("/api/documents/1", json={"doc_type": "nonsense"}).status_code in (400, 404)
    assert admin.get("/api/documents?limit=99999").status_code == 422


def test_local_roots_restriction(admin, fresh_db, tmp_path, monkeypatch):
    from app import config

    allowed = tmp_path / "allowed"; allowed.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    monkeypatch.setattr(config, "LOCAL_ROOTS", [allowed.resolve()])
    assert admin.post("/api/admin/sources", json={"kind": "local", "name": "ok", "local_folder": str(allowed)}).status_code == 200
    r = admin.post("/api/admin/sources", json={"kind": "local", "name": "bad", "local_folder": str(outside)})
    assert r.status_code == 400 and "LOCAL_ROOTS" in r.json()["detail"]
