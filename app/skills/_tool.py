"""A minimal tool descriptor, independent of any agent framework.

The skill modules declare their tools against this rather than against a
vendor's decorator, so the tool surface — names, descriptions, JSON schemas —
is portable. ``app/agent.py`` adapts these into whatever the harness wants;
today that is Pydantic AI, and swapping harness or model provider does not
touch a single tool definition.

That portability is not theoretical: this project moved from the Claude Agent
SDK to Pydantic AI + poolside by changing one import line per skill module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class StoreTool:
    """One tool the model may call."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def tool(name: str, description: str, input_schema: dict[str, Any]):
    """Declare a tool. The wrapped coroutine takes the argument dict."""

    def decorate(fn: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]) -> StoreTool:
        return StoreTool(
            name=name, description=description, input_schema=input_schema, handler=fn
        )

    return decorate
