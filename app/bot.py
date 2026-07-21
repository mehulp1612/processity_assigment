"""Telegram transport — the owner's only interface to the shop.

This module deliberately contains **no business logic and no intent routing**.
It does four things: hand the message text to the agent, stamp the update's id
onto the turn so mutating tools are idempotent, deliver whatever comes back, and
send any files the turn produced. Every decision about what the message *means*
belongs to the model, and every rule about what may happen belongs to the tools.

Idempotency
-----------
Telegram redelivers an update when it doesn't get an acknowledgement — a network
blip mid-turn means the same message arrives twice. The redelivered update
carries the **same ``update_id``**, so it is stamped on the turn as the
idempotency key and ``finalize_bill`` replays its recorded result instead of
billing the customer a second time. This is the one place that key can honestly
come from: the model can't be trusted to invent it, and the transport is the only
layer that knows a redelivery happened.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
from pathlib import Path
from typing import Optional

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .agent import StoreAgent

log = logging.getLogger("supermarket.bot")

TELEGRAM_LIMIT = 4096
TYPING_REFRESH = 4.0          # Telegram clears the indicator after ~5s

WELCOME = (
    "Namaste! I run your shop from this chat.\n\n"
    "Just talk normally — \"2 kg sugar and 4 maggi, cash\", \"received 20 Tata Salt\", "
    "\"how much atta is left?\", \"Ramesh paid 300\".\n\n"
    "/new — start a fresh conversation (your stock, khatas and preferences are kept)"
)


def to_html(text: str) -> str:
    """Render the model's light markdown as Telegram HTML.

    Escaping first and converting after means a stray ``<`` in a product name
    can't produce malformed markup.
    """
    out = html.escape(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out, flags=re.S)
    out = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", out)
    return out


def chunk(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split on line boundaries so a long bill doesn't break mid-number."""
    if len(text) <= limit:
        return [text]
    parts, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:                     # pathological single line
            parts.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return parts


class TelegramTransport:
    """Runs the bot inside the API process, supervised by uvicorn."""

    def __init__(self, agent: StoreAgent, token: Optional[str] = None):
        self.agent = agent
        self.token = (token or os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()
        self.app: Optional[Application] = None

    def configured(self) -> bool:
        return bool(self.token) and "..." not in self.token

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if not self.configured():
            log.warning("TELEGRAM_BOT_TOKEN not set — Telegram transport disabled")
            return

        self.app = ApplicationBuilder().token(self.token).build()
        self.app.add_handler(CommandHandler("start", self.on_start))
        self.app.add_handler(CommandHandler("help", self.on_start))
        self.app.add_handler(CommandHandler("new", self.on_new))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_message))
        self.app.add_error_handler(self.on_error)

        await self.app.initialize()
        await self.app.start()

        webhook_url = os.environ.get("TELEGRAM_WEBHOOK_URL", "").strip()
        if webhook_url:
            # Deployed behind a public URL: Telegram pushes to /telegram/webhook.
            await self.app.bot.set_webhook(
                url=webhook_url, allowed_updates=Update.ALL_TYPES
            )
            log.info("telegram webhook registered at %s", webhook_url)
        else:
            # Local development / tunnel: long-poll, no public URL needed.
            await self.app.bot.delete_webhook()
            await self.app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            log.info("telegram polling started")

        me = await self.app.bot.get_me()
        log.info("telegram bot live as @%s", me.username)

    async def stop(self) -> None:
        if self.app is None:
            return
        try:
            if self.app.updater and self.app.updater.running:
                await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
        except Exception:
            log.warning("telegram shutdown failed", exc_info=True)
        self.app = None

    async def feed(self, payload: dict) -> None:
        """Hand a webhook payload to the bot (used by the FastAPI route).

        Raises ``ValueError`` for anything Telegram wouldn't have sent, so the
        route can answer 400 rather than implying the server broke.
        """
        if self.app is None:
            raise RuntimeError("Telegram transport is not running")
        try:
            update = Update.de_json(payload, self.app.bot)
        except Exception as exc:                  # de_json raises TypeError on junk
            raise ValueError(f"payload is not a Telegram update: {exc}") from exc
        if update is None:
            raise ValueError("payload is not a Telegram update")
        await self.app.update_queue.put(update)

    # --- handlers ----------------------------------------------------------

    async def on_start(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(WELCOME)

    async def on_new(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await self.agent.reset(str(update.effective_chat.id))
        await update.effective_message.reply_text(
            "New conversation. Your stock, khatas and saved preferences are unchanged."
        )

    async def on_message(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        chat_id = str(update.effective_chat.id)
        text = (message.text or "").strip()
        if not text:
            return

        # The transport's own id for this message — stable across redeliveries.
        op_key = f"tg:{update.update_id}"
        typing = asyncio.create_task(self._keep_typing(chat_id, message))

        try:
            reply = await self.agent.send(chat_id, text, op_key=op_key)
        except Exception:
            log.exception("agent turn failed for chat %s", chat_id)
            reply = ("Something went wrong on my side and I've stopped rather than guess. "
                     "Nothing was billed. Please try again.")
        finally:
            typing.cancel()

        await self._deliver(message, reply)
        await self._send_attachments(chat_id, message)

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("telegram error", exc_info=context.error)

    # --- delivery ----------------------------------------------------------

    async def _keep_typing(self, chat_id: str, message) -> None:
        """Hold the 'typing…' indicator — turns can run 10s+ on a small model."""
        try:
            while True:
                await message.chat.send_action(ChatAction.TYPING)
                await asyncio.sleep(TYPING_REFRESH)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.debug("typing indicator failed for %s", chat_id, exc_info=True)

    async def _deliver(self, message, reply: str) -> None:
        if not reply:
            reply = "(no reply)"
        for part in chunk(reply):
            try:
                await message.reply_text(to_html(part), parse_mode=ParseMode.HTML)
            except BadRequest:
                # Never lose the owner's answer to a formatting problem.
                log.warning("HTML send rejected, falling back to plain text")
                await message.reply_text(part)

    async def _send_attachments(self, chat_id: str, message) -> None:
        """Deliver files the turn produced (invoice PDFs, analysis decks)."""
        session = self.agent.peek_session(chat_id)
        if session is None:
            return
        paths, session.turn.attachments = session.turn.attachments, []
        for path in paths:
            file = Path(path)
            if not file.exists():
                log.warning("tool asked to send a missing file: %s", path)
                continue
            try:
                with file.open("rb") as fh:
                    await message.reply_document(fh, filename=file.name)
            except Exception:
                log.exception("failed to send %s", path)
