"""Seed the store with a realistic Indian kirana catalogue and opening stock.

Run:  python -m scripts.seed          (keeps existing data, upserts catalogue)
      python -m scripts.seed --reset  (wipes the DB first)

GST slabs used here are the real staple/FMCG pattern the assignment asks for:
  * loose atta / rice / dal / fresh -> 0% (unbranded, unpackaged)
  * salt                            -> 0%
  * packaged staples (branded atta, oil, sugar, tea, packaged rice) -> 5%
  * dairy fat (butter, ghee)        -> 12%
  * FMCG (biscuits, noodles, chocolate, detergent, toothpaste) -> 18%
HSN codes are the standard chapter codes for each category.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from app import db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# name, brand, variant, hsn, gst_rate, unit, is_loose, cost, mrp, reorder, opening_qty
CATALOGUE = [
    # --- Packaged staples @ 5% -------------------------------------------------
    ("Aashirvaad Atta 5kg", "Aashirvaad", "Whole Wheat", "1101", 5, "packet", 0, 245, 285, 10, 40),
    ("Fortune Sunflower Oil 1L", "Fortune", "Sunflower", "1512", 5, "litre", 0, 118, 140, 8, 30),
    ("Tata Sampann Toor Dal 1kg", "Tata Sampann", "Toor/Arhar", "0713", 5, "packet", 0, 128, 155, 8, 25),
    ("India Gate Basmati Rice 1kg", "India Gate", "Basmati", "1006", 5, "packet", 0, 92, 120, 6, 20),
    ("Red Label Tea 250g", "Brooke Bond", "Red Label", "0902", 5, "packet", 0, 118, 140, 6, 24),

    # --- Salt @ 0% -------------------------------------------------------------
    ("Tata Salt 1kg", "Tata", "Iodised", "2501", 0, "packet", 0, 22, 28, 12, 50),

    # --- Dairy fat @ 12% -------------------------------------------------------
    ("Amul Butter 100g", "Amul", "Salted", "0405", 12, "packet", 0, 54, 62, 10, 36),
    ("Amul Ghee 1L", "Amul", "Pure Ghee", "0405", 12, "litre", 0, 540, 610, 5, 15),

    # --- FMCG @ 18% ------------------------------------------------------------
    ("Maggi 2-Minute Noodles 70g", "Nestle", "Masala", "1902", 18, "packet", 0, 12, 14, 24, 120),
    ("Parle-G Biscuits 100g", "Parle", "Glucose", "1905", 18, "packet", 0, 8, 10, 30, 150),
    ("Surf Excel 1kg", "Surf Excel", "Detergent", "3402", 18, "packet", 0, 118, 145, 8, 30),
    ("Colgate Toothpaste 100g", "Colgate", "MaxFresh", "3306", 18, "packet", 0, 52, 65, 8, 24),
    ("Dairy Milk 50g", "Cadbury", "Chocolate", "1806", 18, "packet", 0, 38, 45, 12, 40),

    # --- Loose staples @ 0% (sold by the kg) -----------------------------------
    ("Loose Sugar", None, "Loose", "1701", 0, "kg", 1, 40, 46, 15, 60),
    ("Loose Wheat Atta", None, "Loose", "1101", 0, "kg", 1, 32, 38, 15, 50),
    ("Loose Sona Masoori Rice", None, "Loose", "1006", 0, "kg", 1, 48, 58, 15, 45),
    ("Loose Toor Dal", None, "Loose", "0713", 0, "kg", 1, 110, 130, 10, 30),
]

SHOP = {
    "name": "Shri Balaji Kirana Store",
    "gstin": "27ABCDE1234F1Z5",   # 27 = Maharashtra
    "address": "Shop 12, Gandhi Market, Pune, Maharashtra 411001",
    "state_code": "27",
    "phone": "+91 98765 43210",
}


def seed(reset: bool = False) -> None:
    db.wait_for_db()
    if reset:
        db.reset_db()
    else:
        db.init_db()

    with db.tx() as cx:
        # Shop identity (single row).
        cx.execute(
            """INSERT INTO shop (id, name, gstin, address, state_code, phone)
               VALUES (1, %(name)s, %(gstin)s, %(address)s, %(state_code)s, %(phone)s)
               ON CONFLICT (id) DO UPDATE SET
                 name=excluded.name, gstin=excluded.gstin, address=excluded.address,
                 state_code=excluded.state_code, phone=excluded.phone""",
            SHOP,
        )

        for (name, brand, variant, hsn, gst, unit, loose, cost, mrp, reorder, qty) in CATALOGUE:
            pid = cx.execute(
                """INSERT INTO products
                     (name, brand, variant, hsn, gst_rate, unit, is_loose,
                      cost_price, mrp, reorder_level)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (name) DO UPDATE SET
                     brand=excluded.brand, variant=excluded.variant, hsn=excluded.hsn,
                     gst_rate=excluded.gst_rate, unit=excluded.unit, is_loose=excluded.is_loose,
                     cost_price=excluded.cost_price, mrp=excluded.mrp,
                     reorder_level=excluded.reorder_level
                   RETURNING id""",
                (name, brand, variant, hsn, gst, unit, bool(loose), cost, mrp, reorder),
            ).fetchone()["id"]
            cx.execute(
                """INSERT INTO stock (product_id, qty) VALUES (%s, %s)
                   ON CONFLICT (product_id) DO UPDATE SET qty=excluded.qty""",
                (pid, qty),
            )

        n = cx.execute("SELECT COUNT(*) AS c FROM products").fetchone()["c"]
    print(f"Seeded {SHOP['name']} with {n} products at {db.database_url()}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="wipe the DB before seeding")
    args = ap.parse_args()
    seed(reset=args.reset)
    db.close_pool()
