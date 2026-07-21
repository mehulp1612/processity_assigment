"""Memory tools — standing preferences that outlive the conversation.

These write to Postgres, not to the transcript, which is what makes a preference
survive ``/new`` and a redeploy. They are loaded back into the system prompt at
the start of every session (see ``app/prompt.py``).
"""

from __future__ import annotations

from ._tool import tool

from ..services import memory as svc
from ._result import call
from .context import Turn


def build_tools(turn: Turn) -> list:
    """Build this chat's tools, closed over its turn context."""

    @tool(
        "set_preference",
        "Remember a standing instruction from the owner — anything phrased as 'always', "
        "'from now on', 'by default', or a correction they clearly expect to stick "
        "('when I say atta I mean the loose one'). Stored durably, so it survives a new "
        "chat and a restart. Use a short stable key like default_payment or default_atta.",
        {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Short snake_case identifier for the preference."},
                "value": {"type": "string", "description": "The preference itself, in plain words."},
            },
            "required": ["key", "value"],
        },
    )
    async def set_preference(args: dict) -> dict:
        return await call(svc.set_preference, turn.chat_id, args["key"], args["value"])


    @tool(
        "get_preferences",
        "Every standing preference for this owner. They are already in your system "
        "prompt; call this only to re-read them after a change.",
        {"type": "object", "properties": {}, "required": []},
    )
    async def get_preferences(args: dict) -> dict:
        return await call(svc.get_preferences, turn.chat_id)


    @tool(
        "forget_preference",
        "Drop a standing preference the owner no longer wants applied.",
        {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    )
    async def forget_preference(args: dict) -> dict:
        return await call(svc.forget_preference, turn.chat_id, args["key"])

    return [set_preference, get_preferences, forget_preference]
