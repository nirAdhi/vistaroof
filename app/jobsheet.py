"""Vista Roofing "JOB SUMMARY" sheets, read without a model.

Cormac keeps one sheet per job in a job-costing workbook. The layout (first column = labels):

    JOB SUMMARY
    SITE:            | JOB NO:
    Bakers Yard      | 101
    PROJECT MANAGER  | <name>
    CONTRACT VALUE/SUM
                     | VALUE | COST | MARK UP | GROSS PROFIT%
    TOTAL            | 0.00  | 0    | 0       | 0
    Retention        | 0.00
    BALANCE DUE      | -70,000.00
    VALUATION
                     | VALUATION 1 | VALUATION 2 | … | Final Agreed | Draft Final | Final
    Works to date    | 451.00      | 7,010.00    | … | 70,000.00
    increase         | …
    TOTAL            | …
                     | 11/27/2020  | 12/22/2020  | … | 6/27/2022            (the valuation dates)
    INVOICED/PAYMENT
                     | Invoice 1 | Invoice 2 | … | Invoice 10 | Total | Gross Due | Retention Due
    INVOICE AMOUNT Excl VAT |      | 6,659.50  | … | 350        | €70,000.00 | €0.00 | €0.00
    DATE             |           | 3/16/2021 | …
    PAID  N/Y        |           | **Y       | Y …                        (** = paid less 20% RCT tax)

The sheet reaches us as text, one line per row with cells joined by " | " (textract.workbook_sheets), so
splitting a line on " | " gives the columns back. Everything here is deterministic: an invoice column
becomes a *sales invoice* document (net amount, date, paid or not, the job as its project), and the sheet
itself a job record (contract value, valuations, balance due). A model never sees these sheets.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

MODEL = "jobsheet"  # documents.model for figures taken straight from a job summary sheet
SEP = " | "
INV_SEP = "#inv:"  # an invoice document's source_id = "<file>#sheet:<sheet>#inv:<column key>"

_NUM = re.compile(r"^[€£$]?\s*-?\d[\d,]*(?:\.\d+)?$")


def is_job_summary(text: str) -> bool:
    first = next((ln for ln in text.split("\n") if ln.strip()), "")
    return first.split(SEP)[0].strip().upper().startswith("JOB SUMMARY")


def _num(cell: str) -> float | None:
    s = (cell or "").strip().replace(",", "")
    s = s.lstrip("€£$").strip()
    if not s or not _NUM.match((cell or "").strip()):
        return None
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def _date(cell: str) -> str | None:
    """openpyxl hands dates over as "2021-03-16 00:00:00"; a sheet typed by hand or pasted shows 3/16/2021
    (US order in this workbook) or 16/03/2021. None when the cell is not a date."""
    s = (cell or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2}|\d{4})$", s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = y + 2000 if y < 100 else y  # a typed "10/19/26"
        month, day = (a, b) if a <= 12 else (b, a)  # m/d/yyyy as the workbook shows; d/m when the first part cannot be a month
        try:
            return datetime(y, month, day).date().isoformat()
        except ValueError:
            return None
    m = re.match(r"^(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{2}|\d{4})$", s)  # "19 Oct 2026", "19 October 26"
    if m:
        try:
            y = int(m.group(3))
            return datetime.strptime(f"{m.group(1)} {m.group(2)[:3]} {y + 2000 if y < 100 else y}", "%d %b %Y").date().isoformat()
        except ValueError:
            return None
    return None


def _col_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def parse(text: str) -> dict[str, Any] | None:
    """The sheet's figures, or None when it is not a job summary sheet."""
    if not is_job_summary(text):
        return None
    rows = [[c.strip() for c in ln.split(SEP)] for ln in text.split("\n")]

    def cell(r: int, c: int) -> str:
        return rows[r][c] if 0 <= r < len(rows) and 0 <= c < len(rows[r]) else ""

    def find(label: str, start: int = 0) -> int:
        lab = label.lower()
        for i in range(start, len(rows)):
            if rows[i] and rows[i][0].lower().startswith(lab):
                return i
        return -1

    out: dict[str, Any] = {"site": None, "job_no": None, "address": None, "project_manager": None, "contract": {}, "retention": None,
                           "balance_due": None, "valuations": [], "final_agreed": None, "invoices": [], "invoiced_total": None}
    r = find("site")
    if r >= 0:
        out["site"] = cell(r + 1, 0) or None
        for c, v in enumerate(rows[r]):
            if v.lower().startswith("job no"):
                out["job_no"] = cell(r + 1, c) or None
        # a second line under the site (the address / development name), unless it is the next label
        nxt = cell(r + 2, 0)
        out["address"] = nxt if nxt and not nxt.lower().startswith(("project manager", "contract", "valuation", "invoiced")) else None
    r = find("project manager")
    if r >= 0:
        out["project_manager"] = next((v for v in rows[r][1:] if v), None)
    r = find("contract value")
    if r >= 0:
        t = find("total", r + 1)  # the TOTAL row under VALUE | COST | MARK UP | GROSS PROFIT%
        if t >= 0:
            keys = ["value", "cost", "mark_up", "gross_profit_pct"]
            out["contract"] = {k: _num(cell(t, i + 1)) for i, k in enumerate(keys)}
    r = find("retention")
    if r >= 0:
        out["retention"] = _num(cell(r, 1))
    r = find("balance due")
    if r >= 0:
        out["balance_due"] = _num(cell(r, 1))
    # valuations: header row with "VALUATION 1 …", the cumulative "Works to date" row, and the dates row below TOTAL
    v = find("valuation")
    if v >= 0:
        h = next((i for i in range(v, min(v + 4, len(rows))) if len(rows[i]) > 1 and rows[i][1].lower().startswith("valuation")), -1)
        w = find("works to date", v)
        if h >= 0 and w >= 0:
            dates_row = -1
            t = find("total", w)
            for i in range(t + 1, min(t + 4, len(rows))):
                if any(_date(c) for c in rows[i][1:]):
                    dates_row = i
                    break
            for c in range(1, len(rows[h])):
                label = rows[h][c]
                amount = _num(cell(w, c))
                if not label or amount is None:
                    continue
                inc = find("increase", w)
                ret = find("less", w)
                out["valuations"].append({"label": label, "works_to_date": amount, "increase": _num(cell(inc, c)) if inc >= 0 else None,
                                          "retention": _num(cell(ret, c)) if ret >= 0 else None,
                                          "date": _date(cell(dates_row, c)) if dates_row >= 0 else None})
                if label.lower().startswith("final agreed"):
                    out["final_agreed"] = amount
    # invoices: header "Invoice 1 … Total", the "INVOICE AMOUNT Excl VAT" row, the DATE row and the PAID row below it
    p = find("invoiced")
    if p >= 0:
        h = next((i for i in range(p, min(p + 3, len(rows))) if len(rows[i]) > 1 and any(x.lower().startswith("invoice") for x in rows[i][1:])), -1)
        a = find("invoice amount", p)
        if h >= 0 and a >= 0:
            # the invoice columns end where the totals start: "Total", "Total Paid", "Gross Due", "Retention Due"…
            total_col = next((c for c, x in enumerate(rows[h]) if c > 0 and x.lower().startswith(("total", "gross due", "retention due", "gross", "retention"))),
                             max(len(rows[h]), len(rows[a])))
            d = find("date", a)
            paid = find("paid", a)
            for c in range(1, total_col):
                amount = _num(cell(a, c))
                if not amount:  # an empty column, or 0.00: nothing invoiced there
                    continue
                paid_cell = cell(paid, c) if paid >= 0 else ""
                status = "paid" if paid_cell.strip("* ").upper().startswith("Y") else "unpaid" if paid_cell.strip("* ").upper().startswith("N") else "unknown"
                out["invoices"].append({
                    "key": _col_letter(c), "label": rows[h][c] or f"Invoice (column {_col_letter(c)})", "net": amount,
                    "date": _date(cell(d, c)) if d >= 0 else None, "payment_status": status,
                    "rct_deducted": "**" in paid_cell,
                })
            out["invoiced_total"] = _num(cell(a, total_col)) if total_col < len(rows[a]) else None
    if out["invoiced_total"] is None and out["invoices"]:
        out["invoiced_total"] = round(sum(i["net"] for i in out["invoices"]), 2)
    return out


def _eur(v: float | None) -> str:
    return "—" if v is None else f"€{v:,.2f}"


def job_name(parsed: dict[str, Any]) -> str:
    site, no = parsed.get("site"), parsed.get("job_no")
    return f"{site} (job {no})" if site and no else site or (f"job {no}" if no else "job")


def job_fields(parsed: dict[str, Any]) -> dict[str, Any]:
    """The sheet as one ledger document: a job record (type other, counts nothing), dated by its last invoice."""
    inv = parsed["invoices"]
    last = max((i["date"] for i in inv if i["date"]), default=None) or max((v["date"] for v in parsed["valuations"] if v["date"]), default=None)
    paid = round(sum(i["net"] for i in inv if i["payment_status"] == "paid"), 2)
    bits = [f"Job summary for {job_name(parsed)}"]
    if parsed.get("final_agreed") is not None:
        bits.append(f"final agreed {_eur(parsed['final_agreed'])}")
    elif parsed["contract"].get("value"):
        bits.append(f"contract value {_eur(parsed['contract']['value'])}")
    if inv:
        bits.append(f"invoiced {_eur(parsed['invoiced_total'])} excl VAT over {len(inv)} invoice{'s' if len(inv) != 1 else ''} ({_eur(paid)} paid)")
    if parsed["valuations"]:
        v = parsed["valuations"][-1]
        bits.append(f"works to date {_eur(v['works_to_date'])} at {v['label']}" + (f" ({v['date']})" if v["date"] else ""))
    if parsed.get("balance_due") is not None:
        bits.append(f"balance due {_eur(parsed['balance_due'])}")
    return {
        "doc_type": "other", "direction": "none", "payment_status": "not_applicable", "counterparty": parsed.get("site"),
        "counterparty_role": "customer", "document_number": f"Job {parsed['job_no']}" if parsed.get("job_no") else None,
        "issue_date": last, "project": job_name(parsed), "total": parsed.get("final_agreed") or parsed["contract"].get("value"),
        "summary": "; ".join(bits) + ".", "confidence": 1.0, "flags": ["job_summary_sheet"], "raw": parsed,
    }


def invoice_fields(parsed: dict[str, Any], inv: dict[str, Any]) -> dict[str, Any]:
    """One invoice column as a sales invoice: net amount (the sheet carries no VAT), its date, paid or not. The site
    is the customer (the sheet names nobody else: the job is invoiced to whoever the site belongs to)."""
    flags = ["job_summary_sheet", "amount_excl_vat_only"] + (["rct_tax_deducted"] if inv.get("rct_deducted") else [])
    return {
        "doc_type": "sales_invoice", "direction": "money_in", "counterparty": parsed.get("site"), "counterparty_role": "customer",
        "document_number": inv["label"], "issue_date": inv["date"], "project": job_name(parsed), "subtotal": inv["net"],
        "vat_amount": None, "total": inv["net"], "amount_paid": inv["net"] if inv["payment_status"] == "paid" else None,
        "payment_status": inv["payment_status"], "category": None, "confidence": 1.0, "flags": flags,
        "summary": f"{inv['label']} on {job_name(parsed)}: {_eur(inv['net'])} excl VAT, {inv['payment_status']}"
                   + (", paid less 20% RCT tax" if inv.get("rct_deducted") else "") + " (from the job summary sheet).",
        "raw": {"from": "job summary sheet", "column": inv["key"], **{k: v for k, v in inv.items() if k != "key"}},
    }
