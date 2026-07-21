"""Test fixtures.

Isolation model: one dedicated test database, wiped and reseeded before every
test. ``TRUNCATE ... RESTART IDENTITY`` also resets the identity counters, so
tests that assert on generated ids and on sequential invoice numbers start from
a known point every time.

Point the suite at a database with ``TEST_DATABASE_URL`` (defaults to the
``store_test`` database on the same server as ``DATABASE_URL``).
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest


def _default_test_url() -> str:
    base = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/store")
    parts = urlsplit(base)
    return urlunsplit(parts._replace(path="/store_test"))


TEST_URL = os.environ.get("TEST_DATABASE_URL", _default_test_url())


def _admin_url(url: str) -> str:
    """Same server, but the always-present maintenance database."""
    return urlunsplit(urlsplit(url)._replace(path="/postgres"))


def _ensure_database(url: str) -> None:
    """CREATE DATABASE if it isn't there yet (connecting via the maintenance db)."""
    dbname = urlsplit(url).path.lstrip("/")
    with psycopg.connect(_admin_url(url), autocommit=True) as cx:
        exists = cx.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
        ).fetchone()
        if not exists:
            cx.execute(f'CREATE DATABASE "{dbname}"')


@pytest.fixture(scope="session", autouse=True)
def database():
    """Create and migrate the test database once per run."""
    from app import db

    # Wait on the *server* via the maintenance db — the test database itself
    # does not exist yet on a first run.
    try:
        db.wait_for_db(timeout=30, url=_admin_url(TEST_URL))
    except RuntimeError as exc:
        pytest.skip(
            f"PostgreSQL not reachable ({exc}). Start it with `docker compose up -d db`, "
            f"or run the suite inside the stack: `docker compose run --rm app python -m pytest`."
        )

    _ensure_database(TEST_URL)
    os.environ["DATABASE_URL"] = TEST_URL
    db.close_pool()          # (re)open the pool against the test database
    db.init_db()
    yield TEST_URL
    db.close_pool()


@pytest.fixture(autouse=True)
def fresh_store(database):
    """Give every test the seeded catalogue and nothing else."""
    from app import db
    from scripts.seed import seed

    db.truncate_all()
    seed(reset=False)
    yield


@pytest.fixture
def pid():
    """Look up a product id by name."""
    from app import db

    def _pid(name: str) -> int:
        with db.tx() as cx:
            return cx.execute(
                "SELECT id FROM products WHERE name = %s", (name,)
            ).fetchone()["id"]

    return _pid
