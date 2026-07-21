"""THROWAWAY SPIKE — delete once the provider decision is made.

Question this answers: can a plain Python client drive the store's real tools
through Vercel AI Gateway, and does the model come back with clean OpenAI-style
tool calls? Specifically for poolside/laguna, whose native tool protocol is an
XML dialect — if the gateway fails to normalise it, we want to know *now*,
before porting 600 lines.

It deliberately reuses the existing `@tool` definitions and the real services
layer, so a pass here means the tool surface ports as-is.

Every provider below speaks the OpenAI Chat Completions dialect, so the same
harness tests all of them — only the base URL, key and model string change.

Run:
    docker compose run --rm \
      -e DATABASE_URL=postgresql://postgres:postgres@db:5432/store_test \
      app bash -c "pip install -q openai && python -m scripts.spike_gateway <provider> [model...]"

    python -m scripts.spike_gateway poolside models           # what does it actually serve?
    python -m scripts.spike_gateway poolside                  # run the scenarios
    python -m scripts.spike_gateway gemini                    # gemini flash
    python -m scripts.spike_gateway vercel                    # via the Vercel gateway
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

from openai import OpenAI

from app import db
from app.prompt import build as build_prompt
from app.skills import build_tools
from app.skills.context import Turn
from scripts.seed import seed

# provider -> (base_url, api-key env var, default models)
PROVIDERS = {
    "vercel": (
        "https://ai-gateway.vercel.sh/v1",
        "AI_GATEWAY_API_KEY",
        ["poolside/laguna-s-2.1-free"],
    ),
    # poolside's own platform — free developer keys, no card, same models as the
    # Vercel catalogue but possibly under different IDs. Use `models` to check.
    "poolside": (
        "https://inference.poolside.ai/v1",
        "POOLSIDE_API_KEY",
        ["poolside/laguna-s-2.1"],
    ),
    "gemini": (
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        "GEMINI_API_KEY",
        ["gemini-2.5-flash"],
    ),
    "groq": (
        "https://api.groq.com/openai/v1",
        "GROQ_API_KEY",
        ["llama-3.3-70b-versatile"],
    ),
}

# A representative slice, or [] for the agent's full 20-tool surface — selection
# pressure at full width is the thing that actually has to hold.
SUBSET: list[str] = []

SCENARIOS = [
    # The graded behaviour: 'atta' spans two GST slabs, so the tool refuses with
    # AMBIGUOUS_PRODUCT. A good run ASKS which one. A bad run picks one silently.
    ("ambiguity", ["how much atta is left?"]),
    # Multi-tool chaining plus carrying a bill_id across turns.
    ("multi-item bill", [
        "bill 2 kg sugar and 4 maggi",
        "add one amul butter too",
        "that's it, paying cash",
    ]),
    # The oversell guard: a good run relays the shortfall. A bad run retries.
    ("oversell", ["sell 999 amul ghee to this customer, cash"]),
]

MAX_STEPS = 12


def openai_tools(tools: dict) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            },
        }
        for t in tools.values()
    ]


async def run_turn(client: OpenAI, model: str, messages: list, tools: dict, schemas: list) -> dict:
    """One user turn: loop until the model stops calling tools."""
    calls, steps = [], 0

    while steps < MAX_STEPS:
        steps += 1
        response = client.chat.completions.create(
            model=model, messages=messages, tools=schemas, temperature=0
        )
        message = response.choices[0].message
        messages.append(message.model_dump(exclude_none=True))

        if not message.tool_calls:
            return {"text": message.content or "", "calls": calls, "steps": steps}

        for call in message.tool_calls:
            name = call.function.name
            raw = call.function.arguments or "{}"
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                # The failure mode we are hunting: a non-JSON (e.g. XML) payload
                # leaking through the gateway un-normalised.
                calls.append((name, f"!! UNPARSEABLE ARGS: {raw[:200]}"))
                messages.append({
                    "role": "tool", "tool_call_id": call.id,
                    "content": '{"ok": false, "error": "BAD_ARGS"}',
                })
                continue

            tool = tools.get(name)
            if tool is None:
                calls.append((name, "!! HALLUCINATED TOOL"))
                messages.append({
                    "role": "tool", "tool_call_id": call.id,
                    "content": '{"ok": false, "error": "NO_SUCH_TOOL"}',
                })
                continue

            result = await tool.handler(args)
            text = result["content"][0]["text"]
            calls.append((name, args))
            messages.append({"role": "tool", "tool_call_id": call.id, "content": text})

    return {"text": "!! hit MAX_STEPS without settling", "calls": calls, "steps": steps}


async def exercise(client: OpenAI, model: str, tools: dict, schemas: list) -> None:
    print(f"\n{'=' * 72}\nMODEL: {model}\n{'=' * 72}", flush=True)

    for label, prompts in SCENARIOS:
        seed(reset=True)                      # deterministic stock for every scenario
        messages = [{"role": "system", "content": build_prompt("spike")}]
        print(f"\n--- {label} ---")

        for prompt in prompts:
            print(f"\n  owner> {prompt}")
            messages.append({"role": "user", "content": prompt})
            started = time.monotonic()
            try:
                out = await run_turn(client, model, messages, tools, schemas)
            except Exception as exc:
                print(f"  !! FAILED: {type(exc).__name__}: {exc}")
                break

            for name, args in out["calls"]:
                print(f"    → {name}({args if isinstance(args, str) else json.dumps(args)})")
            reply = " ".join(out["text"].split())
            print(f"  agent> {reply[:400]}")
            print(f"         [{out['steps']} step(s), {time.monotonic() - started:.1f}s]")


async def main() -> int:
    args = sys.argv[1:]
    provider = args[0] if args and args[0] in PROVIDERS else "vercel"
    models = [a for a in args if a not in PROVIDERS] or PROVIDERS[provider][2]
    base_url, key_var, _ = PROVIDERS[provider]

    key = os.environ.get(key_var, "").strip()
    if not key or "..." in key:
        print(f"Set {key_var} in .env to use provider '{provider}'.", file=sys.stderr)
        return 2

    db.wait_for_db()
    db.init_db()

    turn = Turn(chat_id="spike")
    tools = {t.name: t for t in build_tools(turn) if not SUBSET or t.name in SUBSET}
    missing = set(SUBSET) - set(tools)
    assert not missing, f"tool names drifted: {missing}"
    schemas = openai_tools(tools)

    client = OpenAI(api_key=key, base_url=base_url)
    print(f"{len(tools)} tools exposed · provider '{provider}' · {base_url}", flush=True)

    # `models` mode: ask the provider what it actually serves, so we use real IDs
    # rather than ones guessed from a catalogue page.
    if "models" in args:
        try:
            for m in sorted(client.models.list(), key=lambda m: m.id):
                print(f"  {m.id}")
        except Exception as exc:
            print(f"!! could not list models: {type(exc).__name__}: {exc}", file=sys.stderr)
        db.close_pool()
        return 0

    for model in models:
        try:
            await exercise(client, model, tools, schemas)
        except Exception as exc:
            print(f"\n!! {model} unusable: {type(exc).__name__}: {exc}", file=sys.stderr)

    db.close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
