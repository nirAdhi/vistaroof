"""Email alerts over SMTP: the document reader going down or coming back, a sync stopped by a reader failure.

Why: the Ollama reader runs on the owner's own PC, not in the cloud. When that PC is switched off,
nothing in the app breaks — the figures live in the database and the dashboard keeps working — but
new documents stop being read, so the admin wants to hear about it. ``llm._set`` calls
``reader_changed`` on every state change; ``sync._abort`` calls ``sync_aborted``.

Settings (Admin → Alerts; initial values from SMTP_* in .env): ``smtp_host``, ``smtp_port``,
``smtp_security`` (starttls | ssl | none), ``smtp_user``, ``smtp_password`` (encrypted at rest),
``smtp_from``, ``alert_to`` (comma-separated), ``alerts_enabled``. Delivery runs in a thread so a
slow mail server never delays the scheduler; the same alert is not repeated within ``COOLDOWN``
seconds, a recovery is always sent, and the last result is shown in the Admin section.
"""
from __future__ import annotations

import logging
import re
import smtplib
import ssl
import threading
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any

from . import config, settings

log = logging.getLogger(__name__)

COOLDOWN = 3600  # seconds before the same alert key is sent again
DELIVER_IN_THREAD = True  # tests switch this off to assert synchronously
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
OK_STATES = ("connected", "ready")

_lock = threading.Lock()
_last_sent: dict[str, float] = {}
_status: dict[str, Any] = {"last_subject": None, "last_sent_at": None, "last_error": None, "sent_count": 0}


def smtp_config() -> dict[str, Any]:
    return {
        "host": (settings.get("smtp_host") or "").strip(), "port": settings.get_int("smtp_port", 587),
        "security": (settings.get("smtp_security") or "starttls").strip().lower(), "user": (settings.get("smtp_user") or "").strip(),
        "password": settings.get("smtp_password") or "", "from": (settings.get("smtp_from") or "").strip(),
        "to": recipients(), "enabled": settings.get_bool("alerts_enabled", True),
    }


def recipients() -> list[str]:
    return [a.strip() for a in (settings.get("alert_to") or "").replace(";", ",").split(",") if a.strip()]


def configured() -> bool:
    c = smtp_config()
    return bool(c["host"] and c["to"] and (c["from"] or c["user"]))


def status() -> dict[str, Any]:
    with _lock:
        return {**_status, "configured": configured(), "enabled": settings.get_bool("alerts_enabled", True), "recipients": recipients()}


def _deliver(msg: EmailMessage, cfg: dict[str, Any]) -> None:
    """One SMTP session. Raises on failure (the caller records it)."""
    ctx = ssl.create_default_context()
    if cfg["security"] == "ssl":
        server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=25, context=ctx)
    else:
        server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=25)
    with server:
        server.ehlo()
        if cfg["security"] == "starttls":
            server.starttls(context=ctx)
            server.ehlo()
        if cfg["user"]:
            server.login(cfg["user"], cfg["password"])
        server.send_message(msg)


def _build(subject: str, body: str, cfg: dict[str, Any]) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = f"[Roofing Ledger] {subject}"
    msg["From"] = cfg["from"] or cfg["user"]
    msg["To"] = ", ".join(cfg["to"])
    link = f"\n\nAdmin: {config.PUBLIC_BASE_URL}/admin" if config.PUBLIC_BASE_URL else ""
    msg.set_content(f"{body}\n\nSent {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} by Roofing Ledger ({settings.get('company_name')}).{link}\n")
    return msg


def _send_and_record(msg: EmailMessage, cfg: dict[str, Any]) -> None:
    try:
        _deliver(msg, cfg)
        with _lock:
            _status.update(last_subject=msg["Subject"], last_sent_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), last_error=None, sent_count=_status["sent_count"] + 1)
        log.info("alert sent: %s → %s", msg["Subject"], msg["To"])
    except Exception as exc:
        with _lock:
            _status.update(last_error=f"{type(exc).__name__}: {str(exc)[:200]}")
        log.warning("alert could not be sent (%s): %s", msg["Subject"], exc)


def send(subject: str, body: str, *, key: str | None = None, force: bool = False) -> bool:
    """Queue an alert. Returns False when alerts are off / unconfigured / the key is still cooling down."""
    cfg = smtp_config()
    if not cfg["enabled"] or not configured():
        return False
    now = time.time()
    with _lock:
        if key and not force and now - _last_sent.get(key, 0) < COOLDOWN:
            return False
        if key:
            _last_sent[key] = now
    msg = _build(subject, body, cfg)
    if DELIVER_IN_THREAD:
        threading.Thread(target=_send_and_record, args=(msg, cfg), daemon=True, name="alert-mail").start()
    else:
        _send_and_record(msg, cfg)
    return True


def send_test() -> None:
    """Synchronous test email for the Admin button; raises with the SMTP error text."""
    cfg = smtp_config()
    if not configured():
        raise ValueError("fill in the mail server, a sender and at least one recipient first")
    _deliver(_build("Test alert", "This is a test from Roofing Ledger. If you can read this, alerts work.", cfg), cfg)
    with _lock:
        _status.update(last_subject="[Roofing Ledger] Test alert", last_sent_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), last_error=None, sent_count=_status["sent_count"] + 1)


# ------------------------------------------------------------------ the events


def reader_changed(old_state: str, new: dict[str, Any]) -> bool:
    """Called by llm._set on every state change of the document reader."""
    state, label, message = new.get("state"), new.get("label", "document reader"), new.get("message", "")
    if state == old_state:
        return False
    if not new.get("ok"):
        body = (f"{label}.\n\n{message}\n\nWhat this means: the dashboard and every figure already read keep working; new or changed "
                f"documents are not read until the reader is back. Nothing needs to be re-synced afterwards — the next check picks up the changes.")
        if new.get("provider") == "ollama" and state == "unreachable":
            body += "\n\nOllama runs on your own PC: check that it is switched on, awake, connected to the network, and that the Ollama service is running."
        return send(label, body, key=f"reader:{state}")
    if old_state not in OK_STATES and old_state != "unknown":
        return send(f"{label} again", f"{label} again ({message}). Reading of new documents has resumed.", key="reader:recovered", force=True)
    return False


def sync_aborted(reason: str) -> bool:
    """Called by sync._abort: a run stopped because the reader could not be used (no credit, key rejected, unreachable)."""
    return send("Sync stopped: reader unavailable",
                f"A sync was stopped because the document reader could not be used:\n\n{reason}\n\nFiles already read are kept; the ones not reached "
                f"stay pending and are read by the next sync once the reader works again.", key="sync:aborted")
