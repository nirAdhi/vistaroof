"""Aggregate the extracted documents into the numbers the dashboard shows.

``compute(docs, from, to)`` is the single source of every figure on the owner dashboard: totals,
monthly series, breakdowns, open invoices, projects, attention list. ``companies(docs)`` feeds the
Companies tab. Both are pure functions over the rows from ``db.all_done_documents()`` (status
``done`` only) so they are easy to unit-test and never touch the database themselves.

Conventions
-----------
* Amounts are net of VAT (``net_amount``) unless a key says ``gross``; sign comes from
  ``extract.TREATMENTS`` (credit notes are -1).
* A document without an issue date is placed by its file date and counted in ``undated_docs``.
* Open (unpaid) = payment_status in ``OPEN_STATUSES``; outstanding = total - amount_paid.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from . import settings

OPEN_STATUSES = {"unpaid", "partial", "overdue", "unknown"}
OUTLIER_FLOOR = 50_000.0   # below this an amount is never called an outlier (a roofing job can cost that)
OUTLIER_FACTOR = 10.0      # ... above it, when it is also 10× the period's median document amount


def _eff_date(d: dict[str, Any]) -> str | None:
    return d.get("issue_date") or (d.get("modified_at") or "")[:10] or None


def _month_range(start: str, end: str) -> list[str]:
    y, m = int(start[:4]), int(start[5:7])
    ey, em = int(end[:4]), int(end[5:7])
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def _r(x: float) -> float:
    return round(x + 0.0, 2)


def _outstanding(d: dict[str, Any]) -> float:
    total = d.get("total") or d.get("net_amount") or 0.0
    paid = d.get("amount_paid") or 0.0
    if d.get("payment_status") == "paid":
        return 0.0
    return max(total - paid, 0.0)


def companies(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every customer / supplier / employee with totals across all time."""
    agg: dict[str, dict[str, Any]] = {}
    for d in docs:
        name = (d.get("counterparty") or "").strip()
        if not name:
            continue
        c = agg.setdefault(name, {"name": name, "roles": {}, "docs": 0, "revenue": 0.0, "expenses": 0.0, "pipeline": 0.0,
                                  "outstanding": 0.0, "first_date": None, "last_date": None, "types": {}})
        c["docs"] += 1
        role = d.get("counterparty_role") or "other"
        c["roles"][role] = c["roles"].get(role, 0) + 1
        c["types"][d.get("doc_type") or "other"] = c["types"].get(d.get("doc_type") or "other", 0) + 1
        amt = (d.get("net_amount") or 0) * (d.get("sign") or 0)
        if d.get("treatment") == "revenue":
            c["revenue"] += amt
        elif d.get("treatment") == "expense":
            c["expenses"] += amt
        elif d.get("treatment") in ("pipeline", "commitment"):
            c["pipeline"] += amt
        if d.get("doc_type") in ("sales_invoice", "supplier_invoice") and d.get("payment_status") in OPEN_STATUSES:
            c["outstanding"] += _outstanding(d)
        ed = _eff_date(d)
        if ed:
            c["first_date"] = min(c["first_date"] or ed, ed)
            c["last_date"] = max(c["last_date"] or ed, ed)
    out = []
    for c in agg.values():
        c["role"] = max(c["roles"], key=c["roles"].get)
        for k in ("revenue", "expenses", "pipeline", "outstanding"):
            c[k] = _r(c[k])
        c["total"] = _r(c["revenue"] + c["expenses"])
        out.append(c)
    out.sort(key=lambda c: -c["total"])
    return out


def compute(docs: list[dict[str, Any]], date_from: str | None, date_to: str | None, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    today_s = today.isoformat()

    dated = []
    undated = 0
    for d in docs:
        ed = _eff_date(d)
        if ed is None:
            undated += 1
            continue
        if date_from and ed < date_from:
            continue
        if date_to and ed > date_to:
            continue
        dated.append({**d, "_date": ed, "_undated": not d.get("issue_date")})

    rev_docs = [d for d in dated if d["treatment"] == "revenue"]
    exp_docs = [d for d in dated if d["treatment"] == "expense"]
    pipe_docs = [d for d in dated if d["treatment"] == "pipeline"]
    commit_docs = [d for d in dated if d["treatment"] == "commitment"]
    pay_docs = [d for d in dated if d["treatment"] == "payment"]

    def s(ds, key="net_amount"):
        return _r(sum((d.get(key) or 0.0) * (d.get("sign") or 0) for d in ds))

    revenue = s(rev_docs)
    expenses = s(exp_docs)
    revenue_gross = s(rev_docs, "total")
    expenses_gross = s(exp_docs, "total")
    vat_out = s(rev_docs, "vat_amount")  # VAT charged on sales
    vat_in = s(exp_docs, "vat_amount")   # VAT paid on purchases

    # Receivables / payables
    receivable_docs = [d for d in rev_docs if d["doc_type"] == "sales_invoice" and d.get("payment_status") in OPEN_STATUSES]
    payable_docs = [d for d in exp_docs if d["doc_type"] in ("supplier_invoice",) and d.get("payment_status") in OPEN_STATUSES]
    receivables = _r(sum(_outstanding(d) for d in receivable_docs))
    payables = _r(sum(_outstanding(d) for d in payable_docs))
    overdue_recv = [d for d in receivable_docs if d.get("due_date") and d["due_date"] < today_s]
    overdue_pay = [d for d in payable_docs if d.get("due_date") and d["due_date"] < today_s]

    # Monthly series
    if dated:
        start = date_from or min(d["_date"] for d in dated)
        end = date_to or max(d["_date"] for d in dated)
    else:
        start = date_from or (today - timedelta(days=365)).isoformat()
        end = date_to or today_s
    months = _month_range(start[:7] + "-01", end[:7] + "-01")
    mrev: dict[str, float] = defaultdict(float)
    mexp: dict[str, float] = defaultdict(float)
    mpipe: dict[str, float] = defaultdict(float)
    for d in rev_docs:
        mrev[d["_date"][:7]] += (d.get("net_amount") or 0) * d["sign"]
    for d in exp_docs:
        mexp[d["_date"][:7]] += (d.get("net_amount") or 0) * d["sign"]
    for d in pipe_docs:
        mpipe[d["_date"][:7]] += (d.get("net_amount") or 0) * d["sign"]
    cumulative = 0.0
    monthly = []
    for m in months:
        r, e = _r(mrev.get(m, 0)), _r(mexp.get(m, 0))
        cumulative += r - e
        monthly.append({"month": m, "revenue": r, "expenses": e, "profit": _r(r - e), "cumulative_profit": _r(cumulative), "pipeline": _r(mpipe.get(m, 0))})

    def group(ds, key, amount_key="net_amount"):
        agg: dict[str, dict[str, Any]] = {}
        for d in ds:
            k = d.get(key) or "(not stated)"
            g = agg.setdefault(k, {"name": k, "amount": 0.0, "count": 0})
            g["amount"] += (d.get(amount_key) or 0) * d["sign"]
            g["count"] += 1
        out = sorted(agg.values(), key=lambda g: -g["amount"])
        for g in out:
            g["amount"] = _r(g["amount"])
        return out

    # Projects: revenue, expenses and margin per job
    projects: dict[str, dict[str, Any]] = {}
    for d in rev_docs + exp_docs + pipe_docs:
        k = d.get("project") or "(no project stated)"
        p = projects.setdefault(k, {"name": k, "revenue": 0.0, "expenses": 0.0, "pipeline": 0.0, "docs": 0})
        amt = (d.get("net_amount") or 0) * d["sign"]
        if d["treatment"] == "revenue":
            p["revenue"] += amt
        elif d["treatment"] == "expense":
            p["expenses"] += amt
        else:
            p["pipeline"] += amt
        p["docs"] += 1
    project_rows = []
    for p in projects.values():
        p["revenue"], p["expenses"], p["pipeline"] = _r(p["revenue"]), _r(p["expenses"]), _r(p["pipeline"])
        p["profit"] = _r(p["revenue"] - p["expenses"])
        p["margin"] = _r(p["profit"] / p["revenue"] * 100) if p["revenue"] else None
        project_rows.append(p)
    project_rows.sort(key=lambda p: -(p["revenue"] + p["expenses"]))

    by_type: dict[str, dict[str, Any]] = {}
    for d in dated:
        t = by_type.setdefault(d["doc_type"] or "other", {"doc_type": d["doc_type"] or "other", "count": 0, "total": 0.0})
        t["count"] += 1
        t["total"] += (d.get("total") or 0)
    for t in by_type.values():
        t["total"] = _r(t["total"])

    currencies = sorted({d.get("currency") for d in dated if d.get("currency")} - {settings.get("base_currency"), None})
    low_conf = [d for d in dated if (d.get("confidence") or 1) < 0.6]
    flagged = [d for d in dated if d.get("flags")]
    # Outliers: a reference number or a statement balance read as a total can swamp every figure on the
    # dashboard. Anything far above the period's typical amount is listed so the owner can check it.
    money_docs = [d for d in rev_docs + exp_docs if (d.get("net_amount") or 0) > 0]
    amounts = sorted(abs(d["net_amount"]) for d in money_docs)
    median = amounts[len(amounts) // 2] if amounts else 0.0
    outlier_threshold = max(OUTLIER_FLOOR, median * OUTLIER_FACTOR)
    outliers = sorted((d for d in money_docs if abs(d["net_amount"]) >= outlier_threshold and len(amounts) >= 3), key=lambda d: -abs(d["net_amount"]))

    def brief(d):
        return {
            "id": d["id"], "name": d["name"], "doc_type": d["doc_type"], "counterparty": d.get("counterparty"),
            "document_number": d.get("document_number"), "issue_date": d.get("issue_date"), "due_date": d.get("due_date"),
            "total": d.get("total"), "outstanding": _r(_outstanding(d)), "payment_status": d.get("payment_status"),
            "project": d.get("project"), "web_link": d.get("web_link"), "confidence": d.get("confidence"),
            "flags": d.get("flags") or [],
        }

    return {
        "period": {"from": date_from, "to": date_to, "today": today_s},
        "currency": settings.get("base_currency"),
        "totals": {
            "revenue": revenue, "expenses": expenses, "profit": _r(revenue - expenses),
            "margin_pct": _r((revenue - expenses) / revenue * 100) if revenue else None,
            "revenue_gross": revenue_gross, "expenses_gross": expenses_gross,
            "vat_on_sales": vat_out, "vat_on_purchases": vat_in, "vat_position": _r(vat_out - vat_in),
            "receivables": receivables, "receivables_count": len(receivable_docs),
            "overdue_receivables": _r(sum(_outstanding(d) for d in overdue_recv)), "overdue_receivables_count": len(overdue_recv),
            "payables": payables, "payables_count": len(payable_docs),
            "overdue_payables": _r(sum(_outstanding(d) for d in overdue_pay)), "overdue_payables_count": len(overdue_pay),
            "pipeline": s(pipe_docs), "pipeline_count": len(pipe_docs),
            "commitments": s(commit_docs), "commitments_count": len(commit_docs),
            "payments_recorded": s(pay_docs), "payments_count": len(pay_docs),
            "documents": len(dated), "revenue_docs": len(rev_docs), "expense_docs": len(exp_docs),
            "undated_docs": undated + sum(1 for d in dated if d["_undated"]),
        },
        "monthly": monthly,
        "expenses_by_category": group(exp_docs, "category"),
        "revenue_by_category": group(rev_docs, "category"),
        "top_customers": group(rev_docs, "counterparty")[:12],
        "top_suppliers": group(exp_docs, "counterparty")[:12],
        "projects": project_rows[:50],
        "by_doc_type": sorted(by_type.values(), key=lambda t: -t["count"]),
        "open_receivables": sorted((brief(d) for d in receivable_docs), key=lambda b: (b["due_date"] or "9999", b["issue_date"] or "")),
        "open_payables": sorted((brief(d) for d in payable_docs), key=lambda b: (b["due_date"] or "9999", b["issue_date"] or "")),
        "pipeline_docs": [brief(d) for d in sorted(pipe_docs, key=lambda d: d["_date"], reverse=True)][:50],
        "attention": {
            "other_currencies": currencies,
            "low_confidence": [brief(d) for d in low_conf][:50],
            "flagged": [brief(d) for d in flagged][:100],
            "outliers": [brief(d) for d in outliers][:10],
            "outlier_threshold": _r(outlier_threshold),
        },
    }

