"""Billing tools — thin adapters over ``services.billing``.

``chat_id`` and the idempotency key are injected from the turn context, not taken
from the model. See ``skills/context.py`` for why.
"""

from __future__ import annotations

from ._tool import tool

from ..services import billing as svc
from ._result import call
from .context import Turn


def build_tools(turn: Turn) -> list:
    """Build this chat's tools, closed over its turn context."""

    @tool(
        "start_bill",
        "Open a new draft bill for this chat. Nothing is charged and no stock moves "
        "until finalize_bill. Returns a bill_id — carry it through the rest of the "
        "conversation. If a draft is already open, prefer view_bill over starting another.",
        {
            "type": "object",
            "properties": {
                "customer": {
                    "type": "string",
                    "description": "Customer name. Required later if this becomes a khata (credit) sale.",
                },
            },
            "required": [],
        },
    )
    async def start_bill(args: dict) -> dict:
        return await call(svc.start_bill, turn.chat_id, args.get("customer"))


    @tool(
        "view_bill",
        "Show a bill with every line, the per-slab GST breakup and the total. "
        "Omit bill_id to get this chat's currently open draft — useful after a "
        "restart or when resuming 'add two more Maggi to that bill'.",
        {
            "type": "object",
            "properties": {"bill_id": {"type": "integer"}},
            "required": [],
        },
    )
    async def view_bill(args: dict) -> dict:
        if args.get("bill_id") is None:
            return await call(svc.open_draft, turn.chat_id)
        return await call(svc.view_bill, args["bill_id"])

    @tool(
        "recent_bills",
        "List recently finalized bills, newest first, with invoice number, customer, "
        "payment mode, total and shop-local time. Use this to resolve phrases like "
        "'the last bill', 'that bill just now' or 'Ramesh's last purchase' into a "
        "bill_id before viewing it or making its invoice PDF. Never guess a bill_id.",
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "How many to return (default 5, max 20)."},
                "customer": {"type": "string", "description": "Only this customer's bills."},
            },
            "required": [],
        },
    )
    async def recent_bills(args: dict) -> dict:
        return await call(svc.recent_bills, args.get("limit", 5), args.get("customer"))


    @tool(
        "add_line",
        "Add an item to a draft bill, or increase it if already present. Quantity is "
        "in the SKU's own unit (kg for loose goods, packets for packaged). Price and "
        "GST slab are taken from the catalogue — never pass them in. Refuses with "
        "INSUFFICIENT_STOCK if the shop doesn't have enough.",
        {
            "type": "object",
            "properties": {
                "bill_id": {"type": "integer"},
                "product_id": {"type": "integer", "description": "Resolve this with find_product first."},
                "qty": {"type": "number", "description": "Quantity to add, in the SKU's unit."},
            },
            "required": ["bill_id", "product_id", "qty"],
        },
    )
    async def add_line(args: dict) -> dict:
        return await call(svc.add_line, args["bill_id"], args["product_id"], args["qty"])


    @tool(
        "set_line_qty",
        "Set an item's quantity to an exact number ('make it 6 Maggi'). Setting 0 "
        "removes the line. Use this for corrections rather than adding a difference.",
        {
            "type": "object",
            "properties": {
                "bill_id": {"type": "integer"},
                "product_id": {"type": "integer"},
                "qty": {"type": "number", "description": "The new total quantity for this line."},
            },
            "required": ["bill_id", "product_id", "qty"],
        },
    )
    async def set_line_qty(args: dict) -> dict:
        return await call(svc.set_line_qty, args["bill_id"], args["product_id"], args["qty"])


    @tool(
        "remove_line",
        "Drop an item from a draft bill entirely ('drop the butter').",
        {
            "type": "object",
            "properties": {"bill_id": {"type": "integer"}, "product_id": {"type": "integer"}},
            "required": ["bill_id", "product_id"],
        },
    )
    async def remove_line(args: dict) -> dict:
        return await call(svc.remove_line, args["bill_id"], args["product_id"])


    @tool(
        "cancel_bill",
        "Void a draft bill the owner has abandoned. Never touches stock, because a "
        "draft never reserved any. Confirm with the owner before calling this.",
        {
            "type": "object",
            "properties": {"bill_id": {"type": "integer"}},
            "required": ["bill_id"],
        },
    )
    async def cancel_bill(args: dict) -> dict:
        return await call(svc.cancel_bill, args["bill_id"])


    @tool(
        "finalize_bill",
        "Commit the bill: re-check stock, compute GST, decrement inventory, assign an "
        "invoice number and record payment — atomically. This is the only operation "
        "that moves stock or money, so confirm the items and the payment mode with the "
        "owner first.\n"
        "payment_mode 'khata' books it to a customer's credit and needs a customer name.\n"
        "May refuse with INSUFFICIENT_STOCK (someone else sold the stock meanwhile) or "
        "BELOW_COST. For BELOW_COST, tell the owner what is being sold under cost and "
        "how much, and only retry with allow_below_cost once they explicitly confirm.",
        {
            "type": "object",
            "properties": {
                "bill_id": {"type": "integer"},
                "payment_mode": {
                    "type": "string",
                    "enum": ["cash", "upi", "card", "khata"],
                    "description": "How the customer paid.",
                },
                "payment_ref": {
                    "type": "string",
                    "description": "UPI transaction id or card reference, if the owner gave one.",
                },
                "customer": {"type": "string", "description": "Required for khata (credit) sales."},
                "allow_below_cost": {
                    "type": "boolean",
                    "description": "Only true after the owner has explicitly confirmed a loss-making sale.",
                },
            },
            "required": ["bill_id", "payment_mode"],
        },
    )
    async def finalize_bill(args: dict) -> dict:
        return await call(
            svc.finalize_bill,
            bill_id=args["bill_id"],
            payment_mode=args["payment_mode"],
            payment_ref=args.get("payment_ref"),
            customer=args.get("customer"),
            # Injected, never model-supplied: a redelivered Telegram update carries the
            # same key and replays the first result instead of billing twice.
            op_key=turn.key(f"finalize:{args['bill_id']}"),
            allow_below_cost=args.get("allow_below_cost", False),
        )

    return [start_bill, view_bill, recent_bills, add_line, set_line_qty, remove_line,
            cancel_bill, finalize_bill]
