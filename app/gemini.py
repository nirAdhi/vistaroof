"""Google Gemini client over the Generative Language REST API (``generateContent``), using httpx.

Why REST and not the ``google-genai`` package: the app needs one call shape (system instruction +
parts + a JSON response schema) and already ships httpx; the SDK would add a dozen transitive
packages to audit for a single endpoint. The key travels in the ``x-goog-api-key`` header, never in
the URL, so it cannot end up in access logs.

Gemini reads PDFs and photos natively (``inlineData`` parts), so it can take every file Claude can.
Schemas are converted from the app's JSON Schema to Gemini's OpenAPI-style ``responseSchema``
(``to_gemini_schema``: upper-case types, ``nullable`` instead of ``["x", "null"]``, ``anyOf`` with a
null branch flattened).

Failures that no retry fixes raise ``errors.ApiUnavailable`` subclasses so the sync stops with one
message: ``KeyRejected`` (400 API_KEY_INVALID / 403), ``ModelMissing`` (404), ``QuotaExhausted``
(429 after retries). 5xx are retried, then raised as a per-file RuntimeError.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any

import httpx

from . import config
from .errors import ApiUnavailable

log = logging.getLogger(__name__)

RETRY_SLEEP = 2.0  # seconds, multiplied by the attempt number (tests set it to 0)
MAX_ATTEMPTS = 3

AUTO = "auto"  # pick the newest Flash model the key can use (Google retires model names for new accounts over time)
SUGGESTED_MODELS = [
    (AUTO, "Newest Flash model available to this key (recommended)"),
    ("gemini-3.8-flash", "Gemini 3.8 Flash"),
    ("gemini-2.5-flash", "Gemini 2.5 Flash (older accounts only)"),
    ("gemini-2.5-pro", "Gemini 2.5 Pro"),
]
_MODEL_CACHE: dict[str, tuple[float, str]] = {}  # key fingerprint -> (time, resolved model)
MODEL_CACHE_TTL = 3600
_SKIP_WORDS = ("lite", "preview", "exp", "image", "tts", "live", "audio", "embedding", "thinking", "native")
_VERSION_RE = re.compile(r"^gemini-(\d+(?:\.\d+)?)-flash$")


class KeyRejected(ApiUnavailable):
    """Google refused the API key (invalid, disabled, or without access to the model)."""


class ModelMissing(ApiUnavailable):
    """The configured model name does not exist for this key."""


class QuotaExhausted(ApiUnavailable):
    """429 after retries: free-tier daily quota or rate limit used up."""


def build_http_client(timeout: float | None = None) -> httpx.Client:
    """One place to build the client so tests can route it through a fake transport."""
    t = timeout if timeout is not None else config.GEMINI_TIMEOUT
    return httpx.Client(base_url=config.GEMINI_BASE_URL, timeout=httpx.Timeout(t, connect=10.0))


def _error_text(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error", {})
        return f"{err.get('status', '')} {err.get('message', '')}".strip()
    except ValueError:
        return resp.text[:300]


def _request(method: str, path: str, key: str, body: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
    headers = {"x-goog-api-key": key}
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with build_http_client(timeout) as http:
                resp = http.request(method, path, json=body, headers=headers)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.NetworkError) as exc:
            raise ApiUnavailable(f"Gemini API is not reachable ({type(exc).__name__}: {str(exc)[:120] or 'no details'}). "
                                 "Check the internet connection / outbound firewall.") from exc
        if resp.status_code < 400:
            return resp.json()
        detail = _error_text(resp)
        if resp.status_code in (401, 403) or (resp.status_code == 400 and "api key" in detail.lower()):
            raise KeyRejected(f"Gemini API rejected the key ({resp.status_code}): {detail[:200]}. Check Admin → Document reader.")
        if resp.status_code == 404:
            raise ModelMissing(f"Gemini model not found ({detail[:160]}). Set the model to 'auto' (newest Flash model for this key) or pick a listed name in Admin → Document reader.")
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_SLEEP * attempt)
            continue
        if resp.status_code == 429:
            raise QuotaExhausted(f"Gemini quota or rate limit exhausted ({detail[:160]}). Wait, or raise the quota in Google AI Studio, then sync again.")
        raise RuntimeError(f"Gemini API {resp.status_code}: {detail[:300]}")
    raise RuntimeError("Gemini API: no response")  # pragma: no cover - loop always returns or raises


def to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """App JSON Schema → Gemini ``responseSchema`` (OpenAPI subset: upper-case types, ``nullable``)."""
    if "anyOf" in schema:
        branches = [b for b in schema["anyOf"] if b.get("type") != "null"]
        out = to_gemini_schema(branches[0]) if branches else {"type": "STRING"}
        if len(branches) < len(schema["anyOf"]):
            out["nullable"] = True
        return out
    t = schema.get("type", "string")
    nullable = False
    if isinstance(t, list):
        nullable = "null" in t
        t = next((x for x in t if x != "null"), "string")
    out: dict[str, Any] = {"type": str(t).upper()}
    if nullable:
        out["nullable"] = True
    for k in ("enum", "description"):
        if k in schema:
            out[k] = schema[k]
    if "properties" in schema:
        out["properties"] = {k: to_gemini_schema(v) for k, v in schema["properties"].items()}
    if "required" in schema:
        out["required"] = list(schema["required"])
    if "items" in schema:
        out["items"] = to_gemini_schema(schema["items"])
    return out


def generate(key: str, model: str, system: str, parts: list[dict[str, Any]], *, schema: dict[str, Any] | None = None,
             temperature: float = 0.0, max_tokens: int = 4096) -> str:
    """One ``generateContent`` call; returns the answer text (JSON text when ``schema`` is given)."""
    gen: dict[str, Any] = {"temperature": temperature, "maxOutputTokens": max_tokens}
    if schema is not None:
        gen["responseMimeType"] = "application/json"
        gen["responseSchema"] = to_gemini_schema(schema)
    body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": parts}], "generationConfig": gen}
    data = _request("POST", f"/models/{model}:generateContent", key, body)
    cands = data.get("candidates") or []
    if not cands:
        reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
        raise RuntimeError(f"Gemini returned no answer ({reason})")
    cand = cands[0]
    finish = cand.get("finishReason", "STOP")
    text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []))
    if finish == "MAX_TOKENS" and schema is not None:
        raise RuntimeError("model output was cut off (max tokens)")
    if finish in ("SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST"):
        raise RuntimeError(f"model declined to process this document ({finish})")
    return text


def generate_resilient(key: str, model: str, system: str, parts: list[dict[str, Any]], **kw: Any) -> tuple[str, str]:
    """``generate`` that survives a retired model name: resolves ``auto``, and when Google answers NOT_FOUND for
    a named model it retries once with the newest Flash model the key can use. Returns (text, model used)."""
    chosen = resolve_model(key, model)
    try:
        return generate(key, chosen, system, parts, **kw), chosen
    except ModelMissing:
        if model == AUTO:
            raise
        _MODEL_CACHE.clear()
        alt = resolve_model(key, AUTO)
        if alt == chosen:
            raise
        log.warning("gemini: model %s is not available to this key any more; using %s (set the model to 'auto' in Admin)", chosen, alt)
        return generate(key, alt, system, parts, **kw), alt


def list_models(key: str) -> list[str]:
    names: list[str] = []
    token = None
    for _ in range(10):
        q = "?pageSize=200" + (f"&pageToken={token}" if token else "")
        data = _request("GET", f"/models{q}", key, timeout=30)
        names += [m.get("name", "").removeprefix("models/") for m in data.get("models", [])]
        token = data.get("nextPageToken")
        if not token:
            break
    return [n for n in names if n]


def pick_model(models: list[str]) -> str | None:
    """The newest plain Flash model in a key's model list (``gemini-3.8-flash`` over ``gemini-2.5-flash``), else any Gemini."""
    best: tuple[float, str] | None = None
    for m in models:
        mm = _VERSION_RE.match(m)
        if mm and not any(w in m for w in _SKIP_WORDS):
            v = float(mm.group(1))
            if best is None or v > best[0]:
                best = (v, m)
    if best:
        return best[1]
    plain = [m for m in models if m.startswith("gemini-") and not any(w in m for w in _SKIP_WORDS)]
    return sorted(plain)[-1] if plain else None


def resolve_model(key: str, model: str) -> str:
    """``auto`` → the newest Flash model this key can use (cached an hour); any other name is returned as is."""
    if model and model != AUTO:
        return model
    fp = hashlib.sha256(key.encode()).hexdigest()[:16]
    hit = _MODEL_CACHE.get(fp)
    if hit and time.time() - hit[0] < MODEL_CACHE_TTL:
        return hit[1]
    chosen = pick_model(list_models(key))
    if not chosen:
        raise ModelMissing("This Gemini key has no usable Gemini model. Check the key in Google AI Studio.")
    _MODEL_CACHE[fp] = (time.time(), chosen)
    log.info("gemini: model 'auto' resolved to %s", chosen)
    return chosen


def test_api_key(key: str, model: str) -> dict[str, Any]:
    """Key valid? Model available? One tiny answer? (The Admin "Test key" / "Check now" button.)"""
    out: dict[str, Any] = {"ok": False, "provider": "gemini", "model": model}
    try:
        models = list_models(key)
        out["models"] = [m for m in models if "gemini" in m][:40]
        if model == AUTO:
            chosen = pick_model(models)
            if not chosen:
                out["message"] = "The key works, but it has no usable Gemini model."
                return out
            out["model"] = model = chosen
            out["auto"] = True
        if model not in models:
            hint = pick_model(models)
            out["message"] = (f"The key works, but model '{model}' is not available to it. "
                              f"{'Use ' + hint + ' or set the model to auto.' if hint else ''} Models with this key: {', '.join(out['models'][:8]) or 'none'}…")
            return out
        try:
            reply = generate(key, model, "Answer in one word.", [{"text": "Reply with OK."}], max_tokens=64)
            out["probe"] = reply.strip()[:40]
        except RuntimeError as exc:  # a cut-off or empty probe still proves the key, model and billing
            out["probe"] = f"({exc})"
        out["ok"] = True
        out["message"] = f"Gemini key works and model {model} answers." + (" (chosen automatically as the newest Flash model)" if out.get("auto") else "")
    except ApiUnavailable as exc:
        out["message"] = str(exc)
    except Exception as exc:
        out["message"] = f"Gemini check failed: {type(exc).__name__}: {str(exc)[:200]}"
    return out
