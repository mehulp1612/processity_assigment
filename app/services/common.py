"""Shared plumbing for the services layer."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional

from psycopg.types.json import Jsonb


class DomainError(Exception):
    """A refusal the agent should relay to the owner, not retry blindly.

    Carries a machine-readable ``code`` plus ``details`` so the model can explain
    precisely what went wrong (e.g. exact shortfall per item) and ask the right
    follow-up question.
    """

    def __init__(self, code: str, message: str, details: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"ok": False, "error": self.code, "message": self.message, **({"details": self.details} if self.details else {})}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def financial_year(when: Optional[date] = None) -> str:
    """Indian FY runs 1 Apr - 31 Mar. 2026-05-01 -> '2026-27'; 2026-02-01 -> '2025-26'."""
    d = when or datetime.now(timezone.utc).date()
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


def next_invoice_no(cx, when: Optional[date] = None) -> str:
    """Allocate the next FY-scoped invoice number. Must be called inside a tx."""
    fy = financial_year(when)
    cx.execute(
        "INSERT INTO invoice_seq (fy, last) VALUES (%s, 0) ON CONFLICT (fy) DO NOTHING", (fy,)
    )
    # UPDATE ... RETURNING takes the row lock and reads the bumped value in one
    # statement, so two concurrent finalizes can never share an invoice number.
    n = cx.execute(
        "UPDATE invoice_seq SET last = last + 1 WHERE fy = %s RETURNING last", (fy,)
    ).fetchone()["last"]
    return f"INV/{fy}/{n:04d}"


# --- Idempotency -----------------------------------------------------------
# Telegram redelivers updates on network hiccups. Any tool that moves money or
# stock takes an op_key; the first successful run records its result, and a
# retry with the same key replays that result instead of acting again.

def cached_op(cx, op_key: str) -> Optional[dict]:
    row = cx.execute(
        "SELECT result FROM processed_ops WHERE op_key = %s", (op_key,)
    ).fetchone()
    return None if row is None else row["result"]   # JSONB comes back decoded


def record_op(cx, op_key: str, result: dict) -> None:
    cx.execute(
        """INSERT INTO processed_ops (op_key, result, at) VALUES (%s,%s,%s)
           ON CONFLICT (op_key) DO NOTHING""",
        (op_key, Jsonb(result), now_iso()),
    )


def row_to_dict(row) -> dict[str, Any]:
    """Rows already arrive as dicts (see ``db._numeric_dict_row``); copy defensively."""
    return dict(row)
