"""Runtime settings (stored in the SQLite ``settings`` table) plus password and session helpers.

Why settings live in the database
---------------------------------
Everything an administrator can change (company details, Claude key and model, Google OAuth client,
sync tuning, passwords) is stored as key/value rows so it can be edited from the Admin screen without
restarting the server. ``.env`` only supplies *initial* defaults (see ``config.ENV_DEFAULTS``).

Secrets
-------
Keys listed in ``SECRET_KEYS`` are encrypted at rest when ``APP_SECRET_KEY`` is set (see ``crypto.py``)
and are never returned by ``public()``; the API key is shown masked instead.

Sessions
--------
Login creates a signed, self-contained token: ``<role>.<expires>.<nonce>.<hmac>``. The HMAC is keyed
with ``session_secret`` (random, generated once, stored in settings). Rotating ``session_secret``
logs every browser out, which is what happens on a password change.

Roles: ``admin`` (everything) and ``owner`` (dashboard only). The owner password is optional; while
it is unset, the dashboard accepts the admin password.

Debugging tips
--------------
* ``get(key)`` reads through an in-process cache; call ``invalidate()`` after writing to the table
  from outside this module (tests, scripts).
* Password hashes look like ``<salt-hex>$<pbkdf2-hex>``. Deleting the ``admin_password_hash`` row
  makes the admin page ask for a new password on the next visit.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from typing import Any

from . import config, crypto, db

SECRET_KEYS = {"anthropic_api_key", "google_client_config", "ms_client_config", "admin_password_hash", "owner_password_hash", "session_secret"}
PUBLIC_KEYS = [
    "company_name", "company_address", "company_vat", "base_currency", "country", "extract_model", "extract_effort",
    "ask_model", "concurrency", "max_file_mb", "auto_sync_minutes",
]
ENCRYPTED_KEYS = {"anthropic_api_key", "google_client_config", "ms_client_config"}  # password hashes and the HMAC secret are not secrets of value
SESSION_TTL_SECONDS = 60 * 60 * 24 * 14  # 14 days
MIN_PASSWORD_LENGTH = 10

_lock = threading.Lock()
_cache: dict[str, str] | None = None


# ---------------------------------------------------------------- read / write


def _load() -> dict[str, str]:
    """Return a copy of all rows, loading them from SQLite on first use."""
    global _cache
    with _lock:
        if _cache is None:
            with db.connect() as conn:
                rows = conn.execute("SELECT key, value FROM settings").fetchall()
            _cache = {r["key"]: r["value"] for r in rows}
        return dict(_cache)


def invalidate() -> None:
    """Drop the cache; the next read goes back to the database."""
    global _cache
    with _lock:
        _cache = None


def get(key: str, default: Any = None) -> str:
    """Stored value, else the .env default, else ``default``/empty string. Decrypts secrets."""
    vals = _load()
    if key in vals:
        value = vals[key]
        return crypto.decrypt(value) if key in ENCRYPTED_KEYS else value
    env = config.ENV_DEFAULTS.get(key)
    if key == "anthropic_api_key" and not looks_like_key(env or ""):
        env = ""  # ignore the ``sk-ant-...`` placeholder from .env.example
    return env if env is not None else (default if default is not None else "")


def get_int(key: str, default: int) -> int:
    try:
        return int(get(key, str(default)) or default)
    except ValueError:
        return default


def get_float(key: str, default: float) -> float:
    try:
        return float(get(key, str(default)) or default)
    except ValueError:
        return default


def set_many(values: dict[str, Any]) -> None:
    """Upsert several keys in one transaction; secrets are encrypted when a key is configured."""
    with db.connect() as conn:
        for k, v in values.items():
            text = "" if v is None else str(v)
            if k in ENCRYPTED_KEYS:
                text = crypto.encrypt(text) or ""
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, text),
            )
    invalidate()


def set_value(key: str, value: Any) -> None:
    set_many({key: value})


# ---------------------------------------------------------------- derived views


def looks_like_key(key: str) -> bool:
    """Cheap sanity check so the placeholder from .env.example is not mistaken for a real key."""
    key = (key or "").strip()
    return len(key) > 20 and not key.endswith("...")


def has_api_key() -> bool:
    return looks_like_key(get("anthropic_api_key"))


def masked_key() -> str:
    """``sk-ant-a…1234`` style display so the admin can recognise the key without exposing it."""
    k = get("anthropic_api_key").strip()
    if not k:
        return ""
    return k[:7] + "…" + k[-4:] if len(k) > 14 else "•" * len(k)


def google_client_config() -> dict | None:
    """The uploaded OAuth client JSON as a dict (``{"installed": {...}}`` or ``{"web": {...}}``), or None."""
    raw = get("google_client_config")
    if not raw:
        return None
    try:
        cfg = json.loads(raw)
    except ValueError:
        return None
    return cfg if isinstance(cfg, dict) and ("installed" in cfg or "web" in cfg) else None


def public() -> dict[str, Any]:
    """Settings safe to send to the admin UI (no secrets, masked key, feature flags)."""
    out = {k: get(k) for k in PUBLIC_KEYS}
    out["anthropic_api_key_masked"] = masked_key()
    out["has_api_key"] = has_api_key()
    cfg = google_client_config()
    out["has_google_client"] = cfg is not None
    if cfg:
        inner = cfg.get("installed") or cfg.get("web") or {}
        out["google_client_type"] = "installed" if "installed" in cfg else "web"
        out["google_client_id"] = inner.get("client_id", "")
    # Microsoft 365 app registration (Admin → Microsoft setup); secret never leaves the server
    ms = None
    try:
        ms = json.loads(get("ms_client_config") or "null")
    except ValueError:
        ms = None
    out["has_ms_client"] = bool(isinstance(ms, dict) and ms.get("client_id"))
    if out["has_ms_client"]:
        out["ms_client_id"] = ms.get("client_id", "")
        out["ms_tenant"] = ms.get("tenant") or "common"
        out["ms_has_secret"] = bool(ms.get("client_secret"))
    out["has_admin_password"] = bool(get("admin_password_hash"))
    out["has_owner_password"] = bool(get("owner_password_hash"))
    out["secrets_encrypted"] = crypto.is_enabled()
    out["public_base_url"] = config.PUBLIC_BASE_URL
    return out


# ---------------------------------------------------------------- passwords


def _hash_password(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 200_000).hex()


def set_password(password: str, role: str = "admin") -> None:
    """Store a salted PBKDF2 hash for ``admin`` or ``owner``."""
    salt = secrets.token_hex(16)
    set_value(f"{role}_password_hash", f"{salt}${_hash_password(password, salt)}")


def check_password(password: str, role: str = "admin") -> bool:
    """Constant-time comparison against the stored hash; False when no password is set."""
    stored = get(f"{role}_password_hash")
    if not stored or "$" not in stored:
        return False
    salt, digest = stored.split("$", 1)
    return hmac.compare_digest(_hash_password(password, salt), digest)


def role_for_password(password: str) -> str | None:
    """Which role a password unlocks on the shared login box: admin first, then owner."""
    if check_password(password, "admin"):
        return "admin"
    if check_password(password, "owner"):
        return "owner"
    return None


def rotate_session_secret() -> None:
    """Invalidate every session (used after a password change)."""
    set_value("session_secret", secrets.token_hex(32))


# ---------------------------------------------------------------- sessions


def session_secret() -> str:
    s = get("session_secret")
    if not s:
        s = secrets.token_hex(32)
        set_value("session_secret", s)
    return s


def _sign(payload: str) -> str:
    return hmac.new(session_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_session_token(role: str) -> str:
    """``role.expires.nonce.signature`` — stateless, so no session table is needed."""
    expires = int(time.time()) + SESSION_TTL_SECONDS
    payload = f"{role}.{expires}.{secrets.token_hex(8)}"
    return f"{payload}.{_sign(payload)}"


def session_role(token: str | None) -> str | None:
    """Return the role encoded in a valid, unexpired token, else None."""
    if not token or token.count(".") != 3:
        return None
    role, expires, nonce, sig = token.split(".")
    if role not in ("admin", "owner"):
        return None
    try:
        if int(expires) < time.time():
            return None
    except ValueError:
        return None
    if not hmac.compare_digest(sig, _sign(f"{role}.{expires}.{nonce}")):
        return None
    return role
