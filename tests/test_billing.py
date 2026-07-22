"""The hard parts, proven: oversell, idempotency, concurrency, guardrails.

These tests exercise the *service* layer directly — the same code the agent's
tools call. That is the point: the invariants hold regardless of what the model
says or how it phrases a request.
"""

import threading

import pytest

from app import db
from app.services import billing, inventory, khata
from app.services.common import DomainError


# --- Multi-turn bill building & edits ---------------------------------------

def test_multi_turn_bill_with_edits(pid):
    """'2kg sugar, 1 atta, 4 Maggi, 1 butter' then 'drop the butter, make it 6 Maggi'."""
    bill = billing.start_bill(chat_id="c1")
    bid = bill["bill_id"]

    billing.add_line(bid, pid("Loose Sugar"), 2)
    billing.add_line(bid, pid("Aashirvaad Atta 5kg"), 1)
    billing.add_line(bid, pid("Maggi 2-Minute Noodles 70g"), 4)
    view = billing.add_line(bid, pid("Amul Butter 100g"), 1)
    assert len(view["lines"]) == 4
    assert view["total"] == 527.0  # matches the hand-computed GST test

    # The edit turn.
    billing.remove_line(bid, pid("Amul Butter 100g"))
    view = billing.set_line_qty(bid, pid("Maggi 2-Minute Noodles 70g"), 6)

    names = {ln["name"] for ln in view["lines"]}
    assert "Amul Butter 100g" not in names
    maggi = next(ln for ln in view["lines"] if "Maggi" in ln["name"])
    assert maggi["qty"] == 6

    # Sugar 92 + atta 285 + maggi 6*14=84 -> taxable 461
    assert view["subtotal"] == 461.0


def test_draft_does_not_touch_stock(pid):
    """Stock must not move until finalize."""
    p = pid("Maggi 2-Minute Noodles 70g")
    before = inventory.get_product(p)["qty"]

    bill = billing.start_bill(chat_id="c1")
    billing.add_line(bill["bill_id"], p, 10)

    assert inventory.get_product(p)["qty"] == before  # unchanged

    billing.finalize_bill(bill["bill_id"], "cash")
    assert inventory.get_product(p)["qty"] == before - 10


def test_draft_survives_restart(pid):
    """A half-built bill is a DB row, so a process restart doesn't lose it."""
    bill = billing.start_bill(chat_id="c9")
    billing.add_line(bill["bill_id"], pid("Tata Salt 1kg"), 3)

    # Simulate a restart: throw away every pooled connection.
    db.close_pool()

    resumed = billing.open_draft("c9")
    assert resumed is not None
    assert resumed["bill_id"] == bill["bill_id"]
    assert resumed["lines"][0]["qty"] == 3


# --- Oversell guard ---------------------------------------------------------

def test_oversell_refused_at_add_time(pid):
    """Billing 10 when 6 are in stock is refused."""
    p = pid("Amul Ghee 1L")  # opening qty 15
    with db.tx() as cx:
        cx.execute("UPDATE stock SET qty = 6 WHERE product_id = %s", (p,))

    bill = billing.start_bill(chat_id="c1")
    with pytest.raises(DomainError) as e:
        billing.add_line(bill["bill_id"], p, 10)

    assert e.value.code == "INSUFFICIENT_STOCK"
    assert e.value.details["available"] == 6
    assert e.value.details["requested"] == 10


def test_oversell_refused_at_finalize_when_stock_drops_mid_bill(pid):
    """Stock passing at add-time but gone by finalize must still be refused."""
    p = pid("Amul Ghee 1L")
    bill = billing.start_bill(chat_id="c1")
    billing.add_line(bill["bill_id"], p, 10)  # fine: 15 in stock

    # Someone else sells it in the meantime.
    with db.tx() as cx:
        cx.execute("UPDATE stock SET qty = 4 WHERE product_id = %s", (p,))

    with pytest.raises(DomainError) as e:
        billing.finalize_bill(bill["bill_id"], "cash")
    assert e.value.code == "INSUFFICIENT_STOCK"

    # Nothing was billed and stock is untouched by the failed attempt.
    assert inventory.get_product(p)["qty"] == 4
    assert billing.view_bill(bill["bill_id"])["status"] == "draft"


def test_stock_never_goes_negative(pid):
    p = pid("Tata Salt 1kg")
    bill = billing.start_bill(chat_id="c1")
    billing.add_line(bill["bill_id"], p, 50)  # exactly all of it
    billing.finalize_bill(bill["bill_id"], "cash")
    assert inventory.get_product(p)["qty"] == 0

    b2 = billing.start_bill(chat_id="c1")
    with pytest.raises(DomainError):
        billing.add_line(b2["bill_id"], p, 1)


# --- Idempotency ------------------------------------------------------------

def test_finalize_is_idempotent_on_op_key(pid):
    """Telegram redelivers the same update: must not double-bill or double-decrement."""
    p = pid("Parle-G Biscuits 100g")
    before = inventory.get_product(p)["qty"]

    bill = billing.start_bill(chat_id="c1")
    billing.add_line(bill["bill_id"], p, 5)

    first = billing.finalize_bill(bill["bill_id"], "upi", op_key="tg:update:4242")
    replay = billing.finalize_bill(bill["bill_id"], "upi", op_key="tg:update:4242")

    assert replay.get("idempotent_replay") is True
    assert replay["invoice_no"] == first["invoice_no"]
    assert replay["total"] == first["total"]
    # Stock decremented exactly once.
    assert inventory.get_product(p)["qty"] == before - 5
    # Exactly one payment row.
    with db.tx() as cx:
        n = cx.execute(
            "SELECT COUNT(*) AS c FROM payments WHERE bill_id = %s", (bill["bill_id"],)
        ).fetchone()["c"]
    assert n == 1


def test_refinalize_without_op_key_is_still_refused(pid):
    """Belt and braces: a retry that lost its key can't re-bill either."""
    bill = billing.start_bill(chat_id="c1")
    billing.add_line(bill["bill_id"], pid("Parle-G Biscuits 100g"), 2)
    billing.finalize_bill(bill["bill_id"], "cash")

    with pytest.raises(DomainError) as e:
        billing.finalize_bill(bill["bill_id"], "cash")
    assert e.value.code == "BILL_ALREADY_FINALIZED"


# --- Concurrency ------------------------------------------------------------

def test_concurrent_finalizes_cannot_oversell(pid):
    """Two bills for the same scarce stock, finalized at once: exactly one wins."""
    p = pid("Amul Ghee 1L")
    with db.tx() as cx:
        cx.execute("UPDATE stock SET qty = 10 WHERE product_id = %s", (p,))

    b1 = billing.start_bill(chat_id="c1")
    b2 = billing.start_bill(chat_id="c2")
    billing.add_line(b1["bill_id"], p, 7)
    billing.add_line(b2["bill_id"], p, 7)  # together 14 > 10

    results, errors = [], []
    barrier = threading.Barrier(2)

    def run(bill_id):
        # Each thread borrows its own connection from the pool.
        barrier.wait()
        try:
            results.append(billing.finalize_bill(bill_id, "cash"))
        except DomainError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(b["bill_id"],)) for b in (b1, b2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 1, "exactly one bill should succeed"
    assert len(errors) == 1
    assert errors[0].code in {"INSUFFICIENT_STOCK", "STOCK_RACE"}
    # Stock reflects exactly one sale and is never negative.
    assert inventory.get_product(p)["qty"] == 3


def test_concurrent_sale_and_stock_in_do_not_corrupt(pid):
    """A sale racing goods-inward must leave a consistent quantity."""
    p = pid("Surf Excel 1kg")  # opening 30
    bill = billing.start_bill(chat_id="c1")
    billing.add_line(bill["bill_id"], p, 10)

    barrier = threading.Barrier(2)
    errs = []

    def sell():
        barrier.wait()
        try:
            billing.finalize_bill(bill["bill_id"], "cash")
        except DomainError as e:
            errs.append(e)

    def receive():
        barrier.wait()
        try:
            inventory.receive_stock(p, 25)
        except DomainError as e:
            errs.append(e)

    ts = [threading.Thread(target=sell), threading.Thread(target=receive)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert not errs, f"both operations should succeed: {errs}"
    # 30 - 10 + 25 = 45 regardless of interleaving.
    assert inventory.get_product(p)["qty"] == 45


# --- Guardrails -------------------------------------------------------------

def test_below_cost_sale_refused_then_allowed_on_confirmation(pid):
    p = pid("Amul Butter 100g")  # cost 54, mrp 62
    bill = billing.start_bill(chat_id="c1")
    billing.add_line(bill["bill_id"], p, 1)
    # Owner discounts below cost.
    with db.tx() as cx:
        cx.execute("UPDATE bill_lines SET unit_price = 50 WHERE bill_id = %s", (bill["bill_id"],))

    with pytest.raises(DomainError) as e:
        billing.finalize_bill(bill["bill_id"], "cash")
    assert e.value.code == "BELOW_COST"
    assert e.value.details["items"][0]["cost_price"] == 54

    # Explicit confirmation lets it through.
    res = billing.finalize_bill(bill["bill_id"], "cash", allow_below_cost=True)
    assert res["ok"] is True


def test_empty_bill_cannot_be_finalized():
    bill = billing.start_bill(chat_id="c1")
    with pytest.raises(DomainError) as e:
        billing.finalize_bill(bill["bill_id"], "cash")
    assert e.value.code == "EMPTY_BILL"


def test_invalid_payment_mode_refused(pid):
    bill = billing.start_bill(chat_id="c1")
    billing.add_line(bill["bill_id"], pid("Tata Salt 1kg"), 1)
    with pytest.raises(DomainError) as e:
        billing.finalize_bill(bill["bill_id"], "bitcoin")
    assert e.value.code == "INVALID_PAYMENT_MODE"


def test_finalized_bill_cannot_be_edited(pid):
    bill = billing.start_bill(chat_id="c1")
    billing.add_line(bill["bill_id"], pid("Tata Salt 1kg"), 1)
    billing.finalize_bill(bill["bill_id"], "cash")
    with pytest.raises(DomainError) as e:
        billing.add_line(bill["bill_id"], pid("Parle-G Biscuits 100g"), 1)
    assert e.value.code == "BILL_NOT_DRAFT"


# --- Khata cycle ------------------------------------------------------------

def test_khata_credit_sale_debits_customer(pid):
    bill = billing.start_bill(chat_id="c1", customer="ramesh")
    billing.add_line(bill["bill_id"], pid("Aashirvaad Atta 5kg"), 1)
    res = billing.finalize_bill(bill["bill_id"], "khata", customer="ramesh")

    assert res["payment"]["mode"] == "khata"
    assert khata.khata_balance("Ramesh")["balance"] == res["total"]


def test_khata_settle_and_refusals():
    khata.khata_add("Ramesh", 500)
    assert khata.khata_balance("ramesh")["balance"] == 500

    khata.khata_settle("Ramesh", 300)
    assert khata.khata_balance("Ramesh")["balance"] == 200

    # Unknown customer.
    with pytest.raises(DomainError) as e:
        khata.khata_settle("Suresh", 100)
    assert e.value.code == "NO_SUCH_KHATA"

    # Overpayment needs confirmation.
    with pytest.raises(DomainError) as e:
        khata.khata_settle("Ramesh", 999)
    assert e.value.code == "OVERPAYMENT"
    assert e.value.details["balance"] == 200

    khata.khata_settle("Ramesh", 999, allow_overpay=True)
    assert khata.khata_balance("Ramesh")["balance"] == 200 - 999


def test_khata_sale_needs_customer(pid):
    bill = billing.start_bill(chat_id="c1")
    billing.add_line(bill["bill_id"], pid("Tata Salt 1kg"), 1)
    with pytest.raises(DomainError) as e:
        billing.finalize_bill(bill["bill_id"], "khata")
    assert e.value.code == "CUSTOMER_REQUIRED"


# --- Inventory --------------------------------------------------------------

def test_ambiguous_product_surfaces_candidates():
    """'atta' matches both a 5% packaged SKU and a 0% loose one -> agent must ask."""
    with pytest.raises(DomainError) as e:
        inventory.get_stock(query="atta")
    assert e.value.code == "AMBIGUOUS_PRODUCT"
    names = {c["name"] for c in e.value.details["candidates"]}
    assert "Aashirvaad Atta 5kg" in names
    assert "Loose Wheat Atta" in names


def test_find_product_ranks_exact_brand_match():
    matches = inventory.find_product("amul butter")
    assert matches[0]["name"] == "Amul Butter 100g"


def test_receive_stock_increments_and_updates_prices(pid):
    p = pid("Maggi 2-Minute Noodles 70g")
    before = inventory.get_product(p)["qty"]
    res = inventory.receive_stock(p, 50, cost_price=12, mrp=15)
    assert res["qty"] == before + 50
    assert inventory.get_product(p)["mrp"] == 15


def test_mrp_below_cost_refused(pid):
    with pytest.raises(DomainError) as e:
        inventory.receive_stock(pid("Tata Salt 1kg"), 10, cost_price=30, mrp=25)
    assert e.value.code == "MRP_BELOW_COST"


def test_add_product_validates_gst_slab():
    with pytest.raises(DomainError) as e:
        inventory.add_product(name="Weird Item", hsn="1234", gst_rate=7,
                              unit="packet", cost_price=10, mrp=12)
    assert e.value.code == "INVALID_GST_RATE"


def test_low_stock_lists_items_at_reorder_level(pid):
    p = pid("Amul Ghee 1L")  # reorder_level 5
    with db.tx() as cx:
        cx.execute("UPDATE stock SET qty = 3 WHERE product_id = %s", (p,))
    names = {r["name"] for r in inventory.low_stock()}
    assert "Amul Ghee 1L" in names


def test_invoice_numbers_are_sequential_and_fy_scoped(pid):
    nos = []
    for _ in range(3):
        b = billing.start_bill(chat_id="c1")
        billing.add_line(b["bill_id"], pid("Parle-G Biscuits 100g"), 1)
        nos.append(billing.finalize_bill(b["bill_id"], "cash")["invoice_no"])
    seq = [int(n.split("/")[-1]) for n in nos]
    assert seq == [1, 2, 3]
    assert all(n.startswith("INV/") for n in nos)


# --- recent_bills: turning "the last bill" into a bill_id --------------------

def test_recent_bills_returns_finalized_newest_first(pid):
    made = []
    for name in ("Tata Salt 1kg", "Parle-G Biscuits 100g", "Amul Butter 100g"):
        b = billing.start_bill(chat_id="c1")
        billing.add_line(b["bill_id"], pid(name), 1)
        made.append(billing.finalize_bill(b["bill_id"], "cash")["invoice_no"])

    out = billing.recent_bills()
    assert out["count"] == 3
    # Newest first is the whole point: recent_bills()[0] must be "the last bill".
    assert [b["invoice_no"] for b in out["bills"]] == list(reversed(made))
    assert out["bills"][0]["payment_mode"] == "cash"


def test_recent_bills_excludes_drafts(pid):
    b = billing.start_bill(chat_id="c1")
    billing.add_line(b["bill_id"], pid("Tata Salt 1kg"), 1)

    out = billing.recent_bills()
    assert out["count"] == 0, "a draft has no invoice, so it is not a past bill"


def test_recent_bills_filters_by_customer(pid):
    for who in ("Ramesh", "Suresh", "Ramesh"):
        b = billing.start_bill(chat_id="c1", customer=who)
        billing.add_line(b["bill_id"], pid("Tata Salt 1kg"), 1)
        billing.finalize_bill(b["bill_id"], "khata", customer=who)

    out = billing.recent_bills(customer="ramesh")   # case-insensitive
    assert out["count"] == 2
    assert {b["customer"] for b in out["bills"]} == {"Ramesh"}


def test_recent_bills_reports_shop_local_time(pid):
    b = billing.start_bill(chat_id="c1")
    billing.add_line(b["bill_id"], pid("Tata Salt 1kg"), 1)
    billing.finalize_bill(b["bill_id"], "cash")

    stamp = billing.recent_bills()["bills"][0]["finalized_at"]
    # "YYYY-MM-DD HH:MM" in shop time, not a UTC ISO string the model would misread.
    assert len(stamp) == 16 and stamp[10] == " " and "T" not in stamp


def test_recent_bills_limit_is_clamped(pid):
    for _ in range(3):
        b = billing.start_bill(chat_id="c1")
        billing.add_line(b["bill_id"], pid("Tata Salt 1kg"), 1)
        billing.finalize_bill(b["bill_id"], "cash")

    assert billing.recent_bills(limit=2)["count"] == 2
    assert billing.recent_bills(limit=999)["count"] == 3   # clamped, not an error
    assert billing.recent_bills(limit=0)["count"] == 1     # floor of 1
