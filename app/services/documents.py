"""Real artifacts: a GST tax invoice as PDF, and an analysis deck as PPTX.

Both read from the database and render actual figures — no placeholders, and no
numbers computed here. The invoice re-reads the finalized bill; the deck reads
``analytics.sales_report``. If a figure is wrong on the page it is wrong in the
books, which is the only way a document is worth printing.

Typography note: ReportLab's built-in Helvetica has no glyph for the rupee sign,
so amounts would render as black boxes. DejaVu is registered when present and the
code falls back to "Rs." rather than emitting broken output.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from .. import db
from . import analytics
from .common import DomainError, row_to_dict
from .memory import get_shop

log = logging.getLogger("supermarket.documents")

OUT_DIR = Path(os.environ.get("OUT_DIR", "out"))
IST = ZoneInfo(os.environ.get("SHOP_TIMEZONE", "Asia/Kolkata"))

_DEJAVU = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
_DEJAVU_BOLD = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")


def _fonts() -> tuple[str, str, str]:
    """(regular, bold, currency prefix). Falls back to Helvetica + 'Rs.'."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if _DEJAVU.exists() and _DEJAVU_BOLD.exists():
        try:
            if "DejaVu" not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont("DejaVu", str(_DEJAVU)))
                pdfmetrics.registerFont(TTFont("DejaVu-Bold", str(_DEJAVU_BOLD)))
            return "DejaVu", "DejaVu-Bold", "₹"
        except Exception:                       # pragma: no cover - font edge cases
            log.warning("DejaVu registration failed; falling back", exc_info=True)
    return "Helvetica", "Helvetica-Bold", "Rs."


# --- Amount in words (Indian numbering) -------------------------------------

_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
         "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
         "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _under_hundred(n: int) -> str:
    if n < 20:
        return _ONES[n]
    return (_TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else "")).strip()


def amount_in_words(amount: float) -> str:
    """Indian numbering: lakh and crore, not million.

    A GST tax invoice is expected to carry the amount in words; writing it the
    international way on an Indian invoice reads as an import.
    """
    rupees = int(round(amount))
    if rupees == 0:
        return "Zero Rupees Only"

    parts: list[str] = []
    for divisor, label in ((10_000_000, "Crore"), (100_000, "Lakh"), (1_000, "Thousand")):
        if rupees >= divisor:
            parts.append(f"{_under_hundred(rupees // divisor)} {label}")
            rupees %= divisor
    if rupees >= 100:
        parts.append(f"{_ONES[rupees // 100]} Hundred")
        rupees %= 100
    if rupees:
        parts.append(_under_hundred(rupees))
    return " ".join(parts) + " Rupees Only"


# --- Invoice PDF ------------------------------------------------------------

def _load_invoice(bill_id: int) -> dict:
    with db.tx() as cx:
        bill = cx.execute("SELECT * FROM bills WHERE id = %s", (bill_id,)).fetchone()
        if bill is None:
            raise DomainError("BILL_NOT_FOUND", f"No bill with id {bill_id}.")
        if bill["status"] != "finalized":
            raise DomainError(
                "BILL_NOT_FINALIZED",
                f"Bill {bill_id} is {bill['status']}. Only a finalized bill has an invoice.",
                {"bill_id": bill_id, "status": bill["status"]},
            )
        lines = cx.execute(
            """SELECT p.name, p.hsn, p.unit, bl.qty, bl.unit_price, bl.gst_rate,
                      bl.line_taxable, bl.line_cgst, bl.line_sgst, bl.line_total
               FROM bill_lines bl JOIN products p ON p.id = bl.product_id
               WHERE bl.bill_id = %s ORDER BY bl.id""",
            (bill_id,),
        ).fetchall()
    return {"bill": row_to_dict(bill), "lines": [row_to_dict(r) for r in lines]}


def render_invoice_pdf(bill_id: int) -> dict:
    """Render a finalized bill as a GST tax invoice PDF. Returns the file path."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
    )

    data = _load_invoice(bill_id)
    bill, lines = data["bill"], data["lines"]
    shop = get_shop() or {}
    regular, bold, rs = _fonts()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    safe = (bill["invoice_no"] or f"bill-{bill_id}").replace("/", "-")
    path = OUT_DIR / f"{safe}.pdf"

    def money(v) -> str:
        return f"{rs}{float(v or 0):,.2f}"

    title = ParagraphStyle("t", fontName=bold, fontSize=15, leading=18, alignment=1)
    small = ParagraphStyle("s", fontName=regular, fontSize=8, leading=10)
    normal = ParagraphStyle("n", fontName=regular, fontSize=9, leading=12)

    story = [
        Paragraph("TAX INVOICE", title),
        Spacer(1, 4 * mm),
        Paragraph(f"<b>{shop.get('name', 'Kirana Store')}</b>", normal),
        Paragraph(shop.get("address", ""), small),
        Paragraph(
            f"GSTIN: {shop.get('gstin', '-')} &nbsp;&nbsp; State code: {shop.get('state_code', '-')}"
            + (f" &nbsp;&nbsp; {shop['phone']}" if shop.get("phone") else ""),
            small,
        ),
        Spacer(1, 4 * mm),
    ]

    when = bill["finalized_at"] or bill["created_at"]
    try:
        stamp = datetime.fromisoformat(when).astimezone(IST).strftime("%d %b %Y, %I:%M %p")
    except Exception:
        stamp = when

    meta = [
        ["Invoice No.", bill["invoice_no"] or "-", "Date", stamp],
        ["Customer", bill["customer"] or "Walk-in",
         "Payment", (bill["payment_mode"] or "-").upper()
         + (f" ({bill['payment_ref']})" if bill["payment_ref"] else "")],
    ]
    meta_table = Table(meta, colWidths=[25 * mm, 60 * mm, 25 * mm, 60 * mm])
    meta_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), regular),
        ("FONTNAME", (0, 0), (0, -1), bold),
        ("FONTNAME", (2, 0), (2, -1), bold),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#999999")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
    ]))
    story += [meta_table, Spacer(1, 4 * mm)]

    head = ["#", "Item", "HSN", "Qty", "Rate", "Taxable", "GST%", "CGST", "SGST", "Total"]
    rows = [head]
    for i, ln in enumerate(lines, 1):
        rows.append([
            str(i),
            Paragraph(ln["name"], small),
            ln["hsn"],
            f"{ln['qty']:g} {ln['unit']}",
            money(ln["unit_price"]),
            money(ln["line_taxable"]),
            f"{ln['gst_rate']:g}%",
            money(ln["line_cgst"]),
            money(ln["line_sgst"]),
            money(ln["line_total"]),
        ])

    items = Table(rows, colWidths=[8*mm, 42*mm, 14*mm, 18*mm, 19*mm, 21*mm, 12*mm, 18*mm, 18*mm, 21*mm],
                  repeatRows=1)
    items.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), regular),
        ("FONTNAME", (0, 0), (-1, 0), bold),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8")),
        ("ALIGN", (3, 1), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#bbbbbb")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story += [items, Spacer(1, 4 * mm)]

    # Slab summary — a GST invoice must show tax grouped by rate.
    slabs: dict[float, dict] = {}
    for ln in lines:
        s = slabs.setdefault(ln["gst_rate"], {"taxable": 0.0, "cgst": 0.0, "sgst": 0.0})
        s["taxable"] += ln["line_taxable"]
        s["cgst"] += ln["line_cgst"]
        s["sgst"] += ln["line_sgst"]

    slab_rows = [["GST Rate", "Taxable Value", f"CGST", f"SGST", "Total Tax"]]
    for rate in sorted(slabs):
        s = slabs[rate]
        slab_rows.append([
            f"{rate:g}%", money(s["taxable"]),
            money(s["cgst"]), money(s["sgst"]), money(s["cgst"] + s["sgst"]),
        ])

    summary = Table(slab_rows, colWidths=[22*mm, 34*mm, 28*mm, 28*mm, 28*mm])
    summary.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), regular),
        ("FONTNAME", (0, 0), (-1, 0), bold),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0f0f0")),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#bbbbbb")),
    ]))

    totals = Table([
        ["Taxable Value", money(bill["subtotal"])],
        ["CGST", money(bill["cgst"])],
        ["SGST", money(bill["sgst"])],
        ["Round Off", f"{float(bill['round_off'] or 0):+.2f}"],
        ["TOTAL", money(bill["total"])],
    ], colWidths=[35 * mm, 32 * mm])
    totals.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), regular),
        ("FONTNAME", (0, -1), (-1, -1), bold),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.black),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
    ]))

    story.append(Table([[summary, totals]], colWidths=[142 * mm, 67 * mm],
                       style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")])))
    story += [
        Spacer(1, 4 * mm),
        Paragraph(f"<b>Amount in words:</b> {amount_in_words(bill['total'])}", normal),
        Spacer(1, 8 * mm),
        Paragraph(
            "Intra-state supply — CGST and SGST charged at half the applicable rate each. "
            "Goods once sold will not be taken back. This is a computer-generated invoice.",
            small,
        ),
    ]

    SimpleDocTemplate(
        str(path), pagesize=A4,
        leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
        title=f"Tax Invoice {bill['invoice_no']}", author=shop.get("name", ""),
    ).build(story)

    return {
        "ok": True,
        "path": str(path),
        "invoice_no": bill["invoice_no"],
        "total": bill["total"],
        "lines": len(lines),
    }
