"""The tool adapter layer, exercised without a model.

These call the ``@tool`` handlers directly. That covers the part of the agent
that is deterministic — argument shaping, result formatting, and the values the
transport injects rather than the model supplying — so the only thing left
needing a live model is whether it *chooses* sensibly.

Two properties matter most here and neither is provable from the services tests:
  * a refusal comes back as a readable result, not a tool error
  * ``chat_id`` and the idempotency key come from the turn, not from arguments
"""

from __future__ import annotations

import json

import pytest

from app import db, skills
from app.skills.context import Turn


def tools_for(turn: Turn) -> dict:
    return {t.name: t for t in skills.build_tools(turn)}


async def run(tool, **args) -> dict:
    """Invoke a tool handler and decode its single text block."""
    result = await tool.handler(args)
    payload = json.loads(result["content"][0]["text"])
    payload["_is_error"] = result.get("is_error", False)
    return payload


@pytest.fixture
def turn():
    return Turn(chat_id="chat-99")


@pytest.fixture
def kit(turn):
    return tools_for(turn)


# --- Surface -----------------------------------------------------------------

def test_every_tool_has_a_unique_name_and_object_schema(turn):
    tools = skills.build_tools(turn)
    names = [t.name for t in tools]
    assert len(names) == len(set(names)), "duplicate tool names would shadow each other"
    assert len(names) == 25
    for t in tools:
        assert t.input_schema["type"] == "object"
        assert t.description.strip(), f"{t.name} needs a description the model can act on"


def test_tool_surface_is_harness_agnostic(turn):
    """Tools carry their own schema, so swapping harness or provider can't move them."""
    for t in skills.build_tools(turn):
        assert isinstance(t.input_schema, dict) and callable(t.handler)
    assert set(skills.tool_names(turn)) == {t.name for t in skills.build_tools(turn)}


# --- Result shaping ----------------------------------------------------------

@pytest.mark.asyncio
async def test_refusal_is_a_result_not_an_error(kit, pid):
    """An oversell must read as an answer, so the model explains instead of retrying."""
    bill = await run(kit["start_bill"])
    out = await run(kit["add_line"], bill_id=bill["bill_id"],
                    product_id=pid("Amul Ghee 1L"), qty=999)

    assert out["ok"] is False
    assert out["error"] == "INSUFFICIENT_STOCK"
    assert out["_is_error"] is False, "refusals must not be flagged as tool errors"
    # The specifics the model needs to explain the refusal.
    assert out["details"]["available"] == 15
    assert out["details"]["requested"] == 999


@pytest.mark.asyncio
async def test_ambiguous_product_returns_candidates_for_the_model_to_ask_about(kit):
    """'atta' spans two GST slabs — the tool surfaces both and leaves the choice open."""
    out = await run(kit["get_stock"], query="atta")

    assert out["error"] == "AMBIGUOUS_PRODUCT"
    names = {c["name"] for c in out["details"]["candidates"]}
    assert {"Aashirvaad Atta 5kg", "Loose Wheat Atta"} <= names
    # Differing slabs are exactly why guessing would be wrong.
    assert len({c["gst_rate"] for c in out["details"]["candidates"]}) > 1


# --- Injected context --------------------------------------------------------

@pytest.mark.asyncio
async def test_bill_is_bound_to_the_turns_chat_not_a_model_argument(kit, turn):
    bill = await run(kit["start_bill"])
    with db.tx() as cx:
        chat = cx.execute(
            "SELECT chat_id FROM bills WHERE id = %s", (bill["bill_id"],)
        ).fetchone()["chat_id"]
    assert chat == turn.chat_id

    # And view_bill with no id resumes that same chat's draft.
    assert (await run(kit["view_bill"]))["bill_id"] == bill["bill_id"]


@pytest.mark.asyncio
async def test_preferences_are_scoped_to_the_turns_chat(kit, turn):
    await run(kit["set_preference"], key="default_payment", value="UPI")
    from app.services import memory as memory_svc

    assert memory_svc.get_preferences(turn.chat_id) == {"default_payment": "UPI"}
    assert memory_svc.get_preferences("someone-else") == {}


@pytest.mark.asyncio
async def test_redelivered_update_replays_instead_of_billing_twice(turn, pid):
    """The transport's message id — not the model — is what makes finalize idempotent."""
    kit = tools_for(turn)
    from app.services import inventory

    p = pid("Parle-G Biscuits 100g")
    before = inventory.get_product(p)["qty"]

    bill = await run(kit["start_bill"])
    await run(kit["add_line"], bill_id=bill["bill_id"], product_id=p, qty=4)

    # Telegram delivers update 5150, then redelivers it after a network hiccup.
    turn.op_key = "tg:update:5150"
    first = await run(kit["finalize_bill"], bill_id=bill["bill_id"], payment_mode="cash")
    replay = await run(kit["finalize_bill"], bill_id=bill["bill_id"], payment_mode="cash")

    assert replay["idempotent_replay"] is True
    assert replay["invoice_no"] == first["invoice_no"]
    assert inventory.get_product(p)["qty"] == before - 4     # decremented once

    with db.tx() as cx:
        n = cx.execute(
            "SELECT COUNT(*) AS c FROM payments WHERE bill_id = %s", (bill["bill_id"],)
        ).fetchone()["c"]
    assert n == 1


@pytest.mark.asyncio
async def test_without_a_transport_id_a_second_finalize_is_still_refused(kit, pid):
    """The CLI has no message id; the status guard has to carry the weight alone."""
    bill = await run(kit["start_bill"])
    await run(kit["add_line"], bill_id=bill["bill_id"], product_id=pid("Tata Salt 1kg"), qty=2)
    await run(kit["finalize_bill"], bill_id=bill["bill_id"], payment_mode="cash")

    again = await run(kit["finalize_bill"], bill_id=bill["bill_id"], payment_mode="cash")
    assert again["error"] == "BILL_ALREADY_FINALIZED"


# --- A whole bill, through the tools only ------------------------------------

@pytest.mark.asyncio
async def test_multi_turn_bill_with_edits_through_the_tool_surface(kit, pid):
    sugar, atta = pid("Loose Sugar"), pid("Aashirvaad Atta 5kg")
    maggi, butter = pid("Maggi 2-Minute Noodles 70g"), pid("Amul Butter 100g")

    bill = await run(kit["start_bill"])
    bid = bill["bill_id"]
    for product_id, qty in ((sugar, 2), (atta, 1), (maggi, 4), (butter, 1)):
        await run(kit["add_line"], bill_id=bid, product_id=product_id, qty=qty)

    built = await run(kit["view_bill"], bill_id=bid)
    assert built["total"] == 527.0          # 495.00 + 15.89 + 15.88, rounded

    # "drop the butter, make it 6 Maggi"
    await run(kit["remove_line"], bill_id=bid, product_id=butter)
    edited = await run(kit["set_line_qty"], bill_id=bid, product_id=maggi, qty=6)
    assert edited["total"] == 490.0
    assert {ln["name"] for ln in edited["lines"]} == {
        "Loose Sugar", "Aashirvaad Atta 5kg", "Maggi 2-Minute Noodles 70g"
    }

    done = await run(kit["finalize_bill"], bill_id=bid, payment_mode="upi",
                     payment_ref="UPI/4471")
    assert done["status"] == "finalized"
    assert done["invoice_no"].startswith("INV/")
    assert done["payment"]["mode"] == "upi"


@pytest.mark.asyncio
async def test_khata_cycle_through_the_tool_surface(kit, pid):
    bill = await run(kit["start_bill"], customer="Ramesh")
    await run(kit["add_line"], bill_id=bill["bill_id"],
              product_id=pid("Aashirvaad Atta 5kg"), qty=1)
    sale = await run(kit["finalize_bill"], bill_id=bill["bill_id"],
                     payment_mode="khata", customer="Ramesh")
    assert sale["payment"]["khata_balance"] == 299.0

    assert (await run(kit["khata_settle"], customer="Ramesh", amount=100))["balance"] == 199.0

    over = await run(kit["khata_settle"], customer="Ramesh", amount=500)
    assert over["error"] == "OVERPAYMENT"
    ok = await run(kit["khata_settle"], customer="Ramesh", amount=500, allow_overpay=True)
    assert ok["balance"] == -301.0

    missing = await run(kit["khata_settle"], customer="Suresh", amount=50)
    assert missing["error"] == "NO_SUCH_KHATA"
