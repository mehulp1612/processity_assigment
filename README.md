# Supermarket Ops Agent

A conversational agent that runs a small Indian kirana store end-to-end from Telegram.
Billing, GST, stock, khata, invoices and analysis — no web app, no admin panel, no forms.

### Live bot: **[@processity_kirana_bot](https://t.me/processity_kirana_bot)**

Running on an always-on AWS Lightsail VM and kept up through the review window.
Say `/start`, then just talk:

> `2 kg sugar and 4 maggi, cash` · `how much atta is left?` · `received 20 Tata Salt` ·
> `Ramesh paid 300` · `invoice for the last bill` · `close the day` ·
> `make me a deck for this week`

`/new` starts a fresh conversation. Stock, khatas and saved preferences survive it.

---

## Harness, and why

**Pydantic AI** (Python) against any OpenAI-compatible endpoint — currently the free
`poolside/laguna-s-2.1`. Changing provider is three lines in `.env`.

The loop is `Agent.run()`: the model observes, picks tools, reads results, continues until
it has an answer. **There is no intent router** — no regex, no keyword matching, no
`if/elif` over commands anywhere on the hot path. When the owner asks "how much atta is
left?" and the shop stocks both _Aashirvaad Atta 5kg_ (5% GST) and _Loose Wheat Atta_ (0%),
`find_product` returns both candidates and **the model** asks which one was meant. That
clarification is not a branch; it is the model doing its job with a tool that declines to
guess on its behalf.

This started on the Claude Agent SDK and moved to Pydantic AI to run on a free model.
Because `services/` never imported an SDK, that migration changed no rules and broke no tests.

## Where the rules live

```
Telegram ──update_id──▶ bot.py ──▶ Agent (model picks tools) ──▶ skills/ ──▶ services/ ──▶ Postgres
                                                             thin adapters   RULES, in transactions
```

**25 tools** across six skill groups: `inventory · billing · khata · analytics · documents · memory`.

`skills/` are thin adapters — argument shaping and formatting, nothing more. Every business
invariant lives in `services/`, plain Python that never imports the agent SDK, enforced
**inside a Postgres transaction at the point the data changes**. The system prompt carries
persona and policy and **zero data**: no prices, no slabs, no stock levels. If a rule can be
broken by the model saying the wrong thing, it is in the wrong layer.

The payoff is that every invariant is provable without a model in the loop: **77 tests**
covering GST math, oversell, idempotency, concurrency, khata and document rendering.

## The hard parts

|                      | How                                                                                                                                                                                                                                                      |
| -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Grounding**        | The prompt holds no data. `find_product` / `get_stock` are the only source of price and stock.                                                                                                                                                           |
| **Oversell**         | `finalize_bill` re-reads live qty inside the transaction, then decrements with a guarded `UPDATE … WHERE qty >= %s`; `rowcount != 1` aborts the sale. A `CHECK (qty >= 0)` sits underneath as a floor that holds even if the application logic is wrong. |
| **GST**              | Computed in `Decimal` at finalize, per line, per slab. CGST = `round(gst/2)`, SGST = `gst − CGST`, so halving can't drift. Bill-level round-off is its own visible line. The invoice carries the legal per-slab breakup with HSN codes.                  |
| **Multi-turn bills** | A draft is a `bills` row, not chat state — so it survives a restart, two drafts are two rows, and "drop the butter, make it 6 Maggi" is an ordinary UPDATE. **Stock moves only at finalize.**                                                            |
| **Idempotency**      | Telegram's `update_id` becomes the turn's `op_key`, injected by the transport and never a model argument — an idempotency key the model could forget to pass wouldn't be one. `processed_ops` replays the original result verbatim.                      |
| **Concurrency**      | Row locks serialize sale-vs-sale and sale-vs-stock-in; stock rows are locked in `product_id` order so two bills sharing items can't deadlock.                                                                                                            |
| **Guardrails**       | Below-cost sales refuse until the owner confirms — the tool reports the loss in rupees and **the model asks**; it cannot set the override itself. Khata settle refuses unknown customers and overpayment. No destructive stock tool exists.              |
| **Artifacts**        | A real GST invoice PDF (ReportLab — DejaVu is embedded because Helvetica has no ₹ glyph, and a test asserts it) and a 7-slide PPTX with three matplotlib charts. Both read from the books; neither computes a number of its own.                         |
| **Memory**           | A `preferences` table rebuilt into the system prompt at session start. Survives `/new` and restart because it is never carried in context.                                                                                                               |

## Running it

```bash
cp .env.example .env          # TELEGRAM_BOT_TOKEN + MODEL_API_KEY
docker compose up -d db
docker compose run --rm app python -m scripts.seed --reset   # 17 real SKUs across 0/5/12/18% slabs
docker compose run --rm app python -m pytest
docker compose up
```

`scripts/smoke.py` exercises the services directly — it proves the numbers are right.
`scripts/scenarios.py` drives the same scenarios **through the model** over `/chat`, which
proves the agent can reach those numbers from ordinary shopkeeper phrasing. Production
topology and the deployment runbook are in `DEPLOY.md`; the design reasoning, schema and
phase log are in `DESIGN.md`.

## Known limitation

Preferences persist reliably, and _actionable_ ones change behaviour — a stored "default to
UPI" shows up in the next bill after `/new`. But soft "remind me about X" instructions are
applied inconsistently by `laguna-s-2.1`. I checked the built system prompt: the instruction
is verifiably there and the model skips it. I strengthened the wording once and then stopped,
because this is a capability ceiling of a small free model rather than a defect in the memory
layer, and tuning the prompt until one anecdote looked fixed would have hidden that.
