"""Business rules for the store.

This layer owns every invariant that money and stock depend on — oversell
refusal, GST computation, idempotency, below-cost guard, khata rules. It runs
against SQLite inside ``BEGIN IMMEDIATE`` transactions.

The agent's ``@tool`` functions (app/skills) are thin adapters over this layer:
they translate arguments and format results. Deliberately, no rule lives in the
tool wrapper or the system prompt — a rule the model could talk its way past is
a rule in the wrong place.
"""
