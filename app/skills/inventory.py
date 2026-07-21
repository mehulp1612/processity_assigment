"""Inventory tools — thin adapters over ``services.inventory``.

Every rule (valid GST slabs, valid units, MRP-below-cost, ambiguity detection)
lives in the service. These functions only translate arguments and shape results.
"""

from __future__ import annotations

from ._tool import tool

from ..services import inventory as svc
from ._result import call
from .context import Turn


def build_tools(turn: Turn) -> list:
    """Build this chat's tools, closed over its turn context."""

    @tool(
        "find_product",
        "Search the catalogue by the owner's shorthand ('atta', 'amul butter', 'maggi'). "
        "Returns candidate SKUs with id, price, GST slab, unit and current stock. "
        "Use this first whenever the owner names a product loosely. If several "
        "candidates come back and they differ in price or GST slab, ask the owner "
        "which one they mean instead of guessing.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Product name or shorthand."},
                "limit": {"type": "integer", "description": "Max candidates (default 8)."},
            },
            "required": ["query"],
        },
    )
    async def find_product(args: dict) -> dict:
        return await call(svc.find_product, args["query"], args.get("limit", 8))


    @tool(
        "get_stock",
        "Current quantity of one SKU, by product_id or by name. If the name is "
        "ambiguous this returns an AMBIGUOUS_PRODUCT refusal listing the candidates "
        "and their GST slabs — ask the owner which one, then call again with product_id.",
        {
            "type": "object",
            "properties": {
                "product_id": {"type": "integer", "description": "Exact SKU id, if known."},
                "query": {"type": "string", "description": "Product name or shorthand."},
            },
            "required": [],
        },
    )
    async def get_stock(args: dict) -> dict:
        return await call(svc.get_stock, args.get("product_id"), args.get("query"))


    @tool(
        "low_stock",
        "Everything at or below its reorder level — answers 'what's running out?' "
        "and 'what should I reorder?'.",
        {"type": "object", "properties": {}, "required": []},
    )
    async def low_stock(args: dict) -> dict:
        return await call(svc.low_stock)


    @tool(
        "receive_stock",
        "Goods inward: add received quantity to a SKU already in the catalogue, "
        "optionally updating cost price and MRP. Use this for 'received 20 packets "
        "of Tata Salt'. For a product that does not exist yet, use add_product.",
        {
            "type": "object",
            "properties": {
                "product_id": {"type": "integer", "description": "SKU receiving stock."},
                "qty": {"type": "number", "description": "Quantity received, in the SKU's unit."},
                "cost_price": {"type": "number", "description": "New cost price per unit, if it changed."},
                "mrp": {"type": "number", "description": "New selling price per unit, if it changed."},
            },
            "required": ["product_id", "qty"],
        },
    )
    async def receive_stock(args: dict) -> dict:
        return await call(
            svc.receive_stock,
            args["product_id"], args["qty"], args.get("cost_price"), args.get("mrp"),
        )


    @tool(
        "add_product",
        "Create a new SKU in the catalogue. Requires an HSN code and a GST slab "
        "(0, 5, 12, 18 or 28) — if the owner hasn't said which, ask rather than "
        "assuming, because the slab changes the tax on every future sale. Loose "
        "unbranded staples are usually 0%, packaged staples 5%, dairy fat 12%, "
        "other FMCG 18%.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Full SKU name, e.g. 'Aashirvaad Atta 5kg'."},
                "hsn": {"type": "string", "description": "HSN code for the category."},
                "gst_rate": {"type": "number", "description": "GST percent: 0, 5, 12, 18 or 28."},
                "unit": {
                    "type": "string",
                    "description": "One of: kg, g, litre, ml, packet, dozen, piece.",
                },
                "cost_price": {"type": "number", "description": "What the shop pays per unit."},
                "mrp": {"type": "number", "description": "What the customer pays per unit."},
                "is_loose": {"type": "boolean", "description": "True for loose/unpackaged goods sold by weight."},
                "brand": {"type": "string"},
                "variant": {"type": "string"},
                "reorder_level": {"type": "number", "description": "Alert threshold."},
                "opening_qty": {"type": "number", "description": "Stock on hand right now."},
            },
            "required": ["name", "hsn", "gst_rate", "unit", "cost_price", "mrp"],
        },
    )
    async def add_product(args: dict) -> dict:
        return await call(
            svc.add_product,
            name=args["name"], hsn=args["hsn"], gst_rate=args["gst_rate"], unit=args["unit"],
            cost_price=args["cost_price"], mrp=args["mrp"],
            is_loose=args.get("is_loose", False), brand=args.get("brand"),
            variant=args.get("variant"), reorder_level=args.get("reorder_level", 0),
            opening_qty=args.get("opening_qty", 0),
        )

    return [find_product, get_stock, low_stock, receive_stock, add_product]
