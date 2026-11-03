"""Forgot the admin password? Clear it so the admin page asks for a new one on the next visit.

Run where the app runs (the database must be reachable):

    docker compose exec app python -m app.reset_admin        # production (Docker)
    .venv\\Scripts\\python -m app.reset_admin                  # on a PC

Afterwards open /admin: it shows "Create the admin password". Every signed-in browser is logged out
(the session secret is rotated) so a forgotten-password reset cannot leave old sessions alive.
Sources, settings, documents and user accounts are untouched; user passwords are reset from
Admin → Users & security instead.
"""
from __future__ import annotations

from . import config, db, settings


def main() -> None:
    db.init_db()
    if settings.admin_from_env():
        print("ADMIN_PASSWORD is set in the environment: change it there and restart the app instead.")
        return
    had_password = bool(settings.get("admin_password_hash"))
    settings.set_value("admin_password_hash", "")
    settings.rotate_session_secret()
    print(f"database: {config.DB_PATH}")
    print("admin password cleared" if had_password else "no admin password was set")
    print("open /admin in the browser to create a new one; all sessions have been logged out")


if __name__ == "__main__":
    main()
