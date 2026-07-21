"""GST computation for an intra-state kirana bill.

Intra-state supply (shop and customer in the same state) splits the tax into
CGST + SGST at half the slab each. The math, per the invoice we render:

    line_taxable = round2(unit_price * qty)
    line_gst     = round2(line_taxable * gst_rate/100)
    line_cgst    = round2(line_gst / 2)
    line_sgst    = line_gst - line_cgst        # sgst absorbs the odd paise so
                                               # cgst + sgst == line_gst exactly
    line_total   = line_taxable + line_gst

Bill totals sum the (already rounded) line amounts, then round the grand total
to the nearest rupee and expose the round-off as its own line — the standard
kirana convention.

A per-slab breakup (taxable / CGST / SGST grouped by rate) is produced for the
tax table an Indian GST invoice must legally show.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .money import D, paise, rupee, as_float


@dataclass(frozen=True)
class LineTax:
    name: str
    unit_price: Decimal
    qty: Decimal
    gst_rate: Decimal
    taxable: Decimal
    cgst: Decimal
    sgst: Decimal
    gst: Decimal
    total: Decimal


@dataclass(frozen=True)
class SlabRow:
    gst_rate: Decimal
    taxable: Decimal
    cgst: Decimal
    sgst: Decimal


@dataclass(frozen=True)
class BillTax:
    lines: list[LineTax]
    slabs: list[SlabRow]
    subtotal: Decimal   # sum of taxable values
    cgst: Decimal
    sgst: Decimal
    grand: Decimal      # subtotal + cgst + sgst, before round-off
    round_off: Decimal  # total - grand (can be + or -)
    total: Decimal      # grand rounded to nearest rupee — the amount payable


def compute_line(unit_price, qty, gst_rate, name: str = "") -> LineTax:
    up, q, rate = D(unit_price), D(qty), D(gst_rate)
    taxable = paise(up * q)
    gst = paise(taxable * rate / D(100))
    cgst = paise(gst / D(2))
    sgst = gst - cgst  # exact remainder; guarantees cgst + sgst == gst
    total = taxable + gst
    return LineTax(
        name=name, unit_price=up, qty=q, gst_rate=rate,
        taxable=taxable, cgst=cgst, sgst=sgst, gst=gst, total=total,
    )


def compute_bill(items: list[dict]) -> BillTax:
    """items: list of {name?, unit_price, qty, gst_rate}."""
    lines = [
        compute_line(
            unit_price=it["unit_price"], qty=it["qty"],
            gst_rate=it["gst_rate"], name=it.get("name", ""),
        )
        for it in items
    ]

    subtotal = sum((ln.taxable for ln in lines), Decimal("0"))
    cgst = sum((ln.cgst for ln in lines), Decimal("0"))
    sgst = sum((ln.sgst for ln in lines), Decimal("0"))
    grand = subtotal + cgst + sgst
    total = rupee(grand)
    round_off = total - grand

    # Per-slab breakup, ordered by rate for a stable invoice table.
    by_rate: dict[Decimal, list[Decimal]] = {}
    for ln in lines:
        acc = by_rate.setdefault(ln.gst_rate, [Decimal("0"), Decimal("0"), Decimal("0")])
        acc[0] += ln.taxable
        acc[1] += ln.cgst
        acc[2] += ln.sgst
    slabs = [
        SlabRow(gst_rate=rate, taxable=vals[0], cgst=vals[1], sgst=vals[2])
        for rate, vals in sorted(by_rate.items())
    ]

    return BillTax(
        lines=lines, slabs=slabs, subtotal=subtotal, cgst=cgst, sgst=sgst,
        grand=grand, round_off=round_off, total=total,
    )


def bill_to_floats(b: BillTax) -> dict:
    """Flatten to plain floats/dicts for DB storage and tool JSON results."""
    return {
        "subtotal": as_float(b.subtotal),
        "cgst": as_float(b.cgst),
        "sgst": as_float(b.sgst),
        "round_off": as_float(b.round_off),
        "total": as_float(b.total),
        "lines": [
            {
                "name": ln.name,
                "unit_price": as_float(ln.unit_price),
                "qty": as_float(ln.qty),
                "gst_rate": as_float(ln.gst_rate),
                "taxable": as_float(ln.taxable),
                "cgst": as_float(ln.cgst),
                "sgst": as_float(ln.sgst),
                "total": as_float(ln.total),
            }
            for ln in b.lines
        ],
        "slabs": [
            {
                "gst_rate": as_float(s.gst_rate),
                "taxable": as_float(s.taxable),
                "cgst": as_float(s.cgst),
                "sgst": as_float(s.sgst),
            }
            for s in b.slabs
        ],
    }
