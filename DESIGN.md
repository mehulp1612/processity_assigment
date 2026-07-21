# Supermarket Ops Agent — Design & Implementation Plan

> A conversational agent that runs an Indian kirana store end-to-end from Telegram.
> Harness: **Pydantic AI (Python)**. Model: any OpenAI-compatible endpoint (currently
> `poolside/laguna-s-2.1`). Store of record: **PostgreSQL**. Process: **FastAPI + uvicorn**.

---

## 1. Design principle (the thing being graded)

**The model orchestrates; thin tools own the rules.**

> Implementation refinement (phase 3): rules live in a `services/` layer of plain Python that
> never imports the agent SDK, and `skills/` holds thin `@tool` adapters over it. This keeps the
> tool surface thin *and* makes every invariant — oversell, idempotency, concurrency — provable
> with ordinary unit tests, independent of what the model says.

- Natural language → Claude → **tool calls**. No regex/keyword intent router anywhere on the hot path.
- Every business invariant (oversell, GST math, idempotency, khata, "don't sell below cost")
  is enforced **inside the tool, in a DB transaction** — never "hoped for" in the prompt.
- The prompt gives the agent *persona + policy + how to behave*; it gives the agent **no data** —
  all prices, slabs, stock, balances are fetched live via tools ("grounding").

If a rule can be violated by the model saying the wrong thing, it's in the wrong layer.
Our rules live where the data changes.

---

## 2. Why this harness

**Pydantic AI (Python)** — justification for the README:

| Need | How the SDK gives it |
|---|---|
| Observe→reason→act→feed-back loop, multi-tool chaining in one turn | Native to `Agent.run()`; the model selects tools, we never route |
| Author a tool surface with typed schemas | `Tool.from_schema` takes our hand-written JSON Schema verbatim |
| Continuous multi-turn conversation per chat | `message_history` per chat; a draft bill resumes across turns |
| Control which tools are callable | Only the tools we register exist — there is no built-in shell or file access |
| Persona + policy injection | `system_prompt`, rebuilt per session so stored preferences load |
| Python's mature doc stack | ReportLab (PDF), python-pptx + matplotlib (PPTX) |

We deliberately avoid a LangGraph node-per-command state machine — the assignment calls that a
misread. The "graph" here is just: model ⇄ tools ⇄ Postgres.

---

## 3. High-level architecture

```
┌────────────┐   update (update_id = idempotency key)   ┌──────────────────────────┐
│  Telegram  │ ───────────────────────────────────────▶ │  bot.py (python-telegram- │
│  (owner)   │ ◀─────────── reply / document ─────────── │  bot, long-poll)          │
└────────────┘                                            └───────────┬──────────────┘
                                                                      │ per-chat
                                                                      ▼
                                              ┌────────────────────────────────────┐
                                              │  Agent (Pydantic AI)                │
                                              │  system_prompt = persona + policy   │
                                              │      + owner preferences (loaded)   │
                                              └───────────────┬─────────────────────┘
                                                              │ model picks tools
                                                              ▼
        ┌─────────────────────────── tools registered on the agent ────────────────────────────┐
        │  SKILLS (tool groups) — all rules enforced here, inside Postgres transactions        │
        │                                                                                      │
        │  inventory   add_product · receive_stock · get_stock · low_stock · find_product      │
        │  billing     start_bill · add_line · edit_line · remove_line · view_bill ·           │
        │              finalize_bill        (oversell + GST + idempotency + below-cost guard)  │
        │  khata       khata_add · khata_settle · khata_balance · khata_statement              │
        │  analytics   daily_close · sales_report                                              │
        │  documents   render_invoice_pdf · build_analysis_pptx                                │
        │  memory      set_preference · get_preferences                                        │
        └───────────────────────────────────────┬──────────────────────────────────────────────┘
                                                 ▼
                              ┌──────────────────────────────────┐
                              │  PostgreSQL — durable store       │
                              │  survives restart & /new chat     │
                              └──────────────────────────────────┘
```

**FastAPI is transport, not a product surface.** The brief is explicit: no web app, no admin panel,
no forms — the chat is the product. So the ASGI layer exposes exactly three things: `/healthz` for the
compose healthcheck and the deployment platform, `/telegram/webhook` to receive updates, and `/chat` to
drive the same agent from `curl` while developing without a Telegram client. **There is no route that
reads or mutates store data directly** — every business operation goes through the agent's tools, which
is where the rules live. uvicorn is there to supervise and hot-reload the process, nothing more.

**Per-chat agent lifecycle.** One Pydantic AI `Agent` per Telegram chat, held in memory keyed by
`chat_id`. Conversation context (the in-progress *dialogue*) lives in that chat's `message_history`. **Durable store
state** (stock, khata, bills, prefs) lives in Postgres. A `/new` command disposes the session (fresh
conversation) but the DB — and therefore preferences and all books — is untouched. That's the memory story.

**Draft bills live in the DB, not the chat.** A multi-turn bill is a `bills` row with `status='draft'`
plus `bill_lines`. The model just carries a `bill_id`. This one decision gives us: durability across
restarts, clean concurrency (two drafts = two rows), and a natural idempotency boundary at finalize.

---

## 4. Data model (PostgreSQL)

`app/schema.sql` is the authority; this is the shape and the reasoning behind it.

```sql
CREATE TABLE shop (                      -- invoice header + memory anchor, single row
  id INTEGER PRIMARY KEY CHECK (id = 1),
  name TEXT NOT NULL, gstin TEXT, address TEXT,
  state_code TEXT NOT NULL, phone TEXT   -- state_code drives the intra-state CGST/SGST split
);

CREATE TABLE preferences (               -- cross-session memory, key/value per owner
  owner_id TEXT NOT NULL,
  key TEXT NOT NULL,                     -- e.g. default_payment, default_atta
  value TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (owner_id, key)
);

CREATE TABLE products (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,             -- "Aashirvaad Atta 5kg"
  brand TEXT, variant TEXT,
  hsn TEXT NOT NULL,                     -- HSN code
  gst_rate NUMERIC(5,2) NOT NULL,        -- 0, 5, 12, 18, 28 (percent)
  unit TEXT NOT NULL,                    -- kg | g | litre | ml | packet | dozen | piece
  is_loose BOOLEAN NOT NULL DEFAULT FALSE,
  cost_price NUMERIC(12,2) NOT NULL,     -- for the below-cost guard & margins
  mrp NUMERIC(12,2) NOT NULL,
  reorder_level NUMERIC(12,3) NOT NULL DEFAULT 0
);

CREATE TABLE stock (                     -- narrow table: a decrement is one guarded UPDATE
  product_id INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
  qty NUMERIC(12,3) NOT NULL DEFAULT 0,
  CHECK (qty >= 0)                       -- hard floor, independent of application logic
);

CREATE TABLE bills (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  invoice_no TEXT UNIQUE,                -- assigned at finalize, FY-scoped
  chat_id TEXT NOT NULL, customer TEXT,
  status TEXT NOT NULL DEFAULT 'draft',  -- draft | finalized | void
  payment_mode TEXT, payment_ref TEXT,
  subtotal NUMERIC(12,2), cgst NUMERIC(12,2), sgst NUMERIC(12,2),
  round_off NUMERIC(12,2), total NUMERIC(12,2),
  created_at TEXT NOT NULL, finalized_at TEXT,
  CHECK (status IN ('draft','finalized','void'))
);

CREATE TABLE bill_lines (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  bill_id INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
  product_id INTEGER NOT NULL REFERENCES products(id),
  qty NUMERIC(12,3) NOT NULL CHECK (qty > 0),
  unit_price NUMERIC(12,2) NOT NULL,     -- snapshot of MRP at add time
  gst_rate NUMERIC(5,2) NOT NULL,        -- snapshot of the slab at add time
  line_taxable NUMERIC(12,2), line_cgst NUMERIC(12,2),
  line_sgst NUMERIC(12,2), line_total NUMERIC(12,2),
  UNIQUE (bill_id, product_id)           -- one line per SKU; "make it 6" updates it
);

CREATE TABLE khata (                     -- one running ledger per customer
  customer TEXT PRIMARY KEY,
  balance NUMERIC(12,2) NOT NULL DEFAULT 0,   -- positive = customer owes the shop
  updated_at TEXT NOT NULL
);
CREATE TABLE khata_txns (                -- audit trail; balance is reconstructable
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  customer TEXT NOT NULL REFERENCES khata(customer),
  delta NUMERIC(12,2) NOT NULL, reason TEXT,
  bill_id INTEGER REFERENCES bills(id), at TEXT NOT NULL
);

CREATE TABLE payments (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  bill_id INTEGER NOT NULL REFERENCES bills(id),
  mode TEXT NOT NULL, ref TEXT, amount NUMERIC(12,2) NOT NULL, at TEXT NOT NULL
);

CREATE TABLE processed_ops (             -- idempotency: Telegram redelivers updates
  op_key TEXT PRIMARY KEY,
  result JSONB,                          -- the original result, replayed verbatim
  at TEXT NOT NULL
);

CREATE TABLE invoice_seq (               -- a plain sequence would not reset on 1 April
  fy TEXT PRIMARY KEY, last INTEGER NOT NULL DEFAULT 0
);
```

Every mutating service wraps its reads+writes in a single transaction. Postgres gives
READ COMMITTED with row-level locking: concurrent bills contend on the `stock` row, and
the loser re-evaluates the guard against the winner's committed qty instead of overselling.
Stock rows are locked in `product_id` order so two bills sharing items cannot deadlock.
Money and quantities are `NUMERIC`, never floating point — the GST engine computes in
`Decimal` and the database stores the result exactly, so a day's `SUM()` cannot drift.

---

## 5. The skill / tool surface (heart of the grade)

Tools are **thin, single-purpose, and validate at the boundary**. Signatures (Python `@tool`):

### inventory
- `find_product(query)` → resolve fuzzy owner text ("atta") to candidate SKUs. Returns matches so the
  **model** can ask a clarifying question when ambiguous (Aashirvaad 5kg vs loose). No auto-guess.
- `add_product(name, hsn, gst_rate, unit, is_loose, cost_price, mrp, reorder_level, brand?, variant?)`
- `receive_stock(product_id, qty, cost_price?, mrp?)` → increments stock atomically; can update cost/MRP.
- `get_stock(product_id | query)` → current qty.
- `low_stock()` → SKUs at/below reorder level.

### billing  (draft-in-DB, finalize is the only stock mutation)
- `start_bill(chat_id, customer?)` → new draft, returns `bill_id`.
- `add_line(bill_id, product_id, qty)` → **soft** availability check + GST snapshot; recomputes totals.
- `edit_line(bill_id, line_id|product_id, qty)` / `remove_line(...)` → supports "drop the butter, make it 6 Maggi".
- `view_bill(bill_id)` → itemized preview with tax breakup.
- `finalize_bill(bill_id, payment_mode, payment_ref?, update_id)` → **the critical tool**:
  1. Idempotency: if `update_id` already in `processed_updates`, return the prior result.
  2. Open the transaction; re-fetch live stock; **oversell guard** (`qty >= line.qty` for every line, else refuse with the shortfall).
  3. **Below-cost guard**: if any `unit_price < cost_price`, refuse/flag for confirmation.
  4. Recompute GST fresh (never trust the draft snapshot for money), decrement stock, write payment or khata debit, mark finalized, record `update_id`. `COMMIT`.

### khata
- `khata_add(customer, amount, bill_id?)` · `khata_settle(customer, amount)` (refuse if no such khata / overpay → confirm) · `khata_balance(customer)` · `khata_statement(customer)`.

### analytics
- `daily_close(date?)` → totals, tax collected, cash vs UPI vs card, top items.
- `sales_report(from, to)` → aggregates feeding the deck.

### documents
- `render_invoice_pdf(bill_id)` → ReportLab GST invoice; returns file path → bot uploads.
- `build_analysis_pptx(from, to)` → python-pptx deck; matplotlib charts (sales trend, top items,
  stock health, GST collected) embedded as images. Returns file path.

### memory
- `get_preferences(owner_id)` (loaded into the system prompt at session start) ·
  `set_preference(owner_id, key, value)` ("always assume UPI", "default atta = Aashirvaad 5kg").

---

## 6. GST engine (correctness spec)

Per line, intra-state (shop & customer same state → CGST + SGST):

```
line_taxable = round(unit_price * qty, 2)              # MRP treated as taxable value
line_gst     = round(line_taxable * gst_rate/100, 2)
line_cgst    = round(line_gst / 2, 2)
line_sgst    = line_gst - line_cgst                    # avoid double-rounding drift
line_total   = line_taxable + line_gst
```
Bill: sum lines → `subtotal, cgst, sgst`; `grand = subtotal+cgst+sgst`;
`round_off = round(grand) - grand`; `total = round(grand)`.
Invoice shows a **per-slab tax breakup table** (taxable | CGST% | SGST% | amount) — the legal format.
Slabs seeded realistically: loose atta/rice/produce **0%**, packaged atta/salt/oil **5%**,
biscuits/soap/chocolate **12–18%**.

---

## 7. How each hard part is solved (README table)

| Hard part | Mechanism |
|---|---|
| **Grounding** | Prompt carries zero data; `find_product`/`get_stock` are the only price/stock source. |
| **Oversell guard** | `finalize_bill` re-checks live qty in-transaction, then decrements via a guarded `UPDATE … WHERE qty >= %s`; `rowcount != 1` aborts. Tool-layer, not prompt. |
| **GST correctness** | §6 engine, computed in tool at finalize; per-slab breakup on invoice. |
| **Multi-turn bills** | Draft `bills`/`bill_lines` rows; edits mutate the draft; **stock only moves at finalize**. |
| **Idempotency** | `processed_updates(update_id)` PK + finalize returns cached result on retry → no double-bill. |
| **Concurrency** | Postgres row locks serialize sale vs sale and sale vs stock-in; stock rows are locked in `product_id` order to avoid deadlocks. |
| **Guardrails** | Below-cost refuse/confirm; khata settle refuses unknown customer/overpay; no destructive stock delete tool. |
| **Real artifacts** | ReportLab PDF + python-pptx/matplotlib deck, produced by tools, uploaded by the bot. |
| **Memory** | `preferences` table, loaded into `system_prompt` each session; survives `/new` and restart. |

---

## 8. Control loop (what one message does)

1. Telegram update arrives with `update_id`; bot routes to the `chat_id`'s `ClaudeSDKClient`.
2. `client.query(owner_text)` — model observes, reasons, calls tools (possibly several in one turn:
   `find_product` → `add_line` → `view_bill`).
3. Each tool result feeds back; model continues until it has a final natural-language reply
   (or a document to send). Bot streams the reply; uploads any generated file.
4. All money/stock effects already committed to Postgres inside the tools.

---

## 9. Project layout

```
supermarket-agent/
  app/
    bot.py                # Telegram long-poll, per-chat session registry, /new, file upload
    agent.py             # ClaudeSDKClient wiring, system prompt assembly, pref loading
    db.py                # psycopg pool, schema bootstrap, tx helper
    api.py               # FastAPI: /healthz, Telegram webhook, /chat — transport only
    prompts/system.md    # persona + policy (NO data)
    services/            # RULES live here (plain Python, DB-backed, no SDK import)
      common.py          # DomainError, idempotency ledger, FY invoice numbers
      inventory.py       # catalogue + stock
      billing.py         # drafts + finalize (the crown jewel)
      khata.py
      analytics.py
      documents.py       # pdf.py + pptx.py helpers
      memory.py
    skills/              # thin @tool adapters over services/ (arg mapping + formatting)
      inventory.py · billing.py · khata.py · analytics.py · documents.py · memory.py
    domain/
      gst.py             # pure GST math (unit-tested)
      money.py           # rounding helpers
  (Postgres runs as its own container; data lives in the `pgdata` volume)
  scripts/seed.py        # realistic SKUs + slabs + opening stock
  tests/                 # gst math, oversell, idempotency, concurrency
  .env.example           # ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN
  README.md              # ~1 page: harness/why, control loop, tool design, hard parts
  requirements.txt
```

---

## 10. Build phases

1. **Skeleton + DB** — schema, migrations, seed script, tx helper. *(foundation)*
2. **Domain core** — `gst.py`, `money.py` + unit tests (prove correctness before wiring agent).
3. **Inventory + billing tools** — including the finalize crown jewel; tests for oversell/idempotency/concurrency.
4. **Agent wiring** — tools, system prompt, preference loading; a CLI harness and a `/chat`
   endpoint to drive the agent without Telegram.
   *Built.* 20 tools across four skill groups. Two decisions worth noting: `chat_id` and `op_key`
   are bound per chat via a `Turn` the tools close over, so neither is a model-supplied argument
   (an idempotency key the model could forget would not be one); and tools are declared against a
   local `StoreTool` descriptor rather than a vendor decorator, which is what made the later
   harness switch a one-line import change per skill module.
   *Provider note.* Originally built on the Claude Agent SDK, then moved to Pydantic AI over an
   OpenAI-compatible endpoint so the project runs on a free model. Because `services/` never
   imported the SDK, all 34 rules tests were untouched by the move.
5. **Telegram** — `app/bot.py`, per-chat sessions, `/new`, `/start`, file delivery.
   *Built.* Runs inside the uvicorn process (supervised, one container). Long-polls locally so
   no tunnel is needed; set `TELEGRAM_WEBHOOK_URL` and it registers a webhook instead — same
   handlers either way. `update_id` becomes the turn's idempotency key, so a redelivered update
   replays rather than re-billing. Tools append generated files to `Turn.attachments` and the
   transport flushes them, which keeps document tools ignorant of Telegram.
6. **khata + analytics** tools.
   *Built.* `daily_close` and `sales_report`. Two things this layer gets right that a naive
   version wouldn't: a **day is a shop-local day** (Asia/Kolkata), because timestamps are stored
   UTC and a sale at 00:30 IST would otherwise be filed under yesterday; and **margin is computed
   on taxable value**, since GST is the government's money passing through the till, not revenue.
   The `by_day` series includes zero-sale days so a chart gap reads as a quiet day, not missing
   data — which is what the phase-7 deck plots.
7. **Artifacts** — PDF invoice + PPTX deck.
   *Built.* `render_invoice_pdf` produces a real GST tax invoice (HSN codes, per-line CGST/SGST,
   slab summary, amount in words in **lakh/crore**, not millions). `build_analysis_pptx` produces
   a 7-slide deck with three matplotlib charts embedded as images — deliberately not native
   PowerPoint charts, which would carry their own copy of the data and could drift from the
   invoice totals. Both read from the books; neither computes a number of its own.
   Two traps handled: ReportLab's built-in Helvetica has no ₹ glyph, so DejaVu is embedded (a
   test asserts this — otherwise every amount silently renders as a black box); and tools append
   the file path to `Turn.attachments` rather than delivering it, so the document services never
   learn what Telegram is.
8. **Memory** polish + end-to-end run of all §3 scenarios.
9. **README + recording script**; then deploy (tunnel/host).

Testable slices land early (phases 2–3 are pure-Python, no API key needed).

---

## 11. Stack / dependencies

`pydantic-ai-slim[openai]`, `python-telegram-bot`, `reportlab`, `python-pptx`, `matplotlib`,
`python-dotenv`. PostgreSQL 17 via `psycopg` 3 with a connection pool; FastAPI + uvicorn as the
process host. Python 3.13. Everything runs under `docker compose` (app + db).

---

## 12. Open questions for BigMantra (the "knowing what to ask" signal)

1. **Single-owner or multi-tenant?** Assumed single shop / single owner (`owner_id` still parameterized so multi-tenant is a config change, not a rewrite).
2. **Invoice numbering** — sequential per financial year (FY-scoped) OK?
3. **Rounding convention** — bill-level round-to-nearest-rupee with a shown `round_off` line (standard kirana). Confirm.
4. **Intra-state only** (CGST+SGST) — assumed; IGST/inter-state out of scope unless flagged.
5. **"Below cost"** — hard refuse, or refuse-unless-confirmed? Plan: refuse-unless-owner-confirms.
```
