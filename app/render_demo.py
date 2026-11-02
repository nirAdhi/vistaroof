"""Draw a plausible-looking page for a demo document so previews can be shown without real files.

Only used for rows with ``source='demo'``. Picks a layout from the document type: A4 invoice/quote/
PO page with a PAID/OVERDUE stamp, narrow till receipt with a handwritten note, handwritten receipt
for private clients, payslip, bank statement. Fonts fall back to Pillow's default when the Windows
fonts are not present (Docker). Output is JPEG. ``random`` is seeded by the document id so the same
document always renders the same page.
"""
from __future__ import annotations

import io
import random
from typing import Any

from PIL import Image, ImageDraw, ImageFont

FONT_DIR = "C:/Windows/Fonts/"


def _font(name: str, size: int):
    for candidate in (name, "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(FONT_DIR + candidate, size)
        except OSError:
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _money(v: Any) -> str:
    try:
        return f"€{float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


def render(row: dict[str, Any], company: str, company_address: str) -> bytes:
    rng = random.Random(row["id"])
    doc_type = row.get("doc_type") or "other"
    if doc_type == "receipt":
        img = _till_receipt(row, rng)
    elif doc_type == "sales_invoice" and (row.get("counterparty") or "").startswith("Private client") and rng.random() < 0.5:
        img = _handwritten_receipt(row, company)
    elif doc_type == "bank_statement":
        img = _statement(row, company, rng)
    elif doc_type == "payslip":
        img = _payslip(row, company)
    else:
        img = _page(row, company, company_address)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _page(row: dict[str, Any], company: str, address: str) -> Image.Image:
    W, H = 1240, 1754  # A4 at 150 dpi
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    h1, h2, body, small = _font("arialbd.ttf", 54), _font("arialbd.ttf", 30), _font("arial.ttf", 26), _font("arial.ttf", 20)
    money_in = row.get("direction") == "money_in"
    issuer = company if money_in else (row.get("counterparty") or "Supplier")
    issuer_addr = address if money_in else "Head office · Ireland"
    recipient = (row.get("counterparty") or "Customer") if money_in else company
    recipient_addr = "" if money_in else address
    title = {"sales_invoice": "INVOICE", "supplier_invoice": "INVOICE", "proforma_invoice": "PROFORMA INVOICE",
             "quote_estimate": "QUOTATION", "credit_note": "CREDIT NOTE", "purchase_order": "PURCHASE ORDER",
             "insurance": "POLICY SCHEDULE", "tax_document": "NOTICE", "remittance_advice": "REMITTANCE ADVICE"}.get(row.get("doc_type"), "DOCUMENT")
    d.rectangle((0, 0, W, 18), fill=(31, 92, 150))
    d.text((90, 70), issuer.upper(), fill=(31, 92, 150), font=h1)
    y = 140
    for line in [s.strip() for s in issuer_addr.split(",") if s.strip()][:4]:
        d.text((90, y), line, fill=(90, 90, 90), font=small); y += 26
    d.text((W - 90 - d.textlength(title, font=h1), 70), title, fill=(40, 40, 40), font=h1)
    meta_y = 150
    for label, value in [("Number", row.get("document_number") or "—"), ("Date", row.get("issue_date") or "—"), ("Due", row.get("due_date") or "—")]:
        txt = f"{label}: {value}"
        d.text((W - 90 - d.textlength(txt, font=body), meta_y), txt, fill=(60, 60, 60), font=body); meta_y += 34
    d.text((90, 330), "Bill to", fill=(120, 120, 120), font=small)
    d.text((90, 356), recipient, fill=(20, 20, 20), font=h2)
    y = 396
    for line in [s.strip() for s in recipient_addr.split(",") if s.strip()][:4]:
        d.text((90, y), line, fill=(90, 90, 90), font=small); y += 26
    if row.get("project"):
        d.text((90, y + 10), f"Job / site: {row['project']}", fill=(60, 60, 60), font=body)
    # table
    top = 560
    d.rectangle((90, top, W - 90, top + 44), fill=(240, 243, 247))
    for x, t in ((110, "Description"), (820, "Qty"), (920, "Unit"), (1070, "Amount")):
        d.text((x, top + 10), t, fill=(60, 60, 60), font=body)
    items = row.get("line_items") or []
    if not items:
        items = [{"description": (row.get("summary") or "Roofing works")[:60], "quantity": 1, "unit_price": row.get("subtotal"), "amount": row.get("subtotal")}]
    y = top + 60
    for it in items[:12]:
        d.text((110, y), str(it.get("description") or "")[:55], fill=(30, 30, 30), font=body)
        d.text((820, y), str(it.get("quantity") if it.get("quantity") is not None else ""), fill=(30, 30, 30), font=body)
        d.text((920, y), _money(it.get("unit_price")) if it.get("unit_price") is not None else "", fill=(30, 30, 30), font=body)
        amt = _money(it.get("amount")) if it.get("amount") is not None else ""
        d.text((W - 110 - d.textlength(amt, font=body), y), amt, fill=(30, 30, 30), font=body)
        y += 40
        d.line((90, y - 6, W - 90, y - 6), fill=(230, 230, 230), width=1)
    y += 30
    for label, value, bold in (("Subtotal", row.get("subtotal"), False), ("VAT", row.get("vat_amount"), False), ("TOTAL", row.get("total"), True)):
        f = h2 if bold else body
        d.text((820, y), label, fill=(30, 30, 30), font=f)
        txt = _money(value)
        d.text((W - 110 - d.textlength(txt, font=f), y), txt, fill=(30, 30, 30), font=f)
        y += 46
    status = row.get("payment_status")
    if status == "paid":
        _stamp(d, (140, y + 40), "PAID", (20, 140, 60))
    elif status == "overdue":
        _stamp(d, (140, y + 40), "OVERDUE", (200, 40, 40))
    d.text((90, H - 120), "Payment by bank transfer within 30 days. Thank you for your business.", fill=(120, 120, 120), font=small)
    d.text((90, H - 90), "Sample rendering generated from demo data.", fill=(170, 170, 170), font=small)
    return img


def _stamp(d: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, color: tuple[int, int, int]) -> None:
    f = _font("arialbd.ttf", 64)
    x, y = xy
    w = d.textlength(text, font=f)
    d.rounded_rectangle((x - 20, y - 14, x + w + 20, y + 76), radius=12, outline=color, width=5)
    d.text((x, y), text, fill=color, font=f)


def _till_receipt(row: dict[str, Any], rng: random.Random) -> Image.Image:
    W, H = 600, 1000
    img = Image.new("RGB", (W, H), (252, 251, 246))
    d = ImageDraw.Draw(img)
    mono, hand = _font("consola.ttf", 24), _font("segoepr.ttf", 34)
    shop = (row.get("counterparty") or "SHOP").upper()
    lines = [shop.center(30), "VAT No. IE 8231446L".center(30), "-" * 30]
    items = row.get("line_items") or []
    if not items:
        n = rng.randint(2, 5)
        remaining = float(row.get("subtotal") or 50)
        names = ["Roof felt", "Galv nails 2kg", "Sealant", "Timber batten", "Lead flashing", "Diesel", "PPE gloves", "Fixings", "Bitumen primer"]
        for i in range(n):
            amt = round(remaining / (n - i) * rng.uniform(0.6, 1.4), 2) if i < n - 1 else round(remaining, 2)
            remaining -= amt
            items.append({"description": rng.choice(names), "amount": amt})
    for it in items[:10]:
        desc = str(it.get("description") or "")[:20]
        amt = f"{float(it.get('amount') or 0):.2f}"
        lines.append(f"{desc:<20}{amt:>10}")
    lines += ["-" * 30, f"{'SUBTOTAL':<20}{float(row.get('subtotal') or 0):>10.2f}", f"{'VAT 23%':<20}{float(row.get('vat_amount') or 0):>10.2f}",
              f"{'TOTAL EUR':<20}{float(row.get('total') or 0):>10.2f}", "", f"CARD ****{rng.randint(1000, 9999)}  APPROVED", f"{row.get('issue_date') or ''}  {rng.randint(8, 17):02d}:{rng.randint(0, 59):02d}", "", "Thank you for shopping".center(30)]
    y = 50
    for ln in lines:
        d.text((40, y), ln, fill=(45, 45, 45), font=mono); y += 34
    note = rng.choice(["PAID - cash", "paid, job " + (row.get("project") or "Oak Dr"), "Paid card", "PAID"])
    d.text((90, y + 50), note, fill=(30, 60, 160), font=hand)
    d.line((80, y + 100, 420, y + 106), fill=(30, 60, 160), width=3)
    d.text((40, H - 40), "Sample rendering from demo data", fill=(180, 180, 180), font=_font("arial.ttf", 16))
    return img.rotate(rng.uniform(-2.5, 2.5), expand=True, fillcolor=(225, 222, 214))


def _handwritten_receipt(row: dict[str, Any], company: str) -> Image.Image:
    W, H = 1000, 700
    img = Image.new("RGB", (W, H), (255, 253, 243))
    d = ImageDraw.Draw(img)
    hand, small = _font("segoepr.ttf", 38), _font("arial.ttf", 16)
    for y in range(90, H - 60, 70):
        d.line((40, y + 48, W - 40, y + 48), fill=(215, 215, 230), width=1)
    who = (row.get("counterparty") or "").replace("Private client: ", "")
    text = [f"Received from {who}", f"the sum of  {_money(row.get('total'))}", f"for {row.get('summary', 'roofing work').split(' for ')[-1][:40]}",
            f"{'Paid in full - cash' if row.get('payment_status') == 'paid' else 'Balance outstanding'}", f"{company}   {row.get('issue_date') or ''}"]
    for i, ln in enumerate(text):
        d.text((60, 70 + i * 100), ln, fill=(25, 40, 120), font=hand)
    d.text((40, H - 30), "Sample rendering from demo data", fill=(180, 180, 180), font=small)
    return img


def _payslip(row: dict[str, Any], company: str) -> Image.Image:
    W, H = 1240, 900
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    h1, body, small = _font("arialbd.ttf", 40), _font("arial.ttf", 26), _font("arial.ttf", 18)
    d.text((80, 60), f"{company} — Payslip", fill=(31, 92, 150), font=h1)
    d.text((80, 130), f"Employee: {row.get('counterparty')}", fill=(30, 30, 30), font=body)
    d.text((80, 170), f"Pay date: {row.get('issue_date')}", fill=(30, 30, 30), font=body)
    net = float(row.get("total") or 0)
    gross = round(net / 0.72, 2)
    rows = [("Gross pay", gross), ("PAYE", round(gross * 0.2, 2)), ("PRSI", round(gross * 0.04, 2)), ("USC", round(gross * 0.04, 2)), ("NET PAY", net)]
    y = 260
    for label, val in rows:
        f = h1 if label == "NET PAY" else body
        d.text((80, y), label, fill=(30, 30, 30), font=f)
        txt = _money(val)
        d.text((W - 80 - d.textlength(txt, font=f), y), txt, fill=(30, 30, 30), font=f)
        y += 60
        d.line((80, y - 10, W - 80, y - 10), fill=(230, 230, 230))
    d.text((80, H - 50), "Sample rendering generated from demo data.", fill=(170, 170, 170), font=small)
    return img


def _statement(row: dict[str, Any], company: str, rng: random.Random) -> Image.Image:
    W, H = 1240, 1400
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    h1, body, small = _font("arialbd.ttf", 40), _font("arial.ttf", 24), _font("arial.ttf", 18)
    d.text((80, 60), f"{row.get('counterparty') or 'Bank'} — Business Current Account", fill=(31, 92, 150), font=h1)
    d.text((80, 120), f"{company} · Statement {row.get('issue_date')}", fill=(60, 60, 60), font=body)
    y = 220
    for x, t in ((80, "Date"), (260, "Details"), (820, "Out"), (980, "In"), (1120, "Balance")):
        d.text((x, y), t, fill=(120, 120, 120), font=small)
    bal = rng.uniform(5000, 40000)
    y += 40
    for _ in range(22):
        out = rng.choice([0, 0, round(rng.uniform(40, 4000), 2)])
        inc = 0 if out else round(rng.uniform(500, 12000), 2)
        bal += inc - out
        d.text((80, y), row.get("issue_date") or "", fill=(30, 30, 30), font=small)
        d.text((260, y), rng.choice(["CHADWICKS", "CIRCLE K", "MURPHY HOMES", "PAYROLL", "REVENUE", "ALLIANZ", "LIDL IRELAND", "SCREWFIX"]), fill=(30, 30, 30), font=small)
        for x, v in ((820, out), (980, inc), (1120, bal)):
            if v:
                d.text((x, y), f"{v:,.2f}", fill=(30, 30, 30), font=small)
        y += 40
    d.text((80, H - 50), "Sample rendering generated from demo data.", fill=(170, 170, 170), font=small)
    return img
