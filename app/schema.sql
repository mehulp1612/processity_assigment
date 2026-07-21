-- Supermarket Ops Agent — durable store of record (PostgreSQL).
--
-- Every invariant that money or stock depends on is enforced *here* and in the
-- service layer that writes it, inside a single transaction — never in a prompt.
--
-- Two deliberate choices:
--   * Money and quantities are NUMERIC, never floating point. GST is computed in
--     Decimal and stored exactly, so SUM() over a day's bills cannot drift.
--   * Timestamps are ISO-8601 UTC text. They sort lexicographically, which is all
--     the range scans (daily close, statements) need, and keeps one canonical
--     string form across DB, tool results and invoices.

-- Owner & store identity (invoice header + memory anchor). Single row.
CREATE TABLE IF NOT EXISTS shop (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    name        TEXT NOT NULL,
    gstin       TEXT,
    address     TEXT,
    state_code  TEXT NOT NULL,   -- GST state code; drives intra-state CGST/SGST split
    phone       TEXT
);

-- Cross-session memory: standing preferences per owner. Loaded into the
-- system prompt at session start; survives /new and process restart.
CREATE TABLE IF NOT EXISTS preferences (
    owner_id    TEXT NOT NULL,
    key         TEXT NOT NULL,       -- e.g. default_payment, default_atta, currency
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (owner_id, key)
);

-- Catalogue. Price/GST/HSN are the single source of truth (grounding).
CREATE TABLE IF NOT EXISTS products (
    id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,        -- "Aashirvaad Atta 5kg"
    brand         TEXT,
    variant       TEXT,
    hsn           TEXT NOT NULL,               -- HSN code
    gst_rate      NUMERIC(5,2)  NOT NULL,      -- percent: 0, 5, 12, 18, 28
    unit          TEXT NOT NULL,               -- kg | g | litre | ml | packet | dozen | piece
    is_loose      BOOLEAN NOT NULL DEFAULT FALSE,
    cost_price    NUMERIC(12,2) NOT NULL,      -- for below-cost guard & margins
    mrp           NUMERIC(12,2) NOT NULL,      -- sell price
    reorder_level NUMERIC(12,3) NOT NULL DEFAULT 0,
    CHECK (gst_rate >= 0),
    CHECK (cost_price >= 0),
    CHECK (mrp >= 0)
);

-- Stock kept in its own narrow table so a decrement is one guarded UPDATE and
-- concurrent sales contend on a single short-lived row lock.
CREATE TABLE IF NOT EXISTS stock (
    product_id INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
    qty        NUMERIC(12,3) NOT NULL DEFAULT 0,
    CHECK (qty >= 0)                          -- hard floor: stock can never go negative
);

-- Bills. A draft is a real row; multi-turn edits mutate it; stock only moves
-- when status flips to 'finalized'.
CREATE TABLE IF NOT EXISTS bills (
    id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    invoice_no    TEXT UNIQUE,                     -- assigned at finalize (FY-scoped)
    chat_id       TEXT NOT NULL,
    customer      TEXT,                            -- optional (khata name)
    status        TEXT NOT NULL DEFAULT 'draft',   -- draft | finalized | void
    payment_mode  TEXT,                            -- cash | upi | card | khata
    payment_ref   TEXT,
    subtotal      NUMERIC(12,2),
    cgst          NUMERIC(12,2),
    sgst          NUMERIC(12,2),
    round_off     NUMERIC(12,2),
    total         NUMERIC(12,2),
    created_at    TEXT NOT NULL,
    finalized_at  TEXT,
    CHECK (status IN ('draft', 'finalized', 'void'))
);

CREATE TABLE IF NOT EXISTS bill_lines (
    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    bill_id      INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    product_id   INTEGER NOT NULL REFERENCES products(id),
    qty          NUMERIC(12,3) NOT NULL,
    unit_price   NUMERIC(12,2) NOT NULL,       -- snapshot of MRP at add time
    gst_rate     NUMERIC(5,2)  NOT NULL,       -- snapshot of slab at add time
    line_taxable NUMERIC(12,2),
    line_cgst    NUMERIC(12,2),
    line_sgst    NUMERIC(12,2),
    line_total   NUMERIC(12,2),
    CHECK (qty > 0),
    UNIQUE (bill_id, product_id)               -- one line per SKU; edits update it
);

-- Khata: one running balance per customer (positive = customer owes shop).
CREATE TABLE IF NOT EXISTS khata (
    customer   TEXT PRIMARY KEY,
    balance    NUMERIC(12,2) NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS khata_txns (
    id       INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer TEXT NOT NULL REFERENCES khata(customer),
    delta    NUMERIC(12,2) NOT NULL,   -- +debit (bought on credit) / -credit (paid)
    reason   TEXT,
    bill_id  INTEGER REFERENCES bills(id),
    at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
    id      INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    bill_id INTEGER NOT NULL REFERENCES bills(id),
    mode    TEXT NOT NULL,
    ref     TEXT,
    amount  NUMERIC(12,2) NOT NULL,
    at      TEXT NOT NULL
);

-- Idempotency ledger: Telegram redelivers updates. A retried finalize keyed by
-- the same op_key returns the cached result instead of double-billing.
CREATE TABLE IF NOT EXISTS processed_ops (
    op_key TEXT PRIMARY KEY,
    result JSONB,                    -- snapshot of the tool result, replayed verbatim
    at     TEXT NOT NULL
);

-- Running invoice number sequence, per financial year. A plain sequence would
-- not reset on 1 April, so the counter is a row we lock and bump.
CREATE TABLE IF NOT EXISTS invoice_seq (
    fy   TEXT PRIMARY KEY,           -- e.g. "2026-27"
    last INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_bill_lines_bill ON bill_lines(bill_id);
CREATE INDEX IF NOT EXISTS idx_bills_status    ON bills(status);
CREATE INDEX IF NOT EXISTS idx_bills_chat      ON bills(chat_id, status);
CREATE INDEX IF NOT EXISTS idx_bills_finalized ON bills(finalized_at);
CREATE INDEX IF NOT EXISTS idx_khata_txns_cust ON khata_txns(customer);
