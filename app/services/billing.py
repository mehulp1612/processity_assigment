"""Billing: multi-turn draft bills and the one place stock ever moves.

Design
------
A bill under construction is a real ``bills`` row with ``status='draft'`` plus
``bill_lines``. The agent only carries a ``bill_id`` between turns. That gives us:

  * multi-turn edits ("drop the butter, make it 6 Maggi") as plain row mutations
  * durability — a half-built bill survives a restart
  * concurrency — two bills in flight are simply two rows
  * one clean commit point for stock: ``finalize_bill``

Nothing decrements stock until finalize. Finalize is idempotent, re-checks live
stock under a write lock, and refuses rather than overselling.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from .. import db
from ..domain.gst import compute_bill, bill_to_floats
from ..domain.money import as_float
from . import khata as khata_svc
from .common import (
    SHOP_TZ, DomainError, cached_op, next_invoice_no, now_iso, record_op, row_to_dict,
)

PAYMENT_MODES = {"cash", "upi", "card", "khata"}


# --- Internal helpers ------------------------------------------------------

def _load_bill_row(cx, bill_id: int):
    row = cx.execute("SELECT * FROM bills WHERE id = %s", (bill_id,)).fetchone()
    if row is None:
        raise DomainError("BILL_NOT_FOUND", f"No bill with id {bill_id}.")
    return row


def _require_draft(row) -> None:
    if row["status"] != "draft":
        raise DomainError(
            "BILL_NOT_DRAFT",
            f"Bill {row['id']} is already {row['status']} and cannot be changed.",
            {"bill_id": row["id"], "status": row["status"], "invoice_no": row["invoice_no"]},
        )


def _lines_for_tax(cx, bill_id: int) -> list[dict]:
    rows = cx.execute(
        """SELECT bl.*, p.name FROM bill_lines bl
           JOIN products p ON p.id = bl.product_id
           WHERE bl.bill_id = %s ORDER BY bl.id""",
        (bill_id,),
    ).fetchall()
    return [
        {"name": r["name"], "unit_price": r["unit_price"], "qty": r["qty"],
         "gst_rate": r["gst_rate"], "line_id": r["id"], "product_id": r["product_id"]}
        for r in rows
    ]


def _recompute(cx, bill_id: int) -> dict:
    """Recompute every line amount and the bill totals from the GST engine.

    We always recompute from (unit_price, qty, gst_rate) rather than trusting
    stored amounts, so a bug or a stale row can never silently change the money.
    """
    items = _lines_for_tax(cx, bill_id)
    tax = compute_bill(items)

    for item, ln in zip(items, tax.lines):
        cx.execute(
            """UPDATE bill_lines
               SET line_taxable = %s, line_cgst = %s, line_sgst = %s, line_total = %s
               WHERE id = %s""",
            (as_float(ln.taxable), as_float(ln.cgst), as_float(ln.sgst),
             as_float(ln.total), item["line_id"]),
        )

    cx.execute(
        """UPDATE bills SET subtotal = %s, cgst = %s, sgst = %s, round_off = %s, total = %s
           WHERE id = %s""",
        (as_float(tax.subtotal), as_float(tax.cgst), as_float(tax.sgst),
         as_float(tax.round_off), as_float(tax.total), bill_id),
    )
    return bill_to_floats(tax)


def _bill_view(cx, bill_id: int) -> dict:
    row = _load_bill_row(cx, bill_id)
    items = _lines_for_tax(cx, bill_id)
    tax = bill_to_floats(compute_bill(items))
    for item, ln in zip(items, tax["lines"]):
        ln["line_id"] = item["line_id"]
        ln["product_id"] = item["product_id"]
    return {
        "bill_id": row["id"],
        "invoice_no": row["invoice_no"],
        "status": row["status"],
        "customer": row["customer"],
        "payment_mode": row["payment_mode"],
        "payment_ref": row["payment_ref"],
        "created_at": row["created_at"],
        "finalized_at": row["finalized_at"],
        **tax,
    }


# --- Draft lifecycle -------------------------------------------------------

def start_bill(chat_id: str, customer: Optional[str] = None) -> dict:
    with db.tx() as cx:
        bill_id = cx.execute(
            """INSERT INTO bills (chat_id, customer, status, created_at)
               VALUES (%s,%s,'draft',%s) RETURNING id""",
            (str(chat_id), customer, now_iso()),
        ).fetchone()["id"]
        view = _bill_view(cx, bill_id)
    return view


def open_draft(chat_id: str) -> Optional[dict]:
    """Most recent draft for this chat, if any — lets the agent resume a bill."""
    with db.tx() as cx:
        row = cx.execute(
            """SELECT id FROM bills WHERE chat_id = %s AND status = 'draft'
               ORDER BY id DESC LIMIT 1""",
            (str(chat_id),),
        ).fetchone()
        if row is None:
            return None
        return _bill_view(cx, row["id"])


def recent_bills(limit: int = 5, customer: Optional[str] = None) -> dict:
    """Finalized bills, newest first.

    Exists so "the last bill" is answerable. Without it the model has no way to
    turn that phrase into a bill_id, and its only honest options are to ask the
    owner or to guess an id — and guessing an id that resolves to a *real other
    bill* is the worse failure, because it invoices the wrong customer silently.

    Times come back in shop-local form so the model can say "the 6:40 pm one"
    without doing timezone arithmetic it has no business doing.
    """
    limit = max(1, min(int(limit), 20))
    sql = """SELECT id, invoice_no, customer, payment_mode, total, finalized_at
               FROM bills
              WHERE status = 'finalized'"""
    params: list = []
    if customer:
        sql += " AND lower(customer) = lower(%s)"
        params.append(customer.strip())
    sql += " ORDER BY finalized_at DESC LIMIT %s"
    params.append(limit)

    with db.tx() as cx:
        rows = cx.execute(sql, tuple(params)).fetchall()

    for row in rows:
        stamp = row.get("finalized_at")
        if stamp:
            row["finalized_at"] = (
                datetime.fromisoformat(stamp).astimezone(SHOP_TZ).strftime("%Y-%m-%d %H:%M")
            )
    return {"ok": True, "bills": rows, "count": len(rows)}


def view_bill(bill_id: int) -> dict:
    with db.tx() as cx:
        return _bill_view(cx, bill_id)


def add_line(bill_id: int, product_id: int, qty: float) -> dict:
    """Add (or top up) a line on a draft.

    Performs an *advisory* stock check so the owner hears about a shortfall
    immediately. The binding check happens again at finalize under a write lock —
    stock may change between building and finalizing the bill.
    """
    if qty <= 0:
        raise DomainError("BAD_ARGS", "Quantity must be positive.")

    with db.tx() as cx:
        bill = _load_bill_row(cx, bill_id)
        _require_draft(bill)

        prod = cx.execute(
            """SELECT p.*, COALESCE(s.qty, 0) AS qty FROM products p
               LEFT JOIN stock s ON s.product_id = p.id WHERE p.id = %s""",
            (product_id,),
        ).fetchone()
        if prod is None:
            raise DomainError("PRODUCT_NOT_FOUND", f"No product with id {product_id}.")

        existing = cx.execute(
            "SELECT * FROM bill_lines WHERE bill_id = %s AND product_id = %s",
            (bill_id, product_id),
        ).fetchone()
        new_qty = (existing["qty"] if existing else 0) + float(qty)

        if new_qty > prod["qty"]:
            raise DomainError(
                "INSUFFICIENT_STOCK",
                f"Only {prod['qty']:g} {prod['unit']} of {prod['name']} in stock; "
                f"the bill would need {new_qty:g}.",
                {"product_id": product_id, "name": prod["name"],
                 "available": prod["qty"], "requested": new_qty, "unit": prod["unit"]},
            )

        if existing:
            cx.execute("UPDATE bill_lines SET qty = %s WHERE id = %s", (new_qty, existing["id"]))
        else:
            cx.execute(
                """INSERT INTO bill_lines (bill_id, product_id, qty, unit_price, gst_rate)
                   VALUES (%s,%s,%s,%s,%s)""",
                (bill_id, product_id, float(qty), prod["mrp"], prod["gst_rate"]),
            )
        _recompute(cx, bill_id)
        view = _bill_view(cx, bill_id)
    return view


def set_line_qty(bill_id: int, product_id: int, qty: float) -> dict:
    """Set an exact quantity ('make it 6 Maggi'). qty=0 removes the line."""
    if qty < 0:
        raise DomainError("BAD_ARGS", "Quantity cannot be negative.")
    if qty == 0:
        return remove_line(bill_id, product_id)

    with db.tx() as cx:
        bill = _load_bill_row(cx, bill_id)
        _require_draft(bill)
        line = cx.execute(
            "SELECT * FROM bill_lines WHERE bill_id = %s AND product_id = %s",
            (bill_id, product_id),
        ).fetchone()
        if line is None:
            raise DomainError(
                "LINE_NOT_FOUND",
                f"That item isn't on bill {bill_id}.",
                {"bill_id": bill_id, "product_id": product_id},
            )
        prod = cx.execute(
            """SELECT p.name, p.unit, COALESCE(s.qty,0) AS qty FROM products p
               LEFT JOIN stock s ON s.product_id = p.id WHERE p.id = %s""",
            (product_id,),
        ).fetchone()
        if float(qty) > prod["qty"]:
            raise DomainError(
                "INSUFFICIENT_STOCK",
                f"Only {prod['qty']:g} {prod['unit']} of {prod['name']} in stock; asked for {float(qty):g}.",
                {"product_id": product_id, "name": prod["name"],
                 "available": prod["qty"], "requested": float(qty), "unit": prod["unit"]},
            )
        cx.execute("UPDATE bill_lines SET qty = %s WHERE id = %s", (float(qty), line["id"]))
        _recompute(cx, bill_id)
        view = _bill_view(cx, bill_id)
    return view


def remove_line(bill_id: int, product_id: int) -> dict:
    """Drop an item from the draft ('drop the butter')."""
    with db.tx() as cx:
        bill = _load_bill_row(cx, bill_id)
        _require_draft(bill)
        cur = cx.execute(
            "DELETE FROM bill_lines WHERE bill_id = %s AND product_id = %s",
            (bill_id, product_id),
        )
        if cur.rowcount == 0:
            raise DomainError(
                "LINE_NOT_FOUND",
                f"That item isn't on bill {bill_id}.",
                {"bill_id": bill_id, "product_id": product_id},
            )
        _recompute(cx, bill_id)
        view = _bill_view(cx, bill_id)
    return view


def cancel_bill(bill_id: int) -> dict:
    """Void a draft. Never touches stock (a draft never reserved any)."""
    with db.tx() as cx:
        bill = _load_bill_row(cx, bill_id)
        _require_draft(bill)
        cx.execute("UPDATE bills SET status = 'void' WHERE id = %s", (bill_id,))
        view = _bill_view(cx, bill_id)
    return view


# --- Finalize: the only place stock moves ----------------------------------

def finalize_bill(
    bill_id: int,
    payment_mode: str,
    payment_ref: Optional[str] = None,
    customer: Optional[str] = None,
    op_key: Optional[str] = None,
    allow_below_cost: bool = False,
) -> dict:
    """Commit the bill: check stock, compute GST, decrement, record payment.

    Guarantees, all enforced here rather than in the prompt:
      * **Idempotent** — a repeated ``op_key`` replays the first result instead of
        billing twice (Telegram redelivers updates).
      * **Never oversells** — live stock is re-read under ``BEGIN IMMEDIATE`` and
        each decrement is a guarded UPDATE; a shortfall aborts the whole tx.
      * **Never sells below cost** without explicit confirmation.
      * **Atomic** — stock, payment/khata and bill status commit together or not
        at all.
    """
    mode = (payment_mode or "").strip().lower()
    if mode not in PAYMENT_MODES:
        raise DomainError(
            "INVALID_PAYMENT_MODE",
            f"'{payment_mode}' is not a payment mode.",
            {"valid": sorted(PAYMENT_MODES)},
        )

    with db.tx() as cx:
        # 1. Idempotency: replay a prior result for the same operation.
        if op_key:
            prior = cached_op(cx, op_key)
            if prior is not None:
                return {**prior, "idempotent_replay": True}

        bill = _load_bill_row(cx, bill_id)

        # A retry that lost its op_key still must not double-bill.
        if bill["status"] == "finalized":
            raise DomainError(
                "BILL_ALREADY_FINALIZED",
                f"Bill {bill_id} is already finalized as {bill['invoice_no']}.",
                {"bill_id": bill_id, "invoice_no": bill["invoice_no"], "total": bill["total"]},
            )
        _require_draft(bill)

        items = _lines_for_tax(cx, bill_id)
        if not items:
            raise DomainError("EMPTY_BILL", "This bill has no items yet.", {"bill_id": bill_id})

        cust = customer or bill["customer"]
        if mode == "khata":
            if not cust:
                raise DomainError(
                    "CUSTOMER_REQUIRED",
                    "A credit (khata) sale needs a customer name.",
                    {"bill_id": bill_id},
                )

        # 2. Below-cost guard — refuse unless the owner explicitly confirmed.
        if not allow_below_cost:
            below = []
            for it in items:
                cost = cx.execute(
                    "SELECT cost_price, name FROM products WHERE id = %s", (it["product_id"],)
                ).fetchone()
                if it["unit_price"] < cost["cost_price"]:
                    below.append({"name": cost["name"], "unit_price": it["unit_price"],
                                  "cost_price": cost["cost_price"]})
            if below:
                raise DomainError(
                    "BELOW_COST",
                    "Some items are priced below cost. Confirm before selling at a loss.",
                    {"items": below},
                )

        # 3. Oversell guard against *live* stock, under the write lock.
        shortfalls = []
        for it in items:
            row = cx.execute(
                """SELECT COALESCE(s.qty,0) AS qty, p.name, p.unit FROM products p
                   LEFT JOIN stock s ON s.product_id = p.id WHERE p.id = %s""",
                (it["product_id"],),
            ).fetchone()
            if row["qty"] < it["qty"]:
                shortfalls.append({
                    "product_id": it["product_id"], "name": row["name"],
                    "available": row["qty"], "requested": it["qty"], "unit": row["unit"],
                })
        if shortfalls:
            raise DomainError(
                "INSUFFICIENT_STOCK",
                "Not enough stock to finalize this bill.",
                {"shortfalls": shortfalls},
            )

        # 4. Compute the money fresh, then move stock.
        tax = compute_bill(items)
        # Always take stock row locks in product_id order. Two bills sharing items
        # in opposite order would otherwise deadlock; a fixed order makes one wait
        # and then fail the guard cleanly instead.
        for it in sorted(items, key=lambda i: i["product_id"]):
            cur = cx.execute(
                "UPDATE stock SET qty = qty - %s WHERE product_id = %s AND qty >= %s",
                (it["qty"], it["product_id"], it["qty"]),
            )
            if cur.rowcount != 1:
                # Guarded UPDATE matched nothing -> someone raced us. Abort everything.
                raise DomainError(
                    "STOCK_RACE",
                    "Stock changed while finalizing. Nothing was billed; please retry.",
                    {"product_id": it["product_id"]},
                )

        total = as_float(tax.total)
        invoice_no = next_invoice_no(cx)
        cx.execute(
            """UPDATE bills SET status='finalized', invoice_no=%s, payment_mode=%s,
                 payment_ref=%s, customer=%s, subtotal=%s, cgst=%s, sgst=%s, round_off=%s,
                 total=%s, finalized_at=%s
               WHERE id = %s""",
            (invoice_no, mode, payment_ref, cust, as_float(tax.subtotal),
             as_float(tax.cgst), as_float(tax.sgst), as_float(tax.round_off),
             total, now_iso(), bill_id),
        )
        _recompute(cx, bill_id)

        # 5. Record how it was paid.
        if mode == "khata":
            balance = khata_svc._apply(cx, khata_svc._normalise(cust), total,
                                       "credit sale", bill_id)
            payment = {"mode": "khata", "customer": khata_svc._normalise(cust),
                       "khata_balance": balance}
        else:
            cx.execute(
                "INSERT INTO payments (bill_id, mode, ref, amount, at) VALUES (%s,%s,%s,%s,%s)",
                (bill_id, mode, payment_ref, total, now_iso()),
            )
            payment = {"mode": mode, "ref": payment_ref, "amount": total}

        result = {"ok": True, **_bill_view(cx, bill_id), "payment": payment}

        # 6. Record the op so a redelivered update replays instead of re-billing.
        if op_key:
            record_op(cx, op_key, result)

    return result
