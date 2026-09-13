"""Copy state from the original local SQLite file and runs/*.json into the configured store.

    python scripts/migrate_local.py            # into Supabase when SUPABASE_URL/SUPABASE_SERVICE_KEY are set

Safe to run twice: every write is an upsert on the table's primary key, and chat messages are only
copied into an empty chat table. The ledger is copied first and exactly, because it is what stops
an email that already went out from going out again.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from warmpath.core import LEAD_COLUMNS, Run, load_env  # noqa: E402

load_env()

from warmpath import store  # noqa: E402

OLD = ROOT / "warmpath.db"


def rows(db: sqlite3.Connection, table: str) -> list[dict]:
    try:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute(f"SELECT * FROM {table}")]
    except sqlite3.OperationalError:
        return []


def main() -> None:
    s = store.store()
    print(f"target store: {s.backend}")
    counts = {}
    if OLD.exists():
        db = sqlite3.connect(OLD)
        for r in rows(db, "actions"):
            s.upsert("action_ledger", r)
        counts["action_ledger"] = len(rows(db, "actions"))
        for r in rows(db, "leads"):
            data = json.loads(r["data"])
            row = {k: v for k, v in data.items() if k in LEAD_COLUMNS and k != "data"}
            row.update(key=r["key"], updated_at=r["updated"],
                       data={k: v for k, v in data.items() if k not in LEAD_COLUMNS})
            s.upsert("leads", row)
        counts["leads"] = len(rows(db, "leads"))
        for table in ("approvals", "claims", "watches", "settings"):
            got = rows(db, table)
            for r in got:
                if table == "watches":
                    r["active"] = bool(r.get("active"))
                s.upsert(table, r)
            counts[table] = len(got)
        if not s.select("chat_messages", limit=1):
            old_chat = rows(db, "chat")
            for r in old_chat:
                s.insert("chat_messages", {"ts": r["ts"], "author": r["author"], "kind": r["kind"],
                                           "text": r["text"], "payload": json.loads(r["payload"] or "{}")})
            counts["chat_messages"] = len(old_chat)
    runs = sorted((ROOT / "runs").glob("*.json")) if (ROOT / "runs").exists() else []
    for p in runs:
        if p.name.startswith("_"):
            continue
        try:
            Run.load(p).save()
        except Exception as exc:  # noqa: BLE001
            print(f"  skipped {p.name}: {exc}")
    counts["runs"] = len(runs)
    for k, v in counts.items():
        print(f"  {k:16} {v}")
    print("done")


if __name__ == "__main__":
    main()
