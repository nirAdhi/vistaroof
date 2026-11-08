"""Which model backend reads the documents, and whether it is usable right now.

The reader is a setting (``llm_provider``: Admin → Document reader, defaulting to LLM_PROVIDER from
.env): ``ollama`` = self-hosted, documents never leave the network; ``claude`` = Anthropic API;
``gemini`` = Google API (both cloud readers also take photos and scanned PDFs). ``llm_fallback``
names a second reader used when the first is unreachable / unfunded / cannot read a file. The
dispatch itself lives in ``extract.extract`` and ``ask.answer``; this module answers "is it
configured?" and "does it work?" for the UI and the CLI.

Adding a provider: a transport module like ``ollama.py`` / ``gemini.py``, an extractor in
``extract.EXTRACTORS``, an answerer branch in ``ask._answer``, ``config.PROVIDERS`` /
``PROVIDER_LABELS``, ``settings.provider_ready`` / ``provider_model``, a ``health`` branch below.

Live status
-----------
``refresh_status()`` asks the backend how it is (Ollama: /api/version + /api/tags, no model call;
cloud readers: is a key saved — no paid call) and keeps the answer in ``_status``. The scheduler
thread calls it every minute and at start-up, the Admin "Check now" button calls ``health()`` (same,
plus a probe / key test). ``status()`` rides on /api/version, /api/sync/status and /api/config so
the dashboard badge and the admin strip always show *connected*, *unreachable*, *model_missing*,
*ready*, *no_key* or *key_invalid*. A state change is logged.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

from . import config, extract, gemini, ollama, settings

log = logging.getLogger(__name__)

WORDS = {"connected": "connected", "unreachable": "unreachable", "model_missing": "model not installed", "ready": "ready",
         "no_key": "no API key", "key_invalid": "key rejected", "unknown": "checking…"}

_lock = threading.Lock()
_status: dict[str, Any] = {"state": "unknown", "ok": None, "message": "Not checked yet", "checked_at": None,
                           "provider": None, "model": None, "url": "", "fallback": "", "label": "AI: checking…"}


def provider() -> str:
    return settings.llm_provider()


def ready() -> bool:
    """True when documents can be read with the chosen reader."""
    return settings.llm_ready()


def describe() -> dict[str, Any]:
    """Provider summary for the admin UI and /api/config (no secrets)."""
    return settings.llm_public()


def status() -> dict[str, Any]:
    with _lock:
        return dict(_status)


def label_for(prov: str, state: str) -> str:
    return f"{config.PROVIDER_LABELS.get(prov, 'AI')} {WORDS.get(state, state)}"


def _set(prov: str, state: str, ok: bool, message: str, **extra: Any) -> dict[str, Any]:
    fb = settings.llm_fallback_provider()
    new = {"state": state, "ok": ok, "message": message, "label": label_for(prov, state), "provider": prov,
           "model": settings.provider_model(prov), "url": config.OLLAMA_BASE_URL if prov == "ollama" else "",
           "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "fallback": fb if fb and fb != prov and settings.provider_ready(fb) else "", **extra}
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
    """Re-check the backend and store the result (cheap: no paid call, no model call unless ``probe``)."""
    prov = provider()
    if prov == "ollama":
        h = ollama.health(probe=probe)
        return _set(prov, _ollama_state(h), h["ok"], h["message"], models=h.get("models"), version=h.get("version"), probe=h.get("probe"))
    ok = settings.provider_ready(prov)
    name = config.PROVIDER_LABELS[prov]
    return _set(prov, "ready" if ok else "no_key", ok, f"{name} API key saved" if ok else f"No {name} API key saved (Admin → Document reader).")


def health() -> dict[str, Any]:
    """Live check of the chosen reader (the Admin "Check now" button and ``python -m app.llm_check``)."""
    prov = provider()
    if prov == "ollama":
        return refresh_status(probe=True)
    if not settings.provider_ready(prov):
        return refresh_status()
    if prov == "claude":
        t = extract.test_api_key(settings.get("anthropic_api_key").strip())
    else:
        t = gemini.test_api_key(settings.get("gemini_api_key").strip(), settings.provider_model("gemini"))
    return _set(prov, "ready" if t["ok"] else "key_invalid", t["ok"], t["message"], models=t.get("models"), probe=t.get("probe"))


def refresh_in_background() -> None:
    threading.Thread(target=refresh_status, daemon=True, name="llm-status").start()
