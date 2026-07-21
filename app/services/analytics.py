"""Reporting: what happened today, and what's happening over time.

Two things here are easy to get quietly wrong, so they are handled explicitly:

**A day is a shop-local day.** Timestamps are stored as UTC ISO-8601, but the
owner's "today" runs midnight to midnight in Asia/Kolkata. Closing the day on UTC
dates would push an evening sale into tomorrow's books (and, past 18:30 UTC, put
today's late sales on yesterday). Every boundary here is converted through the
shop's timezone.

**Margin is computed on taxable value, not on the total.** GST collected is the
government's money passing through the till — counting it as revenue would
overstate profit by the tax rate on every line.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from .. import db
from .common import DomainError, row_to_dict

SHOP_TZ = ZoneInfo(os.environ.get("SHOP_TIMEZONE", "Asia/Kolkata"))

# Postgres expression turning a stored UTC timestamp into the shop's local date.
_LOCAL_DATE = "((b.finalized_at::timestamptz) AT TIME ZONE %s)::date"


def today() -> str:
    """The shop's current date, not the server's."""
    return datetime.now(SHOP_TZ).date().isoformat()


def _parse_day(value: Optional[str]) -> date:
    if not value:
        return datetime.now(SHOP_TZ).date()
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise DomainError(
            "BAD_ARGS", f"'{value}' is not a date. Use YYYY-MM-DD."
        ) from exc


def _bounds(first: date, last: date) -> tuple[str, str]:
    """UTC half-open range [start, end) covering these shop-local days inclusive."""
    start = datetime.combine(first, datetime.min.time(), tzinfo=SHOP_TZ)
    end = datetime.combine(last + timedelta(days=1), datetime.min.time(), tzinfo=SHOP_TZ)
    return start.astimezone(ZoneInfo("UTC")).isoformat(), end.astimezone(ZoneInfo("UTC")).isoformat()


def _totals(cx, lo: str, hi: str) -> dict:
    row = cx.execute(
        """SELECT COUNT(*) AS bills,
                  COALESCE(SUM(subtotal),0)  AS subtotal,
                  COALESCE(SUM(cgst),0)      AS cgst,
                  COALESCE(SUM(sgst),0)      AS sgst,
                  COALESCE(SUM(round_off),0) AS round_off,
                  COALESCE(SUM(total),0)     AS total
           FROM bills
           WHERE status = 'finalized' AND finalized_at >= %s AND finalized_at < %s""",
        (lo, hi),
    ).fetchone()
    out = row_to_dict(row)
    out["gst"] = round(out["cgst"] + out["sgst"], 2)
    return out


def _payment_mix(cx, lo: str, hi: str) -> list[dict]:
    """How the money arrived. Khata sales are counted from the bill, not payments —
    a credit sale never writes a payment row, and would otherwise vanish."""
    rows = cx.execute(
        """SELECT payment_mode AS mode, COUNT(*) AS bills, COALESCE(SUM(total),0) AS amount
           FROM bills
           WHERE status = 'finalized' AND finalized_at >= %s AND finalized_at < %s
           GROUP BY payment_mode ORDER BY amount DESC""",
        (lo, hi),
    ).fetchall()
    return [row_to_dict(r) for r in rows]


def _slabs(cx, lo: str, hi: str) -> list[dict]:
    rows = cx.execute(
        """SELECT bl.gst_rate,
                  COALESCE(SUM(bl.line_taxable),0) AS taxable,
                  COALESCE(SUM(bl.line_cgst),0)    AS cgst,
                  COALESCE(SUM(bl.line_sgst),0)    AS sgst
           FROM bill_lines bl JOIN bills b ON b.id = bl.bill_id
           WHERE b.status = 'finalized' AND b.finalized_at >= %s AND b.finalized_at < %s
           GROUP BY bl.gst_rate ORDER BY bl.gst_rate""",
        (lo, hi),
    ).fetchall()
    return [row_to_dict(r) for r in rows]


def _top_items(cx, lo: str, hi: str, limit: int) -> list[dict]:
    rows = cx.execute(
        """SELECT p.name, p.unit,
                  SUM(bl.qty)                                        AS qty,
                  SUM(bl.line_taxable)                               AS revenue,
                  SUM(bl.line_taxable - bl.qty * p.cost_price)       AS margin
           FROM bill_lines bl
           JOIN bills b    ON b.id = bl.bill_id
           JOIN products p ON p.id = bl.product_id
           WHERE b.status = 'finalized' AND b.finalized_at >= %s AND b.finalized_at < %s
           GROUP BY p.id, p.name, p.unit
           ORDER BY revenue DESC LIMIT %s""",
        (lo, hi, limit),
    ).fetchall()
    return [row_to_dict(r) for r in rows]


def _margin(cx, lo: str, hi: str) -> dict:
    """Gross margin on taxable value.

    Uses each product's *current* cost price — the shop keeps one cost per SKU
    rather than per-batch costing, so a mid-period cost change is applied
    retrospectively. Good enough to steer a kirana; not a costing system.
    """
    row = cx.execute(
        """SELECT COALESCE(SUM(bl.line_taxable),0)                   AS revenue,
                  COALESCE(SUM(bl.qty * p.cost_price),0)             AS cost
           FROM bill_lines bl
           JOIN bills b    ON b.id = bl.bill_id
           JOIN products p ON p.id = bl.product_id
           WHERE b.status = 'finalized' AND b.finalized_at >= %s AND b.finalized_at < %s""",
        (lo, hi),
    ).fetchone()
    revenue, cost = row["revenue"], row["cost"]
    margin = round(revenue - cost, 2)
    return {
        "revenue_ex_gst": round(revenue, 2),
        "cost_of_goods": round(cost, 2),
        "gross_margin": margin,
        "margin_pct": round(margin / revenue * 100, 2) if revenue else 0.0,
    }


def _khata_movement(cx, lo: str, hi: str) -> dict:
    row = cx.execute(
        """SELECT COALESCE(SUM(CASE WHEN delta > 0 THEN delta END),0)  AS credit_given,
                  COALESCE(SUM(CASE WHEN delta < 0 THEN -delta END),0) AS received
           FROM khata_txns WHERE at >= %s AND at < %s""",
        (lo, hi),
    ).fetchone()
    outstanding = cx.execute(
        "SELECT COALESCE(SUM(balance),0) AS total FROM khata WHERE balance > 0"
    ).fetchone()["total"]
    return {**row_to_dict(row), "outstanding_total": outstanding}


# --- Public API -------------------------------------------------------------

def daily_close(day: Optional[str] = None, top: int = 5) -> dict:
    """The day's books: takings, tax collected, credit moved, best sellers.

    ``day`` is a shop-local date (YYYY-MM-DD); defaults to today in the shop's
    timezone.
    """
    d = _parse_day(day)
    lo, hi = _bounds(d, d)

    with db.tx() as cx:
        totals = _totals(cx, lo, hi)
        mix = _payment_mix(cx, lo, hi)
        khata = _khata_movement(cx, lo, hi)
        result = {
            "date": d.isoformat(),
            "timezone": str(SHOP_TZ),
            **totals,
            "payment_mix": mix,
            "cash_in_hand": next((m["amount"] for m in mix if m["mode"] == "cash"), 0.0),
            "credit_sales": next((m["amount"] for m in mix if m["mode"] == "khata"), 0.0),
            "khata": khata,
            "slabs": _slabs(cx, lo, hi),
            "top_items": _top_items(cx, lo, hi, top),
            **_margin(cx, lo, hi),
        }
    return result


def sales_report(start: Optional[str] = None, end: Optional[str] = None,
                 top: int = 10) -> dict:
    """Totals and a per-day series over a shop-local date range (inclusive).

    Defaults to the last 7 days. The ``by_day`` series is what the analysis deck
    charts, so it includes days with no sales rather than skipping them — a gap in
    a bar chart should read as a quiet day, not as missing data.
    """
    last = _parse_day(end)
    first = _parse_day(start) if start else last - timedelta(days=6)
    if first > last:
        raise DomainError("BAD_ARGS", "Start date is after the end date.")

    lo, hi = _bounds(first, last)

    with db.tx() as cx:
        rows = cx.execute(
            f"""SELECT {_LOCAL_DATE} AS day,
                       COUNT(*) AS bills,
                       COALESCE(SUM(b.total),0)    AS total,
                       COALESCE(SUM(b.subtotal),0) AS subtotal
                FROM bills b
                WHERE b.status = 'finalized' AND b.finalized_at >= %s AND b.finalized_at < %s
                GROUP BY 1 ORDER BY 1""",
            (str(SHOP_TZ), lo, hi),
        ).fetchall()
        seen = {str(r["day"]): row_to_dict(r) for r in rows}

        series, cursor = [], first
        while cursor <= last:
            key = cursor.isoformat()
            found = seen.get(key)
            series.append({
                "day": key,
                "bills": found["bills"] if found else 0,
                "total": found["total"] if found else 0.0,
                "subtotal": found["subtotal"] if found else 0.0,
            })
            cursor += timedelta(days=1)

        result = {
            "start": first.isoformat(),
            "end": last.isoformat(),
            "timezone": str(SHOP_TZ),
            **_totals(cx, lo, hi),
            "by_day": series,
            "payment_mix": _payment_mix(cx, lo, hi),
            "slabs": _slabs(cx, lo, hi),
            "top_items": _top_items(cx, lo, hi, top),
            "khata": _khata_movement(cx, lo, hi),
            **_margin(cx, lo, hi),
        }
    return result
