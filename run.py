"""WarmPath command line.

    python run.py comments <linkedin-post-url | fixtures/x.json> [--contacts contacts.json] [--approve slack|auto|ask]
    python run.py replies  [--approve slack|auto|ask]
    python run.py audit
    python run.py serve    [--port 8765]

Mode comes from WARMPATH_MODE and is dry_run unless set to live. `--approve auto` is refused
in live mode: an automatic yes is only a test harness, never a founder.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from warmpath.core import Leads, Ledger, load_env, mode  # noqa: E402

load_env()

from warmpath import audit, pipeline  # noqa: E402
from warmpath.agents import quinn  # noqa: E402
from warmpath.integrations import google, slack  # noqa: E402


def approver(kind: str):
    if kind == "slack":
        return slack.approver_from_slack()
    if kind == "auto":
        if mode() == "live":
            raise SystemExit("--approve auto is not allowed in live mode")
        return lambda run, payload: "approved"

    def ask(run, payload):
        print("\n" + payload["card"])
        answer = input("send / review / ignore > ").strip().lower()
        return {"send": "approved", "review": "review"}.get(answer, "rejected")
    return ask


def cmd_comments(args) -> None:
    contacts = json.loads(Path(args.contacts).read_text(encoding="utf-8")) if args.contacts else []
    post, comments = quinn.read_post(args.source)
    print(f"[{mode()}] post from {post['source']} · {len(comments)} comments")
    led, leads = Ledger(), Leads()
    runs = []
    for c in comments:
        run = pipeline.process_comment(c, post_text=post.get("text", ""), approver=approver(args.approve),
                                       contacts=contacts, ledger=led, leads=leads)
        run.save()
        runs.append(run)
        print(f"  {run.run_id}  {c.get('author_name', '')[:22]:22} -> {run.state:18} {run.reason_code}")
    audit.print_report(audit.audit_all(runs))


def cmd_replies(args) -> None:
    led, leads = Ledger(), Leads()
    waiting = [l for l in leads.all() if l.get("status") in ("awaiting_reply", None) and l.get("email")]
    print(f"[{mode()}] {len(waiting)} leads waiting for a reply")
    runs = []
    for lead in waiting:
        for reply in google.replies_from(lead["email"], lead["sent_at"]):
            run = pipeline.process_reply(lead, reply, approver=approver(args.approve),
                                         notify=pipeline.slack_notifier, ledger=led, leads=leads)
            run.save()
            runs.append(run)
            print(f"  {run.run_id}  {lead['email']:32} -> {run.state:18} {run.reason_code}")
    if runs:
        audit.print_report(audit.audit_all(runs))


def cmd_audit(_args) -> None:
    report = audit.audit_all()
    audit.print_report(report)
    (ROOT / "runs").mkdir(exist_ok=True)
    (ROOT / "runs" / "_audit.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("comments")
    c.add_argument("source")
    c.add_argument("--contacts", default="")
    c.add_argument("--approve", default="slack", choices=["slack", "auto", "ask"])
    r = sub.add_parser("replies")
    r.add_argument("--approve", default="slack", choices=["slack", "auto", "ask"])
    sub.add_parser("audit")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    if args.cmd == "serve":
        from server import serve
        serve(args.port)
        return
    {"comments": cmd_comments, "replies": cmd_replies, "audit": cmd_audit}[args.cmd](args)


if __name__ == "__main__":
    main()
