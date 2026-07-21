"""Reporting tools — thin adapters over ``services.analytics``.

Dates are shop-local (Asia/Kolkata), not UTC, so "today" means what the owner
means by it. The service owns that conversion; these just pass the string.
"""

from __future__ import annotations

from ..services import analytics as svc
from ._result import call
from ._tool import tool
from .context import Turn


def build_tools(turn: Turn) -> list:
    """Build this chat's tools, closed over its turn context."""

    @tool(
        "daily_close",
        "Close the day's books: number of bills, total takings, GST collected split "
        "into CGST/SGST, how customers paid, credit given and received, gross margin, "
        "and the best-selling items. Use this for 'close the day', 'today's total', "
        "'how much did I sell today'. Dates are the shop's own (Asia/Kolkata), so "
        "'today' means today in the shop, not UTC.",
        {
            "type": "object",
            "properties": {
                "day": {
                    "type": "string",
                    "description": "Shop-local date as YYYY-MM-DD. Omit for today.",
                },
                "top": {"type": "integer", "description": "How many best sellers (default 5)."},
            },
            "required": [],
        },
    )
    async def daily_close(args: dict) -> dict:
        return await call(svc.daily_close, args.get("day"), args.get("top", 5))

    @tool(
        "sales_report",
        "Sales over a date range with a per-day breakdown — the data behind 'how was "
        "this week', 'compare last month', or an analysis deck. Returns totals, a "
        "day-by-day series (including days with no sales), payment mix, per-slab GST, "
        "best sellers and gross margin. Ranges are inclusive and end TODAY unless the "
        "owner clearly means a finished past period — 'this week' includes today, so "
        "omit both dates for it. Defaults to the last 7 days ending today.",
        {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "First day, YYYY-MM-DD (inclusive)."},
                "end": {"type": "string", "description": "Last day, YYYY-MM-DD (inclusive). Omit for today."},
                "top": {"type": "integer", "description": "How many best sellers (default 10)."},
            },
            "required": [],
        },
    )
    async def sales_report(args: dict) -> dict:
        return await call(
            svc.sales_report, args.get("start"), args.get("end"), args.get("top", 10)
        )

    return [daily_close, sales_report]
