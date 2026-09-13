"""Alex - the orchestrator. Moves one comment, or one reply, through the agents and the gates.

The shape of this file is the product: agents propose, the state machine only allows legal
moves, the ledger makes every external write happen at most once, and each run is recorded
step by step so the auditor can check it against every agent's instructions afterwards.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, timezone

from . import claims
from .agents import casey, jordan, morgan, quinn
from .core import Leads, Ledger, Run, action_id, evaluate, mode
from .integrations import slack

NAME = "Alex"
Approver = Callable[[Run, dict], str]          # approved | rejected | review | timeout
Notifier = Callable[[str], None]


def slack_notifier(text: str) -> None:
    """Booking alerts go to the founder's DM with the bot (where approvals already are) and to the team channel."""
    sent = False
    for target in dict.fromkeys(filter(None, (os.environ.get("SLACK_APPROVER_ID", "").strip(),
                                              os.environ.get("SLACK_NOTIFY_CHANNEL", "").strip()))):
        try:
            slack.post(target, text)
            sent = True
        except Exception:  # noqa: BLE001 - one destination failing must not lose the alert
            continue
    if not sent:
        raise RuntimeError("no Slack destination accepted the alert")


def _card(run: Run, comment: dict, ident: dict, rel: dict, cls: dict, channel: str,
          draft: dict, collisions: list[str]) -> str:
    warm = rel["people"][0] if rel["people"] else None
    lines = [
        f"{'🔥 High-intent lead' if cls['confidence'] >= 0.85 else 'Lead'} · run `{run.run_id}` · `{run.mode.upper()}`",
        f"*{ident['name']}* — {ident['company'] or 'company unknown'}"
        f"{' (' + ident['domain'] + ')' if ident['domain'] else ''}",
        f"*Commented:* \"{comment['text'][:220]}\"",
        f"*Quinn:* {cls['intent']} {cls['confidence']:.2f} · signals: {', '.join(cls['signals']) or 'none'}",
        f"*Jordan:* " + (f"existing relationship — {warm['name'] or warm['email']}, {warm['evidence']}"
                         if warm else f"no prior contact at this company{' — ' + rel['refused'] if rel['refused'] else ''}"),
        f"*Address:* {ident['email'] + ' (from ' + ident['email_source'] + ')' if ident['verified'] else 'none verified — replying on LinkedIn instead of guessing one'}",
    ]
    if collisions:
        lines.append("*Not used:* " + "; ".join(collisions[:2]))
    lines += ["", f"*Casey's {'email' if channel == 'email' else 'LinkedIn reply'}* (every claim checked):"]
    if channel == "email":
        lines.append(f"Subject: {draft['subject']}")
    lines += [f"```{claims.render(draft)}```", "Reply *send*, *review* or *ignore*."]
    return "\n".join(lines)


LEAD_STAGE = {"AWAITING_REPLY": "Emailed · waiting for reply", "COMMENT_REPLIED": "Replied on LinkedIn",
              "IGNORED": "Not a lead", "BLOCKED": "Blocked", "REVIEW_REQUIRED": "Needs you", "STOPPED": "Stopped"}
REPLY_STAGE = {"AWAITING_REPLY": "Times offered · waiting", "NOTIFIED": "Meeting booked",
               "IGNORED": "Not interested", "REVIEW_REQUIRED": "Needs you",
               "CALENDAR_FAILED": "Calendar failed · retrying", "STOPPED": "Stopped"}


def process_goal(goal: str, *, approver: Approver, ledger: Ledger | None = None) -> Run:
    """A one-line goal ("I want to grow") becomes a LinkedIn post - sourced, approved, published once."""
    import hashlib
    ledger = ledger or Ledger()
    live = mode() == "live"
    run = Run.start(f"goal:{hashlib.sha1(goal.encode()).hexdigest()[:12]}", kind="post", subject="LinkedIn post")
    run.record("goal.read", agent=NAME, input=goal[:500])
    draft, problems, _ = quinn.write_post(run, goal)
    run.move("ACTION_PROPOSED")
    if problems:
        run.move("REVIEW_REQUIRED")
        return run.finish("REVIEW_REQUIRED", "UNSUPPORTED_CLAIM", problems)
    text = "\n\n".join(s["text"] for s in draft["sentences"])
    key = action_id("post", hashlib.sha1(text.encode()).hexdigest())
    run.move("AWAITING_APPROVAL")
    card = (f"📣 Quinn's LinkedIn post · run `{run.run_id}` · `{run.mode.upper()}`\n*Goal:* {goal[:200]}\n"
            f"```{text}```\nEvery sentence about the product cites your product brief.\n"
            "Reply *send*, *review* or *ignore*.")
    answer = run.step("slack.approval", lambda: approver(run, {"card": card}), agent=NAME, output=card[:1500])
    run.steps[-1].decision = answer
    if answer != "approved":
        end = "REVIEW_REQUIRED" if answer == "review" else "STOPPED"
        run.move(end)
        return run.finish(end, "APPROVAL_TIMEOUT" if answer == "timeout" else
                          ("FOUNDER_WANTS_REVIEW" if answer == "review" else "APPROVAL_REJECTED"), [f"answer: {answer}"])
    run.move("APPROVED")
    if not ledger.begin(key, run.run_id, "post"):
        run.move("BLOCKED_DUPLICATE")
        return run.finish("BLOCKED_DUPLICATE", "ACTION_ALREADY_EXECUTED", ["this exact post was already published"])
    try:
        res = quinn.publish_post(run, text, live)
    except Exception as exc:  # noqa: BLE001
        ledger.failed(key, str(exc))
        run.move("STOPPED")
        return run.finish("STOPPED", "SEND_FAILED", [str(exc)[:200]])
    run.steps[-1].decision = "published"
    run.steps[-1].data = {**res, "action_id": key, "sentences": draft["sentences"]}
    ledger.done(key, res.get("post_id") or "simulated")
    run.move("POST_PUBLISHED")
    return run.finish("POST_PUBLISHED", "", [f"post {res.get('post_id') or '(dry run)'}"])


def _step_data(run: Run, tool: str):
    s = next((x for x in reversed(run.steps) if x.tool == tool), None)
    return s, ((s.data or {}) if s else {})


def process_comment(comment: dict, *, post_text: str, approver: Approver, contacts: list[dict],
                    ledger: Ledger | None = None, leads: Leads | None = None, rubric: str = "v2",
                    ours: set[str] | None = None) -> Run:
    """One comment through the team - and a row in the leads table for the person, whatever happened."""
    leads = leads or Leads()
    run = _process_comment(comment, post_text=post_text, approver=approver, contacts=contacts,
                           ledger=ledger, leads=leads, rubric=rubric, ours=ours)
    if run.state != "BLOCKED_DUPLICATE":
        cls, _ = _step_data(run, "intent.classify")
        _, ident = _step_data(run, "identity.resolve")
        rel, rdata = _step_data(run, "relationship.resolve")
        people = rdata.get("people") or []
        key = ident.get("email") or f"comment:{comment['comment_id']}"
        leads.save(key, {
            "email": ident.get("email") or None, "name": comment.get("author_name", ""),
            "company": ident.get("company", ""), "domain": ident.get("domain", ""),
            "comment": comment.get("text", ""), "comment_id": comment["comment_id"],
            "intent": cls.decision if cls else None, "confidence": cls.confidence if cls else None,
            "warm": bool(rel and rel.decision == "warm"),
            "knows": (people[0].get("name") or people[0].get("email")) if people else "",
            "stage": LEAD_STAGE.get(run.state, run.state), "lead_run": run.run_id,
            "status": (leads.get(key) or {}).get("status") or ("awaiting_reply" if run.state == "AWAITING_REPLY" else run.state.lower())})
    return run


def _process_comment(comment: dict, *, post_text: str, approver: Approver, contacts: list[dict],
                     ledger: Ledger | None = None, leads: Leads | None = None, rubric: str = "v2",
                     ours: set[str] | None = None) -> Run:
    ledger, leads = ledger or Ledger(), leads or Leads()
    ours = ours if ours is not None else _our_domains()
    live = mode() == "live"
    run = Run.start(comment["comment_id"], subject=comment.get("author_name", ""))

    # ── Quinn: is this buying intent? ──────────────────────────────────────────
    cls = quinn.classify(run, comment, post_text, rubric)
    run.move("CLASSIFIED")
    gate = evaluate(intent=cls["intent"], confidence=cls["confidence"], verified=True,
                    verify_evidence=[], shared_mailbox=False, approved=None)
    if not gate.allowed:
        run.record("policy.evaluate", agent=NAME, decision=gate.decision,
                   data={"reason_code": gate.reason_code})
        end = {"IGNORE": "IGNORED", "HUMAN_REVIEW": "REVIEW_REQUIRED"}.get(gate.decision, "BLOCKED")
        run.move(end)
        return run.finish(end, gate.reason_code, gate.evidence + [f"comment: \"{comment['text'][:120]}\""])

    # ── Jordan: who, which company, do we know them ────────────────────────────
    ident = jordan.resolve_identity(run, comment, contacts, ours)
    run.move("IDENTITY_RESOLVED")
    rel = jordan.relationship(run, ident, contacts, ours)
    collisions = jordan.company_name_collision(ident, contacts)
    if collisions:
        run.record("relationship.collision", agent=jordan.NAME, decision="not_used",
                   data={"collisions": collisions})
    run.move("RELATIONSHIP_RESOLVED")

    # An unverified or shared address is not a reason to stop - it is a reason not to email.
    channel = "email" if ident["verified"] and not ident["shared_mailbox"] else "linkedin_reply"
    if channel == "linkedin_reply":
        why = ("SHARED_MAILBOX" if ident["shared_mailbox"] else "CONTACT_NOT_VERIFIED")
        run.record("policy.evaluate", agent=NAME, decision="SWITCH_TO_LINKEDIN_REPLY",
                   data={"reason_code": why, "evidence": ident["verify_evidence"]})

    # ── Casey: write it, prove it ──────────────────────────────────────────────
    # A LinkedIn reply is public. What Gmail and Calendar told us about who you know there is
    # private, so it never reaches a public reply - only the email may use it.
    evidence = casey.evidence_for(comment, ident, rel if channel == "email" else {**rel, "people": []},
                                  post_text=post_text)
    allowed = {ident["email"]} if ident["verified"] else set()
    draft, problems = casey.write(run, channel=channel, comment=comment, evidence=evidence,
                                  allowed=allowed)
    run.move("ACTION_PROPOSED")
    if problems:
        run.move("REVIEW_REQUIRED")
        return run.finish("REVIEW_REQUIRED", "UNSUPPORTED_CLAIM", problems)

    key = action_id("send", ident["email"]) if channel == "email" else action_id("reply", comment["comment_id"])
    if ledger.status(key) == "done":
        run.record("ledger.check", agent=NAME, decision="already_executed", data={"action_id": key})
        run.move("BLOCKED_DUPLICATE")
        return run.finish("BLOCKED_DUPLICATE", "ACTION_ALREADY_EXECUTED", [f"{key} already done"])
    run.record("ledger.check", agent=NAME, decision="not_yet_executed", data={"action_id": key})

    # ── Alex: the founder decides ──────────────────────────────────────────────
    run.move("AWAITING_APPROVAL")
    card = _card(run, comment, ident, rel, cls, channel, draft, collisions)
    answer = run.step("slack.approval", lambda: approver(run, {"card": card}), agent=NAME,
                      output=card[:1500])
    run.steps[-1].decision = answer
    decision = evaluate(intent=cls["intent"], confidence=cls["confidence"],
                        verified=ident["verified"] or channel == "linkedin_reply",
                        verify_evidence=ident["verify_evidence"], shared_mailbox=False,
                        approved="review" if answer == "review" else answer == "approved")
    if not decision.allowed:
        end = "REVIEW_REQUIRED" if answer == "review" else "STOPPED"
        run.move(end)
        reason = "APPROVAL_TIMEOUT" if answer == "timeout" else decision.reason_code
        return run.finish(end, reason, decision.evidence + [f"slack answer: {answer}"])
    run.move("APPROVED")

    # ── the one external write, through the ledger ─────────────────────────────
    if not ledger.begin(key, run.run_id, "send" if channel == "email" else "reply"):
        run.record("ledger.check", agent=NAME, decision="already_executed", data={"action_id": key})
        run.move("BLOCKED_DUPLICATE")
        return run.finish("BLOCKED_DUPLICATE", "ACTION_ALREADY_EXECUTED", [f"{key} already done"])
    body = claims.render(draft)
    try:
        if channel == "email":
            res = casey.send_email(run, to=ident["email"], subject=draft["subject"], body=body, live=live)
        else:
            res = quinn.reply_on_linkedin(run, comment, " ".join(s["text"] for s in draft["sentences"]), live)
    except Exception as exc:  # noqa: BLE001
        ledger.failed(key, str(exc))
        run.move("STOPPED")
        return run.finish("STOPPED", "SEND_FAILED", [str(exc)[:200]])
    run.steps[-1].decision = "sent"
    run.steps[-1].data = {**res, "action_id": key, "to": ident["email"], "address": ident["email"],
                          "sentences": draft["sentences"], "evidence_ids": sorted(evidence)}
    ledger.done(key, "simulated" if res.get("simulated") else "sent")

    if channel == "linkedin_reply":
        run.move("COMMENT_REPLIED")
        return run.finish("COMMENT_REPLIED", "", ["public reply posted, asking where to send details"])
    leads.save(ident["email"], {"email": ident["email"], "name": ident["name"],
                                "company": ident["company"], "domain": ident["domain"],
                                "comment_id": comment["comment_id"], "lead_run": run.run_id,
                                "sent_at": datetime.now(timezone.utc).isoformat(),
                                "subject": draft["subject"], "warm": rel["warm"],
                                "offered": {}, "status": "awaiting_reply"})
    run.move("EMAIL_SENT")
    run.move("AWAITING_REPLY")
    return run.finish("AWAITING_REPLY", "", [f"email to {ident['email']}", "waiting for their reply"])


def process_reply(lead: dict, reply: dict, *, approver: Approver, notify: Notifier,
                  ledger: Ledger | None = None, leads: Leads | None = None) -> Run:
    """One reply through Morgan - and the person's stage in the leads table moves with it."""
    leads = leads or Leads()
    run = _process_reply(lead, reply, approver=approver, notify=notify, ledger=ledger, leads=leads)
    if run.state != "BLOCKED_DUPLICATE":
        leads.save(lead["email"], {"stage": REPLY_STAGE.get(run.state, run.state)})
    return run


def _process_reply(lead: dict, reply: dict, *, approver: Approver, notify: Notifier,
                   ledger: Ledger | None = None, leads: Leads | None = None) -> Run:
    ledger, leads = ledger or Ledger(), leads or Leads()
    live = mode() == "live"
    meet_key = action_id("meeting", lead["email"])
    reply_key = action_id("replyread", reply["message_id"])
    lead = leads.get(lead["email"]) or lead

    # ── resume, never restart ──────────────────────────────────────────────────
    pending = lead.get("pending_booking")
    if pending and ledger.status(meet_key) != "done":
        run = Run.start(reply["message_id"], kind="reply", subject=lead.get("name", ""),
                        state="CALENDAR_FAILED")
        run.record("lead.link", agent=NAME, data={"email": lead["email"]})
        run.record("ledger.resume", agent=NAME, decision="book_agreed_time_only",
                   data={"action_id": meet_key, "start_local": pending})
        return _book(run, lead, pending, ledger, leads, notify, live, meet_key, reply_key)

    run = Run.start(reply["message_id"], kind="reply", subject=lead.get("name", ""),
                    state="REPLY_RECEIVED")
    run.record("lead.link", agent=NAME, data={"email": lead["email"]})
    if ledger.status(reply_key) == "done" or ledger.status(meet_key) == "done":
        run.record("ledger.check", agent=NAME, decision="already_executed",
                   data={"action_id": meet_key if ledger.status(meet_key) == "done" else reply_key})
        run.move("BLOCKED_DUPLICATE")
        return run.finish("BLOCKED_DUPLICATE", "ACTION_ALREADY_EXECUTED",
                          ["this reply was already handled or the meeting already exists"])

    offered = lead.get("offered") or {}
    m = morgan.read_reply(run, reply, offered)
    run.move("MEETING_READ")
    if m["wants_meeting"] == "no":
        ledger.begin(reply_key, run.run_id, "replyread")
        ledger.done(reply_key)
        run.move("IGNORED")
        return run.finish("IGNORED", "NO_MEETING_INTENT", [f"reply: \"{reply['body'][:120]}\""])
    if m["wants_meeting"] == "unclear":
        run.move("REVIEW_REQUIRED")
        return run.finish("REVIEW_REQUIRED", "MEETING_INTENT_UNCLEAR", [f"reply: \"{reply['body'][:120]}\""])

    start_local, why = morgan.agreed_time(m, offered)
    if not start_local and not (m["day_key"] and m["time_24h"]):
        # They want to meet but haven't settled a time - "next week", "Tuesday works", or no
        # hint at all. Offer real free times inside whatever window they gave.
        earliest = m.get("earliest_day_key") or m.get("day_key")
        latest = m.get("latest_day_key") or m.get("day_key")
        return _offer_times(run, lead, reply, approver, ledger, leads, live, earliest, latest)
    if not start_local:
        run.move("REVIEW_REQUIRED")
        return run.finish("REVIEW_REQUIRED", "TIME_NOT_AGREED", [why])

    free, checked = morgan.slot_is_free(run, start_local)
    run.move("SLOT_CHECKED")
    if not free:
        run.move("REVIEW_REQUIRED")
        return run.finish("REVIEW_REQUIRED", "SLOT_TAKEN", [f"{start_local} is no longer free"])
    return _book(run, lead, start_local, ledger, leads, notify, live, meet_key, reply_key)


def _book(run: Run, lead: dict, start_local: str, ledger: Ledger, leads: Leads, notify: Notifier,
          live: bool, meet_key: str, reply_key: str) -> Run:
    if not ledger.begin(meet_key, run.run_id, "meeting"):
        run.record("ledger.check", agent=NAME, decision="already_executed", data={"action_id": meet_key})
        run.move("BLOCKED_DUPLICATE")
        return run.finish("BLOCKED_DUPLICATE", "ACTION_ALREADY_EXECUTED", [f"{meet_key} already done"])
    leads.save(lead["email"], {"pending_booking": start_local})
    title = f"{lead.get('name') or lead['email']} <> Rahul — {lead.get('company') or 'intro'}"
    try:
        ev = morgan.book(run, title=title, start_local=start_local, attendee=lead["email"],
                         description=f"From a LinkedIn comment. WarmPath run {run.run_id}.", live=live)
    except Exception as exc:  # noqa: BLE001
        ledger.failed(meet_key, str(exc))
        run.move("CALENDAR_FAILED")
        return run.finish("CALENDAR_FAILED", "CALENDAR_WRITE_FAILED",
                          [str(exc)[:200], f"agreed time kept: {start_local}",
                           "a retry books this time only - nothing is re-read or re-sent"])
    run.steps[-1].decision = "created"
    run.steps[-1].data = {**ev, "action_id": meet_key, "start_local": start_local}
    ledger.done(meet_key, ev.get("event_id", "simulated"))
    ledger.begin(reply_key, run.run_id, "replyread")
    ledger.done(reply_key)
    leads.save(lead["email"], {"pending_booking": "", "status": "meeting_booked", "meeting": start_local})
    run.move("MEETING_BOOKED")
    text = (f"✅ Meeting booked{' (dry run)' if not live else ''}\n*{lead.get('name')}* — "
            f"{lead.get('company') or lead['email']}\n{start_local} {morgan.tz()}\n"
            f"Source: LinkedIn comment · {'warm account' if lead.get('warm') else 'cold lead'}")
    try:
        run.step("slack.notify", lambda: notify(text), agent=NAME, output=text)
    except Exception:  # noqa: BLE001 - the meeting exists; a failed ping is recorded, not fatal
        pass
    run.move("NOTIFIED")
    return run.finish("MEETING_BOOKED", "", [f"event at {start_local} {morgan.tz()}"])


def _offer_times(run: Run, lead: dict, reply: dict, approver: Approver, ledger: Ledger,
                 leads: Leads, live: bool, earliest: str | None = None, latest: str | None = None) -> Run:
    """They want to talk but named no time: offer real free slots in their window, with approval."""
    slots = morgan.offer_slots(run, earliest=earliest, latest=latest)
    if not slots:
        run.move("REVIEW_REQUIRED")
        return run.finish("REVIEW_REQUIRED", "NO_FREE_TIME_IN_WINDOW",
                          [f"no free half-hour on your calendar between {earliest or 'tomorrow'} "
                           f"and {latest or 'two weeks out'}"])
    evidence = {"reply": {"kind": "comment", "text": f"{lead.get('name')} replied: \"{reply['body'][:500]}\""},
                **{sid: {"kind": "slot", "text": f"{'Free slot' if s.get('checked') else 'Proposed time (calendar not checked)'} "
                                    f"{s['label']}"} for sid, s in slots.items()}}
    draft, problems = casey.write(run, channel="email", comment={"text": reply["body"]},
                                  evidence=evidence, allowed={lead["email"]})
    run.move("ACTION_PROPOSED")
    if problems:
        run.move("REVIEW_REQUIRED")
        return run.finish("REVIEW_REQUIRED", "UNSUPPORTED_CLAIM", problems)
    key = action_id("offer", reply["message_id"])
    run.move("AWAITING_APPROVAL")
    card = (f"📅 {lead.get('name')} wants to meet · run `{run.run_id}`\n*They wrote:* \"{reply['body'][:200]}\"\n"
            f"*Morgan's times:* " + "; ".join(s["label"] for s in slots.values())
            + f"\n```{claims.render(draft)}```\nReply *send*, *review* or *ignore*.")
    answer = run.step("slack.approval", lambda: approver(run, {"card": card}), agent=NAME, output=card[:1500])
    run.steps[-1].decision = answer
    if answer != "approved":
        end = "REVIEW_REQUIRED" if answer == "review" else "STOPPED"
        run.move(end)
        return run.finish(end, "APPROVAL_TIMEOUT" if answer == "timeout" else "APPROVAL_REJECTED",
                          [f"slack answer: {answer}"])
    run.move("APPROVED")
    if not ledger.begin(key, run.run_id, "send"):
        run.move("BLOCKED_DUPLICATE")
        return run.finish("BLOCKED_DUPLICATE", "ACTION_ALREADY_EXECUTED", [f"{key} already done"])
    subject = "Re: " + (lead.get("subject") or "times to talk")
    res = casey.send_email(run, to=lead["email"], subject=subject, body=claims.render(draft), live=live)
    run.steps[-1].decision = "sent"
    run.steps[-1].data = {**res, "action_id": key, "to": lead["email"], "address": lead["email"],
                          "sentences": draft["sentences"], "evidence_ids": sorted(evidence)}
    ledger.done(key)
    leads.save(lead["email"], {"offered": slots, "sent_at": datetime.now(timezone.utc).isoformat()})
    run.move("EMAIL_SENT")
    run.move("AWAITING_REPLY")
    return run.finish("AWAITING_REPLY", "", ["offered three free times"])


def _our_domains() -> set[str]:
    """Us: the founder's own addresses and any domain that is ours. Never a warm path to a lead.

    Addresses and domains share one set because the relationship engine checks both an
    address and a domain against it - and a founder on gmail.com has an address, not a domain.
    """
    from .relationships import FREEMAIL, domain_of
    addresses = {a.strip().lower() for a in (os.environ.get("WARMPATH_OUR_ADDRESSES", "") + ","
                                             + os.environ.get("GMAIL_USER", "")).split(",") if "@" in a}
    domains = {domain_of(d) for d in os.environ.get("WARMPATH_OUR_DOMAINS", "").split(",")}
    domains |= {domain_of(a) for a in addresses}
    return addresses | (domains - FREEMAIL - {""})
