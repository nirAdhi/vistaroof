"""Reading a JSON object out of a model answer (shared by the Ollama and Gemini paths)."""
from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


def parse_json_object(text: str) -> dict[str, Any]:
    """The JSON object in a model answer; tolerates code fences and prose around the braces."""
    m = _FENCE_RE.match(text)
    if m:
        text = m.group(1)
    text = text.strip()
    try:
        obj = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object in the answer")
        obj = json.loads(text[start:end + 1])
    if not isinstance(obj, dict):
        raise ValueError("answer is not a JSON object")
    return obj
