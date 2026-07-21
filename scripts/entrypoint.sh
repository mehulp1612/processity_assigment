#!/usr/bin/env bash
# Wait for Postgres, apply the schema, seed on first boot, then run the CMD.
set -euo pipefail

echo "[entrypoint] waiting for postgres…"
# Apply the schema, then seed only when the catalogue is empty, so restarts and
# redeploys never clobber real data.
python - <<'PY'
from app import db
from scripts.seed import seed

db.wait_for_db(timeout=90)
db.init_db()
print("[entrypoint] schema applied")

with db.tx() as cx:
    n = cx.execute("SELECT COUNT(*) AS c FROM products").fetchone()["c"]

if n == 0:
    print("[entrypoint] empty catalogue — seeding")
    seed(reset=False)
else:
    print(f"[entrypoint] {n} products present — leaving data intact")

db.close_pool()   # don't leave pool threads behind for the exec below
PY

exec "$@"
