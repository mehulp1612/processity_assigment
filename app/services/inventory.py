"""Catalogue and stock rules.

Grounding lives here: prices, GST slabs, HSN codes and quantities are only ever
read from the DB. The agent has no other source for them, so it cannot invent a
product or a price.
"""

from __future__ import annotations

import difflib
from typing import Optional

from .. import db
from .common import DomainError, now_iso, row_to_dict

# Slabs an Indian kirana realistically deals in. Anything else is a data-entry
# mistake and is refused rather than silently stored.
VALID_GST_RATES = {0.0, 5.0, 12.0, 18.0, 28.0}
VALID_UNITS = {"kg", "g", "litre", "ml", "packet", "dozen", "piece"}


# --- Lookup ----------------------------------------------------------------

def _score(query: str, row) -> float:
    """Rank a catalogue row against the owner's shorthand ("atta", "amul butter")."""
    q = query.lower().strip()
    haystack = " ".join(
        str(row[k]) for k in ("name", "brand", "variant") if row[k]
    ).lower()
    # Exact substring is the strongest signal ("maggi" in "Maggi 2-Minute...").
    base = 1.0 if q in haystack else 0.0
    # Every query token present is nearly as strong ("amul butter").
    tokens = [t for t in q.split() if t]
    if tokens and all(t in haystack for t in tokens):
        base = max(base, 0.95)
    # Fall back to fuzzy similarity for typos.
    fuzzy = difflib.SequenceMatcher(None, q, row["name"].lower()).ratio()
    return max(base, fuzzy)


def find_product(query: str, limit: int = 8) -> list[dict]:
    """Return candidate SKUs for a fuzzy query, best first.

    This is a *lookup*, not an intent router. When it returns more than one
    plausible match (e.g. "atta" -> Aashirvaad 5kg vs Loose Wheat Atta, which sit
    in different GST slabs), the agent is expected to ask the owner which one
    rather than guessing.
    """
    with db.tx() as cx:
        rows = cx.execute(
            """SELECT p.*, s.qty FROM products p
               LEFT JOIN stock s ON s.product_id = p.id"""
        ).fetchall()
    scored = [(_score(query, r), r) for r in rows]
    scored = [(sc, r) for sc, r in scored if sc >= 0.45]
    scored.sort(key=lambda t: (-t[0], t[1]["name"]))
    return [{**row_to_dict(r), "match_score": round(sc, 3)} for sc, r in scored[:limit]]


def get_product(product_id: int) -> dict:
    with db.tx() as cx:
        row = cx.execute(
            """SELECT p.*, s.qty FROM products p
               LEFT JOIN stock s ON s.product_id = p.id WHERE p.id = %s""",
            (product_id,),
        ).fetchone()
    if row is None:
        raise DomainError("PRODUCT_NOT_FOUND", f"No product with id {product_id}.")
    return row_to_dict(row)


def get_stock(product_id: Optional[int] = None, query: Optional[str] = None) -> dict:
    """Current quantity for one SKU, resolved by id or by fuzzy name."""
    if product_id is not None:
        p = get_product(product_id)
        return {"product": p, "qty": p["qty"], "unit": p["unit"]}
    if not query:
        raise DomainError("BAD_ARGS", "Provide product_id or query.")
    matches = find_product(query)
    if not matches:
        raise DomainError("PRODUCT_NOT_FOUND", f"Nothing in the catalogue matches '{query}'.")
    if len(matches) > 1 and matches[0]["match_score"] - matches[1]["match_score"] < 0.05:
        raise DomainError(
            "AMBIGUOUS_PRODUCT",
            f"'{query}' matches more than one product — ask which one.",
            {"candidates": [{"id": m["id"], "name": m["name"], "gst_rate": m["gst_rate"], "unit": m["unit"]} for m in matches[:5]]},
        )
    p = matches[0]
    return {"product": p, "qty": p["qty"], "unit": p["unit"]}


def low_stock() -> list[dict]:
    """SKUs at or below their reorder level — 'what's running out?'."""
    with db.tx() as cx:
        rows = cx.execute(
            """SELECT p.*, s.qty FROM products p
               JOIN stock s ON s.product_id = p.id
               WHERE s.qty <= p.reorder_level
               ORDER BY (s.qty - p.reorder_level), p.name"""
        ).fetchall()
    return [row_to_dict(r) for r in rows]


# --- Mutations -------------------------------------------------------------

def add_product(
    name: str,
    hsn: str,
    gst_rate: float,
    unit: str,
    cost_price: float,
    mrp: float,
    is_loose: bool = False,
    brand: Optional[str] = None,
    variant: Optional[str] = None,
    reorder_level: float = 0,
    opening_qty: float = 0,
) -> dict:
    name = name.strip()
    if not name:
        raise DomainError("BAD_ARGS", "Product name is required.")
    if float(gst_rate) not in VALID_GST_RATES:
        raise DomainError(
            "INVALID_GST_RATE",
            f"{gst_rate}% is not a GST slab. Valid slabs: 0, 5, 12, 18, 28.",
        )
    if unit not in VALID_UNITS:
        raise DomainError("INVALID_UNIT", f"'{unit}' is not a known unit.", {"valid": sorted(VALID_UNITS)})
    if cost_price < 0 or mrp < 0:
        raise DomainError("BAD_ARGS", "Prices cannot be negative.")
    if mrp < cost_price:
        # Guardrail: pricing below cost is almost always a typo. Refuse loudly.
        raise DomainError(
            "MRP_BELOW_COST",
            f"MRP ₹{mrp} is below cost ₹{cost_price} for {name}. Confirm the correct prices.",
            {"cost_price": cost_price, "mrp": mrp},
        )

    with db.tx() as cx:
        existing = cx.execute("SELECT id FROM products WHERE name = %s", (name,)).fetchone()
        if existing:
            raise DomainError(
                "PRODUCT_EXISTS",
                f"'{name}' is already in the catalogue. Use receive_stock to add quantity.",
                {"product_id": existing["id"]},
            )
        pid = cx.execute(
            """INSERT INTO products
                 (name, brand, variant, hsn, gst_rate, unit, is_loose,
                  cost_price, mrp, reorder_level)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING id""",
            (name, brand, variant, hsn, float(gst_rate), unit, bool(is_loose),
             float(cost_price), float(mrp), float(reorder_level)),
        ).fetchone()["id"]
        cx.execute("INSERT INTO stock (product_id, qty) VALUES (%s,%s)", (pid, float(opening_qty)))
    return get_product(pid)


def receive_stock(
    product_id: int,
    qty: float,
    cost_price: Optional[float] = None,
    mrp: Optional[float] = None,
) -> dict:
    """Goods inward: increment stock, optionally refreshing cost/MRP."""
    if qty <= 0:
        raise DomainError("BAD_ARGS", "Received quantity must be positive.")

    with db.tx() as cx:
        row = cx.execute("SELECT * FROM products WHERE id = %s", (product_id,)).fetchone()
        if row is None:
            raise DomainError("PRODUCT_NOT_FOUND", f"No product with id {product_id}.")

        new_cost = float(cost_price) if cost_price is not None else row["cost_price"]
        new_mrp = float(mrp) if mrp is not None else row["mrp"]
        if new_mrp < new_cost:
            raise DomainError(
                "MRP_BELOW_COST",
                f"MRP ₹{new_mrp} would be below cost ₹{new_cost} for {row['name']}.",
                {"cost_price": new_cost, "mrp": new_mrp},
            )
        if cost_price is not None or mrp is not None:
            cx.execute(
                "UPDATE products SET cost_price = %s, mrp = %s WHERE id = %s",
                (new_cost, new_mrp, product_id),
            )
        # Atomic increment — safe against a concurrent sale decrementing the row.
        new_qty = cx.execute(
            """INSERT INTO stock (product_id, qty) VALUES (%s, %s)
               ON CONFLICT (product_id) DO UPDATE SET qty = stock.qty + excluded.qty
               RETURNING qty""",
            (product_id, float(qty)),
        ).fetchone()["qty"]

    return {"ok": True, "product_id": product_id, "name": row["name"],
            "received": float(qty), "qty": new_qty,
            "cost_price": new_cost, "mrp": new_mrp}
