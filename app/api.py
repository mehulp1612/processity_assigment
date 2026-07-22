"""ASGI entrypoint.

Deliberately *not* a web app. The brief is explicit — no admin panel, no forms,
the chat is the product — so this module is transport and process supervision
only:

  * ``GET  /healthz``            liveness + database reachability, for compose
                                 healthchecks and the deployment platform
  * ``POST /telegram/webhook``   receives Telegram updates when deployed publicly
                                 (locally the bot long-polls instead)
  * ``POST /chat``               drives the same agent from curl, so the store can
                                 be exercised without a Telegram client

There are no routes that read or mutate store data directly. Every business
operation goes through the agent's tools, which is where the rules live.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import agent as agent_mod
from . import db
from .agent import StoreAgent
from .bot import TelegramTransport

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
# Long-polling logs one httpx line per request; only its problems are interesting.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Updater").setLevel(logging.WARNING)

log = logging.getLogger("supermarket.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.wait_for_db()
    db.init_db()
    log.info("database ready at %s", db.database_url())

    app.state.agent = StoreAgent()
    app.state.telegram = TelegramTransport(app.state.agent)
    try:
        await app.state.telegram.start()
    except Exception:
        # A bad token must not take the whole service down — /healthz and /chat
        # stay useful, and the logs say exactly what failed.
        log.exception("Telegram transport failed to start")

    yield

    await app.state.telegram.stop()
    await app.state.agent.close()
    db.close_pool()


app = FastAPI(title="Supermarket Ops Agent", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get("/healthz")
def healthz() -> JSONResponse:
    """Liveness probe: the process is up *and* can reach its store of record."""
    try:
        with db.tx() as cx:
            cx.execute("SELECT 1")
    except Exception as exc:
        return JSONResponse({"ok": False, "db": str(exc)}, status_code=503)
    return JSONResponse({"ok": True, "db": "up"})


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> JSONResponse:
    """Receive updates when deployed behind a public URL.

    Used only when TELEGRAM_WEBHOOK_URL is set; locally the transport long-polls
    instead, so no tunnel is needed during development. Acknowledge fast and let
    the bot process asynchronously — Telegram redelivers on a slow response, and
    a redelivery is exactly what the idempotency key exists to absorb.
    """
    transport: TelegramTransport = app.state.telegram
    if not transport.configured():
        raise HTTPException(status_code=503, detail="Telegram transport is not configured.")
    try:
        await transport.feed(await request.json())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("failed to enqueue telegram update")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return JSONResponse({"ok": True})


@app.post("/chat")
async def chat(request: Request):
    """Drive the agent without a Telegram client.

        curl -s localhost:8000/chat -H 'content-type: application/json' \\
             -d '{"chat_id":"dev","message":"how much atta is left?"}'

    Same agent, same tools, same rules as the bot — only the transport differs.
    ``op_key`` stands in for Telegram's update_id: send the same one twice and any
    billing done in that turn replays instead of repeating.
    """
    if not agent_mod.api_key_configured():
        raise HTTPException(
            status_code=503,
            detail="Model API key is not configured — set MODEL_API_KEY in .env and restart.",
        )

    body = await request.json()
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    chat_id = str(body.get("chat_id") or "dev")
    agent: StoreAgent = app.state.agent

    if message == "/new":
        await agent.reset(chat_id)
        return JSONResponse({"reply": "New conversation. Your books are unchanged."})

    try:
        reply = await agent.send(chat_id, message, op_key=body.get("op_key"))
    except Exception as exc:
        # A bad key or an unreachable model endpoint surfaces here. Say so
        # plainly instead of a bare 500.
        log.exception("agent turn failed for chat %s", chat_id)
        raise HTTPException(
            status_code=502, detail=f"Agent turn failed: {type(exc).__name__}: {exc}"
        ) from exc

    # Telegram delivers queued files and clears the list; over HTTP there is
    # nowhere to push a document, so report the paths and drain the queue. Left
    # undrained they would accumulate on the session for the life of the chat.
    files: list[str] = []
    session = agent.peek_session(chat_id)
    if session is not None:
        files, session.turn.attachments = session.turn.attachments, []

    return JSONResponse({"reply": reply, "files": files})
