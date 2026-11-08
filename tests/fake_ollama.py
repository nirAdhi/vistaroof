"""An in-memory Ollama server (``/api/version``, ``/api/tags``, ``/api/chat``) behind httpx.MockTransport.

Answers are canned by the document text it is shown, or taken from ``answers`` (a queue of strings
or callables) so tests can feed malformed JSON and check the retry. ``down = True`` simulates the
Ollama PC being asleep. Every /api/chat body is kept in ``requests``.
"""
from __future__ import annotations

import json

import httpx

from app import config, ollama, settings

CANNED = {
    "sales": {"category": "sales_invoice", "direction": "money_in", "confidence": 0.9, "reason": "Invoice issued by the company to a customer.",
              "counterparty": "John Murphy", "document_number": "INV-2026-014", "issue_date": "2026-03-04", "due_date": "2026-04-03",
              "currency": "EUR", "subtotal": 4200.0, "vat_amount": 567.0, "total": 4767.0, "amount_paid": None,
              "payment_status": "unpaid", "cost_category": "roof_repair", "project": "12 Oak Drive"},
    "receipt": {"category": "receipt", "direction": "money_out", "confidence": 0.8, "reason": "Till receipt marked paid.",
                "counterparty": "Chadwicks", "document_number": None, "issue_date": "2026-02-14", "due_date": None,
                "currency": "EUR", "subtotal": "98.50", "vat_amount": "22.66", "total": "€121.16", "amount_paid": "121.16",
                "payment_status": "paid", "cost_category": "materials_roofing", "project": None},
    "bank": {"category": "bank_statement", "direction": "none", "confidence": 0.95, "reason": "Monthly bank statement.",
             "counterparty": "AIB", "document_number": None, "issue_date": "2026-03-31", "due_date": None, "currency": "EUR",
             "subtotal": None, "vat_amount": None, "total": None, "amount_paid": None, "payment_status": "not_applicable",
             "cost_category": None, "project": None},
}


class FakeOllama:
    def __init__(self, models: tuple[str, ...] = ("qwen2.5:3b",), version: str = "0.40.14") -> None:
        self.models = list(models)
        self.version = version
        self.requests: list[dict] = []
        self.answers: list = []
        self.down = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @staticmethod
    def canned(text: str) -> dict:
        low = text.lower()
        if "statement" in low:
            return CANNED["bank"]
        if "inv-" in low or "invoice" in low:
            return CANNED["sales"]
        return CANNED["receipt"]

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        path = request.url.path
        if path == "/api/version":
            return httpx.Response(200, json={"version": self.version})
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m, "size": 1} for m in self.models]})
        if path == "/api/chat":
            body = json.loads(request.content)
            self.requests.append(body)
            if body["model"] not in self.models:
                return httpx.Response(404, json={"error": f"model '{body['model']}' not found"})
            text = "\n".join(m["content"] for m in body["messages"] if m["role"] == "user")
            if self.answers:
                a = self.answers.pop(0)
                content = a(body) if callable(a) else a
            else:
                content = json.dumps(self.canned(text))
            return httpx.Response(200, json={"model": body["model"], "message": {"role": "assistant", "content": content},
                                             "done": True, "done_reason": "stop"})
        return httpx.Response(404, text=f"unhandled {path}")


def install_fake_ollama(monkeypatch, fake: FakeOllama) -> None:
    """Switch the provider to Ollama (as Admin would) and route app/ollama.py through the fake."""
    settings.set_value("llm_provider", "ollama")
    monkeypatch.setattr(ollama, "build_http_client",
                        lambda timeout=None: httpx.Client(base_url=config.OLLAMA_BASE_URL, transport=fake.transport(), timeout=5))


def pdf_with_text(text: str) -> bytes:
    """A minimal one-page PDF whose text layer is ``text`` (no parentheses/backslashes in ``text``)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out
