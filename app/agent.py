"""The agent: one conversation per chat, over the store's tool surface.

Control loop
------------
There is no router. A message goes to the model with the store's tools attached;
the model decides which tools to call and in what order, and the loop simply
relays whatever it says back to the owner. Multi-step work ("bill these four
things, drop one, take UPI") is the model calling tools in sequence within a
single turn — not a state machine we wrote. Pydantic AI owns that loop; we own
the tools.

Provider independence
---------------------
The model is reached over an OpenAI-compatible endpoint chosen by configuration,
so switching provider — poolside, Gemini, Groq, OpenAI, a local server — is two
environment variables and no code. The tool definitions never move.

Isolation
---------
Each chat gets its own tools (bound to its ``Turn``) and its own message history.
Turns within a chat are serialized by a lock, because the turn's idempotency key
is stamped just before the model runs.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.tools import Tool

from . import prompt, skills
from .skills.context import Turn

log = logging.getLogger("supermarket.agent")

DEFAULT_BASE_URL = "https://inference.poolside.ai/v1"
DEFAULT_MODEL = "poolside/laguna-s-2.1"
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "20"))


def api_key() -> str:
    """The model provider's key. Named generically — the provider is configurable."""
    return os.environ.get("MODEL_API_KEY") or os.environ.get("POOLSIDE_API_KEY", "")


def api_key_configured() -> bool:
    """True only for a real key — ``.env.example`` ships an obvious placeholder."""
    key = api_key().strip()
    return bool(key) and "..." not in key


def _as_pydantic_tool(store_tool: skills.StoreTool) -> Tool:
    """Adapt a StoreTool to Pydantic AI, keeping our hand-written JSON schema.

    ``from_schema`` takes the schema verbatim rather than inferring one from a
    signature, so the descriptions the model actually reads stay exactly as
    written in the skill modules.
    """

    async def call(**kwargs) -> str:
        result = await store_tool.handler(kwargs)
        return result["content"][0]["text"]

    return Tool.from_schema(
        call,
        name=store_tool.name,
        description=store_tool.description,
        json_schema=store_tool.input_schema,
    )


class _Session:
    """One chat's agent and conversation history."""

    def __init__(self, chat_id: str, model: OpenAIChatModel):
        self.turn = Turn(chat_id=chat_id)
        self.lock = asyncio.Lock()
        self.history: list[ModelMessage] = []
        self.agent = Agent(
            model,
            # Built per session, so preferences saved in an earlier conversation
            # are present from the very first message.
            system_prompt=prompt.build(chat_id),
            tools=[_as_pydantic_tool(t) for t in skills.build_tools(self.turn)],
            retries=2,
        )


class StoreAgent:
    """Registry of per-chat sessions."""

    def __init__(self, model_name: Optional[str] = None, base_url: Optional[str] = None):
        self.model_name = model_name or os.environ.get("AGENT_MODEL") or DEFAULT_MODEL
        self.base_url = base_url or os.environ.get("MODEL_BASE_URL") or DEFAULT_BASE_URL
        self._model = OpenAIChatModel(
            self.model_name,
            provider=OpenAIProvider(base_url=self.base_url, api_key=api_key()),
        )
        self._sessions: dict[str, _Session] = {}
        self._registry_lock = asyncio.Lock()

    async def _session(self, chat_id: str) -> _Session:
        async with self._registry_lock:
            session = self._sessions.get(chat_id)
            if session is None:
                session = _Session(chat_id, self._model)
                self._sessions[chat_id] = session
            return session

    async def send(self, chat_id: str, text: str, op_key: Optional[str] = None) -> str:
        """Run one turn and return the assistant's reply as plain text.

        ``op_key`` is the transport's stable id for this message. It is stamped on
        the turn so mutating tools can key their idempotency ledger by it, which
        is what makes a redelivered Telegram update replay instead of re-billing.
        """
        session = await self._session(str(chat_id))

        async with session.lock:
            session.turn.op_key = op_key
            try:
                result = await session.agent.run(
                    text, message_history=session.history or None
                )
            finally:
                session.turn.op_key = None

            session.history = list(result.all_messages())
            return str(result.output).strip()

    def peek_session(self, chat_id: str) -> Optional[_Session]:
        """This chat's live session, if any. Lets a transport drain the turn's
        attachments after a run without taking ownership of session state."""
        return self._sessions.get(str(chat_id))

    async def reset(self, chat_id: str) -> None:
        """Drop this chat's conversation (the ``/new`` command).

        Only the *dialogue* is discarded. Stock, bills, khatas and the owner's
        standing preferences live in Postgres and are untouched — which is why a
        fresh chat still remembers them.
        """
        async with self._registry_lock:
            self._sessions.pop(str(chat_id), None)

    async def close(self) -> None:
        async with self._registry_lock:
            self._sessions.clear()
