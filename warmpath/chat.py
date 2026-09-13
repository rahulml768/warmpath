"""Alex in chat. The founder writes to Alex; Alex delegates; every agent reports back as a card.

Alex's reading of the message is a model call, and a model can invent - so what it may do is
narrow and checked: it picks one of four actions, and a post URL it wants to act on must be
one the founder actually wrote. Everything after that is the same deterministic pipeline the
CLI runs, with the same gates, ledger, trace and audit. Chat is a new door, not a new path.

Approval comes from whichever the founder answers first: the buttons on the card here, or a
reply to the same card in Slack.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from . import audit, claims, llm, pipeline
from .agents import quinn
from .core import DB_PATH, Leads, Ledger, Run, mode
from .integrations import google, slack

ROOT = Path(__file__).resolve().parent.parent

ROUTER_SYSTEM = """You are Alex, chief of staff to a founder, Rahul. You run his outreach team:
- Quinn, Social Media Manager: reads comments on his LinkedIn posts and scores buying intent.
- Jordan, SDR: works out who a commenter is, their company, and whether Rahul already knows
  someone there (from his Gmail and Calendar).
- Casey, Outreach: writes the message, proves every sentence, sends only after Rahul approves.
- Morgan, Account Executive: reads replies and books meetings they accept.

Rahul's message is between <data> tags. Choose exactly ONE action:
- "grow": he wants more demand, visibility, leads or growth, or asks for a LinkedIn post. Quinn
  writes a post from his product brief for his approval; once published, the team watches it.
  Put what he wants in "goal", in his words.
- "scan_post": read the comments on a LinkedIn post and follow up with real buying intent.
  "source" is the post URL exactly as Rahul wrote it, or "demo" if he asks for the demo/test post.
- "check_replies": look for replies from people we emailed and turn a yes into a meeting.
- "report": tell him what the team did and whether any agent broke its instructions.
- "none": just answer him (greetings, questions about the team, anything else).

Never invent a URL. If he wants a post scanned but gave no URL and did not ask for the demo,
choose "none" and ask for the link.

Return ONE JSON object: {"reply": "one or two plain sentences Alex says first",
 "action": "grow" | "scan_post" | "check_replies" | "report" | "none", "source": "url, demo, or null",
 "goal": "what he wants, or null"}"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Chat:
    """Messages and approvals, in the same database as everything else (Supabase or SQLite)."""

    def __init__(self, path: Path | str | None = None):
        from . import store
        self.s = store.SQLiteStore(path) if path else store.store()

    def post(self, author: str, kind: str, text: str = "", **payload) -> int:
        row = self.s.insert("chat_messages", {"ts": _now(), "author": author, "kind": kind, "text": text,
                                              "payload": json.loads(json.dumps(payload, default=str))})
        return row["id"]

    def update(self, msg_id: int, **payload) -> None:
        row = self.s.get("chat_messages", msg_id)
        merged = {**((row or {}).get("payload") or {}), **payload}
        self.s.update("chat_messages", msg_id, {"payload": json.loads(json.dumps(merged, default=str))})

    def messages(self, limit: int = 400) -> list[dict]:
        rows = self.s.select("chat_messages", order="id", desc=True, limit=limit)
        return [{"id": r["id"], "ts": r["ts"], "author": r["author"], "kind": r["kind"], "text": r["text"] or "",
                 "payload": r.get("payload") or {}} for r in reversed(rows)]

    def decide(self, run_id: str, decision: str) -> None:
        self.s.upsert("approvals", {"run_id": run_id, "decision": decision, "ts": _now()})

    def decision(self, run_id: str) -> str | None:
        row = self.s.get("approvals", run_id)
        return row["decision"] if row else None

    def clear(self) -> None:
        self.s.delete("chat_messages")


# ── cards drawn from the trace, so the chat can never say something the run didn't do ──

def _step(run: Run, tool: str):
    return next((s for s in reversed(run.steps) if s.tool == tool), None)


def cards_so_far(chat: Chat, run: Run, comment: dict, sent: set[str]) -> None:
    cls = _step(run, "intent.classify")
    if cls and "intent" not in sent:
        sent.add("intent")
        chat.post("Quinn", "intent", run_id=run.run_id, name=comment.get("author_name"),
                  headline=comment.get("headline"), comment=comment.get("text"), intent=cls.decision,
                  confidence=cls.confidence, signals=(cls.data or {}).get("signals", []))
    ident, rel = _step(run, "identity.resolve"), _step(run, "relationship.resolve")
    if ident and rel and "jordan" not in sent:
        sent.add("jordan")
        i, r = ident.data or {}, rel.data or {}
        col = _step(run, "relationship.collision")
        mem = _step(run, "memory.check")
        mdata = (mem.data or {}) if mem else {}
        chat.post("Jordan", "relationship", run_id=run.run_id, name=i.get("name"), company=i.get("company"),
                  domain=i.get("domain"), domain_source=i.get("domain_source"), email=i.get("email"),
                  email_source=i.get("email_source"), verified=i.get("verified"),
                  network=i.get("network_distance", ""),
                  shared=i.get("shared_mailbox"), warm=rel.decision == "warm", people=r.get("people", []),
                  weak=r.get("weak", 0), refused=r.get("refused"),
                  timeline=r.get("timeline", []), records_found=r.get("records_found", 0),
                  desks_ignored=r.get("desks_ignored", 0),
                  memory=[{k: m.get(k) for k in ("scope", "kind", "text", "source", "expires", "inferred")}
                          for m in mdata.get("records", [])],
                  holds=[h.get("text") for h in mdata.get("holds", [])],
                  collisions=(col.data or {}).get("collisions", []) if col else [])
    checks = [s for s in run.steps if s.tool == "claims.check"]
    if checks and "casey" not in sent:
        sent.add("casey")
        last = checks[-1].data or {}
        draft_step = _step(run, "llm.draft")
        chat.post("Casey", "draft", run_id=run.run_id,
                  channel="introduction" if run.kind == "introduction" else ("email" if "channel=email" in (draft_step.input if draft_step else "") else "linkedin_reply"),
                  sentences=last.get("sentences", []),
                  evidence={k: v.get("text") for k, v in (last.get("evidence") or {}).items()},
                  passed=checks[-1].decision == "pass", attempts=len(checks),
                  problems=[p for c in checks for p in (c.data or {}).get("problems", [])])


class ChatApprover:
    """Posts the approval card and waits for the founder - here or in Slack, first answer wins."""

    def __init__(self, chat: Chat, comment: dict, sent: set[str], timeout_s: int = 900):
        self.chat, self.comment, self.sent, self.timeout_s = chat, comment, sent, timeout_s

    def __call__(self, run: Run, payload: dict) -> str:
        cards_so_far(self.chat, run, self.comment, self.sent)
        msg = self.chat.post("Alex", "approval", "Your call.", run_id=run.run_id, status="pending",
                             card=payload["card"], run_kind=run.kind,
                             author=os.environ.get("WARMPATH_POST_AS_NAME", "").strip())
        where = None
        if os.environ.get("SLACK_APPROVER_ID") and os.environ.get("WARMPATH_SLACK_APPROVALS", "1") != "0":
            try:
                where = slack.post(os.environ["SLACK_APPROVER_ID"].strip(), payload["card"] +
                                   "\nReply in this message's thread with exactly: send, review, or ignore.")
            except Exception as exc:  # noqa: BLE001 - chat approval still works without Slack
                self.chat.post("Alex", "text", f"Slack didn't take the card ({str(exc)[:80]}), so answer here.")
        answer, via, end, last_slack = "timeout", "", time.time() + self.timeout_s, 0.0
        while time.time() < end:
            d = self.chat.decision(run.run_id)
            if d:
                answer, via = d, "chat"
                break
            if where and time.time() - last_slack > 4:
                last_slack = time.time()
                try:
                    got = slack.check_decision(channel=where["channel"], ts=where["ts"])
                    if got:
                        answer, via = got, "slack"
                        break
                except Exception:  # noqa: BLE001 - Slack hiccup: keep waiting on the chat buttons
                    pass
            time.sleep(1.2)
        self.chat.update(msg, status=answer, via=via)
        return answer


def result_card(chat: Chat, run: Run) -> None:
    send = next((s for s in run.steps if s.tool in ("gmail.send", "linkedin.reply", "linkedin.post", "calendar.create") and s.ok), None)
    who = {"gmail.send": "Casey", "linkedin.reply": "Quinn", "linkedin.post": "Quinn", "calendar.create": "Morgan"}.get(send.tool, "Alex") if send else "Alex"
    chat.post(who, "result", run_id=run.run_id, state=run.state, reason_code=run.reason_code,
              evidence=run.evidence, action=send.tool if send else "",
              simulated=(send.data or {}).get("simulated") if send else None,
              to=(send.data or {}).get("to") if send else "",
              route=(_step(run, "intro.route").data if _step(run, "intro.route") else {}),
              memory_holds=[hold for step in run.steps if step.tool == "memory.check"
                            for hold in (step.data or {}).get("holds", [])],
              violations=[v.invariant for v in audit.audit_run(run)])


# ── Alex ───────────────────────────────────────────────────────────────────────

_busy = threading.Lock()


def _source_files() -> tuple[str, str]:
    """Demo comments and the founder's contacts: local files, or env vars on a host (they hold real
    test addresses, so they are never committed), or the synthetic evaluation fixtures."""
    import tempfile
    out = []
    for local, env, fallback in ((ROOT / "fixtures" / "demo_post.local.json", "WARMPATH_DEMO_POST_JSON", "eval_post.json"),
                                 (ROOT / "contacts.local.json", "WARMPATH_CONTACTS_JSON", "eval_contacts.json")):
        if local.exists():
            out.append(str(local))
        elif os.environ.get(env, "").strip():
            f = Path(tempfile.gettempdir()) / f"warmpath_{env.lower()}.json"
            f.write_text(os.environ[env], encoding="utf-8")
            out.append(str(f))
        else:
            out.append(str(ROOT / "fixtures" / fallback))
    return out[0], out[1]


def route(text: str) -> dict:
    try:
        out = llm._call(ROUTER_SYSTEM, f"<data>\n{text}\n</data>")
    except Exception as exc:  # noqa: BLE001
        return {"reply": f"I couldn't reach the model just now ({str(exc)[:60]}). Try again in a moment.",
                "action": "none", "source": None}
    action = out.get("action") if out.get("action") in {"grow", "scan_post", "check_replies", "report", "none"} else "none"
    source = out.get("source") if isinstance(out.get("source"), str) else None
    if action == "scan_post" and source != "demo":
        # A URL Alex acts on must be one the founder wrote. An invented link is the same class of
        # failure as an invented email address.
        urls = re.findall(r"https?://\S+", text)
        if not source or source not in text:
            source = urls[0] if urls else None
        if not source:
            return {"reply": "Send me the link to the post and I'll have Quinn go through the comments.",
                    "action": "none", "source": None}
    return {"reply": str(out.get("reply") or "On it.")[:400], "action": action, "source": source,
            "goal": str(out.get("goal") or text)[:500]}


def handle(chat: Chat, text: str) -> None:
    chat.post("you", "text", text)
    if not _busy.acquire(blocking=False):
        chat.post("Alex", "text", "The team is still working on your last request - I'll pick this up when they're done.")
        return
    threading.Thread(target=_run, args=(chat, text), daemon=True).start()


def _run(chat: Chat, text: str) -> None:
    try:
        plan = route(text)
        chat.post("Alex", "text", plan["reply"], action=plan["action"])
        from . import autopilot
        pilot = autopilot.AUTOPILOT
        if plan["action"] == "grow":
            threading.Thread(target=_grow, args=(chat, plan["goal"]), daemon=True).start()
        elif plan["action"] == "scan_post" and pilot:
            label = "the demo post" if plan["source"] == "demo" else "your post"
            pilot.watch(plan["source"], label)
            chat.post("Alex", "text", f"I'm watching {label} now. Every {pilot.interval_s}s Quinn checks it for new "
                                      "comments and the team takes it from there - you'll only hear from me when "
                                      "something needs your approval." + ("" if pilot.enabled else
                                      " Autopilot is paused, so turn it on in the sidebar."))
        elif plan["action"] == "check_replies" and pilot:
            pilot.poke()
            chat.post("Alex", "text", "Morgan is checking the inbox now - replies are also checked on every heartbeat.")
        elif plan["action"] == "scan_post":
            _scan(chat, plan["source"])
        elif plan["action"] == "check_replies":
            _replies(chat)
        elif plan["action"] == "report":
            _report(chat)
    except Exception as exc:  # noqa: BLE001
        chat.post("Alex", "text", f"Something broke on my side: {type(exc).__name__}: {str(exc)[:160]}")
        traceback.print_exc()
    finally:
        _busy.release()


def _grow(chat: Chat, goal: str) -> None:
    """Goal -> Quinn's post -> approval -> published -> watched. Runs beside the chat, not in its way."""
    from . import autopilot
    chat.post("Alex", "delegation", "Quinn, write a LinkedIn post from Rahul's product brief.", to="Quinn")
    sent: set[str] = set()

    class PostApprover(ChatApprover):
        def __call__(self, run, payload):
            last = next((x for x in reversed(run.steps) if x.tool == "claims.check"), None)
            d = (last.data or {}) if last else {}
            chat.post("Quinn", "post_draft", run_id=run.run_id, sentences=d.get("sentences", []),
                      author=os.environ.get("WARMPATH_POST_AS_NAME", "").strip() or "Rahul Mittal",
                      as_page=bool(os.environ.get("WARMPATH_POST_AS_ORGANIZATION", "").strip()),
                      evidence={k: v.get("text") for k, v in (d.get("evidence") or {}).items()},
                      problems=[p for x in run.steps if x.tool == "claims.check" for p in (x.data or {}).get("problems", [])])
            self.sent.update({"intent", "jordan", "casey"})
            return super().__call__(run, payload)

    pilot = autopilot.AUTOPILOT
    run = pipeline.process_goal(goal, approver=PostApprover(chat, {"text": goal}, sent,
                                                            timeout_s=getattr(pilot, "approval_timeout_s", 900)))
    run.save()
    result_card(chat, run)
    pub = next((x for x in run.steps if x.tool == "linkedin.post" and x.ok), None)
    post_id = (pub.data or {}).get("post_id") if pub else ""
    if post_id and pilot:
        pilot.watch(post_id, "your new post")
        chat.post("Alex", "text", "It's live on LinkedIn. I'm watching it now - every comment goes through Quinn, "
                                  "Jordan and Casey, and you'll only hear from me when something needs your approval.")
    elif pub:
        chat.post("Alex", "text", "Dry run: the post wasn't published, so there's nothing to watch yet.")


def _scan(chat: Chat, source: str) -> None:
    demo_post, contacts_file = _source_files()
    path = demo_post if source == "demo" else source
    label = "the demo post" if source == "demo" else "your post"
    chat.post("Alex", "delegation", f"Quinn, go through the comments on {label}.", to="Quinn")
    try:
        post, comments = quinn.read_post(path)
    except Exception as exc:  # noqa: BLE001
        chat.post("Quinn", "text", f"I couldn't read that post: {str(exc)[:160]}")
        return
    contacts = json.loads(Path(contacts_file).read_text(encoding="utf-8"))
    chat.post("Quinn", "comments", f"{len(comments)} comments on {label}.", source=post["source"],
              comments=[{"name": c.get("author_name"), "headline": c.get("headline"), "text": c.get("text")}
                        for c in comments])
    led, leads = Ledger(), Leads()
    runs = []
    for c in comments:
        sent: set[str] = set()
        if c is not comments[0]:
            chat.post("Alex", "delegation", f"Next: {c.get('author_name')}.", to="Quinn")
        try:
            run = pipeline.process_comment(c, post_text=post.get("text", ""), approver=ChatApprover(chat, c, sent),
                                           contacts=contacts, ledger=led, leads=leads)
        except Exception as exc:  # noqa: BLE001 - one comment failing must not abandon the others
            chat.post("Alex", "text", f"I couldn't finish {c.get('author_name')}'s comment ({type(exc).__name__}: "
                                      f"{str(exc)[:90]}). Nothing was sent for them - ask me to check the post again.")
            continue
        run.save()
        runs.append(run)
        if run.state in ("IGNORED", "BLOCKED") and "intent" not in sent:
            cards_so_far(chat, run, c, sent)
        elif "jordan" not in sent or "casey" not in sent:
            cards_so_far(chat, run, c, sent)
        result_card(chat, run)
    _summary(chat, runs, "post")


def _replies(chat: Chat) -> None:
    led, leads = Ledger(), Leads()
    from . import introductions
    introductions.check_replies(leads, chat)
    waiting = [l for l in leads.all() if l.get("email") and l.get("status") in ("awaiting_reply", "meeting_pending", None)]
    chat.post("Alex", "delegation", f"Morgan, check replies from the {len(waiting)} people we emailed.", to="Morgan")
    runs = []
    for lead in waiting:
        try:
            replies = google.replies_from(lead["email"], lead["sent_at"])
        except Exception as exc:  # noqa: BLE001
            chat.post("Morgan", "text", f"I couldn't read the inbox for {lead['email']}: {str(exc)[:120]}")
            continue
        if not replies:
            chat.post("Morgan", "text", f"No reply yet from {lead.get('name') or lead['email']}.")
        for reply in replies:
            chat.post("Morgan", "reply", f"{lead.get('name') or lead['email']} replied.", body=reply["body"][:600])

            def notify(msg: str) -> None:
                chat.post("Morgan", "meeting", msg)
                pipeline.slack_notifier(msg)
            run = pipeline.process_reply(lead, reply, approver=ChatApprover(chat, {"text": reply["body"]}, {"intent", "jordan"}),
                                         notify=notify, ledger=led, leads=leads)
            run.save()
            runs.append(run)
            m = _step(run, "meeting.read")
            if m:
                chat.post("Morgan", "meeting_read", run_id=run.run_id, wants=m.decision, **{k: (m.data or {}).get(k) for k in ("offered_slot_id", "day_key", "time_24h", "timezone_stated", "signals")})
            result_card(chat, run)
    _summary(chat, runs, "replies")


def _summary(chat: Chat, runs: list[Run], what: str) -> None:
    report = audit.audit_all(runs)
    acted = [r for r in runs if any(s.tool in audit.WRITES and s.ok for s in r.steps)]
    held = [r for r in runs if r not in acted]
    parts = [f"{len(acted)} acted on", f"{len(held)} held back"]
    reasons = sorted({r.reason_code for r in held if r.reason_code})
    text = (f"Done. {', '.join(parts)}" + (f" ({', '.join(reasons)})" if reasons else "") +
            f". I audited all {len(runs)} runs against every agent's instructions: "
            + ("nothing was broken." if not report["violations"] else f"{report['violations']} violations - see below."))
    if mode() != "live":
        text += " Dry run: nothing actually left the building."
    chat.post("Alex", "audit", text, runs=report["runs"], violations=report["violations"], issues=report["issues"])


def _report(chat: Chat) -> None:
    runs = audit.load_runs()
    real = [r for r in runs if not r.run_id.startswith("eval_")]
    report = audit.audit_all(real, write_regressions=False)
    text = (f"Across {len(real)} real runs: " + ", ".join(f"{n} {s.lower().replace('_', ' ')}" for s, n in report["results"].items())
            + f". Instruction violations: {report['violations']}.") if real else "No real runs yet - send me a post link."
    chat.post("Alex", "audit", text, runs=report["runs"], violations=report["violations"], issues=report["issues"])
