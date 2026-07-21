"""Per-turn context the tools need but the model must never be trusted to supply.

Two values are injected by the transport (Telegram / CLI) rather than passed as
tool arguments:

``chat_id``
    Which conversation a draft bill belongs to. If the model supplied it, a
    hallucinated id would attach a bill to the wrong chat.

``op_key``
    The idempotency key for this turn, derived from the transport's own
    identifier for the message (Telegram's ``update_id``). Telegram redelivers
    updates after a network hiccup; the redelivered update carries the *same*
    id, so ``finalize_bill`` replays its recorded result instead of billing
    twice. Deriving it here rather than asking the model for it is the whole
    point — an idempotency key the model could forget is not an idempotency key.

Why a bound object and not a ContextVar
---------------------------------------
The SDK dispatches tool calls from tasks it created when the session connected,
which is *before* any given turn begins. A ContextVar set at the start of a turn
would not be visible inside those tasks. So each chat gets its own ``Turn`` and
its own MCP server whose tools close over it. ``chat_id`` is then fixed for the
life of the session, and ``op_key`` is updated under the chat's turn lock, so
concurrent chats cannot see each other's keys.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Turn:
    """The transport-supplied context for one chat, mutated per incoming message."""

    chat_id: str
    op_key: Optional[str] = None

    # Files produced during this turn (invoice PDFs, analysis decks). Tools append
    # paths here rather than trying to deliver them; the transport flushes the list
    # once the turn ends. Keeps document tools ignorant of Telegram.
    attachments: list[str] = field(default_factory=list)

    def key(self, suffix: str) -> Optional[str]:
        """Namespaced idempotency key for one mutating operation within this turn.

        ``None`` when the transport has no stable message id (an interactive CLI),
        in which case the service layer falls back to its status guards — a
        finalized bill still refuses to finalize twice.
        """
        return None if self.op_key is None else f"{self.op_key}:{suffix}"
