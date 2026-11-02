"""Seed the database with realistic sample documents so the dashboard can be explored without Drive or an API key.

Rows are generated deterministically (fixed random seed) for the 12 months up to today: sales
invoices with a mix of paid/unpaid/overdue, quotes and proformas, supplier bills and till receipts,
payslips, purchase orders, credit notes, statements. They are stored with ``source='demo'`` and no
``source_ref``; ``previews.get`` renders a sample page for them (render_demo.py). ``clear()`` removes
them without touching real documents. ``random`` here is for sample data, not security.
"""
from __future__ import annotations

import json
import random
from datetime import date, timedelta

from . import config, db
from .extract import derive

CUSTOMERS = [
    ("Kildare County Council", "roof_repair", "Naas Civic Offices"),
    ("Murphy Homes Ltd", "roof_installation", "Ashfield Grove, Phase 2"),
    ("St. Brigid's National School", "flat_roofing", "St. Brigid's NS, Celbridge"),
    ("Lidl Ireland", "roof_maintenance", "Lidl Newbridge"),
    ("Brennan & Daughters Builders", "roof_installation", "Riverside Apartments"),
    ("O'Connor Property Management", "guttering_fascia", "The Mews, Maynooth"),
    ("Sheehan Farm Partners", "cladding", "Grain store, Kilcullen"),
    ("Private client: A. Byrne", "roof_repair", "14 Oak Drive, Leixlip"),
    ("Private client: M. Dunne", "chimney_leadwork", "Rathcoole cottage"),
    ("Greenfield Logistics", "flat_roofing", "Greenfield Depot Unit 4"),
]
SUPPLIERS = [
    ("Chadwicks", "materials_roofing", "supplier_invoice"),
    ("Tegral Building Products", "materials_roofing", "supplier_invoice"),
    ("Brooks Timber & Building Supplies", "materials_general", "supplier_invoice"),
    ("Kildare Scaffolding", "scaffolding_access", "supplier_invoice"),
    ("Hire Direct", "equipment_hire", "supplier_invoice"),
    ("Circle K", "vehicle_fuel", "receipt"),
    ("Screwfix", "tools", "receipt"),
    ("Allianz Ireland", "insurance", "supplier_invoice"),
    ("Thorntons Recycling", "waste_disposal", "supplier_invoice"),
    ("Flynn Roofing Subcontract", "subcontractor", "supplier_invoice"),
    ("Vodafone Business", "software_phone", "supplier_invoice"),
    ("Kavanagh Accountants", "professional_fees", "supplier_invoice"),
    ("Safety Direct", "safety_ppe", "receipt"),
    ("Woodie's", "materials_general", "receipt"),
]
EMPLOYEES = ["J. Walsh", "D. Keane", "P. Nolan", "S. Moore"]


def _folder(doc_type: str, d: date) -> str:
    base = {
        "sales_invoice": "Sales Invoices", "proforma_invoice": "Quotes & Proformas", "quote_estimate": "Quotes & Proformas",
        "supplier_invoice": "Supplier Bills", "receipt": "Receipts", "payslip": "Payroll", "credit_note": "Sales Invoices",
        "purchase_order": "Purchase Orders", "bank_statement": "Bank", "insurance": "Insurance", "tax_document": "Revenue",
    }.get(doc_type, "Misc")
    return f"My Drive/Accounts/{d.year}/{base}"


def load(today: date | None = None, seed: int = 7) -> int:
    rng = random.Random(seed)
    today = today or date.today()
    start = date(today.year - 1, today.month, 1)
    rows: list[dict] = []
    inv_no = 1040
    po_no = 210

    day = start
    while day <= today:
        month_start = day
        # 3-6 sales invoices per month
        for _ in range(rng.randint(3, 6)):
            cust, cat, proj = rng.choice(CUSTOMERS)
            d = month_start + timedelta(days=rng.randint(0, 27))
            if d > today:
                continue
            net = round(rng.choice([1850, 2400, 3600, 4800, 6500, 9200, 12750, 18400, 26500]) * rng.uniform(0.85, 1.15), 2)
            vat = round(net * 0.135, 2)
            due = d + timedelta(days=30)
            age = (today - due).days
            status = "paid" if age > 45 or rng.random() < 0.55 else ("overdue" if age > 0 else "unpaid")
            paid = net + vat if status == "paid" else (round((net + vat) * 0.5, 2) if rng.random() < 0.2 else 0.0)
            if paid and status != "paid":
                status = "partial"
            inv_no += 1
            rows.append(dict(
                name=f"INV-{inv_no} {cust.split(':')[-1].strip()}.pdf", doc_type="sales_invoice", direction="money_in",
                counterparty=cust, counterparty_role="customer", document_number=f"INV-{inv_no}", issue_date=d.isoformat(),
                due_date=due.isoformat(), currency="EUR", subtotal=net, vat_amount=vat, total=round(net + vat, 2),
                amount_paid=paid, payment_status=status, category=cat, project=proj,
                summary=f"Invoice to {cust} for {cat.replace('_', ' ')} at {proj}.", confidence=round(rng.uniform(0.82, 0.99), 2),
                flags=[], line_items=[{"description": f"{cat.replace('_', ' ').title()} works", "quantity": 1, "unit_price": net, "amount": net}],
            ))
        # quotes / proformas
        for _ in range(rng.randint(1, 3)):
            cust, cat, proj = rng.choice(CUSTOMERS)
            d = month_start + timedelta(days=rng.randint(0, 27))
            if d > today:
                continue
            net = round(rng.choice([3200, 5400, 8800, 14000, 22000, 41000]) * rng.uniform(0.9, 1.1), 2)
            dt = rng.choice(["quote_estimate", "proforma_invoice"])
            rows.append(dict(
                name=f"{'Quote' if dt == 'quote_estimate' else 'Proforma'} - {proj}.pdf", doc_type=dt, direction="money_in",
                counterparty=cust, counterparty_role="customer", document_number=f"Q-{rng.randint(300, 999)}",
                issue_date=d.isoformat(), due_date=None, currency="EUR", subtotal=net, vat_amount=round(net * 0.135, 2),
                total=round(net * 1.135, 2), amount_paid=None, payment_status="not_applicable", category=cat, project=proj,
                summary=f"{dt.replace('_', ' ').title()} for {cat.replace('_', ' ')} at {proj}.", confidence=0.9, flags=[], line_items=[],
            ))
        # supplier bills and receipts
        for _ in range(rng.randint(8, 14)):
            sup, cat, dt = rng.choice(SUPPLIERS)
            d = month_start + timedelta(days=rng.randint(0, 27))
            if d > today:
                continue
            base = {"materials_roofing": 2800, "materials_general": 900, "scaffolding_access": 1600, "equipment_hire": 650,
                    "vehicle_fuel": 140, "tools": 220, "insurance": 1450, "waste_disposal": 380, "subcontractor": 3200,
                    "software_phone": 160, "professional_fees": 600, "safety_ppe": 180}[cat]
            net = round(base * rng.uniform(0.5, 1.6), 2)
            vat = round(net * (0.23 if cat not in ("insurance",) else 0.0), 2)
            paid = dt == "receipt" or rng.random() < 0.7
            due = d + timedelta(days=30)
            status = "paid" if paid else ("overdue" if due < today else "unpaid")
            flags = ["poor_scan"] if rng.random() < 0.08 else []
            conf = round(rng.uniform(0.5, 0.75), 2) if flags else round(rng.uniform(0.8, 0.99), 2)
            proj = rng.choice(CUSTOMERS)[2] if cat in ("materials_roofing", "scaffolding_access", "subcontractor") and rng.random() < 0.7 else None
            rows.append(dict(
                name=f"{sup} {d.strftime('%d-%m-%Y')}.{'jpg' if dt == 'receipt' else 'pdf'}", doc_type=dt, direction="money_out",
                counterparty=sup, counterparty_role="supplier", document_number=f"{sup[:3].upper()}-{rng.randint(10000, 99999)}",
                issue_date=d.isoformat(), due_date=None if dt == "receipt" else due.isoformat(), currency="EUR",
                subtotal=net, vat_amount=vat, total=round(net + vat, 2), amount_paid=round(net + vat, 2) if paid else 0.0,
                payment_status=status, category=cat, project=proj,
                summary=f"{'Receipt' if dt == 'receipt' else 'Bill'} from {sup} for {cat.replace('_', ' ')}.", confidence=conf,
                flags=flags, line_items=[],
            ))
        # payroll
        for emp in EMPLOYEES:
            d = month_start + timedelta(days=27)
            if d > today:
                continue
            net = round(rng.choice([2900, 3300, 3650, 4100]) * rng.uniform(0.95, 1.05), 2)
            rows.append(dict(
                name=f"Payslip {emp} {d.strftime('%b %Y')}.pdf", doc_type="payslip", direction="money_out", counterparty=emp,
                counterparty_role="employee", document_number=None, issue_date=d.isoformat(), due_date=None, currency="EUR",
                subtotal=net, vat_amount=0.0, total=net, amount_paid=net, payment_status="paid", category="labour_wages",
                project=None, summary=f"Monthly payslip for {emp}.", confidence=0.97, flags=[], line_items=[],
            ))
        # purchase order + the odd credit note / non-financial doc
        if rng.random() < 0.5:
            sup, cat, _ = rng.choice(SUPPLIERS[:4])
            d = month_start + timedelta(days=rng.randint(0, 27))
            if d <= today:
                po_no += 1
                net = round(rng.uniform(1500, 7000), 2)
                rows.append(dict(
                    name=f"PO-{po_no} {sup}.pdf", doc_type="purchase_order", direction="money_out", counterparty=sup,
                    counterparty_role="supplier", document_number=f"PO-{po_no}", issue_date=d.isoformat(), due_date=None,
                    currency="EUR", subtotal=net, vat_amount=round(net * 0.23, 2), total=round(net * 1.23, 2), amount_paid=None,
                    payment_status="not_applicable", category=cat, project=rng.choice(CUSTOMERS)[2],
                    summary=f"Purchase order to {sup}.", confidence=0.93, flags=[], line_items=[],
                ))
        if rng.random() < 0.25:
            cust, cat, proj = rng.choice(CUSTOMERS)
            d = month_start + timedelta(days=rng.randint(0, 27))
            if d <= today:
                net = round(rng.uniform(300, 1800), 2)
                rows.append(dict(
                    name=f"CN {cust}.pdf", doc_type="credit_note", direction="money_in", counterparty=cust, counterparty_role="customer",
                    document_number=f"CN-{rng.randint(100, 400)}", issue_date=d.isoformat(), due_date=None, currency="EUR",
                    subtotal=net, vat_amount=round(net * 0.135, 2), total=round(net * 1.135, 2), amount_paid=None,
                    payment_status="not_applicable", category=cat, project=proj, summary=f"Credit note issued to {cust}.",
                    confidence=0.9, flags=[], line_items=[],
                ))
        d = month_start + timedelta(days=2)
        if d <= today:
            rows.append(dict(
                name=f"AIB statement {d.strftime('%b %Y')}.pdf", doc_type="bank_statement", direction="none", counterparty="AIB",
                counterparty_role="bank", document_number=None, issue_date=d.isoformat(), due_date=None, currency="EUR",
                subtotal=None, vat_amount=None, total=None, amount_paid=None, payment_status="not_applicable", category=None,
                project=None, summary="Monthly business current account statement.", confidence=0.98, flags=[], line_items=[],
            ))
        day = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)

    rows.append(dict(
        name="Public liability insurance schedule.pdf", doc_type="insurance", direction="none", counterparty="Allianz Ireland",
        counterparty_role="other", document_number="PL-88211", issue_date=date(today.year, 1, 10).isoformat(), due_date=None,
        currency="EUR", subtotal=None, vat_amount=None, total=None, amount_paid=None, payment_status="not_applicable",
        category=None, project=None, summary="Public and employer liability policy schedule.", confidence=0.95, flags=[], line_items=[],
    ))
    rows.append(dict(
        name="RCT notification.pdf", doc_type="tax_document", direction="none", counterparty="Revenue Commissioners",
        counterparty_role="authority", document_number=None, issue_date=date(today.year, 3, 4).isoformat(), due_date=None,
        currency="EUR", subtotal=None, vat_amount=None, total=None, amount_paid=None, payment_status="not_applicable",
        category=None, project=None, summary="Relevant Contracts Tax deduction notification.", confidence=0.9, flags=[], line_items=[],
    ))

    with db.connect() as conn:
        conn.execute("DELETE FROM documents WHERE source='demo'")
    n = 0
    for i, r in enumerate(rows):
        d = date.fromisoformat(r["issue_date"])
        meta = dict(source="demo", source_ref=None, source_id=f"demo-{seed}-{i}", name=r["name"], mime="application/pdf",
                    folder=_folder(r["doc_type"], d), web_link="", size=rng.randint(40_000, 900_000),
                    modified_at=f"{r['issue_date']}T10:00:00+00:00")
        doc_id = db.upsert_file_metadata(meta)
        fields = derive(r)
        fields["raw"] = {k: v for k, v in r.items()}
        db.save_extraction(doc_id, fields, "demo-data")
        n += 1
    return n


def clear() -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM documents WHERE source='demo'")


if __name__ == "__main__":
    db.init_db()
    print(f"Loaded {load()} demo documents into {config.DB_PATH}")


def count() -> int:
    with db.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM documents WHERE source='demo'").fetchone()[0]
