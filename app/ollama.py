"""Ollama client (self-hosted models) over its native HTTP API, using httpx.

Why the native API and not the OpenAI-compatible ``/v1`` endpoint or the ``ollama`` package:
* ``/api/chat`` takes ``options.num_ctx`` (context size), ``keep_alive`` and a JSON *schema* in
  ``format`` per request. The OpenAI-compatible endpoint exposes none of those (only
  ``response_format: json_object``), and the context size then falls back to the model's default
  (2048 on most Modelfiles), which silently truncates long documents.
* ``httpx`` is already a dependency (Microsoft Graph client), so no new package and the same
  ``build_http_client()`` seam the tests use to swap in a fake transport.

Settings come from the environment (``config``): OLLAMA_BASE_URL, OLLAMA_MODEL, OLLAMA_NUM_CTX,
OLLAMA_TIMEOUT, OLLAMA_KEEP_ALIVE. The server has no authentication; it must only be reachable from
the app's network, never from the internet.

Debugging tips
--------------
* ``python -m app.llm_check`` prints version, installed models and runs a one-line probe.
* ``Unreachable`` (a subclass of ``errors.ApiUnavailable``) carries the URL and the reason; the sync
  stops the whole run on it instead of failing file after file.
* The raw model answer of every document is kept in ``documents.raw``.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from . import config, modelout
from .errors import ApiUnavailable

log = logging.getLogger(__name__)

MAX_JSON_RETRIES = 2  # extra attempts when the answer is not valid JSON / does not match the schema


class Unreachable(ApiUnavailable):
    """Ollama did not answer at all (host asleep, Wi-Fi off, service stopped, wrong URL)."""


class ModelMissing(ApiUnavailable):
    """The configured model is not pulled on the server."""


def build_http_client(timeout: float | None = None) -> httpx.Client:
    """One place to build the client so tests can route it through a fake transport."""
    t = timeout if timeout is not None else config.OLLAMA_TIMEOUT
    return httpx.Client(base_url=config.OLLAMA_BASE_URL, timeout=httpx.Timeout(t, connect=10.0))


def _unreachable(exc: Exception) -> Unreachable:
    return Unreachable(
        f"Ollama at {config.OLLAMA_BASE_URL} is not reachable ({type(exc).__name__}: {str(exc)[:120] or 'no details'}). "
        "Is the Ollama PC awake and on the network, and is OLLAMA_BASE_URL in .env correct?"
    )


def _post(path: str, body: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
    try:
        with build_http_client(timeout) as http:
            resp = http.post(path, json=body)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.NetworkError) as exc:
        raise _unreachable(exc) from exc
    if resp.status_code == 404 and "model" in resp.text.lower():
        raise ModelMissing(f"Ollama has no model named '{body.get('model')}'. On the Ollama PC run: ollama pull {body.get('model')}")
    if resp.status_code >= 400:
        raise RuntimeError(f"Ollama {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def _get(path: str) -> dict[str, Any]:
    try:
        with build_http_client(20) as http:
            resp = http.get(path)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.NetworkError) as exc:
        raise _unreachable(exc) from exc
    if resp.status_code >= 400:
        raise RuntimeError(f"Ollama {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def _options(num_predict: int) -> dict[str, Any]:
    return {"temperature": 0, "num_ctx": max(config.OLLAMA_NUM_CTX, 8192), "num_predict": num_predict}


def chat(messages: list[dict[str, str]], *, schema: dict[str, Any] | None = None, num_predict: int = 1024) -> str:
    """One ``/api/chat`` call; returns the assistant text. ``schema`` turns on constrained JSON output."""
    body: dict[str, Any] = {
        "model": config.OLLAMA_MODEL, "messages": messages, "stream": False,
        "options": _options(num_predict), "keep_alive": config.OLLAMA_KEEP_ALIVE,
    }
    if schema is not None:
        body["format"] = schema
    data = _post("/api/chat", body)
    if data.get("error"):
        raise RuntimeError(f"Ollama error: {data['error']}")
    return (data.get("message") or {}).get("content", "") or ""


parse_json_object = modelout.parse_json_object


def chat_json(messages: list[dict[str, str]], schema: dict[str, Any], validate, *, num_predict: int = 1024) -> tuple[dict[str, Any], int]:
    """``chat`` with JSON output, parsed and checked by ``validate(obj) -> obj``; retries on a bad answer.

    ``validate`` raises ValueError for anything it cannot accept. Up to MAX_JSON_RETRIES extra calls are
    made, each with the model's bad answer and a correction appended. Returns (object, attempts)."""
    history = list(messages)
    last_error = ""
    for attempt in range(1, MAX_JSON_RETRIES + 2):
        text = chat(history, schema=schema, num_predict=num_predict)
        try:
            return validate(parse_json_object(text)), attempt
        except ValueError as exc:
            last_error = str(exc)
            log.warning("ollama answer rejected (attempt %d): %s", attempt, last_error)
            history = history + [
                {"role": "assistant", "content": text[:2000]},
                {"role": "user", "content": f"That answer was not acceptable: {last_error}. Return only the JSON object, "
                                            "with exactly the required keys and allowed values."},
            ]
    raise RuntimeError(f"Ollama ({config.OLLAMA_MODEL}) did not return a valid answer after {MAX_JSON_RETRIES + 1} attempts: {last_error}")


# ------------------------------------------------------------------ health


def installed_models() -> list[str]:
    tags = _get("/api/tags")
    return [m.get("name", "") for m in tags.get("models", []) if m.get("name")]


def model_installed(name: str, models: list[str]) -> bool:
    return name in models or (":" not in name and f"{name}:latest" in models)


def health(probe: bool = True) -> dict[str, Any]:
    """Can the app use Ollama? Version, installed models, whether OLLAMA_MODEL is pulled, and a tiny probe."""
    out: dict[str, Any] = {"ok": False, "provider": "ollama", "url": config.OLLAMA_BASE_URL, "model": config.OLLAMA_MODEL}
    try:
        out["version"] = _get("/api/version").get("version", "?")
        models = installed_models()
        out["models"] = models
        if not model_installed(config.OLLAMA_MODEL, models):
            out["message"] = (f"Ollama {out['version']} at {config.OLLAMA_BASE_URL} answers, but model '{config.OLLAMA_MODEL}' is not installed "
                              f"(installed: {', '.join(models) or 'none'}). Run on the Ollama PC: ollama pull {config.OLLAMA_MODEL}")
            return out
        if probe:
            reply = chat([{"role": "user", "content": "Reply with the single word OK."}], num_predict=5)
            out["probe"] = reply.strip()[:40]
        out["ok"] = True
        out["message"] = f"Ollama {out['version']} at {config.OLLAMA_BASE_URL}: model {config.OLLAMA_MODEL} is installed and answers."
    except ApiUnavailable as exc:
        out["message"] = str(exc)
    except Exception as exc:
        out["message"] = f"Ollama check failed: {type(exc).__name__}: {str(exc)[:200]}"
    return out
