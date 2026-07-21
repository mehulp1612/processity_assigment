"""End-to-end smoke run over the services layer — no model, no Telegram.

Reproduces the §3 scenarios against a real database so the numbers can be
checked by hand. Run it against a scratch database:

    docker compose run --rm \
      -e DATABASE_URL=postgresql://postgres:postgres@db:5432/store_test \
      app python -m scripts.smoke
"""

from __future__ import annotations

from app import db
from app.services import billing, inventory, khata
from app.services.common import DomainError
from scripts.seed import seed


def pid(name: str) -> int:
    with db.tx() as cx:
        return cx.execute("SELECT id FROM products WHERE name = %s", (name,)).fetchone()["id"]


def show(bill: dict, title: str) -> None:
    print(f"\n=== {title} ===")
    for ln in bill["lines"]:
        print(f"  {ln['name']:<32}{ln['qty']:>5g} x {ln['unit_price']:>7.2f}"
              f"  GST {ln['gst_rate']:>3g}%  = {ln['total']:>8.2f}")
    print(f"  subtotal {bill['subtotal']:.2f}  CGST {bill['cgst']:.2f}  SGST {bill['sgst']:.2f}"
          f"  round-off {bill['round_off']:+.2f}  TOTAL {bill['total']:.2f}")
    for slab in bill["slabs"]:
        print(f"    slab {slab['gst_rate']:>3g}%  taxable {slab['taxable']:>8.2f}"
              f"  cgst {slab['cgst']:>6.2f}  sgst {slab['sgst']:>6.2f}")


def main() -> None:
    db.wait_for_db()
    seed(reset=True)

    # --- multi-item bill across all four slabs ------------------------------
    b = billing.start_bill(chat_id="smoke")
    billing.add_line(b["bill_id"], pid("Loose Sugar"), 2)
    billing.add_line(b["bill_id"], pid("Aashirvaad Atta 5kg"), 1)
    billing.add_line(b["bill_id"], pid("Maggi 2-Minute Noodles 70g"), 4)
    billing.add_line(b["bill_id"], pid("Amul Butter 100g"), 1)
    show(billing.view_bill(b["bill_id"]), "bill: 2kg sugar, 1 atta, 4 Maggi, 1 butter")

    # --- mid-build edit ------------------------------------------------------
    billing.remove_line(b["bill_id"], pid("Amul Butter 100g"))
    billing.set_line_qty(b["bill_id"], pid("Maggi 2-Minute Noodles 70g"), 6)
    show(billing.view_bill(b["bill_id"]), "edit: drop the butter, make it 6 Maggi")

    # --- oversell guard ------------------------------------------------------
    print("\n=== oversell guard: try 999 Maggi ===")
    try:
        billing.add_line(b["bill_id"], pid("Maggi 2-Minute Noodles 70g"), 999)
    except DomainError as e:
        print(f"  REFUSED [{e.code}] {e.message}")

    # --- finalize + idempotent retry ----------------------------------------
    print("\n=== finalize (UPI) + idempotent retry ===")
    r1 = billing.finalize_bill(b["bill_id"], "upi", payment_ref="UPI/998877",
                               op_key="tg:update:1001")
    r2 = billing.finalize_bill(b["bill_id"], "upi", payment_ref="UPI/998877",
                               op_key="tg:update:1001")
    print(f"  invoice {r1['invoice_no']}  total {r1['total']:.2f}  via {r1['payment']['mode']}")
    print(f"  retry -> replay={r2.get('idempotent_replay')} "
          f"same invoice={r1['invoice_no'] == r2['invoice_no']}")
    print(f"  Maggi stock now: {inventory.get_product(pid('Maggi 2-Minute Noodles 70g'))['qty']:g}"
          f" (decremented once)")

    # --- khata cycle ---------------------------------------------------------
    print("\n=== khata cycle ===")
    b2 = billing.start_bill(chat_id="smoke", customer="Ramesh")
    billing.add_line(b2["bill_id"], pid("Aashirvaad Atta 5kg"), 1)
    r3 = billing.finalize_bill(b2["bill_id"], "khata", customer="Ramesh")
    print(f"  credit sale {r3['invoice_no']} -> Ramesh owes "
          f"{r3['payment']['khata_balance']:.2f}")
    print(f"  +500 credit -> balance {khata.khata_add('Ramesh', 500)['balance']:.2f}")
    print(f"  paid 300     -> balance {khata.khata_settle('Ramesh', 300)['balance']:.2f}")
    try:
        khata.khata_settle("Suresh", 100)
    except DomainError as e:
        print(f"  REFUSED [{e.code}] {e.message}")

    # --- ambiguity, resolved by the model not a branch -----------------------
    print("\n=== ambiguity: 'how much atta is left?' ===")
    try:
        inventory.get_stock(query="atta")
    except DomainError as e:
        cands = ", ".join(f"{c['name']} ({c['gst_rate']:g}%)" for c in e.details["candidates"])
        print(f"  [{e.code}] candidates -> {cands}")

    db.close_pool()


if __name__ == "__main__":
    main()
