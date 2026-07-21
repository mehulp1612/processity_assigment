"""Reporting correctness.

The interesting cases are the ones a naive implementation gets wrong: a day that
is the shop's day rather than UTC's, margin that excludes the tax the shop is only
collecting on the government's behalf, credit sales that write no payment row, and
quiet days that must still appear in a chart series.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import db
from app.services import analytics, billing, inventory, khata
from app.services.common import DomainError

IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")


def sell(pid_, qty, mode="cash", customer=None, when=None) -> dict:
    """Finalize a one-item bill, optionally backdated to a shop-local moment."""
    bill = billing.start_bill(chat_id="rep", customer=customer)
    billing.add_line(bill["bill_id"], pid_, qty)
    out = billing.finalize_bill(bill["bill_id"], mode, customer=customer)
    if when is not None:
        with db.tx() as cx:
            cx.execute(
                "UPDATE bills SET finalized_at = %s WHERE id = %s",
                (when.astimezone(UTC).isoformat(), bill["bill_id"]),
            )
    return out


# --- Shop-local days --------------------------------------------------------

def test_a_late_evening_sale_belongs_to_the_day_the_shop_had_it(pid):
    """21:00 IST is 15:30 UTC the same day — trivially fine. The real trap is the
    other side of midnight, covered below; this pins the ordinary case."""
    day = datetime(2026, 7, 21, 21, 0, tzinfo=IST)
    sell(pid("Tata Salt 1kg"), 2, when=day)

    assert analytics.daily_close("2026-07-21")["bills"] == 1
    assert analytics.daily_close("2026-07-22")["bills"] == 0


def test_after_midnight_shop_time_is_a_new_day_even_though_utc_disagrees(pid):
    """00:30 IST on the 22nd is 19:00 UTC on the 21st.

    Closing the books on UTC dates would file this sale under the 21st and leave
    the owner's till short. It belongs to the 22nd, which is when it happened.
    """
    after_midnight = datetime(2026, 7, 22, 0, 30, tzinfo=IST)
    assert after_midnight.astimezone(UTC).date().isoformat() == "2026-07-21"

    sell(pid("Tata Salt 1kg"), 3, when=after_midnight)

    assert analytics.daily_close("2026-07-21")["bills"] == 0
    assert analytics.daily_close("2026-07-22")["bills"] == 1


def test_bad_date_is_refused_not_guessed():
    with pytest.raises(DomainError) as e:
        analytics.daily_close("yesterday")
    assert e.value.code == "BAD_ARGS"


# --- Daily close ------------------------------------------------------------

def test_daily_close_totals_and_tax_match_the_bills(pid):
    day = datetime(2026, 7, 21, 11, 0, tzinfo=IST)
    sell(pid("Maggi 2-Minute Noodles 70g"), 4, when=day)     # 56.00 @18% -> 66.08
    sell(pid("Loose Sugar"), 2, when=day)                    # 92.00 @0%  -> 92.00

    close = analytics.daily_close("2026-07-21")
    assert close["bills"] == 2
    assert close["subtotal"] == 148.00
    assert close["cgst"] == 5.04 and close["sgst"] == 5.04
    assert close["gst"] == 10.08
    # 66.08 -> 66, 92.00 -> 92
    assert close["total"] == 158.00
    assert close["timezone"] == "Asia/Kolkata"


def test_credit_sales_are_counted_even_though_they_write_no_payment_row(pid):
    """A khata sale never touches `payments`. Reading the mix from there would
    make the day's takings look smaller than the day's sales."""
    day = datetime(2026, 7, 21, 12, 0, tzinfo=IST)
    sell(pid("Tata Salt 1kg"), 2, mode="cash", when=day)
    sell(pid("Aashirvaad Atta 5kg"), 1, mode="khata", customer="Ramesh", when=day)

    close = analytics.daily_close("2026-07-21")
    modes = {m["mode"]: m["amount"] for m in close["payment_mix"]}
    assert modes["cash"] == 56.00
    assert modes["khata"] == 299.00
    assert close["cash_in_hand"] == 56.00        # only cash is actually in the till
    assert close["credit_sales"] == 299.00
    assert close["total"] == 355.00


def test_khata_movement_separates_credit_given_from_money_received(pid):
    day = datetime(2026, 7, 21, 12, 0, tzinfo=IST)
    sell(pid("Aashirvaad Atta 5kg"), 1, mode="khata", customer="Ramesh", when=day)
    khata.khata_settle("Ramesh", 100)

    close = analytics.daily_close(analytics.today())
    assert close["khata"]["received"] == 100.0
    assert close["khata"]["outstanding_total"] == 199.0


def test_top_items_rank_by_revenue(pid):
    day = datetime(2026, 7, 21, 12, 0, tzinfo=IST)
    sell(pid("Amul Ghee 1L"), 2, when=day)          # 610 x 2 = 1220 taxable
    sell(pid("Parle-G Biscuits 100g"), 5, when=day) # 10 x 5  = 50

    top = analytics.daily_close("2026-07-21")["top_items"]
    assert [t["name"] for t in top][0] == "Amul Ghee 1L"
    assert top[0]["revenue"] == 1220.0


# --- Margin -----------------------------------------------------------------

def test_margin_excludes_gst_because_it_is_not_the_shops_money(pid):
    """Amul Butter: cost 54, MRP 62, 12% GST.

    Selling 10 earns 620 - 540 = 80 of margin. Counting the 74.40 of GST as
    revenue would report 154.40 — nearly double, and wrong.
    """
    day = datetime(2026, 7, 21, 12, 0, tzinfo=IST)
    sell(pid("Amul Butter 100g"), 10, when=day)

    close = analytics.daily_close("2026-07-21")
    assert close["revenue_ex_gst"] == 620.00
    assert close["cost_of_goods"] == 540.00
    assert close["gross_margin"] == 80.00
    assert close["margin_pct"] == pytest.approx(12.90, abs=0.01)
    # The bill total does include GST — that's the customer's payment, not profit.
    assert close["total"] == 694.00


# --- Range report -----------------------------------------------------------

def test_sales_report_series_includes_quiet_days(pid):
    """A gap in a bar chart should read as a day with no sales, not missing data."""
    sell(pid("Tata Salt 1kg"), 1, when=datetime(2026, 7, 20, 10, 0, tzinfo=IST))
    sell(pid("Tata Salt 1kg"), 1, when=datetime(2026, 7, 22, 10, 0, tzinfo=IST))

    report = analytics.sales_report("2026-07-20", "2026-07-22")
    days = [d["day"] for d in report["by_day"]]
    assert days == ["2026-07-20", "2026-07-21", "2026-07-22"]
    assert [d["bills"] for d in report["by_day"]] == [1, 0, 1]
    assert report["bills"] == 2


def test_sales_report_defaults_to_the_last_seven_days():
    report = analytics.sales_report()
    assert len(report["by_day"]) == 7
    assert report["end"] == analytics.today()


def test_sales_report_refuses_a_backwards_range():
    with pytest.raises(DomainError) as e:
        analytics.sales_report("2026-07-22", "2026-07-20")
    assert e.value.code == "BAD_ARGS"


def test_slab_breakup_groups_by_rate(pid):
    day = datetime(2026, 7, 21, 12, 0, tzinfo=IST)
    sell(pid("Loose Sugar"), 1, when=day)                     # 0%
    sell(pid("Amul Butter 100g"), 1, when=day)                # 12%
    sell(pid("Maggi 2-Minute Noodles 70g"), 1, when=day)      # 18%

    slabs = analytics.sales_report("2026-07-21", "2026-07-21")["slabs"]
    assert [s["gst_rate"] for s in slabs] == [0.0, 12.0, 18.0]
    by_rate = {s["gst_rate"]: s for s in slabs}
    assert by_rate[12.0]["cgst"] == 3.72 and by_rate[12.0]["sgst"] == 3.72


def test_draft_bills_are_never_reported(pid):
    """An abandoned draft is not a sale."""
    bill = billing.start_bill(chat_id="rep")
    billing.add_line(bill["bill_id"], pid("Tata Salt 1kg"), 5)

    assert analytics.daily_close(analytics.today())["bills"] == 0
    assert analytics.sales_report()["total"] == 0
