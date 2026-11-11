"""Spreadsheet previews as HTML: a grid per sheet instead of a pipe-separated text dump, and for a Vista
JOB SUMMARY sheet (app/jobsheet.py) a structured view first — the job header, the contract figures, the
valuations and the invoices as proper tables — with the row the owner opened highlighted.

Cell text comes from the owner's files and is escaped; the page has no scripts (the app's CSP allows
inline styles only). ``render`` returns the page as a string.
"""
from __future__ import annotations

import html
from typing import Any

from . import jobsheet

_CSS = """
body{margin:0;padding:14px 16px 24px;background:#fffefb;color:#0b0b0b;font:13px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
h1{font-size:17px;margin:0 0 2px}h2{font-size:14px;margin:18px 0 6px}
.sub{color:#86827a;font-size:12px;margin-bottom:12px}
table{border-collapse:collapse;font-size:12.5px;margin:4px 0 10px;max-width:100%}
th,td{border-bottom:1px solid #e4e0d5;padding:4px 8px;text-align:left;vertical-align:top;white-space:nowrap}
th{color:#86827a;font-weight:500;font-size:11.5px}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
tr.focus td{background:#fbf1d2}
td.focus,th.focus{background:#fbf1d2}
.wrap{overflow:auto;max-width:100%}
.grid td,.grid th{font-size:12px;padding:3px 7px}
.grid th.col{text-align:center;color:#b5b0a4;font-weight:400}
.grid td.rown{color:#b5b0a4;text-align:right;font-size:11px}
.kv{display:grid;grid-template-columns:max-content 1fr;gap:3px 14px;margin:6px 0 10px}
.kv dt{color:#86827a}.kv dd{margin:0}
.tag{display:inline-block;padding:0 7px;border-radius:999px;font-size:11px;background:#f1eee6;color:#52514e}
.tag.paid{background:#d9f2d9;color:#006300}.tag.unpaid{background:#fde9c9;color:#7a4b00}
.nav a{margin-right:10px;color:#8a6a14}
details{margin-top:10px}summary{cursor:pointer;color:#52514e}
"""


def _e(s: Any) -> str:
    return html.escape("" if s is None else str(s))


def _money(v: float | None) -> str:
    return "" if v is None else f"{v:,.2f}"


def _is_num(s: str) -> bool:
    return jobsheet._num(s) is not None


def _grid(text: str, focus_col: int | None = None) -> str:
    rows = [ln.split(jobsheet.SEP) for ln in text.split("\n")]
    width = max((len(r) for r in rows), default=0)
    head = "".join(f'<th class="col{" focus" if focus_col == i else ""}">{jobsheet._col_letter(i)}</th>' for i in range(width))
    body = []
    for n, r in enumerate(rows, 1):
        cells = []
        for i in range(width):
            v = r[i] if i < len(r) else ""
            cls = ("n" if _is_num(v) else "") + (" focus" if focus_col == i else "")
            cells.append(f'<td class="{cls.strip()}">{_e(v)}</td>')
        body.append(f'<tr><td class="rown">{n}</td>{"".join(cells)}</tr>')
    return f'<div class="wrap"><table class="grid"><thead><tr><th></th>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def _job_block(parsed: dict[str, Any], focus_inv: str | None) -> str:
    out = [f"<h1>{_e(jobsheet.job_name(parsed))}</h1>"]
    sub = [x for x in (parsed.get("address"), f"project manager {parsed['project_manager']}" if parsed.get("project_manager") else None) if x]
    out.append(f'<div class="sub">{_e(" · ".join(sub))}</div>' if sub else "")
    c = parsed.get("contract") or {}
    kv = [("Contract value", _money(c.get("value"))), ("Cost", _money(c.get("cost"))), ("Mark-up", _money(c.get("mark_up"))),
          ("Gross profit %", "" if c.get("gross_profit_pct") is None else f"{c['gross_profit_pct']:g} %"),
          ("Final agreed", _money(parsed.get("final_agreed"))), ("Retention", _money(parsed.get("retention"))),
          ("Balance due", _money(parsed.get("balance_due"))), ("Invoiced excl VAT", _money(parsed.get("invoiced_total")))]
    out.append('<dl class="kv">' + "".join(f"<dt>{_e(k)}</dt><dd>{_e(v) or '—'}</dd>" for k, v in kv) + "</dl>")
    inv = parsed.get("invoices") or []
    if inv:
        out.append("<h2>Invoices</h2><table><thead><tr><th>Invoice</th><th>Date</th><th class=n>Net excl VAT</th><th>Paid</th></tr></thead><tbody>")
        for i in inv:
            tag = {"paid": "paid", "unpaid": "unpaid"}.get(i["payment_status"], "")
            paid = {"paid": "Paid" + (" (less 20% RCT)" if i.get("rct_deducted") else ""), "unpaid": "Not paid"}.get(i["payment_status"], "Unknown")
            out.append(f'<tr{" class=focus" if focus_inv == i["key"] else ""}><td>{_e(i["label"])}</td><td>{_e(i["date"] or "")}</td>'
                       f'<td class=n>{_money(i["net"])}</td><td><span class="tag {tag}">{_e(paid)}</span></td></tr>')
        out.append(f'<tr><th>Total</th><th></th><th class=n>{_money(parsed.get("invoiced_total"))}</th><th></th></tr></tbody></table>')
    vals = parsed.get("valuations") or []
    if vals:
        out.append("<h2>Valuations</h2><table><thead><tr><th>Valuation</th><th>Date</th><th class=n>Works to date</th><th class=n>Increase</th><th class=n>Retention</th></tr></thead><tbody>")
        for v in vals:
            out.append(f'<tr><td>{_e(v["label"])}</td><td>{_e(v["date"] or "")}</td><td class=n>{_money(v["works_to_date"])}</td>'
                       f'<td class=n>{_money(v.get("increase"))}</td><td class=n>{_money(v.get("retention"))}</td></tr>')
        out.append("</tbody></table>")
    return "".join(out)


def render(sheets: list[tuple[str, str]], title: str, focus_sheet: str | None = None, focus_inv: str | None = None) -> str:
    """The HTML page for a workbook (every sheet) or one sheet (``focus_sheet``), ``focus_inv`` = the column key of
    the invoice row being previewed (highlighted in the invoices table and in the grid)."""
    parts = [f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport content=\"width=device-width\"><title>{_e(title)}</title><style>{_CSS}</style></head><body>"]
    shown = [(n, t) for n, t in sheets if focus_sheet is None or n == focus_sheet]
    if focus_sheet is None and len(shown) > 1:
        parts.append('<div class="nav sub">' + " ".join(f'<a href="#s{i}">{_e(n)}</a>' for i, (n, _) in enumerate(shown)) + "</div>")
    for i, (name, text) in enumerate(shown):
        parsed = jobsheet.parse(text)
        if focus_sheet is None:
            parts.append(f'<h2 id="s{i}">Sheet: {_e(name)}</h2>')
        if parsed:
            focus_col = None
            if focus_inv:
                hit = next((x for x in parsed.get("invoices", []) if x["key"] == focus_inv), None)
                focus_col = next((c for c in range(0, 60) if jobsheet._col_letter(c) == focus_inv), None) if hit else None
            parts.append(_job_block(parsed, focus_inv))
            parts.append(f"<details><summary>The sheet as laid out</summary>{_grid(text, focus_col)}</details>")
        else:
            parts.append(_grid(text))
    if not shown:
        parts.append('<p class="sub">This sheet is no longer in the workbook.</p>')
    parts.append("</body></html>")
    return "".join(parts)
