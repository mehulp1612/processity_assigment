"""Document tools — thin adapters over ``services.documents`` and ``services.deck``.

These are the only tools with a side effect outside the database: they write a
file. Rather than trying to deliver it, they record the path on the turn and let
the transport send it — so the document services never learn what Telegram is,
and the same tools work unchanged from the CLI or ``/chat``.
"""

from __future__ import annotations

from ..services import deck as deck_svc
from ..services import documents as doc_svc
from ._result import call
from ._tool import tool
from .context import Turn


def build_tools(turn: Turn) -> list:
    """Build this chat's tools, closed over its turn context."""

    def _attach(result: dict) -> dict:
        """Queue a generated file for delivery, and keep the path out of the reply."""
        path = result.get("path")
        if path:
            turn.attachments.append(path)
            result = {**result, "delivered": True}
            result.pop("path", None)
        return result

    @tool(
        "render_invoice_pdf",
        "Produce a proper GST tax invoice as a PDF for a finalized bill, and send it "
        "to the owner. Includes the shop's GSTIN, HSN codes, per-line CGST/SGST, the "
        "slab-wise tax summary and the amount in words. Only works on a finalized "
        "bill — a draft has no invoice number yet. The file is delivered "
        "automatically, so just tell the owner it's on its way.",
        {
            "type": "object",
            "properties": {
                "bill_id": {"type": "integer", "description": "The finalized bill to invoice."},
            },
            "required": ["bill_id"],
        },
    )
    async def render_invoice_pdf(args: dict) -> dict:
        result = await call(doc_svc.render_invoice_pdf, args["bill_id"])
        return _wrap(result, _attach)

    @tool(
        "build_analysis_pptx",
        "Build a PowerPoint analysis deck for a date range and send it to the owner: "
        "headline numbers, a sales-by-day chart, payment mix, best sellers, GST by "
        "slab, and what to act on. Use this for 'make me a deck', 'analysis of last "
        "week', 'monthly report'. Ranges are inclusive and end TODAY unless the owner "
        "clearly means a finished past period — 'this week' and 'last 7 days' both "
        "include today, so omit both dates for those. The file is delivered automatically.",
        {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "First day, YYYY-MM-DD (inclusive)."},
                "end": {"type": "string", "description": "Last day, YYYY-MM-DD (inclusive). Omit for today."},
            },
            "required": [],
        },
    )
    async def build_analysis_pptx(args: dict) -> dict:
        result = await call(deck_svc.build_analysis_pptx, args.get("start"), args.get("end"))
        return _wrap(result, _attach)

    return [render_invoice_pdf, build_analysis_pptx]


def _wrap(result: dict, attach) -> dict:
    """Apply ``attach`` to a successful tool result, leaving refusals untouched."""
    import json

    payload = json.loads(result["content"][0]["text"])
    if payload.get("ok"):
        payload = attach(payload)
        return {"content": [{"type": "text", "text": json.dumps(payload)}]}
    return result
