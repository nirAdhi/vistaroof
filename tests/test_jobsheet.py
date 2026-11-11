"""Cormac's JOB SUMMARY sheets (one per job in the Vista Job Summary workbook) are read without a model: every
invoice column is a sales invoice in the ledger, the sheet itself a job record."""
from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path

import openpyxl

from app import jobsheet, sync, textract
from app.sources import FileContent
from conftest import wait_sync

XL = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _job_sheet(ws, site: str, no: str, invoices: list[tuple[str, float | None, datetime | None, str]]) -> None:
    """The real layout (labels in column A, one invoice per column from B); ``invoices`` = (header, net, date, paid)."""
    ws.append(["JOB SUMMARY"])
    ws.append([])
    ws.append(["SITE:", None, None, "JOB NO:"]); ws.append([site, None, None, no])
    ws.append([]); ws.append(["PROJECT MANAGER", "Rares"]); ws.append([])
    ws.append(["CONTRACT VALUE/SUM"]); ws.append([None, "VALUE", "COST", "MARK UP", "GROSS PROFIT%"]); ws.append([])
    ws.append(["LABOUR / MATERIAL", 0, None, 0]); ws.append([])
    ws.append(["TOTAL", 0, 0, 0, 0]); ws.append(["Retention", 0]); ws.append(["BALANCE DUE", -70000]); ws.append(["Total", -70000])
    ws.append(["VALUATION"]); ws.append([])
    ws.append([None, "VALUATION 1", "VALUATIONS 2", "VALUATION 3", "Final Agreed", "Draft Final", "Final"]); ws.append([])
    ws.append(["Works to date", 451, 7010, 11805.6, 70000]); ws.append(["increase", 451, 6559, 4795.6, 58194.4, None, 70000])
    ws.append(["Less : Retention 0%", 0, 0, 0, 0]); ws.append([])
    ws.append([None, 451, 7010, 11805.6, 70000]); ws.append(["increase", 451, 6559, 4795.6, 58194.4])
    ws.append(["TOTAL", 902, 7010, 11805.6, 70000])
    ws.append([None, datetime(2020, 11, 27), datetime(2020, 12, 22), datetime(2021, 4, 30), datetime(2022, 6, 27)])
    ws.append([]); ws.append(["Retention  5% Exclu Vat"]); ws.append([])
    ws.append(["INVOICED/PAYMENT"])
    ws.append([None] + [h for h, _, _, _ in invoices] + ["Total", "Gross Due", "Retention Due"])
    ws.append(["INVOICE NO"]); ws.append(["Cert Payment"])
    ws.append(["INVOICE AMOUNT Excl VAT"] + [n for _, n, _, _ in invoices] + [sum(n or 0 for _, n, _, _ in invoices), 0, 0])
    ws.append(["INVOICE AMOUNT"]); ws.append([])
    ws.append(["DATE"] + [d for _, _, d, _ in invoices])
    ws.append([]); ws.append(["PAID  N/Y"] + [p for _, _, _, p in invoices])
    ws.append(["** paid less 20% rct tax"]); ws.append(["DATE", "€5327.60 nett after 20% tax deducted"])


BAKERS = [("Invoice 1", None, None, ""), ("Invoice 2", 6659.5, datetime(2021, 3, 16), "**Y"), ("Invoice 3", None, None, ""),
          ("Invoice 4", 5767.07, datetime(2021, 6, 24), "Y"), ("", 19000, datetime(2022, 5, 4), "Y"), ("Invoice 9", 350, datetime(2025, 4, 7), "N")]


def test_the_sheet_text_is_parsed_exactly():
    wb = openpyxl.Workbook(); _job_sheet(wb.active, "Bakers Yard", "101", BAKERS); wb.active.title = "101"
    buf = io.BytesIO(); wb.save(buf)
    (name, text), = textract.workbook_sheets(FileContent(mime=XL, data=buf.getvalue()))
    assert name == "101" and jobsheet.is_job_summary(text)
    p = jobsheet.parse(text)
    assert p["site"] == "Bakers Yard" and p["job_no"] == "101" and p["project_manager"] == "Rares"
    assert p["contract"] == {"value": 0, "cost": 0, "mark_up": 0, "gross_profit_pct": 0} and p["balance_due"] == -70000 and p["retention"] == 0
    assert [(v["label"], v["works_to_date"], v["date"]) for v in p["valuations"]] == [
        ("VALUATION 1", 451, "2020-11-27"), ("VALUATIONS 2", 7010, "2020-12-22"), ("VALUATION 3", 11805.6, "2021-04-30"), ("Final Agreed", 70000, "2022-06-27")]
    assert p["final_agreed"] == 70000
    inv = {i["label"]: i for i in p["invoices"]}
    assert set(inv) == {"Invoice 2", "Invoice 4", "Invoice (column F)", "Invoice 9"}  # empty columns are not invoices; an unlabelled one is named by its column
    assert inv["Invoice 2"]["net"] == 6659.5 and inv["Invoice 2"]["date"] == "2021-03-16" and inv["Invoice 2"]["payment_status"] == "paid" and inv["Invoice 2"]["rct_deducted"]
    assert inv["Invoice (column F)"]["net"] == 19000 and inv["Invoice 9"]["payment_status"] == "unpaid" and not inv["Invoice 4"]["rct_deducted"]
    assert p["invoiced_total"] == 31776.57
    # the pasted / typed form of the same sheet (m/d/yyyy dates, € and thousands separators) parses the same way
    typed = ("JOB SUMMARY\n\nSITE: |  | JOB NO:\nBakers Yard |  | 101\nINVOICED/PAYMENT\n | Invoice 1 | Invoice 2 | Total\n"
             "INVOICE AMOUNT Excl VAT |  | 6,659.50 | €6,659.50\nDATE |  | 3/16/2021\nPAID  N/Y |  | **Y")
    q = jobsheet.parse(typed)
    assert q["invoices"][0]["date"] == "2021-03-16" and q["invoices"][0]["net"] == 6659.5 and q["invoiced_total"] == 6659.5
    assert jobsheet.parse("Some other sheet\n1 | 2") is None


def test_a_sheet_invoice_that_doubles_an_invoice_document_is_counted_once(admin, fresh_db, fake_claude, tmp_path):
    """The Oak Drive job sheet lists Invoice 1 for €5,000 on 1 Mar 2026; the invoice PDF for the same job
    (INV-9, €5,000 net, 10 Mar 2026) is in the Invoices library too. Revenue must count €5,000 once."""
    import os

    folder = tmp_path / "Docs"; folder.mkdir()
    (folder / "INV-9 Murphy.pdf").write_bytes(b"%PDF-1.4 fake invoice")  # fake Claude: sales invoice, net 5000, 2026-03-10, project Oak Drive
    wb = openpyxl.Workbook(); wb.remove(wb.active)
    _job_sheet(wb.create_sheet("102"), "Oak Drive", "102", [("Invoice 1", 5000, datetime(2026, 3, 1), "N"), ("Invoice 2", 8000, datetime(2026, 4, 9), "N")])
    _job_sheet(wb.create_sheet("103"), "Mill Lane", "103", [("Invoice 1", 5000, datetime(2023, 1, 5), "Y")])  # same amount, years apart, other site: not a match
    _job_sheet(wb.create_sheet("104"), "Quay St", "104", [])
    wb.save(folder / "Vista Job Summary.xlsx")
    src = admin.post("/api/admin/sources", json={"kind": "local", "name": "Docs", "local_folder": str(folder)}).json()
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    docs = {d["name"]: d for d in admin.get("/api/documents?limit=100&from=2000-01-01").json()}
    pdf, inv1, inv2 = docs["INV-9 Murphy.pdf"], docs["Vista Job Summary.xlsx › 102 › Invoice 1"], docs["Vista Job Summary.xlsx › 102 › Invoice 2"]
    assert inv1["duplicate_of"] == pdf["id"] and inv1["duplicate_name"] == "INV-9 Murphy.pdf"
    assert inv2["duplicate_of"] is None and pdf["duplicate_of"] is None and docs["Vista Job Summary.xlsx › 103 › Invoice 1"]["duplicate_of"] is None
    s = admin.get("/api/summary?from=2026-01-01&to=2026-12-31").json()
    assert s["totals"]["revenue"] == 13000 and s["totals"]["duplicates_counted_once"] == 1  # 5000 (PDF) + 8000, not 18000
    assert s["totals"]["receivables"] == 5675 + 8000  # the PDF's gross outstanding + the sheet's second invoice
    comp = {c["name"]: c for c in admin.get("/api/companies").json()}
    assert comp["Murphy Homes"]["revenue"] == 5000 and comp["Oak Drive"]["revenue"] == 8000 and comp["Mill Lane"]["revenue"] == 5000
    # the PDF's amount is corrected by hand → no longer the same invoice → the sheet row counts again
    assert admin.patch(f"/api/documents/{pdf['id']}", json={"subtotal": 4000, "total": 4540}).status_code == 200
    assert admin.get(f"/api/documents/{inv1['id']}").json()["duplicate_of"] is None
    assert admin.get("/api/summary?from=2026-01-01&to=2026-12-31").json()["totals"]["revenue"] == 4000 + 5000 + 8000
    assert admin.patch(f"/api/documents/{pdf['id']}", json={"subtotal": 5000, "total": 5675}).status_code == 200
    assert admin.get(f"/api/documents/{inv1['id']}").json()["duplicate_of"] == pdf["id"]
    # the PDF leaves the library → the sheet row is the only record again
    os.remove(folder / "INV-9 Murphy.pdf")
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    assert admin.get(f"/api/documents/{inv1['id']}").json()["duplicate_of"] is None
    assert admin.get("/api/summary?from=2026-01-01&to=2026-12-31").json()["totals"]["revenue"] == 13000


def test_job_sheets_become_sales_invoices_in_the_ledger_without_a_model(admin, fresh_db, fake_claude, tmp_path):
    folder = tmp_path / "Turnover"; folder.mkdir()
    wb = openpyxl.Workbook(); wb.remove(wb.active)
    _job_sheet(wb.create_sheet("101"), "Bakers Yard", "101", BAKERS)
    _job_sheet(wb.create_sheet("102"), "Oak Drive", "102", [("Invoice 1", 12000, datetime(2026, 2, 3), "Y"), ("Invoice 2", 8000, datetime(2026, 3, 9), "N")])
    _job_sheet(wb.create_sheet("103"), "Mill Lane", "103", [])
    wb.save(folder / "Vista Job Summary.xlsx")
    src = admin.post("/api/admin/sources", json={"kind": "local", "name": "VistaTurnover", "local_folder": str(folder)}).json()
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    assert fake_claude.calls == []  # nothing went to a model
    docs = {d["name"]: d for d in admin.get("/api/documents?limit=100&from=2000-01-01").json()}
    book = docs["Vista Job Summary.xlsx"]
    assert book["status"] == "split"
    job = docs["Vista Job Summary.xlsx › 101"]
    assert job["status"] == "done" and job["model"] == "jobsheet" and job["doc_type"] == "other" and job["treatment"] == "none"
    assert job["project"] == "Bakers Yard (job 101)" and job["total"] == 70000 and job["issue_date"] == "2025-04-07"
    assert "final agreed €70,000.00" in job["summary"] and "invoiced €31,776.57 excl VAT over 4 invoices (€31,426.57 paid)" in job["summary"]
    inv2 = docs["Vista Job Summary.xlsx › 101 › Invoice 2"]
    assert inv2["doc_type"] == "sales_invoice" and inv2["treatment"] == "revenue" and inv2["net_amount"] == 6659.5 and inv2["total"] == 6659.5
    assert inv2["issue_date"] == "2021-03-16" and inv2["payment_status"] == "paid" and inv2["amount_paid"] == 6659.5 and inv2["project"] == "Bakers Yard (job 101)"
    assert "rct_tax_deducted" in inv2["flags"] and inv2["model"] == "jobsheet" and inv2["folder"] == book["folder"]
    assert inv2["counterparty"] == "Bakers Yard" and job["counterparty"] == "Bakers Yard"  # the site is the customer
    inv9 = docs["Vista Job Summary.xlsx › 101 › Invoice 9"]
    assert inv9["payment_status"] == "unpaid" and inv9["amount_paid"] is None
    assert docs["Vista Job Summary.xlsx › 102 › Invoice 1"]["net_amount"] == 12000
    assert "Vista Job Summary.xlsx › 103 › Invoice 1" not in docs  # a job with nothing invoiced yet has no invoice rows
    # company revenue: the invoices count as sales by date and by job
    s = admin.get("/api/summary?from=2026-01-01&to=2026-12-31").json()
    assert s["totals"]["revenue"] == 20000
    jobs = {p["name"]: p for p in s["projects"]}
    assert jobs["Oak Drive (job 102)"]["revenue"] == 20000
    assert {c["name"]: c["amount"] for c in s["top_customers"]} == {"Oak Drive": 20000}
    assert any(c["name"] == "Bakers Yard" and c["revenue"] == 31776.57 for c in admin.get("/api/companies").json())
    assert admin.get("/api/summary?from=2021-01-01&to=2021-12-31").json()["totals"]["revenue"] == 6659.5 + 5767.07
    # the invoice row previews as a structured page of its sheet with that invoice highlighted; the job row too;
    # the workbook row shows every sheet; numbers are shown as money, not binary floats
    r = admin.get(f"/api/documents/{inv2['id']}/preview")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "<h1>Bakers Yard (job 101)</h1>" in r.text and "<tr class=focus><td>Invoice 2</td><td>2021-03-16</td><td class=n>6,659.50</td>" in r.text
    assert "Paid (less 20% RCT)" in r.text and "<h2>Valuations</h2>" in r.text and "70,000.00" in r.text and "JOB SUMMARY" in r.text
    assert "<script" not in r.text and "&lt;" not in r.text
    assert "<tr class=focus>" not in admin.get(f"/api/documents/{job['id']}/preview").text
    whole = admin.get(f"/api/documents/{book['id']}/preview").text
    assert "Sheet: 101" in whole and "Sheet: 102" in whole and "Oak Drive (job 102)" in whole
    assert admin.post(f"/api/documents/{inv9['id']}/reprocess").json()["payment_status"] == "unpaid"
    assert admin.patch(f"/api/documents/{inv9['id']}", json={"payment_status": "paid"}).status_code == 200
    admin.post(f"/api/admin/sources/{src['id']}/sync?force=1"); wait_sync()
    docs = {d["name"]: d for d in admin.get("/api/documents?limit=100&from=2000-01-01").json()}
    assert docs["Vista Job Summary.xlsx › 101 › Invoice 9"]["payment_status"] == "paid" and docs["Vista Job Summary.xlsx › 101 › Invoice 9"]["overridden"]
    assert sync.status()["phase"] == "done"
    # an invoice column deleted from the sheet disappears from the ledger
    import os
    wb = openpyxl.Workbook(); wb.remove(wb.active)
    _job_sheet(wb.create_sheet("101"), "Bakers Yard", "101", BAKERS[:4])
    _job_sheet(wb.create_sheet("102"), "Oak Drive", "102", [("Invoice 1", 12000, datetime(2026, 2, 3), "Y")])
    _job_sheet(wb.create_sheet("103"), "Mill Lane", "103", [])
    wb.save(folder / "Vista Job Summary.xlsx")
    st = os.stat(folder / "Vista Job Summary.xlsx"); os.utime(folder / "Vista Job Summary.xlsx", (st.st_atime + 100, st.st_mtime + 100))
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    names = {d["name"] for d in admin.get("/api/documents?limit=100&from=2000-01-01").json()}
    assert "Vista Job Summary.xlsx › 101 › Invoice 9" not in names and "Vista Job Summary.xlsx › 102 › Invoice 2" not in names
    assert "Vista Job Summary.xlsx › 101 › Invoice 4" in names
