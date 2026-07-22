"""End-to-end scenario run **through the model**.

``scripts/smoke.py`` proves the services layer computes the right numbers. This
proves the agent *reaches* those services correctly from ordinary shopkeeper
phrasing — that it asks when a request is ambiguous, relays a refusal instead of
routing around it, and carries a draft bill across several messages.

Everything goes over ``/chat``, so it exercises the same agent, tools and prompt
the Telegram transport uses; only the transport differs. Run it inside the app
container (its ``localhost`` is uvicorn):

    docker exec ops-phase8 python -m scripts.scenarios          # everything
    docker exec ops-phase8 python -m scripts.scenarios khata    # one group

This costs real model calls, so it is a manual tool, not part of ``pytest``.
"""

from __future__ import annotations

import sys
import time

import httpx

BASE = "http://localhost:8000"

# (label, message, op_key). A shared op_key across two steps is how an idempotent
# redelivery is simulated — Telegram would resend the same update_id.
SCENARIOS: dict[str, list[tuple[str, str, str | None]]] = {
    "ambiguity": [
        ("vague product", "how much atta is left?", None),
        ("disambiguated", "the aashirvaad one", None),
    ],
    "bill": [
        ("start", "2 kg sugar and 1 aashirvaad atta", None),
        ("add more", "add 4 maggi and 1 amul butter", None),
        ("edit", "drop the butter, make it 6 maggi", None),
        ("review", "what's the total?", None),
        ("finalize", "cash", "run:bill:final"),
        ("redelivery", "cash", "run:bill:final"),
    ],
    "oversell": [
        ("start", "new bill: 3 parle-g", None),
        ("impossible qty", "add 999 maggi", None),
    ],
    "khata": [
        ("credit sale", "1 aashirvaad atta for Ramesh on khata", "run:khata:final"),
        ("more credit", "Ramesh took 500 rupees of goods on credit", None),
        ("settle", "Ramesh paid 300", None),
        ("balance", "what does Ramesh owe?", None),
        ("unknown customer", "Suresh paid 100", None),
    ],
    "belowcost": [
        ("under cost", "sell 1 aashirvaad atta at 200 rupees", None),
    ],
    "memory": [
        ("set", "from now on always remind me to check expiry on dairy", None),
        ("recall", "what preferences have I set?", None),
    ],
    "stock": [
        ("receive", "received 20 tata salt at 22 rupees cost", None),
        ("low stock", "what's running low?", None),
    ],
    "reports": [
        ("daily close", "close the day", None),
        ("invoice", "send me the pdf invoice for the last bill", None),
        ("deck", "make me an analysis deck for this week", None),
    ],
}

ORDER = ["ambiguity", "bill", "oversell", "khata", "belowcost", "memory", "stock", "reports"]


def say(chat_id: str, message: str, op_key: str | None) -> str:
    body: dict = {"chat_id": chat_id, "message": message}
    if op_key:
        body["op_key"] = op_key
    started = time.monotonic()
    r = httpx.post(f"{BASE}/chat", json=body, timeout=180)
    elapsed = time.monotonic() - started
    if r.status_code != 200:
        return f"!! HTTP {r.status_code}: {r.text[:400]}  ({elapsed:.1f}s)"
    return f"{r.json()['reply']}\n   [{elapsed:.1f}s]"


def main() -> None:
    groups = sys.argv[1:] or ORDER
    for group in groups:
        steps = SCENARIOS.get(group)
        if not steps:
            print(f"!! unknown scenario {group!r}; known: {', '.join(ORDER)}")
            continue
        # A fresh chat per group keeps one scenario's context from rescuing the
        # next one — each has to stand on its own.
        chat_id = f"scn-{group}"
        httpx.post(f"{BASE}/chat", json={"chat_id": chat_id, "message": "/new"}, timeout=60)

        print(f"\n{'=' * 72}\n== {group.upper()}\n{'=' * 72}")
        for label, message, op_key in steps:
            print(f"\n--- {label}\n>> {message}")
            print(f"<< {say(chat_id, message, op_key)}")


if __name__ == "__main__":
    main()
