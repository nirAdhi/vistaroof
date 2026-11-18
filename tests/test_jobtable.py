"""Vista's turnover workbook (one row per job: a "Turnover and WIH <year>" sheet per year and a "Retention" sheet)
is read without a model, and the Admin "Workbook layout" option says which layout carries the revenue."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import openpyxl

from app import db, jobtable, sync
from conftest import wait_sync
from test_jobsheet import BAKERS, _job_sheet

RETENTION_HEAD = ["Job Nr", "Project", "Client", "Total Contract Value", "Valuations to Date", "Paid to Date", "Current Due", "Overdue", "Retention Due"]
RETENTION = [
    [101, "Bakers Yard", "Townlink", 42988, 70000, 70000, 0, None, 0],
    ["102.A", "Foxrock", "Materials supplied", 6411.38, 6411.38, 6411.38, 0, None, None, "invoice includes 23% vat"],
    [108, "Alexandra College", "Clancy Construction", 248691, 256139.4, 256149.1, 0, None, 0, "5% retention"],
    ["118-2", "Allenwood", "Derreen Construction", 14892, 14892, 14892, 0, None, 0, "0% retention"],
    [150, "Glenveagh", "Citywest", 1864322, 1864322, 1864322, 0, None, 0, "5% retention"],
    ["150.1", "Glenveagh", "Citywest - windows", 6940, 6940, 0, 0, None, 0, "0% retention", "€6940 written off"],
]


def _year_sheet(ws, rows: list[list]) -> None:
    ws.append(["Job Nr", "Project", "Client", "Contract Value", "Turnover", "WIH", "Notes"])
    ws.append([])
    for r in rows:
        ws.append(r)
    ws.append(["Total", None, None, None, sum(r[4] or 0 for r in rows)])


def _month_sheet(ws, rows: list[list]) -> None:
    ws.append(["Job Nr", "Project", "Client", "Jan", "Feb", "Mar 26", "Total"])
    for r in rows:
        ws.append(r)


def _turnover_book(path: Path) -> None:
    wb = openpyxl.Workbook(); wb.remove(wb.active)
    _year_sheet(wb.create_sheet("Turnover and WIH 2020-21"), [[101, "Bakers Yard", "Townlink", 42988, 12000, 30988]])
    _year_sheet(wb.create_sheet("Turnover and WIH 2025"), [[101, "Bakers Yard", "Townlink", 42988, 20000, 10988, "final"],
                                                          [150, "Glenveagh", "Citywest", 1864322, 900000, 964322]])
    _month_sheet(wb.create_sheet("Turnover and WIH 2026"), [[150, "Glenveagh", "Citywest", 100000, 0, 250000.5, 350000.5],
                                                            [273, "THF072", "Sisk Living", None, None, None, None]])
    ws = wb.create_sheet("Retention"); ws.append(RETENTION_HEAD); ws.append([])
    for r in RETENTION:
        ws.append(r)
    wb.save(path)


def _touch(path: Path) -> None:
    st = os.stat(path); os.utime(path, (st.st_atime + 100, st.st_mtime + 100))


def test_the_table_is_parsed_by_its_header_row():
    text = "\n".join([" | ".join(RETENTION_HEAD), " |  | ", " | ".join("" if v is None else str(v) for v in RETENTION[2]),
                      "Total |  |  | 2184244.38"])
    p = jobtable.parse("Retention", text)
    assert p and p["year"] is None and not p["has_turnover"] and len(p["jobs"]) == 1
    j = p["jobs"][0]
    assert j["job_no"] == "108" and j["project"] == "Alexandra College" and j["client"] == "Clancy Construction" and j["note"] == "5% retention"
    assert (j["contract_value"], j["valuations_to_date"], j["paid_to_date"], j["current_due"], j["overdue"], j["retention_due"]) == (248691, 256139.4, 256149.1, 0, None, 0)
    f = jobtable.job_fields(p, j)
    assert f["doc_type"] == "other" and f["counterparty"] == "Clancy Construction" and f["project"] == "Alexandra College (job 108)" and f["total"] == 248691
    assert "paid to date €256,149.10" in f["summary"] and "5% retention" in f["summary"]
    assert jobtable.sheet_year("Turnover and WIH 2020-21") == 2021 and jobtable.sheet_year("Turnover and WIH 2027") == 2027 and jobtable.sheet_year("Retention") is None
    assert not jobtable.is_job_table("JOB SUMMARY\nSITE: | JOB NO:") and not jobtable.is_job_table("Date | Description | Amount\n1/1/2026 | x | 5")
    p = jobtable.parse("Turnover and WIH 2026", "Job Nr | Project | Client | Jan | Feb 26 | Mar-26 | Total\n101 | Bakers Yard | Townlink | 100 | 0 | 250.5 | 350.5")
    assert [(r["label"], r["net"], r["date"]) for r in jobtable.turnover_rows(p, p["jobs"][0])] == [("Jan 2026", 100, "2026-01-01"), ("Mar 2026", 250.5, "2026-03-01")]
    assert p["jobs"][0]["other"] == {"Total": 350.5}


def test_turnover_tables_become_revenue_and_job_records_without_a_model(admin, fresh_db, fake_claude, tmp_path):
    folder = tmp_path / "Turnover"; folder.mkdir()
    _turnover_book(folder / "Vista Turnover.xlsx")
    src = admin.post("/api/admin/sources", json={"kind": "local", "name": "VistaTurnover", "local_folder": str(folder)}).json()
    assert src["workbook_layout"] in (None, "auto")
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    assert fake_claude.calls == []  # nothing went to a model
    docs = {d["name"]: d for d in admin.get("/api/documents?limit=200&from=2000-01-01").json()}
    book = docs["Vista Turnover.xlsx"]
    assert book["status"] == "split" and "4 turnover tables with 11 job rows and 5 turnover figures" in book["error"]
    # the Retention sheet: a job record per row, the Client as the customer, the figures in the summary
    rec = docs["Vista Turnover.xlsx › Retention › 108 Alexandra College"]
    assert rec["status"] == "done" and rec["model"] == "jobtable" and rec["doc_type"] == "other" and rec["treatment"] == "none"
    assert rec["counterparty"] == "Clancy Construction" and rec["project"] == "Alexandra College (job 108)" and rec["total"] == 248691
    raw = admin.get(f"/api/documents/{rec['id']}").json()["raw"]
    assert "valued to date €256,139.40" in rec["summary"] and raw["retention_due"] == 0 and raw["note"] == "5% retention"
    assert "invoice includes 23% vat" in docs["Vista Turnover.xlsx › Retention › 102.A Foxrock"]["summary"]
    sheet = docs["Vista Turnover.xlsx › Retention"]
    assert sheet["doc_type"] == "other" and "6 job rows" in sheet["summary"] and "contract value €2,184,244.38 in all" in sheet["summary"]
    # a year sheet: each turnover figure is a sales invoice dated in that year, never money owed
    t25 = docs["Vista Turnover.xlsx › Turnover and WIH 2025 › 150 Glenveagh › Turnover 2025"]
    assert t25["doc_type"] == "sales_invoice" and t25["treatment"] == "revenue" and t25["net_amount"] == 900000 and t25["total"] == 900000
    assert t25["issue_date"] == "2025-01-01" and t25["counterparty"] == "Citywest" and t25["project"] == "Glenveagh (job 150)"
    assert t25["payment_status"] == "not_applicable" and "turnover_table" in t25["flags"] and "work in hand €964,322.00" in t25["summary"]
    assert docs["Vista Turnover.xlsx › Turnover and WIH 2020-21 › 101 Bakers Yard › Turnover 2021"]["issue_date"] == "2021-01-01"
    assert docs["Vista Turnover.xlsx › Turnover and WIH 2026 › 150 Glenveagh › Mar 2026"]["net_amount"] == 250000.5
    assert "Vista Turnover.xlsx › Turnover and WIH 2026 › 150 Glenveagh › Feb 2026" not in docs  # 0 = nothing that month
    assert docs["Vista Turnover.xlsx › Turnover and WIH 2026 › 273 THF072"]["doc_type"] == "other"  # nothing invoiced yet: a job record
    s = admin.get("/api/summary?from=2026-01-01&to=2026-12-31").json()
    assert s["totals"]["revenue"] == 350000.5 and s["totals"]["receivables"] == 0
    assert {c["name"]: c["amount"] for c in s["top_customers"]} == {"Citywest": 350000.5}
    assert admin.get("/api/summary?from=2025-01-01&to=2025-12-31").json()["totals"]["revenue"] == 920000
    jobs = {p["name"]: p for p in admin.get("/api/summary?from=2000-01-01&to=2030-12-31").json()["projects"]}
    assert jobs["Bakers Yard (job 101)"]["revenue"] == 32000 and jobs["Glenveagh (job 150)"]["revenue"] == 1250000.5
    assert any(c["name"] == "Clancy Construction" for c in admin.get("/api/companies").json())
    # previews: a row document opens its sheet as a table with that row highlighted
    r = admin.get(f"/api/documents/{rec['id']}/preview")
    assert r.status_code == 200 and "<h1>Retention</h1>" in r.text and "<tr class=focus><td>108</td><td>Alexandra College</td><td>Clancy Construction</td>" in r.text
    assert "<th class=n>Retention due</th>" in r.text and "<script" not in r.text
    assert "<tr class=focus>" not in admin.get(f"/api/documents/{sheet['id']}/preview").text
    assert "Sheet: Retention" in admin.get(f"/api/documents/{book['id']}/preview").text
    # re-read and hand edits on a row
    assert admin.post(f"/api/documents/{t25['id']}/reprocess").json()["net_amount"] == 900000
    assert admin.patch(f"/api/documents/{t25['id']}", json={"total": 901000}).status_code == 200
    admin.post(f"/api/admin/sources/{src['id']}/sync?force=1"); wait_sync()
    docs = {d["name"]: d for d in admin.get("/api/documents?limit=200&from=2000-01-01").json()}
    assert docs["Vista Turnover.xlsx › Turnover and WIH 2025 › 150 Glenveagh › Turnover 2025"]["total"] == 901000
    # a job removed from a sheet disappears from the ledger
    wb = openpyxl.load_workbook(folder / "Vista Turnover.xlsx"); wb["Retention"].delete_rows(8, 1); wb.save(folder / "Vista Turnover.xlsx")
    _touch(folder / "Vista Turnover.xlsx")
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    names = {d["name"] for d in admin.get("/api/documents?limit=200&from=2000-01-01").json()}
    assert "Vista Turnover.xlsx › Retention › 150.1 Glenveagh" not in names and "Vista Turnover.xlsx › Retention › 150 Glenveagh" in names
    assert sync.status()["phase"] == "done"


def test_the_workbook_layout_option_says_which_layout_counts(admin, fresh_db, fake_claude, tmp_path):
    folder = tmp_path / "Turnover"; folder.mkdir()
    _turnover_book(folder / "Vista Turnover.xlsx")
    wb = openpyxl.Workbook(); wb.remove(wb.active)
    _job_sheet(wb.create_sheet("101"), "Bakers Yard", "101", BAKERS)
    _job_sheet(wb.create_sheet("102"), "Oak Drive", "102", [("Invoice 1", 12000, datetime(2026, 2, 3), "Y")])
    wb.save(folder / "Vista Job Summary.xlsx")
    src = admin.post("/api/admin/sources", json={"kind": "local", "name": "VistaTurnover", "local_folder": str(folder)}).json()
    admin.post(f"/api/admin/sources/{src['id']}/sync"); wait_sync()
    both = admin.get("/api/summary?from=2026-01-01&to=2026-12-31").json()["totals"]["revenue"]
    assert both == 12000 + 350000.5  # auto: both layouts count (the old file and the new one are both in the folder)

    assert admin.patch(f"/api/admin/sources/{src['id']}", json={"workbook_layout": "nonsense"}).status_code == 400
    r = admin.patch(f"/api/admin/sources/{src['id']}", json={"workbook_layout": "jobtable"})
    assert r.status_code == 200 and r.json()["workbook_layout"] == "jobtable"
    wait_sync()  # the change queued the workbooks and started a sync of this source
    docs = {d["name"]: d for d in admin.get("/api/documents?limit=200&from=2000-01-01").json()}
    assert "Vista Job Summary.xlsx › 102 › Invoice 1" not in docs  # job sheets no longer give invoice rows…
    assert docs["Vista Job Summary.xlsx › 102"]["project"] == "Oak Drive (job 102)"  # …but still the job record
    assert "(not counted: the Turnover tables layout is selected)" in docs["Vista Job Summary.xlsx"]["error"]
    assert admin.get("/api/summary?from=2026-01-01&to=2026-12-31").json()["totals"]["revenue"] == 350000.5
    assert fake_claude.calls == []

    admin.patch(f"/api/admin/sources/{src['id']}", json={"workbook_layout": "jobsheets"}); wait_sync()
    docs = {d["name"]: d for d in admin.get("/api/documents?limit=200&from=2000-01-01").json()}
    assert docs["Vista Job Summary.xlsx › 102 › Invoice 1"]["net_amount"] == 12000
    assert "Vista Turnover.xlsx › Turnover and WIH 2026 › 150 Glenveagh › Mar 2026" not in docs
    assert docs["Vista Turnover.xlsx › Turnover and WIH 2026 › 150 Glenveagh"]["doc_type"] == "other"  # the row stays as a job record
    assert docs["Vista Turnover.xlsx › Retention › 108 Alexandra College"]["doc_type"] == "other"
    assert "figures not counted: the Job summary sheets layout is selected" in docs["Vista Turnover.xlsx"]["error"]
    assert admin.get("/api/summary?from=2026-01-01&to=2026-12-31").json()["totals"]["revenue"] == 12000
    assert db.get_source(src["id"])["workbook_layout"] == "jobsheets"
    assert admin.get("/api/admin/overview").json()["sources"][0]["workbook_layout"] == "jobsheets"

    admin.patch(f"/api/admin/sources/{src['id']}", json={"workbook_layout": "auto"}); wait_sync()
    assert admin.get("/api/summary?from=2026-01-01&to=2026-12-31").json()["totals"]["revenue"] == both
