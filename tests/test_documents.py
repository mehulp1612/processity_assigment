"""Artifacts: the invoice PDF and the analysis deck.

These assert on structure and on numbers, not on pixels. What matters is that a
real file appears, that it carries the figures the books hold, and that it can't
be produced for a bill that has no invoice.
"""

from __future__ import annotations

import re
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app import db
from app.services import analytics, billing, deck, documents
from app.services.common import DomainError
from app.services.documents import amount_in_words
from app.skills import build_tools
from app.skills.context import Turn

IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")


@pytest.fixture
def sold(pid):
    """The §3 bill: 2kg sugar, 1 atta, 4 Maggi, 1 butter — paid by UPI."""
    bill = billing.start_bill(chat_id="doc")
    for name, qty in (("Loose Sugar", 2), ("Aashirvaad Atta 5kg", 1),
                      ("Maggi 2-Minute Noodles 70g", 4), ("Amul Butter 100g", 1)):
        billing.add_line(bill["bill_id"], pid(name), qty)
    return billing.finalize_bill(bill["bill_id"], "upi", payment_ref="UPI/8891")


# --- Amount in words --------------------------------------------------------

@pytest.mark.parametrize("amount,expected", [
    (0, "Zero Rupees Only"),
    (76, "Seventy Six Rupees Only"),
    (527, "Five Hundred Twenty Seven Rupees Only"),
    (1_250, "One Thousand Two Hundred Fifty Rupees Only"),
    (100_000, "One Lakh Rupees Only"),
    (12_345_678, "One Crore Twenty Three Lakh Forty Five Thousand "
                 "Six Hundred Seventy Eight Rupees Only"),
])
def test_amount_in_words_uses_indian_numbering(amount, expected):
    """Lakh and crore, not million — an Indian tax invoice reads wrong otherwise."""
    assert amount_in_words(amount) == expected


# --- Invoice PDF ------------------------------------------------------------

def test_invoice_pdf_is_a_real_pdf_with_the_bills_figures(sold):
    out = documents.render_invoice_pdf(sold["bill_id"])
    path = Path(out["path"])

    assert path.exists() and path.suffix == ".pdf"
    assert path.stat().st_size > 5_000, "a 4-line GST invoice should not be near-empty"
    assert path.read_bytes().startswith(b"%PDF-")
    assert out["invoice_no"] == sold["invoice_no"] == "INV/2026-27/0001"
    assert out["total"] == 527.0
    assert out["lines"] == 4
    assert path.name == "INV-2026-27-0001.pdf", "slashes must not create directories"


def test_invoice_embeds_a_font_carrying_the_rupee_sign(sold):
    """ReportLab's built-in Helvetica has no U+20B9, so every amount would render
    as a black box. The invoice must ship a font that has it."""
    out = documents.render_invoice_pdf(sold["bill_id"])
    assert b"DejaVu" in Path(out["path"]).read_bytes()


def test_a_draft_bill_has_no_invoice(pid):
    """An invoice number is assigned at finalize; a draft simply doesn't have one."""
    bill = billing.start_bill(chat_id="doc")
    billing.add_line(bill["bill_id"], pid("Tata Salt 1kg"), 2)

    with pytest.raises(DomainError) as exc:
        documents.render_invoice_pdf(bill["bill_id"])
    assert exc.value.code == "BILL_NOT_FINALIZED"


def test_unknown_bill_is_refused():
    with pytest.raises(DomainError) as exc:
        documents.render_invoice_pdf(999_999)
    assert exc.value.code == "BILL_NOT_FOUND"


# --- Analysis deck ----------------------------------------------------------

def _pptx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        slides = [n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)]
        return " ".join(
            " ".join(re.findall(r"<a:t>([^<]*)</a:t>", z.read(s).decode())) for s in slides
        )


def _pptx_images(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as z:
        return [n for n in z.namelist() if n.startswith("ppt/media/")]


@pytest.fixture
def week(pid):
    """Three trading days so the charts have something to plot."""
    def sell(name, qty, mode="cash", customer=None, when=None):
        bill = billing.start_bill(chat_id="doc", customer=customer)
        billing.add_line(bill["bill_id"], pid(name), qty)
        billing.finalize_bill(bill["bill_id"], mode, customer=customer)
        if when:
            with db.tx() as cx:
                cx.execute("UPDATE bills SET finalized_at = %s WHERE id = %s",
                           (when.astimezone(UTC).isoformat(), bill["bill_id"]))

    sell("Amul Ghee 1L", 2, when=datetime(2026, 7, 20, 11, 0, tzinfo=IST))
    sell("Parle-G Biscuits 100g", 10, when=datetime(2026, 7, 22, 16, 0, tzinfo=IST))
    sell("Aashirvaad Atta 5kg", 1, "khata", "Ramesh",
         when=datetime(2026, 7, 22, 18, 0, tzinfo=IST))
    return ("2026-07-20", "2026-07-22")


def test_deck_is_a_real_pptx_with_embedded_charts(week):
    start, end = week
    out = deck.build_analysis_pptx(start, end)
    path = Path(out["path"])

    assert path.exists() and path.suffix == ".pptx"
    assert out["slides"] == 7
    images = _pptx_images(path)
    assert len(images) == 3, "sales-by-day, payment mix and best sellers must all render"
    with zipfile.ZipFile(path) as z:
        assert all(z.getinfo(i).file_size > 5_000 for i in images), "charts look blank"


def test_deck_numbers_match_the_books(week):
    """The deck is a view of the books; it must not compute its own totals."""
    start, end = week
    out = deck.build_analysis_pptx(start, end)
    report = analytics.sales_report(start, end)

    assert out["bills"] == report["bills"] == 3
    assert out["total"] == report["total"]

    text = _pptx_text(Path(out["path"]))
    assert f"Rs. {report['total']:,.0f}" in text
    assert f"Rs. {report['gst']:,.2f}" in text
    assert "CGST" in text and "SGST" in text


def test_deck_covers_a_quiet_day_without_dying(pid):
    """A range with no sales at all still has to produce a deck."""
    out = deck.build_analysis_pptx("2026-01-01", "2026-01-03")
    assert Path(out["path"]).exists()
    assert out["bills"] == 0


# --- Delivery ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_document_tools_queue_the_file_for_the_transport(sold):
    """Tools record the path on the turn instead of delivering it themselves, so
    the document services never learn what Telegram is."""
    turn = Turn(chat_id="doc")
    kit = {t.name: t for t in build_tools(turn)}

    assert turn.attachments == []
    result = await kit["render_invoice_pdf"].handler({"bill_id": sold["bill_id"]})

    assert len(turn.attachments) == 1
    assert turn.attachments[0].endswith(".pdf")
    assert Path(turn.attachments[0]).exists()
    # The raw path is not pushed at the model — it has nothing useful to do with it.
    import json
    payload = json.loads(result["content"][0]["text"])
    assert payload["delivered"] is True and "path" not in payload


@pytest.mark.asyncio
async def test_a_refused_document_queues_nothing(pid):
    turn = Turn(chat_id="doc")
    kit = {t.name: t for t in build_tools(turn)}
    bill = billing.start_bill(chat_id="doc")
    billing.add_line(bill["bill_id"], pid("Tata Salt 1kg"), 1)

    import json
    result = await kit["render_invoice_pdf"].handler({"bill_id": bill["bill_id"]})
    payload = json.loads(result["content"][0]["text"])

    assert payload["ok"] is False
    assert payload["error"] == "BILL_NOT_FINALIZED"
    assert turn.attachments == []
