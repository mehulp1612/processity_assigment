"""Memory that outlives the context window.

Standing preferences ("always bill Maggi as a dozen", "default payment is UPI")
are rows in SQLite, not facts in a transcript. They are read back into the
system prompt at the start of *every* session, so a `/new` chat — or a process
restart, or a redeploy — still knows them.
"""

from __future__ import annotations

from typing import Optional

from .. import db
from .common import DomainError, now_iso, row_to_dict


def set_preference(owner_id: str, key: str, value: str) -> dict:
    key = " ".join(str(key).split()).strip().lower().replace(" ", "_")
    value = str(value).strip()
    if not key or not value:
        raise DomainError("BAD_ARGS", "Both a preference key and a value are required.")

    with db.tx() as cx:
        cx.execute(
            """INSERT INTO preferences (owner_id, key, value, updated_at) VALUES (%s,%s,%s,%s)
               ON CONFLICT (owner_id, key) DO UPDATE SET
                 value = excluded.value, updated_at = excluded.updated_at""",
            (str(owner_id), key, value, now_iso()),
        )
    return {"ok": True, "key": key, "value": value}


def get_preferences(owner_id: str) -> dict[str, str]:
    with db.tx() as cx:
        rows = cx.execute(
            "SELECT key, value FROM preferences WHERE owner_id = %s ORDER BY key",
            (str(owner_id),),
        ).fetchall()
    return {r["key"]: r["value"] for r in rows}


def forget_preference(owner_id: str, key: str) -> dict:
    key = " ".join(str(key).split()).strip().lower().replace(" ", "_")
    with db.tx() as cx:
        cur = cx.execute(
            "DELETE FROM preferences WHERE owner_id = %s AND key = %s", (str(owner_id), key)
        )
        if cur.rowcount == 0:
            raise DomainError("NO_SUCH_PREFERENCE", f"No standing preference named '{key}'.")
    return {"ok": True, "forgotten": key}


def get_shop() -> Optional[dict]:
    with db.tx() as cx:
        row = cx.execute("SELECT * FROM shop WHERE id = 1").fetchone()
    return None if row is None else row_to_dict(row)
