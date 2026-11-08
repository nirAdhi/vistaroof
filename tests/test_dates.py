"""Dates as models write them, the start-up / Admin date repair, and period filtering that agrees between
the dashboard figures and the Documents list."""
from __future__ import annotations

import json

from app import db, extract, repair
from app.sources import FileMeta

from conftest import ADMIN_PW  # noqa: F401


def test_dates_in_common_notations_are_accepted():
    d = extract._date
    assert d("2026-03-04") == "2026-03-04"
    assert d("04/03/2026") == "2026-03-04" and d("4/3/26") == "2026-03-04" and d("04.03.2026") == "2026-03-04" and d("4-3-2026") == "2026-03-04"
    assert d("2026/03/04") == "2026-03-04" and d("2026-3-4") == "2026-03-04" and d("2026-03-04T10:15:00Z") == "2026-03-04"
    assert d("4 Mar 2026") == "2026-03-04" and d("4th March 2026") == "2026-03-04" and d("14 February 2026") == "2026-02-14" and d("1st Sept 2026") == "2026-09-01"
    assert d("31/02/2026") is None and d("yesterday") is None and d("") is None and d(None) is None and d(20260304) is None
    assert extract.derive({"doc_type": "sales_invoice", "direction": "money_in", "total": 10, "issue_date": "14/02/2026", "due_date": "14 Mar 2026"})["issue_date"] == "2026-02-14"


def _stored(name: str, issue, due, raw: dict, overridden: int = 0, source: str = "local") -> int:
    meta = FileMeta(source, 1, f"x:{name}", name, "application/pdf", "Docs", "", 10, "2026-05-01T00:00:00+00:00")
    doc_id = db.upsert_file_metadata(meta.as_dict())
    db.save_extraction(doc_id, {"doc_type": "sales_invoice", "direction": "money_in", "treatment": "revenue", "sign": 1, "total": 100.0,
                                "net_amount": 100.0, "issue_date": issue, "due_date": due, "raw": raw}, "ollama/qwen2.5:3b")
    if overridden:
        db.update_document(doc_id, {"project": "x"}, {})
    return doc_id


def test_repair_fills_dates_from_stored_answers_without_touching_edits(admin, fresh_db):
    a = _stored("INV-1.pdf", None, None, {"issue_date": "14/02/2026", "due_date": "14th March 2026"})
    b = _stored("INV-2.pdf", "2026-01-05", None, {"issue_date": "2026-01-05", "due_date": "05 Feb 2026"})
    c = _stored("INV-3.pdf", None, None, {"issue_date": "no date", "due_date": None})
    d = _stored("INV-4.pdf", None, None, {"issue_date": "14/02/2026"}, overridden=1)
    r = admin.post("/api/admin/repair-dates").json()
    assert r == {"ok": True, "checked": 3, "updated": 2}  # the hand-edited one is not even considered
    docs = {x["id"]: x for x in admin.get("/api/documents?limit=100").json()}
    assert docs[a]["issue_date"] == "2026-02-14" and docs[a]["due_date"] == "2026-03-14"
    assert docs[b]["issue_date"] == "2026-01-05" and docs[b]["due_date"] == "2026-02-05"
    assert docs[c]["issue_date"] is None and docs[d]["issue_date"] is None
    assert repair.repair_dates() == {"checked": 1, "updated": 0}  # idempotent: only the hopeless one is left


def test_documents_list_and_summary_agree_on_the_period(admin, fresh_db):
    _stored("dated-march.pdf", "2026-03-10", None, {})
    _stored("undated-filed-in-may.pdf", None, None, {})  # placed by its file date, 2026-05-01
    in_march = admin.get("/api/documents?from=2026-03-01&to=2026-03-31").json()
    in_may = admin.get("/api/documents?from=2026-05-01&to=2026-05-31").json()
    assert [d["name"] for d in in_march] == ["dated-march.pdf"] and [d["name"] for d in in_may] == ["undated-filed-in-may.pdf"]
    assert admin.get("/api/summary?from=2026-03-01&to=2026-03-31").json()["totals"]["documents"] == 1
    s = admin.get("/api/summary?from=2026-05-01&to=2026-05-31").json()["totals"]
    assert s["documents"] == 1 and s["undated_docs"] == 1
    assert len(admin.get("/api/documents").json()) == 2 and admin.get("/api/documents?from=2026-10-01").json() == []
