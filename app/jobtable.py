"""Vista Roofing turnover workbook: one row per job, read without a model.

Since November 2026 Cormac keeps the company figures as tables rather than one JOB SUMMARY sheet per job
(``app/jobsheet.py``). A "Retention" sheet lists every job once, and a "Turnover and WIH <year>" sheet per
year carries that year's turnover (WIH = work in hand). The header row names the columns:

    Job Nr | Project    | Client   | Total Contract Value | Valuations to Date | Paid to Date | Current Due | Overdue | Retention Due |
    101    | Bakers Yard| Townlink | 42988.00             | 70000.00           | 70000.00     | 0.00        |         | 0.00          | 5% retention

Columns are recognised by their names, so the order does not matter and extra columns are kept in ``raw``.
Deterministic, like the job sheets: a row of a year sheet with a turnover figure becomes a *sales invoice*
document (net amount, dated in that year, the client as the customer, the job as its project); a row of
the Retention sheet (or any job table without turnover columns) becomes a job record (contract value,
valued to date, paid, due, retention — the company-health figures). A model never sees these sheets.

Vista's real year sheets look like this (the header row is the 4th line, under a title and a line of
"Budget" / "Total" captions; the month headers are date cells):

    Job  | Description | Margin | Client   | Contract | Variations | Total  | Nov-20 | Dec-20 | Jan-21 … Dec-21 | Value 2020/21 | Gross Profit | Carry Over to Y2
    C101 | Bakers Yard | 22.50% | Townlink | 9,776    | 33,212     | 42,988 | 451    | 6,559  | …               | 34,945        | 7,863        | 8,043

A sheet is a job table when, within its first lines, a row has a "Job Nr / Job No / Job" cell, a "Project",
"Description" or "Site" cell and at least one known figure column. Rows whose Job cell is empty (totals,
blank lines) are skipped. The sheet's year comes from its name ("Turnover and WIH 2026" → 2026, "2020-21"
→ 2021). Month columns are dated from the sheet's name too, never from the header's own year: a "2020-21"
sheet whose months start at Nov runs Nov 2020 … Dec 2021, and a "2023" sheet counts as 2023 even when its
headers still say "Jan-22" (Cormac copies the sheet from the year before). "C101" on a year sheet and "101"
on the Retention sheet are the same job.
"""
from __future__ import annotations

import re
from typing import Any

from .jobsheet import SEP, _num

MODEL = "jobtable"  # documents.model for figures taken straight from a turnover table
# sources.workbook_layout: which workbook layout carries the revenue (Admin → source card → Workbook layout)
LAYOUTS = {"auto": "Auto-detect (both layouts count)", "jobsheets": "Job summary sheets (one sheet per job)",
           "jobtable": "Turnover tables (one row per job)"}
ROW_SEP = "#row:"   # a job row's source_id = "<file>#sheet:<sheet>#row:<job nr>"
COL_SEP = "#col:"   # a turnover figure's source_id = "<row id>#col:<column key>"
HEADER_LINES = 8    # the header row is within the first lines of the sheet

# header text (lower-cased, start) → column key
_COLUMNS: list[tuple[tuple[str, ...], str]] = [
    (("job nr", "job no", "job number", "job #", "job"), "job_no"),
    (("project", "site", "job name", "description"), "project"),
    (("client", "customer", "contractor"), "client"),
    (("total contract value", "contract value", "contract sum", "contract", "total contract"), "contract_value"),
    (("variations", "variation"), "variations"),
    (("valuations to date", "valuation to date", "valued to date", "certified to date", "valuations", "valuation"), "valuations_to_date"),
    (("paid to date", "received to date", "paid", "received"), "paid_to_date"),
    (("current due", "amount due", "due"), "current_due"),
    (("overdue",), "overdue"),
    (("retention due", "retention held", "retention"), "retention_due"),
    (("turnover", "invoiced", "sales", "income", "revenue", "certified", "value this year", "this year", "value"), "turnover"),
    (("wih", "work in hand", "wip", "work in progress", "remaining", "to invoice", "balance to invoice"), "wih"),
    (("gross profit", "profit"), "gross_profit"),
    (("carry over", "carried over", "carry forward", "carried forward"), "carry_over"),
    (("notes", "note", "comments", "comment", "remarks"), "note"),
]
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_MONTH_HEAD = re.compile(r"^(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?"
                         r"|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?(?:['\s\-/]*(?:20)?\d{2})?$")
FIGURE_KEYS = ("contract_value", "variations", "valuations_to_date", "paid_to_date", "current_due", "overdue", "retention_due",
               "turnover", "wih", "gross_profit", "carry_over")
LABELS = {"contract_value": "Contract value", "variations": "Variations", "valuations_to_date": "Valued to date",
          "paid_to_date": "Paid to date", "current_due": "Current due", "overdue": "Overdue", "retention_due": "Retention due",
          "turnover": "Turnover", "wih": "Work in hand", "gross_profit": "Gross profit", "carry_over": "Carry over"}


def _column_key(header: str) -> str | None:
    h = re.sub(r"\s+", " ", (header or "").strip().lower().replace("€", "")).strip(" :")
    if not h:
        return None
    m = re.match(r"^(20\d{2})-(\d{2})-\d{2}$", h)  # a date cell ("Nov-20" typed in Excel is 1 Nov 2020)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"month:{int(m.group(2)):02d}"
    m = _MONTH_HEAD.match(h)  # "Jan", "January", "Sept", "Jan 2026", "Jan-26", "Jan '26" — but not "Margin"
    if m:
        return f"month:{_MONTHS.index(m.group(1)[:3]) + 1:02d}"
    for names, key in _COLUMNS:  # an exact name first ("job" is job_no, "job name" is the project)
        if h in names:
            return key
    for names, key in _COLUMNS:
        if any(h.startswith(n + " ") or h.startswith(n + "/") or h.startswith(n + "(") for n in names):
            return key
    return None


def sheet_year(name: str) -> int | None:
    """"Turnover and WIH 2026" → 2026; "Turnover and WIH 2020-21" → 2021 (the year the period ends in)."""
    m = re.search(r"(20\d{2})\s*[-/]\s*(\d{2,4})", name or "")
    if m:
        y2 = int(m.group(2))
        return y2 + 2000 if y2 < 100 else y2
    m = re.search(r"(20\d{2})", name or "")
    return int(m.group(1)) if m else None


def sheet_start_year(name: str) -> int | None:
    """"Turnover and WIH 2020-21" → 2020: the year its first month column is in."""
    m = re.search(r"(20\d{2})", name or "")
    return int(m.group(1)) if m else None


def _month_columns(months: list[tuple[int, int]], start_year: int | None) -> dict[str, int]:
    """{"month:2020-11": column} for the month columns in sheet order. The year starts at the sheet's first year
    and moves on each time the month number drops (Nov-20, Dec-20, Jan-21 …); the header's own year is ignored
    because a copied sheet keeps last year's headers. Without a year the key is "month:11"."""
    out: dict[str, int] = {}
    year, prev = start_year, 0
    for c, mm in months:
        if mm < prev and year:
            year += 1
        prev = mm
        key = f"month:{year}-{mm:02d}" if year else f"month:{mm:02d}"
        out.setdefault(key, c)
    return out


def _header(rows: list[list[str]], start_year: int | None = None) -> tuple[int, dict[str, int]] | None:
    """(header row index, {column key: column index}) when the sheet is a job table."""
    for i, cells in enumerate(rows[:HEADER_LINES]):
        cols: dict[str, int] = {}
        months: list[tuple[int, int]] = []
        for c, v in enumerate(cells):
            key = _column_key(v)
            if key and key.startswith("month:"):
                months.append((c, int(key[6:])))
            elif key and key not in cols:
                cols[key] = c
        cols.update(_month_columns(months, start_year))
        figures = any(k in cols for k in FIGURE_KEYS) or any(k.startswith("month:") for k in cols)
        if "job_no" in cols and ("project" in cols or "client" in cols) and figures:
            return i, cols
    return None


def is_job_table(text: str) -> bool:
    return _header([[c.strip() for c in ln.split(SEP)] for ln in text.split("\n")]) is not None


_JOB_NO = re.compile(r"^[A-Za-z]?\d[\w.\-/ ]*$")


def parse(name: str, text: str) -> dict[str, Any] | None:
    """The table's rows, or None when the sheet is not a job table."""
    rows = [[c.strip() for c in ln.split(SEP)] for ln in text.split("\n")]
    head = _header(rows, sheet_start_year(name))
    if head is None:
        return None
    hi, cols = head
    headers = rows[hi]
    months = sorted(k for k in cols if k.startswith("month:"))
    jobs: list[dict[str, Any]] = []
    for r in rows[hi + 1:]:
        cell = lambda key: (r[cols[key]] if key in cols and cols[key] < len(r) else "")  # noqa: E731
        job_no = cell("job_no").strip()
        if not job_no or not _JOB_NO.match(job_no) or _column_key(job_no) == "job_no":
            continue  # a total line, a blank line or a repeated header
        values = {k: _num(cell(k)) for k in FIGURE_KEYS if k in cols}
        values["months"] = {k[6:]: _num(cell(k)) for k in months}
        note = cell("note") if "note" in cols else ""
        if not note:  # free text in an unnamed column after the names ("5% retention", "fully paid", "invoice includes 23% vat")
            known = set(cols.values())
            after = max(cols.get("client", 0), cols.get("project", 0), cols["job_no"])
            tail = [v for c, v in enumerate(r) if c > after and c not in known and v and _num(v) is None]
            note = tail[0] if tail else ""
        known = set(cols.values())  # figures under a header the app does not know are kept by their header
        values["other"] = {headers[c]: _num(v) for c, v in enumerate(r) if c not in known and c < len(headers) and headers[c] and _num(v) is not None}
        jobs.append({"job_no": job_no, "project": cell("project") or None, "client": cell("client") or None, "note": note or None, **values})
    return {"sheet": name, "year": sheet_year(name), "headers": headers, "columns": cols, "jobs": jobs,
            "has_turnover": "turnover" in cols or bool(months)}


def job_key(job_no: str | None) -> str | None:
    """The job number without a letter prefix: "C101" and "101" are the same job (Retention vs year sheets)."""
    return re.sub(r"^[A-Za-z]{1,2}(?=\d)", "", job_no.strip()) if job_no else job_no


def job_name(job: dict[str, Any]) -> str:
    site, no = job.get("project"), job_key(job.get("job_no"))
    return f"{site} (job {no})" if site and no else site or f"job {no}"


def month_label(key: str, year: int | None = None) -> str:
    """"2020-11" → "Nov 2020"; "11" → "Nov <year>"."""
    y, _, mm = key.rpartition("-")
    y = y or (str(year) if year else "")
    return f"{_MONTHS[int(mm) - 1].title()} {y}".strip()


def _eur(v: float | None) -> str:
    return "—" if v is None else f"€{v:,.2f}"


def _raw(parsed: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    return {"from": "turnover table", "sheet": parsed["sheet"], "year": parsed.get("year"),
            **{k: v for k, v in job.items() if k not in ("months", "other")},
            **({"months": job["months"]} if job.get("months") else {}), **({"other": job["other"]} if job.get("other") else {})}


def sheet_fields(parsed: dict[str, Any], revenue: bool = True) -> dict[str, Any]:
    """The sheet's own document: a summary of the table (counts nothing; the figures are on the row documents)."""
    jobs = parsed["jobs"]
    totals = {k: round(sum(j[k] for j in jobs if j.get(k) is not None), 2) for k in FIGURE_KEYS if any(j.get(k) is not None for j in jobs)}
    bits = [f"{parsed['sheet']}: {len(jobs)} job rows"]
    if parsed["has_turnover"]:
        figs = sum(len(turnover_rows(parsed, j)) for j in jobs)
        total = round(sum(r["net"] for j in jobs for r in turnover_rows(parsed, j)), 2)
        bits.append(f"{figs} turnover figures totalling {_eur(total)}" + ("" if revenue else " (not counted: the Job summary sheets layout is selected)"))
    bits.extend(f"{LABELS[k].lower()} {_eur(v)} in all" for k, v in totals.items() if k != "turnover")
    return {
        "doc_type": "other", "direction": "none", "payment_status": "not_applicable", "counterparty": None, "counterparty_role": None,
        "document_number": None, "issue_date": f"{parsed['year']}-01-01" if parsed.get("year") else None, "project": None, "total": None,
        "summary": "; ".join(bits) + ".", "confidence": 1.0, "flags": ["job_table"],
        "raw": {"from": "turnover table", "sheet": parsed["sheet"], "year": parsed.get("year"), "headers": parsed["headers"], "jobs": len(jobs), "totals": totals},
    }


def job_fields(parsed: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    """A row as the job's record: contract value, valued to date, paid, due, retention (counts nothing itself)."""
    bits = [f"Job {job['job_no']} {job.get('project') or ''}".strip() + (f" for {job['client']}" if job.get("client") else "")]
    figs = [f"{LABELS[k].lower()} {_eur(job[k])}" for k in FIGURE_KEYS if job.get(k) is not None]
    if figs:
        bits.append(", ".join(figs))
    if job.get("note"):
        bits.append(job["note"])
    return {
        "doc_type": "other", "direction": "none", "payment_status": "not_applicable", "counterparty": job.get("client") or job.get("project"),
        "counterparty_role": "customer", "document_number": f"Job {job['job_no']}", "issue_date": None, "project": job_name(job),
        "total": job.get("contract_value"), "summary": "; ".join(bits) + f" (from the {parsed['sheet']} sheet).",
        "confidence": 1.0, "flags": ["job_table"], "raw": _raw(parsed, job),
    }


def turnover_rows(parsed: dict[str, Any], job: dict[str, Any]) -> list[dict[str, Any]]:
    """The revenue figures a year-sheet row carries: one per month column with an amount, else the turnover column.
    Each is (column key, label, amount, date): a year's figure is dated 1 January of that year, a month's the 1st of
    the month, so the period filters place them in the right year / quarter / month."""
    year = parsed.get("year")
    out = []
    if job.get("months"):
        for key, amount in job["months"].items():
            if amount:
                y, _, mm = key.rpartition("-")
                y = y or (str(year) if year else "")
                out.append({"key": f"month:{key}", "label": month_label(key, year), "net": amount, "date": f"{y}-{mm}-01" if y else None})
        return out
    if job.get("turnover"):
        out.append({"key": "turnover", "label": f"Turnover {year}" if year else "Turnover", "net": job["turnover"],
                    "date": f"{year}-01-01" if year else None})
    return out


def revenue_fields(parsed: dict[str, Any], job: dict[str, Any], rev: dict[str, Any]) -> dict[str, Any]:
    """One turnover figure as a sales invoice: net amount excl VAT (the table carries no VAT), the client as the
    customer, the job as the project. Not an invoice document, so it never counts as money owed."""
    return {
        "doc_type": "sales_invoice", "direction": "money_in", "counterparty": job.get("client") or job.get("project"),
        "counterparty_role": "customer", "document_number": f"{rev['label']} · job {job['job_no']}", "issue_date": rev["date"],
        "project": job_name(job), "subtotal": rev["net"], "vat_amount": None, "total": rev["net"], "amount_paid": None,
        "payment_status": "not_applicable", "category": None, "confidence": 1.0, "flags": ["turnover_table", "amount_excl_vat_only"],
        "summary": f"{rev['label']} on {job_name(job)}: {_eur(rev['net'])} excl VAT (from the {parsed['sheet']} sheet"
                   + (f"; work in hand {_eur(job['wih'])}" if job.get("wih") is not None else "") + ").",
        "raw": {**_raw(parsed, job), "column": rev["key"], "amount": rev["net"]},
    }
