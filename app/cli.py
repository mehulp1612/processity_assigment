"""Chat with the store from a terminal — the same agent Telegram will drive.

    docker compose run --rm app python -m app.cli

Useful because it exercises the whole stack (model → tools → Postgres) without a
bot token or a public URL, and because the transcript is easy to paste into a
bug report. ``/new`` starts a fresh conversation, which is the local equivalent
of the Telegram ``/new`` command: the dialogue resets, the books do not.
"""

from __future__ import annotations

import asyncio
import os
import sys

from . import agent as agent_mod
from . import db
from .agent import StoreAgent

BANNER = """\
Supermarket Ops Agent — CLI harness
  /new    start a fresh conversation (books are kept)
  /quit   exit
"""


async def main() -> int:
    if not agent_mod.api_key_configured():
        print("Model API key is not set. Put MODEL_API_KEY in .env and re-run.",
              file=sys.stderr)
        return 2

    db.wait_for_db()
    db.init_db()

    chat_id = os.environ.get("CLI_CHAT_ID", "cli")
    agent = StoreAgent()
    print(BANNER)

    try:
        while True:
            try:
                line = input("owner> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not line:
                continue
            if line in {"/quit", "/exit"}:
                break
            if line == "/new":
                await agent.reset(chat_id)
                print("— new conversation. Stock, khatas and preferences are unchanged.\n")
                continue

            try:
                reply = await agent.send(chat_id, line)
            except Exception as exc:                     # keep the session usable
                print(f"[error] {type(exc).__name__}: {exc}\n", file=sys.stderr)
                continue

            print(f"\n{reply}\n")
    finally:
        await agent.close()
        db.close_pool()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
