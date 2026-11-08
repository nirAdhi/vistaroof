"""An in-memory Gemini API (``/models`` and ``/models/{m}:generateContent``) behind httpx.MockTransport.

Checks the ``x-goog-api-key`` header against ``key``; answers are canned by what it is shown (an image
part → receipt, text mentioning an invoice → sales invoice, "statement" → bank statement) or taken
from ``answers``. ``quota = True`` makes every call a 429; ``down = True`` a connection error.
"""
from __future__ import annotations

import json

import httpx

from app import config, gemini, settings
from fake_ollama import CANNED as SMALL_CANNED

KEY = "AIzaSyTestKey-" + "k" * 25

FULL = {
    "sales": {"doc_type": "sales_invoice", "direction": "money_in", "counterparty": "John Murphy", "counterparty_role": "customer",
              "document_number": "INV-2026-014", "issue_date": "2026-03-04", "due_date": "2026-04-03", "currency": "EUR",
              "subtotal": 4200.0, "vat_amount": 567.0, "total": 4767.0, "amount_paid": None, "payment_status": "unpaid",
              "category": "roof_repair", "project": "12 Oak Drive", "line_items": [{"description": "Re-roof", "quantity": 1, "unit_price": 4200.0, "amount": 4200.0}],
              "summary": "Sales invoice to John Murphy for a re-roof.", "confidence": 0.93, "flags": []},
    "receipt": {"doc_type": "receipt", "direction": "money_out", "counterparty": "Chadwicks", "counterparty_role": "supplier",
                "document_number": None, "issue_date": "2026-02-14", "due_date": None, "currency": "EUR", "subtotal": 98.5,
                "vat_amount": 22.66, "total": 121.16, "amount_paid": 121.16, "payment_status": "paid", "category": "materials_roofing",
                "project": None, "line_items": [], "summary": "Till receipt from Chadwicks, paid by card.", "confidence": 0.88, "flags": ["handwritten"]},
    "bank": {"doc_type": "bank_statement", "direction": "none", "counterparty": "AIB", "counterparty_role": "bank", "document_number": None,
             "issue_date": "2026-03-31", "due_date": None, "currency": "EUR", "subtotal": None, "vat_amount": None, "total": None,
             "amount_paid": None, "payment_status": "not_applicable", "category": None, "project": None, "line_items": [],
             "summary": "Monthly bank statement.", "confidence": 0.97, "flags": []},
}


class FakeGemini:
    def __init__(self, models: tuple[str, ...] = ("gemini-2.5-flash", "gemini-2.5-pro", "embedding-001"), key: str = KEY) -> None:
        self.models = list(models)
        self.key = key
        self.requests: list[dict] = []
        self.answers: list = []
        self.quota = False
        self.down = False
        self.flaky_503 = 0  # number of 503s to answer before succeeding

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @staticmethod
    def canned(body: dict) -> dict:
        parts = body["contents"][0]["parts"]
        if any("inlineData" in p and p["inlineData"]["mimeType"].startswith("image/") for p in parts):
            return FULL["receipt"]
        if any("inlineData" in p and p["inlineData"]["mimeType"] == "application/pdf" for p in parts):
            return FULL["sales"]
        text = " ".join(p.get("text", "") for p in parts).lower()
        if "statement" in text:
            return FULL["bank"]
        if "invoice" in text or "inv-" in text:
            return FULL["sales"]
        return FULL["receipt"]

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("name resolution failed", request=request)
        assert "key=" not in str(request.url), "the key must travel in the header, never the URL"
        if request.headers.get("x-goog-api-key") != self.key:
            return httpx.Response(400, json={"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "API key not valid. Please pass a valid API key."}})
        path = request.url.path
        if path.endswith("/models") and request.method == "GET":
            return httpx.Response(200, json={"models": [{"name": f"models/{m}"} for m in self.models]})
        if path.endswith(":generateContent") and request.method == "POST":
            model = path.rsplit("/", 1)[1].split(":")[0]
            if model not in self.models:
                return httpx.Response(404, json={"error": {"code": 404, "status": "NOT_FOUND", "message": f"models/{model} is not found"}})
            if self.quota:
                return httpx.Response(429, json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "You exceeded your current quota"}})
            if self.flaky_503 > 0:
                self.flaky_503 -= 1
                return httpx.Response(503, json={"error": {"code": 503, "status": "UNAVAILABLE", "message": "The model is overloaded"}})
            body = json.loads(request.content)
            self.requests.append(body)
            if self.answers:
                a = self.answers.pop(0)
                content = a(body) if callable(a) else a
            else:
                content = json.dumps(self.canned(body))
            return httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [{"text": content}]}, "finishReason": "STOP"}],
                                             "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}})
        return httpx.Response(404, text=f"unhandled {request.method} {path}")


def install_fake_gemini(monkeypatch, fake: FakeGemini, *, as_provider: bool = True) -> None:
    """Route app/gemini.py through the fake, save its key, optionally make Gemini the reader."""
    monkeypatch.setattr(gemini, "build_http_client",
                        lambda timeout=None: httpx.Client(base_url=config.GEMINI_BASE_URL, transport=fake.transport(), timeout=5))
    monkeypatch.setattr(gemini, "RETRY_SLEEP", 0)
    settings.set_many({"gemini_api_key": fake.key, **({"llm_provider": "gemini"} if as_provider else {})})


__all__ = ["FULL", "SMALL_CANNED", "KEY", "FakeGemini", "install_fake_gemini"]
