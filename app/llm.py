"""Which model backend reads the documents, and whether it is reachable right now.

The provider is a setting (``llm_provider``: Admin → Claude API, defaulting to LLM_PROVIDER from
.env): ``ollama`` = self-hosted, documents never leave the network; ``claude`` = Anthropic API,
reads photos and scanned PDFs too. ``llm_fallback`` lets an Ollama setup hand a file to Claude when
Ollama is unreachable or the file needs vision (a Claude key must be saved). The dispatch itself
lives in ``extract.extract`` and ``ask.answer``.

Live status
-----------
``refresh_status()`` asks the backend how it is (Ollama: /api/version + /api/tags, no model call;
Claude: is a key saved) and keeps the answer in ``_status``. The scheduler thread calls it every
minute and at start-up, the Admin "Check now" button calls ``health()`` (same, plus a one-word probe
for Ollama / a key test for Claude). ``status()`` is attached to /api/version, /api/sync/status and
/api/config so the dashboard badge and the admin strip always show *connected*, *unreachable*,
*model_missing*, *ready*, *no_key* or *key_invalid*. A state change is logged.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

from . import config, extract, ollama, settings

log = logging.getLogger(__name__)

STATES = {
    "connected": "Ollama connected", "unreachable": "Ollama unreachable", "model_missing": "Ollama: model not installed",
    "ready": "Claude ready", "no_key": "Claude: no API key", "key_invalid": "Claude: key rejected", "unknown": "Checking…",
}

_lock = threading.Lock()
_status: dict[str, Any] = {"state": "unknown", "ok": None, "message": "Not checked yet", "checked_at": None,
                           "provider": None, "model": None, "url": "", "fallback": False, "label": STATES["unknown"]}


def provider() -> str:
    return settings.llm_provider()


def ready() -> bool:
    """True when documents can be read: Ollama is configured, or a Claude key is saved."""
    return settings.llm_ready()


def describe() -> dict[str, Any]:
    """Provider summary for the admin UI and /api/config (no secrets)."""
    return settings.llm_public()


def status() -> dict[str, Any]:
    with _lock:
        return dict(_status)


def _set(state: str, ok: bool, message: str, **extra: Any) -> dict[str, Any]:
    new = {"state": state, "ok": ok, "message": message, "label": STATES.get(state, state),
           "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "fallback": settings.llm_fallback() and settings.has_api_key(), **extra}
    with _lock:
        old = _status["state"]
        _status.clear()
        _status.update(new)
    if old != state:
        (log.warning if not ok else log.info)("document reader: %s → %s (%s)", old, state, message)
    return dict(new)


def _ollama_state(h: dict[str, Any]) -> str:
    if h["ok"]:
        return "connected"
    return "model_missing" if h.get("models") is not None and "not installed" in h.get("message", "") else "unreachable"


def refresh_status(probe: bool = False) -> dict[str, Any]:
    """Re-check the backend and store the result (cheap: no model call unless ``probe``)."""
    if provider() == "ollama":
        h = ollama.health(probe=probe)
        return _set(_ollama_state(h), h["ok"], h["message"], provider="ollama", model=config.OLLAMA_MODEL,
                    url=config.OLLAMA_BASE_URL, models=h.get("models"), version=h.get("version"), probe=h.get("probe"))
    ok = settings.has_api_key()
    return _set("ready" if ok else "no_key", ok,
                "Claude API key saved" if ok else "No Claude API key saved (Admin → Claude API).",
                provider="claude", model=settings.get("extract_model"), url="")


def health() -> dict[str, Any]:
    """Live check of the configured backend (the Admin "Check now" button and ``python -m app.llm_check``)."""
    if provider() == "ollama":
        return refresh_status(probe=True)
    key = settings.get("anthropic_api_key").strip()
    if not settings.looks_like_key(key):
        return refresh_status()
    t = extract.test_api_key(key)
    return _set("ready" if t["ok"] else "key_invalid", t["ok"], t["message"], provider="claude", model=settings.get("extract_model"), url="")


def refresh_in_background() -> None:
    threading.Thread(target=refresh_status, daemon=True, name="llm-status").start()
