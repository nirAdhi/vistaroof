"""Which model backend reads the documents: a thin façade over ``extract`` / ``ask`` / ``ollama``.

LLM_PROVIDER (environment) picks the backend: ``ollama`` (default, self-hosted, documents never
leave the network) or ``claude`` (Anthropic API, reads photos and scanned PDFs too). LLM_FALLBACK=1
lets an Ollama deployment hand a file to Claude when Ollama is unreachable or the file needs vision,
provided a Claude key is saved. The dispatch itself lives in ``extract.extract`` and ``ask.answer``;
this module answers "is it configured?" and "does it work?" for the UI and the CLI.
"""
from __future__ import annotations

from typing import Any

from . import config, extract, ollama, settings


def provider() -> str:
    return config.LLM_PROVIDER


def ready() -> bool:
    """True when documents can be read: Ollama is configured, or a Claude key is saved."""
    return settings.llm_ready()


def describe() -> dict[str, Any]:
    """Provider summary for the admin UI and /api/config (no secrets)."""
    return settings.llm_public()


def health() -> dict[str, Any]:
    """Live check of the configured backend (the Admin "Check" button and ``python -m app.llm_check``)."""
    if provider() == "ollama":
        out = ollama.health()
        if config.LLM_FALLBACK:
            out["fallback"] = "Claude fallback enabled" + ("" if settings.has_api_key() else " but no Claude key is saved, so it cannot be used")
        return out
    key = settings.get("anthropic_api_key").strip()
    if not settings.looks_like_key(key):
        return {"ok": False, "provider": "claude", "message": "No Claude API key saved (Admin → Claude API)."}
    out = extract.test_api_key(key)
    out.update(provider="claude", model=settings.get("extract_model"))
    return out
