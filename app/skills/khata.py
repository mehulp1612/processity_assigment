"""Khata (credit ledger) tools — thin adapters over ``services.khata``.

Convention the model must not get backwards: a positive balance means the
customer owes the shop.
"""

from __future__ import annotations

from ._tool import tool

from ..services import khata as svc
from ._result import call
from .context import Turn


def build_tools(turn: Turn) -> list:
    """Build this chat's tools, closed over its turn context."""

    @tool(
        "khata_add",
        "Put an amount onto a customer's credit ledger — they now owe the shop more. "
        "Use this for a standalone credit entry; a credit *sale* should instead go "
        "through finalize_bill with payment_mode 'khata', so the invoice and the "
        "ledger stay in step.",
        {
            "type": "object",
            "properties": {
                "customer": {"type": "string"},
                "amount": {"type": "number", "description": "Rupees to add to what they owe."},
                "reason": {"type": "string"},
            },
            "required": ["customer", "amount"],
        },
    )
    async def khata_add(args: dict) -> dict:
        return await call(
            svc.khata_add, args["customer"], args["amount"], args.get("reason", "credit sale")
        )


    @tool(
        "khata_settle",
        "Record a repayment against a customer's khata ('Ramesh paid 300'). Refuses "
        "with NO_SUCH_KHATA if that customer has no ledger — check the spelling with "
        "the owner rather than creating one. Refuses with OVERPAYMENT if the amount "
        "exceeds the balance; relay the actual balance and confirm before retrying "
        "with allow_overpay.",
        {
            "type": "object",
            "properties": {
                "customer": {"type": "string"},
                "amount": {"type": "number", "description": "Rupees received from the customer."},
                "allow_overpay": {
                    "type": "boolean",
                    "description": "Only true after the owner confirms they really are taking an advance.",
                },
            },
            "required": ["customer", "amount"],
        },
    )
    async def khata_settle(args: dict) -> dict:
        return await call(
            svc.khata_settle, args["customer"], args["amount"], args.get("allow_overpay", False)
        )


    @tool(
        "khata_balance",
        "What one customer currently owes the shop.",
        {
            "type": "object",
            "properties": {"customer": {"type": "string"}},
            "required": ["customer"],
        },
    )
    async def khata_balance(args: dict) -> dict:
        return await call(svc.khata_balance, args["customer"])


    @tool(
        "khata_statement",
        "A customer's recent khata history — every credit and repayment, newest first.",
        {
            "type": "object",
            "properties": {
                "customer": {"type": "string"},
                "limit": {"type": "integer", "description": "How many entries (default 20)."},
            },
            "required": ["customer"],
        },
    )
    async def khata_statement(args: dict) -> dict:
        return await call(svc.khata_statement, args["customer"], args.get("limit", 20))


    @tool(
        "list_khatas",
        "Every customer with an outstanding balance, biggest first — 'who owes me money?'.",
        {"type": "object", "properties": {}, "required": []},
    )
    async def list_khatas(args: dict) -> dict:
        return await call(svc.list_khatas)

    return [khata_add, khata_settle, khata_balance, khata_statement, list_khatas]
