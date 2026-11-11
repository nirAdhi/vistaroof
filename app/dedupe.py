"""Count an invoice once when it reaches the ledger twice.

A job's invoices arrive from two places: the JOB SUMMARY sheet (``app/jobsheet.py``: one sales invoice per
column, net amount excl VAT, date, paid flag) and, often, the invoice PDF itself in the Invoices library.
Both are real documents and both stay in the ledger, but the dashboard must count the invoice once.

The PDF is the better record (VAT, customer, line items), so the *sheet* row is the one marked as the
duplicate (``documents.duplicate_of`` = the PDF's id). A sheet row matches a non-sheet sales invoice when
the net amounts agree to the cent and either the dates are within ``WINDOW_DAYS`` of each other or the
other document names the same site. Each PDF can absorb one sheet row; the closest date wins.

``refresh()`` recomputes every mark from scratch (cheap: a few thousand rows) after each sync run, a
re-read and a hand edit, so a deleted PDF or a corrected amount is reflected at once. The analytics
drop marked rows; the Documents list shows them with a "counted once" chip.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

from . import db, jobsheet

log = logging.getLogger(__name__)

WINDOW_DAYS = 60
AMOUNT_TOLERANCE = 0.02


def _days_apart(a: str | None, b: str | None) -> int | None:
    if not a or not b:
        return None
    try:
        return abs((date.fromisoformat(a[:10]) - date.fromisoformat(b[:10])).days)
    except ValueError:
        return None


def _names_site(other: dict[str, Any], site: str) -> bool:
    s = site.strip().lower()
    if len(s) < 4:
        return False
    hay = " ".join(str(other.get(k) or "") for k in ("project", "counterparty", "name", "summary")).lower()
    return s in hay


def find_duplicates(docs: list[dict[str, Any]]) -> dict[int, int]:
    """{sheet invoice id: matching invoice document id} for the ``done`` sales invoices given."""
    sheet = [d for d in docs if d.get("model") == jobsheet.MODEL and d.get("doc_type") == "sales_invoice"]
    others = [d for d in docs if d.get("model") != jobsheet.MODEL and d.get("doc_type") == "sales_invoice"]
    if not sheet or not others:
        return {}
    by_amount: dict[int, list[dict[str, Any]]] = {}
    for o in others:
        if o.get("net_amount") is not None:
            by_amount.setdefault(round(float(o["net_amount"]) * 100), []).append(o)
    pairs: list[tuple[int, int, int]] = []  # (rank, sheet id, other id): smaller rank = better match
    for s in sheet:
        if s.get("net_amount") is None:
            continue
        cents = round(float(s["net_amount"]) * 100)
        tol = round(AMOUNT_TOLERANCE * 100)
        for c in range(cents - tol, cents + tol + 1):
            for o in by_amount.get(c, []):
                days = _days_apart(s.get("issue_date"), o.get("issue_date"))
                site_ok = _names_site(o, s.get("counterparty") or "")
                if (days is not None and days <= WINDOW_DAYS) or site_ok:
                    rank = (days if days is not None else WINDOW_DAYS + 1) - (WINDOW_DAYS if site_ok else 0)
                    pairs.append((rank, s["id"], o["id"]))
    pairs.sort()
    taken_sheet: set[int] = set()
    taken_other: set[int] = set()
    out: dict[int, int] = {}
    for _, sid, oid in pairs:
        if sid in taken_sheet or oid in taken_other:
            continue
        out[sid] = oid
        taken_sheet.add(sid)
        taken_other.add(oid)
    return out


def refresh() -> int:
    """Recompute every duplicate mark. Returns how many sheet invoices are now counted once."""
    docs = db.sales_invoices_for_dedupe()
    mapping = find_duplicates(docs)
    changed = db.set_duplicates(mapping, [d["id"] for d in docs])
    if changed:
        log.info("duplicates: %d sheet invoice(s) matched to invoice documents (%d mark(s) changed)", len(mapping), changed)
    return len(mapping)
