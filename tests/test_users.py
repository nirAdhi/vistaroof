"""User accounts: creation rules, roles, disabling, password reset / self-service, deletion."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import db, main

from conftest import ADMIN_PW, OWNER_PW, login_as, make_user


def test_create_user_validation(admin, fresh_db):
    post = lambda body: admin.post("/api/admin/users", json=body)  # noqa: E731
    assert post({"email": "not-an-email", "password": OWNER_PW}).status_code == 400
    assert post({"email": "a@b.ie", "password": "short"}).status_code == 400
    assert post({"email": "a@b.ie", "password": OWNER_PW, "role": "superuser"}).status_code == 400
    r = post({"email": "  Anne@Example.IE ", "password": OWNER_PW})  # name defaults to the local part, email is normalised
    assert r.status_code == 200
    u = r.json()["user"]
    assert u["email"] == "anne@example.ie" and u["name"] == "Anne" and u["role"] == "user" and u["active"] is True
    assert "password_hash" not in u
    assert post({"email": "anne@example.ie", "name": "Dup", "password": OWNER_PW}).status_code == 400  # duplicate
    users = admin.get("/api/admin/users").json()["users"]
    assert [x["email"] for x in users] == ["anne@example.ie"]
    assert admin.get("/api/admin/overview").json()["users"] == users
    assert admin.get("/api/admin/overview").json()["settings"]["user_count"] == 1


def test_admin_role_user_can_open_admin_area(admin, fresh_db):
    make_user(admin, email="boss@example.ie", name="Boss", role="admin")
    boss = TestClient(main.app)
    r = boss.post("/api/admin/login", json={"email": "boss@example.ie", "password": OWNER_PW})
    assert r.status_code == 200 and r.json()["user"]["role"] == "admin"
    assert boss.get("/api/admin/overview").status_code == 200
    me = boss.get("/api/admin/me").json()
    assert me["authenticated"] is True and me["user"]["email"] == "boss@example.ie"
    assert db.get_user(me["user"]["uid"])["last_login_at"] is not None
    # an admin cannot lock themselves out
    uid = me["user"]["uid"]
    assert boss.patch(f"/api/admin/users/{uid}", json={"role": "user"}).status_code == 400
    assert boss.patch(f"/api/admin/users/{uid}", json={"active": False}).status_code == 400
    assert boss.delete(f"/api/admin/users/{uid}").status_code == 400
    # ...but the built-in admin can demote them, which ends their session at once
    assert admin.patch(f"/api/admin/users/{uid}", json={"role": "user"}).json()["user"]["role"] == "user"
    assert boss.get("/api/admin/overview").status_code == 401


def test_disable_and_enable_takes_effect_immediately(admin, fresh_db):
    u = make_user(admin)
    user = login_as(u["email"])
    assert user.get("/api/summary").status_code == 200
    assert admin.patch(f"/api/admin/users/{u['id']}", json={"active": False}).json()["user"]["active"] is False
    assert user.get("/api/summary").status_code == 401  # existing session dead
    assert TestClient(main.app).post("/api/auth/login", json={"email": u["email"], "password": OWNER_PW}).status_code == 401
    admin.patch(f"/api/admin/users/{u['id']}", json={"active": True, "name": "Cormac S."})
    again = login_as(u["email"])
    assert again.get("/api/config").json()["user"]["name"] == "Cormac S."


def test_admin_password_reset_logs_user_out(admin, fresh_db):
    u = make_user(admin)
    user = login_as(u["email"])
    assert admin.post(f"/api/admin/users/{u['id']}/password", json={"password": "short"}).status_code == 400
    assert admin.post(f"/api/admin/users/{u['id']}/password", json={"password": "brand-new-pass-1"}).status_code == 200
    assert user.get("/api/summary").status_code == 401
    assert TestClient(main.app).post("/api/auth/login", json={"email": u["email"], "password": OWNER_PW}).status_code == 401
    assert login_as(u["email"], "brand-new-pass-1").get("/api/summary").status_code == 200


def test_user_changes_own_password(admin, fresh_db):
    u = make_user(admin)
    phone, laptop = login_as(u["email"]), login_as(u["email"])
    assert laptop.post("/api/auth/password", json={"current": "nope", "new": "another-pass-12"}).status_code == 400
    assert laptop.post("/api/auth/password", json={"current": OWNER_PW, "new": "short"}).status_code == 400
    assert laptop.post("/api/auth/password", json={"current": OWNER_PW, "new": "another-pass-12"}).status_code == 200
    assert laptop.get("/api/summary").status_code == 200  # this browser stays signed in
    assert phone.get("/api/summary").status_code == 401   # the other one is out
    assert login_as(u["email"], "another-pass-12").get("/api/summary").status_code == 200
    # the built-in admin has no account row to change
    assert admin.post("/api/auth/password", json={"current": ADMIN_PW, "new": "another-pass-12"}).status_code == 400
    assert TestClient(main.app).post("/api/auth/password", json={"current": "x", "new": "y" * 10}).status_code == 401


def test_delete_user(admin, fresh_db):
    u = make_user(admin)
    user = login_as(u["email"])
    assert admin.delete(f"/api/admin/users/{u['id']}").status_code == 200
    assert admin.delete(f"/api/admin/users/{u['id']}").status_code == 404
    assert user.get("/api/summary").status_code == 401
    assert admin.get("/api/admin/users").json()["users"] == []


def test_reset_admin_command(admin, fresh_db, capsys):
    from app import reset_admin, settings

    u = make_user(admin)
    user = login_as(u["email"])
    reset_admin.main()
    assert "admin password cleared" in capsys.readouterr().out
    assert admin.get("/api/admin/me").json() == {"authenticated": False, "role": None, "user": None, "needs_setup": True}
    assert user.get("/api/summary").status_code == 401  # everyone is logged out
    assert client_setup_works(admin)


def client_setup_works(c) -> bool:
    """After a reset the admin page can create a new password and user accounts still exist."""
    from app import db

    ok = c.post("/api/admin/setup", json={"password": ADMIN_PW}).status_code == 200
    return ok and db.count_users() == 1


def test_admin_login_from_environment(client, fresh_db, monkeypatch, capsys):
    """ADMIN_EMAIL / ADMIN_PASSWORD in the environment define the built-in admin at startup."""
    from app import config, reset_admin, settings

    monkeypatch.setattr(config, "ADMIN_EMAIL", "boss@vistaroofing.ie")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "short")
    assert "ignored" in settings.apply_env_admin()  # too short: stored password untouched
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "env-admin-pass-123")
    assert settings.apply_env_admin() is None
    try:
        client.cookies.clear()
        assert client.post("/api/admin/login", json={"password": ADMIN_PW}).status_code == 401  # old one is gone
        r = client.post("/api/admin/login", json={"email": "Boss@VistaRoofing.ie", "password": "env-admin-pass-123"})
        assert r.status_code == 200 and r.json()["user"] == {"role": "admin", "uid": 0, "name": "Admin", "email": "boss@vistaroofing.ie"}
        assert client.post("/api/auth/login", json={"email": "", "password": "env-admin-pass-123"}).status_code == 200  # empty email still works
        # cannot be changed from the UI, and the reset command refuses too
        assert client.post("/api/admin/password", json={"current": "env-admin-pass-123", "new": "another-pass-12"}).status_code == 400
        assert client.get("/api/admin/overview").json()["settings"]["admin_from_env"] is True
        reset_admin.main()
        assert "change it there" in capsys.readouterr().out
        assert settings.check_password("env-admin-pass-123", "admin")
        # unchanged password on the next start: no re-hash, sessions survive
        secret = settings.get("session_secret")
        assert settings.apply_env_admin() is None and settings.get("session_secret") == secret
        assert client.get("/api/admin/me").json()["authenticated"] is True
    finally:
        monkeypatch.setattr(config, "ADMIN_PASSWORD", "")
        settings.set_password(ADMIN_PW, "admin")  # restore for the other tests


def test_login_box_accepts_password_only_for_admin(client, fresh_db):
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"password": ADMIN_PW}).json()["role"] == "admin"
    assert client.post("/api/auth/login", json={"email": "", "password": "wrong-password-x"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": "ghost@example.ie", "password": ADMIN_PW}).status_code == 401
