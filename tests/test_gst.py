"""Correctness tests for the GST engine — the money math the whole store trusts.

Expected values are hand-computed in the docstrings so a reviewer can check them
without running the code.
"""

from decimal import Decimal

from app.domain.gst import compute_line, compute_bill
from app.domain.money import paise, rupee, D


def dec(x):
    return Decimal(str(x))


# --- Single line, per slab --------------------------------------------------

def test_zero_percent_loose_item():
    # 2kg loose sugar @ ₹46 = ₹92 taxable, 0% GST.
    ln = compute_line(unit_price=46, qty=2, gst_rate=0, name="Loose Sugar")
    assert ln.taxable == dec("92.00")
    assert ln.gst == dec("0.00")
    assert ln.cgst == dec("0.00") and ln.sgst == dec("0.00")
    assert ln.total == dec("92.00")


def test_five_percent_odd_paise_split():
    # 1 x Aashirvaad Atta @ ₹285, 5% -> gst 14.25; cgst = round(7.125) = 7.13,
    # sgst absorbs remainder = 7.12 so cgst+sgst == 14.25 exactly.
    ln = compute_line(unit_price=285, qty=1, gst_rate=5, name="Aashirvaad Atta 5kg")
    assert ln.taxable == dec("285.00")
    assert ln.gst == dec("14.25")
    assert ln.cgst == dec("7.13")
    assert ln.sgst == dec("7.12")
    assert ln.cgst + ln.sgst == ln.gst
    assert ln.total == dec("299.25")


def test_twelve_percent_item():
    # Amul Butter @ ₹62, 12% -> gst 7.44, split 3.72/3.72.
    ln = compute_line(unit_price=62, qty=1, gst_rate=12, name="Amul Butter 100g")
    assert ln.gst == dec("7.44")
    assert ln.cgst == dec("3.72") and ln.sgst == dec("3.72")
    assert ln.total == dec("69.44")


def test_eighteen_percent_item():
    # 4 x Maggi @ ₹14 = ₹56, 18% -> gst 10.08, split 5.04/5.04.
    ln = compute_line(unit_price=14, qty=4, gst_rate=18, name="Maggi 70g")
    assert ln.taxable == dec("56.00")
    assert ln.gst == dec("10.08")
    assert ln.cgst == dec("5.04") and ln.sgst == dec("5.04")
    assert ln.total == dec("66.08")


def test_cgst_plus_sgst_always_equals_gst():
    # Property: the split never loses or gains a paisa across many rates/qtys.
    for price in (7, 13, 46, 62, 285, 610):
        for qty in (1, 2, 3, 7):
            for rate in (0, 5, 12, 18):
                ln = compute_line(unit_price=price, qty=qty, gst_rate=rate)
                assert ln.cgst + ln.sgst == ln.gst
                assert ln.total == ln.taxable + ln.gst


# --- Full multi-slab bill (the §3 "make a bill" example) --------------------

def test_mixed_slab_bill_totals_and_roundoff():
    # 2kg sugar (0%), 1 Aashirvaad atta (5%), 4 Maggi (18%), 1 Amul butter (12%).
    #   taxable  = 92 + 285 + 56 + 62         = 495.00
    #   cgst     =  0 + 7.13 + 5.04 + 3.72    =  15.89
    #   sgst     =  0 + 7.12 + 5.04 + 3.72    =  15.88
    #   grand    = 495 + 15.89 + 15.88        = 526.77
    #   total    = round(526.77)              = 527
    #   round_off= 527 - 526.77               = +0.23
    bill = compute_bill([
        {"name": "Loose Sugar", "unit_price": 46, "qty": 2, "gst_rate": 0},
        {"name": "Aashirvaad Atta 5kg", "unit_price": 285, "qty": 1, "gst_rate": 5},
        {"name": "Maggi 70g", "unit_price": 14, "qty": 4, "gst_rate": 18},
        {"name": "Amul Butter 100g", "unit_price": 62, "qty": 1, "gst_rate": 12},
    ])
    assert bill.subtotal == dec("495.00")
    assert bill.cgst == dec("15.89")
    assert bill.sgst == dec("15.88")
    assert bill.grand == dec("526.77")
    assert bill.total == dec("527")
    assert bill.round_off == dec("0.23")
    # Grand identity holds.
    assert bill.subtotal + bill.cgst + bill.sgst == bill.grand
    # Payable is a whole rupee.
    assert bill.total == rupee(bill.grand)


def test_slab_breakup_groups_by_rate():
    bill = compute_bill([
        {"name": "Loose Sugar", "unit_price": 46, "qty": 2, "gst_rate": 0},
        {"name": "Aashirvaad Atta 5kg", "unit_price": 285, "qty": 1, "gst_rate": 5},
        {"name": "Maggi 70g", "unit_price": 14, "qty": 4, "gst_rate": 18},
        {"name": "Amul Butter 100g", "unit_price": 62, "qty": 1, "gst_rate": 12},
    ])
    rates = [s.gst_rate for s in bill.slabs]
    assert rates == [dec("0"), dec("5"), dec("12"), dec("18")]  # sorted
    by_rate = {s.gst_rate: s for s in bill.slabs}
    assert by_rate[dec("0")].taxable == dec("92.00")
    assert by_rate[dec("5")].taxable == dec("285.00")
    assert by_rate[dec("5")].cgst == dec("7.13")
    assert by_rate[dec("5")].sgst == dec("7.12")
    assert by_rate[dec("18")].cgst == dec("5.04")
    # Slab CGST/SGST sums reconcile to the bill totals.
    assert sum((s.cgst for s in bill.slabs), Decimal("0")) == bill.cgst
    assert sum((s.sgst for s in bill.slabs), Decimal("0")) == bill.sgst


def test_negative_roundoff_case():
    # Single 5% atta: grand 299.25 -> total 299, round_off -0.25.
    bill = compute_bill([
        {"name": "Aashirvaad Atta 5kg", "unit_price": 285, "qty": 1, "gst_rate": 5},
    ])
    assert bill.grand == dec("299.25")
    assert bill.total == dec("299")
    assert bill.round_off == dec("-0.25")


# --- Money helpers ----------------------------------------------------------

def test_half_up_rounding():
    assert paise("7.125") == dec("7.13")   # half rounds up
    assert paise("7.124") == dec("7.12")
    assert rupee("526.50") == dec("527")   # .50 rounds up
    assert rupee("526.49") == dec("526")


def test_decimal_coercion_avoids_float_drift():
    # 0.1 + 0.2 style float error must not appear.
    assert paise(D("0.1") + D("0.2")) == dec("0.30")
