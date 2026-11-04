"""Process-level configuration read from environment variables (and an optional ``.env`` file).

Only things that must be known before the database is open live here: file paths, the listening
address, and production switches. Everything else is in the ``settings`` table (see ``settings.py``).

Environment variables
---------------------
DATA_DIR          Folder for the database, preview cache and logs (default ``./data``).
DB_PATH           SQLite file (default ``<DATA_DIR>/ledger.db``).
HOST / PORT       Where uvicorn listens (``run.bat`` uses 127.0.0.1:8000; Docker uses 0.0.0.0).
APP_SECRET_KEY    Enables encryption of secrets at rest. REQUIRED in production.
PUBLIC_BASE_URL   The URL users type, e.g. ``https://ledger.example.com``. Pins the Google OAuth
                  redirect URI and makes session cookies ``Secure``. REQUIRED in production.
COOKIE_SECURE     ``auto`` (default: Secure when the request is HTTPS or PUBLIC_BASE_URL is https),
                  ``true`` or ``false``.
APP_DEBUG         ``1`` shows exception details in API error responses. Keep off in production.
LOCAL_ROOTS       Optional ``;``-separated list of directories that local-folder sources may point
                  at. When set, an admin cannot register a folder outside these roots.
LOG_DIR           Folder for rotating log files (default ``<DATA_DIR>/logs``).
ADMIN_PASSWORD    Built-in admin password, applied at every start (10+ characters). When set, the
                  first-run "create the admin password" screen is skipped and the password cannot be
                  changed from the UI: edit the file and restart instead.
ADMIN_EMAIL       Optional sign-in name for that admin (otherwise the email field is left empty).
COMPANY_NAME etc. Initial defaults for settings, used only until changed in Admin.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.getenv("DB_PATH", DATA_DIR / "ledger.db"))
LOG_DIR = Path(os.getenv("LOG_DIR", DATA_DIR / "logs"))

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "auto").strip().lower()
DEBUG = os.getenv("APP_DEBUG", "").strip().lower() in ("1", "true", "yes")
LOCAL_ROOTS = [Path(p.strip()).resolve() for p in os.getenv("LOCAL_ROOTS", "").split(";") if p.strip()]
IS_PRODUCTION = bool(PUBLIC_BASE_URL)  # a public URL means the app is exposed beyond localhost

# Built-in admin login from the environment (see settings.apply_env_admin()).
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

# Initial values for the settings table (used only until the admin changes them in the UI).
ENV_DEFAULTS = {
    "company_name": os.getenv("COMPANY_NAME", "Our Roofing Company"),
    "company_address": os.getenv("COMPANY_ADDRESS", ""),
    "company_vat": os.getenv("COMPANY_VAT", ""),
    "base_currency": os.getenv("BASE_CURRENCY", "EUR"),
    "country": os.getenv("COUNTRY", "Ireland"),
    "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),
    "extract_model": os.getenv("EXTRACT_MODEL", "claude-opus-5-5"),
    "extract_effort": os.getenv("EXTRACT_EFFORT", "medium"),
    "ask_model": os.getenv("ASK_MODEL", ""),
    "concurrency": os.getenv("CONCURRENCY", "3"),
    "max_file_mb": os.getenv("MAX_FILE_MB", "25"),
    "auto_sync_minutes": os.getenv("AUTO_SYNC_MINUTES", "0"),
    # "1" keeps a viewable copy of each file under DATA_DIR/previews for faster previews; "0" (default)
    # streams the file from its source every time, so no document content is stored on the server.
    "cache_previews": os.getenv("CACHE_PREVIEWS", "0"),
}
