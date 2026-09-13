"""Reset one TEST person so a demo can be run again from the start.

    python scripts/reset_test_person.py rahul@ascot.example            # show what would be removed
    python scripts/reset_test_person.py rahul@ascot.example --yes      # remove it

WarmPath deliberately remembers everyone it has contacted: the ledger refuses a second outreach and
memory holds a person with an active conversation. That is right in production and blocks a rerun of
a demo. This clears exactly one person, only if they are on WARMPATH_LIVE_ALLOWLIST (a test inbox you
control), and closes any approval still waiting for them so nothing is sent from the old run.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from warmpath.core import action_id, load_env  # noqa: E402

load_env()

from warmpath import store  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    email = sys.argv[1].strip().lower()
    apply = "--yes" in sys.argv
    allow = {a.strip().lower() for a in os.environ.get("WARMPATH_LIVE_ALLOWLIST", "").split(",") if a.strip()}
    if email not in allow:
        print(f"refused: {email} is not on WARMPATH_LIVE_ALLOWLIST - only test inboxes can be reset")
        return 1
    s = store.store()
    ledger_keys = [action_id(kind, email) for kind in ("send", "meeting", "intro")]
    ledger = [r for r in s.select("action_ledger") if r["action_id"] in ledger_keys]
    leads = [r for r in s.select("leads") if (r.get("email") or "").lower() == email or r["key"].lower() == email]
    lead_runs = {r.get("lead_run") for r in leads if r.get("lead_run")}
    # Only this person's cards: a pending post, or someone else's email, is never touched.
    names = {(r.get("name") or "").strip().lower() for r in leads if r.get("name")}
    def theirs(card: str) -> bool:
        card = (card or "").lower()
        return email in card or any(n and n in card for n in names)
    pending = [m for m in s.select("chat_messages", {"kind": "approval"}, order="id", desc=True, limit=200)
               if (m.get("payload") or {}).get("status") == "pending"
               and (m.get("payload") or {}).get("run_kind") != "post"
               and theirs((m.get("payload") or {}).get("card", ""))]
    memory = [r for r in s.select("settings") if r["key"].startswith("memory:") and email in (r.get("value") or "").lower()]

    print(f"store: {s.backend} | person: {email}")
    print(f"  ledger rows       {[r['action_id'] + '=' + r['status'] for r in ledger]}")
    print(f"  lead rows         {[r['key'] + ' (' + str(r.get('status')) + ')' for r in leads]}")
    print(f"  memory records    {len(memory)}")
    print(f"  pending approvals {[ (m['payload'] or {}).get('run_id') for m in pending]}  (closed as rejected: nothing is sent)")
    if not apply:
        print("\nnothing changed - run again with --yes")
        return 0
    for m in pending:
        run_id = (m.get("payload") or {}).get("run_id")
        if run_id:
            s.upsert("approvals", {"run_id": run_id, "decision": "rejected", "ts": store.now()})
    for r in ledger:
        s.delete("action_ledger", {"action_id": r["action_id"]})
    for r in leads:
        s.delete("leads", {"key": r["key"]})
    for r in memory:
        s.delete("settings", {"key": r["key"]})
    print(f"\nreset done - {email} can go through the flow again. Old runs and traces are kept for the audit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
