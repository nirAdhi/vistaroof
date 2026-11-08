"""Process-level configuration read from environment variables (and an optional ``.env`` file;
``ADMIN_EMAIL`` / ``ADMIN_PASSWORD`` are also taken from ``.env.production`` when not set elsewhere).

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
BACKUP_DIR        Initial backup folder (Admin → Data can change it), e.g. a mounted USB drive.
BACKUP_ROOTS      Optional ``;``-separated list confining where backups may be written.
BACKUP_EVERY_HOURS / BACKUP_KEEP   Automatic application backups (0 = off) and how many to keep.
SMTP_HOST / SMTP_PORT / SMTP_SECURITY / SMTP_USER / SMTP_PASSWORD / SMTP_FROM / ALERT_TO
                  Initial email-alert settings (Admin → Alerts); the password is stored encrypted.
ADMIN_PASSWORD    Built-in admin password, applied at every start (10+ characters). When set, the
                  first-run "create the admin password" screen is skipped and the password cannot be
                  changed from the UI: edit the file and restart instead.
ADMIN_EMAIL       Sign-in name for that admin; when set, the login boxes require it and an empty
                  email is refused. Without it the admin signs in with the email field left empty.
LLM_PROVIDER      ``ollama`` (default), ``claude`` or ``gemini``: which model reads the documents.
LLM_FALLBACK      A second reader (``claude`` / ``gemini`` / ``ollama``; ``1`` = claude) used when the
                  first is unreachable, unfunded, or cannot read a photo / scanned PDF.
GEMINI_API_KEY / GEMINI_MODEL   Google Gemini key (initial value; Admin stores it encrypted) and model.
OLLAMA_BASE_URL   The Ollama server, e.g. ``http://192.168.0.183:11434`` (default: localhost).
OLLAMA_MODEL      Model name as shown by ``ollama list`` (default ``qwen2.5:3b``).
OLLAMA_NUM_CTX / OLLAMA_TIMEOUT / OLLAMA_KEEP_ALIVE   context tokens (min 8192), seconds, keep-alive.
COMPANY_NAME etc. Initial defaults for settings, used only until changed in Admin.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")  # real environment variables win (Docker passes .env.production through env_file)
# A local run also takes the admin login from .env.production, so the same email + password work on
# the PC and on the server. Only these two keys: the rest of that file (public URL, LOCAL_ROOTS…)
# describes the server and would break a localhost run.
_PROD = dotenv_values(ROOT / ".env.production") if (ROOT / ".env.production").exists() else {}
for _k in ("ADMIN_EMAIL", "ADMIN_PASSWORD"):
    if _k not in os.environ and _PROD.get(_k):  # an explicit empty value (tests) opts out
        os.environ[_k] = _PROD[_k]

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
BACKUP_ROOTS = [Path(p.strip()).resolve() for p in os.getenv("BACKUP_ROOTS", "").split(";") if p.strip()]  # where backups may be written
IS_PRODUCTION = bool(PUBLIC_BASE_URL)  # a public URL means the app is exposed beyond localhost

# Built-in admin login from the environment (see settings.apply_env_admin()).
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

# Which model reads the documents (see app/llm.py). ``ollama`` = self-hosted, text only, nothing leaves
# the network; ``claude`` = Anthropic API and ``gemini`` = Google API (both read photos and scanned
# PDFs). No addresses are hard-coded here: the Ollama server's address comes from .env.
PROVIDERS = ("ollama", "claude", "gemini")
PROVIDER_LABELS = {"ollama": "Ollama", "claude": "Claude", "gemini": "Gemini"}
LLM_PROVIDER = (os.getenv("LLM_PROVIDER", "ollama").strip().lower() or "ollama")
if LLM_PROVIDER not in PROVIDERS:
    raise RuntimeError(f"LLM_PROVIDER must be one of {', '.join(PROVIDERS)}, not {LLM_PROVIDER!r}")
_fb = os.getenv("LLM_FALLBACK", "").strip().lower()
LLM_FALLBACK = "claude" if _fb in ("1", "true", "yes", "on") else (_fb if _fb in PROVIDERS else "")  # second reader, or ""
GEMINI_BASE_URL = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta").strip().rstrip("/")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
GEMINI_TIMEOUT = float(os.getenv("GEMINI_TIMEOUT", "120"))
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").strip().rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b").strip()
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "8192"))          # tokens of context the model is given (min 8192)
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "180"))         # seconds per request (the first call loads the model)
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "30m").strip()  # keep the model in memory between documents

# Initial values for the settings table (used only until the admin changes them in the UI).
ENV_DEFAULTS = {
    "company_name": os.getenv("COMPANY_NAME", "Our Roofing Company"),
    "company_address": os.getenv("COMPANY_ADDRESS", ""),
    "company_vat": os.getenv("COMPANY_VAT", ""),
    "base_currency": os.getenv("BASE_CURRENCY", "EUR"),
    "country": os.getenv("COUNTRY", "Ireland"),
    "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),
    "llm_provider": LLM_PROVIDER,                 # Admin → Document reader can switch it; .env is the default
    "llm_fallback": LLM_FALLBACK,                 # "" | claude | gemini | ollama
    "gemini_api_key": os.getenv("GEMINI_API_KEY", ""),
    "gemini_model": GEMINI_MODEL,
    # Email alerts (Admin → Alerts): reader down / back, sync stopped by a reader failure.
    "smtp_host": os.getenv("SMTP_HOST", ""),
    "smtp_port": os.getenv("SMTP_PORT", "587"),
    "smtp_security": os.getenv("SMTP_SECURITY", "starttls"),  # starttls | ssl | none
    "smtp_user": os.getenv("SMTP_USER", ""),
    "smtp_password": os.getenv("SMTP_PASSWORD", ""),
    "smtp_from": os.getenv("SMTP_FROM", ""),
    "alert_to": os.getenv("ALERT_TO", ""),
    "alerts_enabled": os.getenv("ALERTS_ENABLED", "1"),
    "backup_dir": os.getenv("BACKUP_DIR", ""),            # e.g. a mounted USB drive; empty = <DATA_DIR>/backups
    "backup_every_hours": os.getenv("BACKUP_EVERY_HOURS", "0"),
    "backup_keep": os.getenv("BACKUP_KEEP", "10"),
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
