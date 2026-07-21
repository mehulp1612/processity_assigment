"""Khata — the customer credit ledger.

Convention: ``balance`` is what the customer owes the shop.
  * buying on credit  -> delta > 0 (debit)
  * paying it back    -> delta < 0 (credit)

Every movement writes a ``khata_txns`` row, so the balance is always
reconstructable and a statement can be produced.
"""

from __future__ import annotations

from typing import Optional

from .. import db
from .common import DomainError, now_iso, row_to_dict


def _normalise(name: str) -> str:
    """Customers are named by the owner in free text; fold to a stable key."""
    n = " ".join(name.split()).strip()
    if not n:
        raise DomainError("BAD_ARGS", "Customer name is required.")
    return n.title()


def _apply(cx, customer: str, delta: float, reason: str, bill_id: Optional[int] = None) -> float:
    """Apply a signed delta inside an existing transaction. Returns new balance."""
    balance = cx.execute(
        """INSERT INTO khata (customer, balance, updated_at) VALUES (%s,%s,%s)
           ON CONFLICT (customer) DO UPDATE SET
             balance = khata.balance + excluded.balance,
             updated_at = excluded.updated_at
           RETURNING balance""",
        (customer, float(delta), now_iso()),
    ).fetchone()["balance"]
    cx.execute(
        "INSERT INTO khata_txns (customer, delta, reason, bill_id, at) VALUES (%s,%s,%s,%s,%s)",
        (customer, float(delta), reason, bill_id, now_iso()),
    )
    return balance


def khata_add(customer: str, amount: float, reason: str = "credit sale",
              bill_id: Optional[int] = None) -> dict:
    """Put an amount on a customer's credit ('put ₹500 on Ramesh's credit')."""
    if amount <= 0:
        raise DomainError("BAD_ARGS", "Credit amount must be positive.")
    cust = _normalise(customer)
    with db.tx() as cx:
        balance = _apply(cx, cust, float(amount), reason, bill_id)
    return {"ok": True, "customer": cust, "added": float(amount), "balance": balance}


def khata_settle(customer: str, amount: float, allow_overpay: bool = False) -> dict:
    """Record a repayment ('Ramesh paid ₹300').

    Refuses if the customer has no khata at all, or if the payment exceeds the
    outstanding balance — both are almost always a mistaken name or amount, so
    the agent should confirm rather than quietly create a negative balance.
    """
    if amount <= 0:
        raise DomainError("BAD_ARGS", "Settlement amount must be positive.")
    cust = _normalise(customer)

    with db.tx() as cx:
        # FOR UPDATE: hold the row while we decide, so two settlements racing the
        # same khata can't both pass the overpayment check.
        row = cx.execute(
            "SELECT balance FROM khata WHERE customer = %s FOR UPDATE", (cust,)
        ).fetchone()
        if row is None:
            raise DomainError(
                "NO_SUCH_KHATA",
                f"No khata exists for {cust}. Check the name before recording a payment.",
                {"customer": cust},
            )
        balance = row["balance"]
        if float(amount) > balance and not allow_overpay:
            raise DomainError(
                "OVERPAYMENT",
                f"{cust} owes ₹{balance:.2f} but the payment is ₹{float(amount):.2f}. "
                f"Confirm before recording an advance.",
                {"customer": cust, "balance": balance, "amount": float(amount)},
            )
        new_balance = _apply(cx, cust, -float(amount), "settlement")

    return {"ok": True, "customer": cust, "paid": float(amount), "balance": new_balance}


def khata_balance(customer: str) -> dict:
    cust = _normalise(customer)
    with db.tx() as cx:
        row = cx.execute(
            "SELECT balance, updated_at FROM khata WHERE customer = %s", (cust,)
        ).fetchone()
    if row is None:
        raise DomainError("NO_SUCH_KHATA", f"No khata exists for {cust}.", {"customer": cust})
    return {"customer": cust, "balance": row["balance"], "updated_at": row["updated_at"]}


def khata_statement(customer: str, limit: int = 20) -> dict:
    cust = _normalise(customer)
    with db.tx() as cx:
        row = cx.execute("SELECT balance FROM khata WHERE customer = %s", (cust,)).fetchone()
        if row is None:
            raise DomainError("NO_SUCH_KHATA", f"No khata exists for {cust}.", {"customer": cust})
        txns = cx.execute(
            """SELECT delta, reason, bill_id, at FROM khata_txns
               WHERE customer = %s ORDER BY id DESC LIMIT %s""",
            (cust, limit),
        ).fetchall()
    return {
        "customer": cust,
        "balance": row["balance"],
        "transactions": [row_to_dict(t) for t in txns],
    }


def list_khatas() -> list[dict]:
    """All customers with an outstanding balance — 'who owes me money?'."""
    with db.tx() as cx:
        rows = cx.execute(
            "SELECT customer, balance, updated_at FROM khata "
            "WHERE balance <> 0 ORDER BY balance DESC"
        ).fetchall()
    return [row_to_dict(r) for r in rows]
