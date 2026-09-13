"""Where WarmPath keeps its state: Supabase (Postgres) when configured, SQLite otherwise.

One small table API used by everything - ledger, leads, runs, chat, approvals, autopilot claims,
audit issues, eval results - so the product logic never knows which database it is talking to.

    SUPABASE_URL + SUPABASE_SERVICE_KEY set  ->  Postgres through the Supabase REST API
    otherwise                                ->  warmpath.db next to the code, zero setup

The schema is the same in both: supabase/migrations/001_warmpath.sql, mirrored below for SQLite.
The one operation that must be atomic - "may this external action run?" - is a database function
in Postgres (ledger_begin) and a single transaction in SQLite.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "warmpath.sqlite"

#: table -> (primary key column, json columns)
TABLES = {
    "leads": ("key", {"offered", "data"}),
    "runs": ("run_id", {"states", "steps", "evidence"}),
    "action_ledger": ("action_id", set()),
    "chat_messages": ("id", {"payload"}),
    "approvals": ("run_id", set()),
    "claims": ("key", set()),
    "watches": ("source", set()),
    "settings": ("key", set()),
    "audit_violations": ("id", set()),
    "eval_results": ("id", {"report"}),
}

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (key TEXT PRIMARY KEY, email TEXT, name TEXT, company TEXT, domain TEXT,
  comment TEXT, intent TEXT, confidence REAL, warm INTEGER DEFAULT 0, knows TEXT, stage TEXT, status TEXT,
  subject TEXT, sent_at TEXT, offered TEXT DEFAULT '{}', pending_booking TEXT DEFAULT '', meeting TEXT,
  comment_id TEXT, lead_run TEXT, data TEXT DEFAULT '{}', updated_at TEXT);
CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, kind TEXT, subject TEXT, message_id TEXT, mode TEXT,
  state TEXT, states TEXT, steps TEXT, result TEXT, reason_code TEXT, evidence TEXT, started TEXT,
  is_eval INTEGER DEFAULT 0, violations INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS action_ledger (action_id TEXT PRIMARY KEY, run_id TEXT, kind TEXT, status TEXT,
  attempts INTEGER DEFAULT 0, detail TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS chat_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, author TEXT, kind TEXT,
  text TEXT, payload TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS approvals (run_id TEXT PRIMARY KEY, decision TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS claims (key TEXT PRIMARY KEY, kind TEXT, status TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS watches (source TEXT PRIMARY KEY, label TEXT, added TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS audit_violations (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, agent TEXT,
  invariant TEXT, instruction TEXT, detail TEXT, created_at TEXT, UNIQUE (run_id, invariant, detail));
CREATE TABLE IF NOT EXISTS eval_results (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT,
  runs_per_comment INTEGER, scenario_pass INTEGER, scenario_total INTEGER, audit_violations INTEGER, report TEXT);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StoreError(RuntimeError):
    pass


class SQLiteStore:
    backend = "sqlite"

    def __init__(self, path: Path | str = DB_PATH):
        self.path = str(path)
        self._lock = threading.Lock()
        with self._conn() as db:
            db.executescript(SQLITE_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _encode(table: str, row: dict) -> dict:
        json_cols = TABLES[table][1]
        out = {}
        for k, v in row.items():
            if k in json_cols and not isinstance(v, str):
                v = json.dumps(v, default=str)
            elif isinstance(v, bool):
                v = int(v)
            out[k] = v
        return out

    @staticmethod
    def _decode(table: str, row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        json_cols = TABLES[table][1]
        d = dict(row)
        for k in json_cols:
            if isinstance(d.get(k), str):
                try:
                    d[k] = json.loads(d[k])
                except json.JSONDecodeError:
                    pass
        for k in ("warm", "is_eval", "active"):
            if k in d and d[k] is not None:
                d[k] = bool(d[k])
        return d

    def select(self, table: str, where: dict | None = None, *, order: str = "", desc: bool = False,
               limit: int | None = None) -> list[dict]:
        where = where or {}
        sql = f"SELECT * FROM {table}"
        args: list = []
        if where:
            parts = []
            for k, v in where.items():
                if isinstance(v, (list, tuple, set)):
                    parts.append(f"{k} IN ({','.join('?' * len(v))})")
                    args += list(v)
                else:
                    parts.append(f"{k} = ?")
                    args.append(int(v) if isinstance(v, bool) else v)
            sql += " WHERE " + " AND ".join(parts)
        if order:
            sql += f" ORDER BY {order} {'DESC' if desc else 'ASC'}"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._conn() as db:
            return [self._decode(table, r) for r in db.execute(sql, args).fetchall()]

    def get(self, table: str, key) -> dict | None:
        pk = TABLES[table][0]
        rows = self.select(table, {pk: key}, limit=1)
        return rows[0] if rows else None

    def upsert(self, table: str, row: dict) -> dict:
        pk = TABLES[table][0]
        enc = self._encode(table, row)
        cols = list(enc)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != pk) or f"{pk}=excluded.{pk}"
        with self._lock, self._conn() as db:
            db.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
                       f"ON CONFLICT({pk}) DO UPDATE SET {updates}", [enc[c] for c in cols])
        return row

    def insert(self, table: str, row: dict, *, ignore_conflict: bool = False) -> dict:
        enc = self._encode(table, row)
        cols = list(enc)
        verb = "INSERT OR IGNORE" if ignore_conflict else "INSERT"
        with self._lock, self._conn() as db:
            cur = db.execute(f"{verb} INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                             [enc[c] for c in cols])
            return {**row, "id": cur.lastrowid}

    def update(self, table: str, key, patch: dict) -> None:
        pk = TABLES[table][0]
        enc = self._encode(table, patch)
        with self._lock, self._conn() as db:
            db.execute(f"UPDATE {table} SET {', '.join(f'{c}=?' for c in enc)} WHERE {pk}=?",
                       [*enc.values(), key])

    def delete(self, table: str, where: dict | None = None) -> None:
        where = where or {}
        with self._lock, self._conn() as db:
            if where:
                db.execute(f"DELETE FROM {table} WHERE " + " AND ".join(f"{k}=?" for k in where), list(where.values()))
            else:
                db.execute(f"DELETE FROM {table}")

    def ledger_begin(self, action_id: str, run_id: str, kind: str) -> bool:
        """The same rule as the Postgres function, in one transaction."""
        with self._lock, self._conn() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status, run_id, updated FROM action_ledger WHERE action_id=?",
                             (action_id,)).fetchone()
            busy_elsewhere = (row and row["status"] == "pending" and row["run_id"] != run_id and row["updated"]
                              and (datetime.now(timezone.utc) - datetime.fromisoformat(row["updated"])).total_seconds() < 600)
            if row and (row["status"] == "done" or busy_elsewhere):
                db.execute("UPDATE action_ledger SET attempts=attempts+1, updated=? WHERE action_id=?", (now(), action_id))
                db.execute("COMMIT")
                return False
            db.execute("INSERT INTO action_ledger(action_id, run_id, kind, status, attempts, updated) VALUES(?,?,?,'pending',1,?) "
                       "ON CONFLICT(action_id) DO UPDATE SET attempts=attempts+1, status='pending', run_id=excluded.run_id, "
                       "updated=excluded.updated", (action_id, run_id, kind, now()))
            db.execute("COMMIT")
            return True


class SupabaseStore:
    backend = "supabase"

    def __init__(self, url: str, key: str):
        self.base = url.rstrip("/") + "/rest/v1"
        self.headers = {"apikey": key, "Content-Type": "application/json", "User-Agent": "warmpath/0.1"}
        # New-style secret keys (sb_secret_...) go in the apikey header only; legacy service_role
        # keys are JWTs and are also sent as the bearer token.
        if not key.startswith("sb_"):
            self.headers["Authorization"] = f"Bearer {key}"

    def _req(self, method: str, path: str, *, params: dict | None = None, body=None, prefer: str = "") -> list | dict | None:
        url = f"{self.base}/{path}" + ("?" + urllib.parse.urlencode(params, safe=",.()*:") if params else "")
        headers = dict(self.headers)
        if prefer:
            headers["Prefer"] = prefer
        data = json.dumps(body, default=str).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
        except urllib.error.HTTPError as exc:
            raise StoreError(f"supabase {method} {path}: HTTP {exc.code} {exc.read().decode(errors='replace')[:300]}") from exc
        return json.loads(raw) if raw else None

    @staticmethod
    def _filters(where: dict | None) -> dict:
        out = {}
        for k, v in (where or {}).items():
            if isinstance(v, (list, tuple, set)):
                out[k] = "in.(" + ",".join(f'"{x}"' for x in v) + ")"
            elif isinstance(v, bool):
                out[k] = f"is.{str(v).lower()}"
            else:
                out[k] = f"eq.{v}"
        return out

    def select(self, table: str, where: dict | None = None, *, order: str = "", desc: bool = False,
               limit: int | None = None) -> list[dict]:
        params = {"select": "*", **self._filters(where)}
        if order:
            params["order"] = f"{order}.{'desc' if desc else 'asc'}"
        if limit:
            params["limit"] = str(limit)
        return self._req("GET", table, params=params) or []

    def get(self, table: str, key) -> dict | None:
        rows = self.select(table, {TABLES[table][0]: key}, limit=1)
        return rows[0] if rows else None

    def upsert(self, table: str, row: dict) -> dict:
        pk = TABLES[table][0]
        self._req("POST", table, params={"on_conflict": pk}, body=row,
                  prefer="resolution=merge-duplicates,return=minimal")
        return row

    def insert(self, table: str, row: dict, *, ignore_conflict: bool = False) -> dict:
        prefer = "return=representation" + (",resolution=ignore-duplicates" if ignore_conflict else "")
        out = self._req("POST", table, body=row, prefer=prefer)
        return (out[0] if isinstance(out, list) and out else row)

    def update(self, table: str, key, patch: dict) -> None:
        self._req("PATCH", table, params=self._filters({TABLES[table][0]: key}), body=patch, prefer="return=minimal")

    def delete(self, table: str, where: dict | None = None) -> None:
        pk = TABLES[table][0]
        params = self._filters(where) if where else {pk: "not.is.null"}
        self._req("DELETE", table, params=params, prefer="return=minimal")

    def ledger_begin(self, action_id: str, run_id: str, kind: str) -> bool:
        return bool(self._req("POST", "rpc/ledger_begin",
                              body={"p_action_id": action_id, "p_run_id": run_id, "p_kind": kind}))


_STORE = None
_STORE_LOCK = threading.Lock()


def store():
    """The configured store, created once per process."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            url, key = os.environ.get("SUPABASE_URL", "").strip(), os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
            _STORE = SupabaseStore(url, key) if url and key else SQLiteStore(os.environ.get("WARMPATH_DB", DB_PATH))
        return _STORE


def use(s) -> None:
    """Point the process at a specific store (tests use a temporary SQLite file)."""
    global _STORE
    with _STORE_LOCK:
        _STORE = s
