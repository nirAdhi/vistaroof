"""Health check for the configured document reader (Ollama by default).

    python -m app.llm_check            # version, installed models, is OLLAMA_MODEL there, one-line probe
    python -m app.llm_check --sample   # also classify two built-in sample documents end to end

Exit code 0 when everything works, 1 otherwise. Reads LLM_PROVIDER / OLLAMA_* from .env.
"""
from __future__ import annotations

import json
import sys

from . import config, llm


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    print(f"provider: {config.LLM_PROVIDER}")
    if config.LLM_PROVIDER == "ollama":
        print(f"url:      {config.OLLAMA_BASE_URL}\nmodel:    {config.OLLAMA_MODEL}\nnum_ctx:  {max(config.OLLAMA_NUM_CTX, 8192)}"
              f"\ntimeout:  {config.OLLAMA_TIMEOUT:g}s\nkeep:     {config.OLLAMA_KEEP_ALIVE}\nfallback: {'Claude' if config.LLM_FALLBACK else 'off'}")
    h = llm.health()
    print(f"\n{'OK' if h['ok'] else 'FAILED'}: {h.get('message')}")
    if h.get("models"):
        print("installed models: " + ", ".join(h["models"]))
    if h.get("probe"):
        print(f"probe answer: {h['probe']!r}")
    if h.get("fallback"):
        print(h["fallback"])
    if not h["ok"]:
        return 1
    if "--sample" in argv:
        from .sources import FileContent, FileMeta
        from . import extract

        samples = [
            ("INV-2026-014 Murphy.txt", "VISTA ROOFING LIMITED\nUnit 77 Park West, Dublin 12\nINVOICE INV-2026-014  Date: 2026-03-04  Due: 2026-04-03\n"
                                        "Bill to: John Murphy, 12 Oak Drive, Lucan\nRe-roof rear extension, 40 m2 slate\nNet 4,200.00\nVAT 13.5% 567.00\nTOTAL EUR 4,767.00\nUnpaid"),
            ("chadwicks till receipt.txt", "CHADWICKS Cork Branch\n14/02/2026 10:42\nRoof battens 25x50 x40  86.00\nSlate nails 2kg  12.50\nSubtotal 98.50\nVAT 23% 22.66\nTOTAL 121.16\nVISA ****1234  PAID"),
        ]
        for name, text in samples:
            meta = FileMeta("local", None, name, name, "text/plain", "samples", "", len(text), "2026-03-04T00:00:00+00:00")
            fields, model = extract.extract(meta, FileContent(mime="text/plain", text=text))
            print(f"\n{name} -> {model}")
            print(json.dumps({k: fields.get(k) for k in ("doc_type", "direction", "treatment", "counterparty", "total", "vat_amount",
                                                          "payment_status", "category", "confidence", "summary")}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
