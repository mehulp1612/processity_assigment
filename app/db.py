"""PostgreSQL store: pooled connections, schema bootstrap, transaction helper.

Concurrency model
-----------------
Every mutating service wraps its reads *and* writes in one ``with db.tx() as cx:``
block. Postgres gives us READ COMMITTED with row-level locking, which is what
makes the oversell guard safe under real concurrency:

    UPDATE stock SET qty = qty - %s WHERE product_id = %s AND qty >= %s

Two bills finalizing the same SKU contend on that single row. The loser blocks
until the winner commits, then **re-evaluates the predicate against the newly
committed qty** — so it matches zero rows, ``rowcount != 1``, and the whole
transaction aborts rather than overselling. The ``CHECK (qty >= 0)`` on the
table is the backstop if that logic is ever bypassed.

Reads use the same helper: a read-only transaction from the pool is cheap, and
one mechanism is easier to reason about than two.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Iterator, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_SCHEMA = Path(__file__).with_name("schema.sql")
_DEFAULT_URL = "postgresql://postgres:postgres@db:5432/store"

_pool: Optional[ConnectionPool] = None


def database_url() -> str:
    return os.environ.get("DATABASE_URL", _DEFAULT_URL)


def _numeric_dict_row(cursor):
    """dict rows with NUMERIC decoded to float.

    Money is stored as exact NUMERIC so the database never drifts. Python-side we
    hand the services plain floats — the GST engine re-derives every amount in
    ``Decimal`` from (unit_price, qty, gst_rate) anyway, so nothing downstream
    depends on the driver's numeric type.
    """
    make_row = dict_row(cursor)

    def make(values):
        row = make_row(values)
        for k, v in row.items():
            if isinstance(v, Decimal):
                row[k] = float(v)
        return row

    return make


def pool() -> ConnectionPool:
    """Process-wide connection pool, opened lazily."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            database_url(),
            min_size=1,
            max_size=int(os.environ.get("DB_POOL_MAX", "10")),
            kwargs={"row_factory": _numeric_dict_row, "autocommit": False},
            open=True,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def tx() -> Iterator[psycopg.Connection]:
    """Run a block inside a single transaction.

    Commits on success, rolls back on any exception. This is the unit of
    atomicity for every money/stock mutation in the app.
    """
    with pool().connection() as cx:   # commits on clean exit, rolls back on raise
        yield cx


def wait_for_db(timeout: float = 60.0, url: Optional[str] = None) -> None:
    """Block until Postgres accepts connections (container start-up ordering)."""
    target = url or database_url()
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(target, connect_timeout=3) as cx:
                cx.execute("SELECT 1")
            return
        except Exception as exc:       # not up yet
            last = exc
            time.sleep(1.0)
    raise RuntimeError(f"Database not reachable at {target}: {last}")


def init_db() -> None:
    """Apply the schema. Idempotent (CREATE ... IF NOT EXISTS throughout)."""
    with tx() as cx:
        cx.execute(_SCHEMA.read_text(encoding="utf-8"))


def reset_db() -> None:
    """Drop and recreate the whole schema. Dev/tests only."""
    with tx() as cx:
        cx.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    init_db()


def truncate_all() -> None:
    """Wipe every row and reset identity counters, keeping the schema. Tests only."""
    with tx() as cx:
        rows = cx.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
        if rows:
            names = ", ".join(r["tablename"] for r in rows)
            cx.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")


if __name__ == "__main__":
    wait_for_db()
    init_db()
    print(f"Initialised store at {database_url()}")
