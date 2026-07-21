"""The tool surface: the store's capabilities, described once, harness-agnostic.

Each tool is a thin adapter — argument shaping and result formatting, nothing
else. Every rule that money or stock depends on lives one layer down in
``app/services``, enforced inside a database transaction, so it holds whatever
the model decides to do. That split is the point: the model orchestrates, the
tools own the rules.

Tools are built per chat and closed over that chat's ``Turn``, so ``chat_id``
and the idempotency key come from the transport rather than from the model.
"""

from __future__ import annotations

from . import analytics, billing, documents, inventory, khata, memory
from ._tool import StoreTool, tool
from .context import Turn

__all__ = ["StoreTool", "Turn", "build_tools", "tool", "tool_names"]


def build_tools(turn: Turn) -> list[StoreTool]:
    return [
        *inventory.build_tools(turn),
        *analytics.build_tools(turn),
        *documents.build_tools(turn),
        *billing.build_tools(turn),
        *khata.build_tools(turn),
        *memory.build_tools(turn),
    ]


def tool_names(turn: Turn) -> list[str]:
    return [t.name for t in build_tools(turn)]
