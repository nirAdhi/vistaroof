"""End-to-end smoke test against the real Ollama server: classifies three sample documents.

Run from the project root, where .env holds OLLAMA_BASE_URL / OLLAMA_MODEL:

    .venv/bin/python scripts/ollama_smoke.py            # Linux container
    .venv\\Scripts\\python scripts\\ollama_smoke.py        # Windows

It forces LLM_PROVIDER=ollama for this process, builds a text invoice, an Excel bank statement and
a one-page PDF receipt in memory, runs each through ``extract.extract`` (text extraction → Ollama →
bookkeeping rules) and prints what the ledger would store. Exit code 0 when all three are read.
Nothing is written to the database.
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
from pathlib import Path

os.environ["LLM_PROVIDER"] = "ollama"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, extract, llm  # noqa: E402
from app.sources import FileContent, FileMeta  # noqa: E402


def _pdf(text: str) -> bytes:
    stream = f"BT /F1 11 Tf 60 740 Td ({text}) Tj ET".encode("latin-1")
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out)); out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


def _xlsx() -> bytes:
    import openpyxl

    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Statement March 2026"
    ws.append(["AIB Business Current Account 1234", "", ""]); ws.append(["Date", "Description", "Amount"])
    ws.append(["2026-03-02", "Chadwicks Ltd card payment", -121.16]); ws.append(["2026-03-10", "Lodgement J Murphy INV-2026-014", 4767.00])
    ws.append(["", "Closing balance", 9120.44])
    buf = io.BytesIO(); wb.save(buf); return buf.getvalue()


SAMPLES = [
    ("INV-2026-014 Murphy.txt", FileContent(mime="text/plain", text=(
        "VISTA ROOFING LIMITED\nUnit 77, Park West Enterprise Centre, Dublin 12\nINVOICE INV-2026-014\nDate: 2026-03-04   Due: 2026-04-03\n"
        "Bill to: John Murphy, 12 Oak Drive, Lucan, Co. Dublin\nRe-roof of rear extension, 40 m2 natural slate, new lead flashings\n"
        "Net          4,200.00\nVAT 13.5%      567.00\nTOTAL EUR    4,767.00\nPayment terms 30 days. Bank: AIB IBAN IE12AIBK...")),
     "sales_invoice / money_in / revenue"),
    ("chadwicks receipt.pdf", FileContent(mime="application/pdf", data=_pdf(
        "CHADWICKS Cork Branch 14/02/2026 10:42 Roof battens 25x50 x40 86.00 Slate nails 2kg 12.50 Subtotal 98.50 VAT 23% 22.66 TOTAL 121.16 VISA PAID")),
     "receipt / money_out / expense"),
    ("AIB statement March 2026.xlsx", FileContent(mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", data=_xlsx()),
     "bank_statement / none / none"),
]


def main() -> int:
    print(f"Ollama {config.OLLAMA_BASE_URL}  model {config.OLLAMA_MODEL}  num_ctx {max(config.OLLAMA_NUM_CTX, 8192)}  timeout {config.OLLAMA_TIMEOUT:g}s")
    h = llm.health()
    print(("OK: " if h["ok"] else "FAILED: ") + str(h.get("message")))
    if not h["ok"]:
        return 1
    failures = 0
    for name, content, expected in SAMPLES:
        meta = FileMeta("local", None, f"smoke:{name}", name, content.mime, "samples", "", len(content.data or content.text or ""), "2026-03-04T00:00:00+00:00")
        t0 = time.monotonic()
        try:
            fields, model = extract.extract(meta, content)
        except Exception as exc:
            failures += 1
            print(f"\n{name}: FAILED after {time.monotonic() - t0:.1f}s: {type(exc).__name__}: {exc}")
            continue
        got = f"{fields['doc_type']} / {fields['direction']} / {fields['treatment']}"
        print(f"\n{name}  ({model}, {time.monotonic() - t0:.1f}s, {fields['raw'].get('attempts')} attempt(s))\n  expected {expected}\n  got      {got}{'' if got == expected else '   <-- differs'}")
        print("  " + json.dumps({k: fields.get(k) for k in ("counterparty", "document_number", "issue_date", "total", "vat_amount", "net_amount",
                                                            "payment_status", "category", "confidence", "summary")}, default=str))
    print(f"\n{len(SAMPLES) - failures} of {len(SAMPLES)} documents read.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
