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
ADMIN_PASSWORD    Built-in admin password, applied at every start (10+ characters). When set, the
                  first-run "create the admin password" screen is skipped and the password cannot be
                  changed from the UI: edit the file and restart instead.
ADMIN_EMAIL       Sign-in name for that admin; when set, the login boxes require it and an empty
                  email is refused. Without it the admin signs in with the email field left empty.
LLM_PROVIDER      ``ollama`` (default) or ``claude``: which model reads the documents.
LLM_FALLBACK      ``1`` lets an Ollama setup send a file to Claude when Ollama is unreachable or the
                  file is a photo / scanned PDF (needs a saved Claude key).
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
IS_PRODUCTION = bool(PUBLIC_BASE_URL)  # a public URL means the app is exposed beyond localhost

# Built-in admin login from the environment (see settings.apply_env_admin()).
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

# Which model reads the documents (see app/llm.py). ``ollama`` = self-hosted, text only, nothing leaves
# the network; ``claude`` = Anthropic API (also reads photos and scanned PDFs). No addresses are
# hard-coded here: the Ollama server's address comes from .env (OLLAMA_BASE_URL).
LLM_PROVIDER = (os.getenv("LLM_PROVIDER", "ollama").strip().lower() or "ollama")
if LLM_PROVIDER not in ("ollama", "claude"):
    raise RuntimeError(f"LLM_PROVIDER must be 'ollama' or 'claude', not {LLM_PROVIDER!r}")
LLM_FALLBACK = os.getenv("LLM_FALLBACK", "0").strip().lower() in ("1", "true", "yes", "on")  # Ollama → Claude when needed
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
