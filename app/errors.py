"""Exceptions shared by the LLM providers and the sync pipeline (kept apart to avoid import cycles)."""
from __future__ import annotations


class ApiUnavailable(RuntimeError):
    """The model backend refuses or cannot take any request for a reason no retry fixes: invalid or
    unfunded Claude key, Ollama server switched off or unreachable, model not pulled.

    ``sync`` stops the whole run on the first one (one clear message, the touched files go back to
    pending) instead of recording the same error on every file."""
