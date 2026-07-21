"""Turning service calls into MCP tool results.

Design note — refusals are *results*, not errors. When the store refuses to
oversell or to settle a khata that doesn't exist, that is the system working
correctly, and the model's job is to relay it and ask the right follow-up. So a
``DomainError`` comes back as ordinary content with ``ok: false`` plus a code and
structured details, and ``is_error`` is reserved for genuine bugs. Marking a
refusal as an error invites the model to "retry until it works", which is exactly
the behaviour these guards exist to prevent.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

from ..services.common import DomainError

log = logging.getLogger("supermarket.skills")


def _payload(obj: Any) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(obj, default=str, indent=None)}]}


async def call(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> dict:
    """Run a synchronous service function off the event loop and shape the result."""
    try:
        result = await asyncio.to_thread(fn, *args, **kwargs)
    except DomainError as exc:
        # Expected refusal: hand the model the code and details so it can explain
        # precisely what happened and ask the right question.
        return _payload(exc.to_dict())
    except Exception as exc:                       # genuine bug — surface as an error
        log.exception("tool %s failed", getattr(fn, "__name__", fn))
        return {
            **_payload({"ok": False, "error": "INTERNAL", "message": str(exc)}),
            "is_error": True,
        }
    return _payload(result if isinstance(result, dict) else {"ok": True, "result": result})
