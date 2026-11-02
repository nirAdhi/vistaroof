"""Encryption of secrets at rest (Claude API key, Google OAuth client, Google refresh tokens).

How it works
------------
* If the environment variable ``APP_SECRET_KEY`` is set, every secret is encrypted with Fernet
  (AES-128-CBC + HMAC-SHA256, from the ``cryptography`` package) before it is written to SQLite,
  and decrypted when read. The Fernet key is derived from ``APP_SECRET_KEY`` with PBKDF2 so the
  operator can use any long random string, not only a Fernet-formatted key.
* If ``APP_SECRET_KEY`` is NOT set (typical for a laptop), secrets are stored as plain text and the
  admin UI shows a warning. This keeps the app usable without setup but makes the trade-off visible.
* Encrypted values are prefixed with ``enc:`` so plain-text rows written before the key was
  configured are still readable (they are re-encrypted the next time they are saved).

Debugging tips
--------------
* "Could not decrypt" errors mean ``APP_SECRET_KEY`` changed after secrets were saved. Either restore
  the old key or re-enter the secrets in Admin.
* ``is_enabled()`` tells you whether encryption is active for the running process.
"""
from __future__ import annotations

import base64
import hashlib
import os

PREFIX = "enc:"
_SALT = b"roofing-ledger-v1"  # fixed salt is acceptable: the input is a high-entropy operator secret, not a password

_fernet = None
_checked = False


def _get_fernet():
    """Lazily build the Fernet instance from APP_SECRET_KEY (None when encryption is disabled)."""
    global _fernet, _checked
    if _checked:
        return _fernet
    _checked = True
    secret = os.getenv("APP_SECRET_KEY", "").strip()
    if not secret:
        return None
    from cryptography.fernet import Fernet

    key = hashlib.pbkdf2_hmac("sha256", secret.encode(), _SALT, 100_000, dklen=32)
    _fernet = Fernet(base64.urlsafe_b64encode(key))
    return _fernet


def is_enabled() -> bool:
    """True when secrets written from now on will be encrypted."""
    return _get_fernet() is not None


def encrypt(value: str | None) -> str | None:
    """Encrypt a string for storage. Empty / None values are stored as-is."""
    if not value:
        return value
    f = _get_fernet()
    if f is None:
        return value
    return PREFIX + f.encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    """Reverse of encrypt(). Plain-text (un-prefixed) values pass through unchanged."""
    if not value or not value.startswith(PREFIX):
        return value
    f = _get_fernet()
    if f is None:
        raise RuntimeError("A stored secret is encrypted but APP_SECRET_KEY is not set for this process.")
    from cryptography.fernet import InvalidToken

    try:
        return f.decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("Could not decrypt a stored secret: APP_SECRET_KEY does not match the key used to save it.") from exc


def _reset_for_tests() -> None:
    """Forget the cached key so tests can switch APP_SECRET_KEY on and off."""
    global _fernet, _checked
    _fernet, _checked = None, False
