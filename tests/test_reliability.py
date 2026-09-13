"""Deterministic reliability tests. Code paths, so they must be 100%.

No network and no model: the LLM and the apps are stubbed, so each test is a rule. Most of
them are failures that return success at every API call - the kind error monitoring never sees.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from warmpath import audit, claims, llm, pipeline
from warmpath.agents import quinn
from warmpath.core import (LEAD, NOT_LEAD, OPT_OUT, IllegalTransition, Leads, Ledger, Run,
                           action_id, evaluate, verify_contact)
from warmpath.integrations import google, unipile
from warmpath.relationships import is_role_mailbox, people_at, strength

FIX = Path(__file__).resolve().parent.parent / "fixtures"


# ── policy gates ───────────────────────────────────────────────────────────────

def gate(**kw):
    base = dict(intent=LEAD, confidence=0.9, verified=True, verify_evidence=["ok"],
                shared_mailbox=False, approved=True)
    return evaluate(**{**base, **kw})


def test_nothing_sends_without_approval():
    assert gate(approved=False).allowed is False
    assert gate(approved=None).decision == "DRAFT_ALLOWED"
    assert gate(approved="review").reason_code == "FOUNDER_WANTS_REVIEW"


def test_every_refusal_has_a_reason_and_evidence():
    for d in (gate(intent=NOT_LEAD), gate(intent=OPT_OUT), gate(confidence=0.4),
              gate(verified=False, verify_evidence=["no address"]), gate(shared_mailbox=True)):
        assert not d.allowed and d.reason_code and d.evidence


def test_state_machine_cannot_jump_to_a_send():
    run = Run.start("m1")
    with pytest.raises(IllegalTransition):
        run.move("EMAIL_SENT")


# ── identity ───────────────────────────────────────────────────────────────────

def test_a_guessed_address_is_never_verified():
    ok, ev = verify_contact("john.smith@acme.example", {"their comment": "Looking for something like this"})
    assert not ok and "does not appear" in ev[0]


@pytest.mark.parametrize("addr", ["sales@acme.example", "info+web@acme.example", "support-2@acme.example"])
def test_desks_are_not_people(addr):
    assert is_role_mailbox(addr)


def test_people_with_desk_like_names_are_people():
    assert not is_role_mailbox("salesforce.dev@acme.example")


# ── relationships (ported from Zavorik) ────────────────────────────────────────

def rec(i, addr, channel="inbound", at="2026-08-01T00:00:00+00:00", name=""):
    return {"id": f"r{i}", "channel": channel, "address": addr, "at": at, "name": name, "subject": "s"}


def test_free_mail_is_never_a_company():
    assert people_at("gmail.com", [rec(1, "a@gmail.com")])["refused"]


def test_our_own_domain_is_never_a_warm_path():
    assert people_at("us.example", [rec(1, "me@us.example")], ours={"us.example"})["refused"]


def test_company_is_matched_by_domain_not_name():
    records = [rec(1, "david@acme.example", "meeting"), rec(2, "david@acme.example", "inbound")]
    assert people_at("acmelogistics.example", records)["people"] == []
    assert people_at("acme.example", records)["people"]


def test_contacts_file_can_place_a_freemail_person_at_a_company():
    got = people_at("acme.example", [rec(1, "dchen.demo@gmail.com", "meeting")],
                    address_company={"dchen.demo@gmail.com": "acme.example"})
    assert got["people"] and got["people"][0]["via"] == "contacts file"


def test_a_relay_domain_is_refused():
    records = [rec(1, "a1@relay.example", name="Pat"), rec(2, "a2@relay.example", name="Pat")]
    assert "relay" in people_at("relay.example", records)["refused"]


def test_one_way_contact_is_never_strong():
    s = strength({"outbound": {"count": 8, "last_at": "2026-09-01T00:00:00+00:00"}})
    assert s["label"] != "strong" and not s["mutual"]


# ── claims ─────────────────────────────────────────────────────────────────────

EV = {"comment": {"kind": "comment", "text": "John commented: looking for this"},
      "rel1": {"kind": "relationship", "text": "We have prior contact with David Chen at acme.example: met once"},
      "slot1": {"kind": "slot", "text": "Free slot Wednesday 17 September at 11:00 Asia/Kolkata"}}


def sent(text, ev=(), prior=False):
    return {"sentences": [{"text": text, "evidence": list(ev), "claims_prior_contact": prior}]}


def test_prior_contact_needs_relationship_evidence():
    assert claims.check(sent("Great to reconnect after our call.", ["comment"], True), EV, allowed_addresses=set())
    assert not claims.check(sent("I met David Chen from your team.", ["rel1"], True), EV, allowed_addresses=set())


def test_a_capability_promise_needs_the_founders_post():
    ev = {**EV, "post": {"kind": "post", "text": "Rahul's post: WarmPath drafts outreach you approve in Slack"}}
    s = {"sentences": [{"text": "Yes, it plugs into Salesforce.", "evidence": ["comment"], "claims_capability": True}]}
    assert claims.check(s, ev, allowed_addresses=set())
    s["sentences"][0].update(text="It drafts outreach you approve in Slack.", evidence=["post"])
    assert not claims.check(s, ev, allowed_addresses=set())


def test_an_invented_figure_or_time_is_caught():
    assert claims.check(sent("Plans start at $49 per seat.", ["comment"]), EV, allowed_addresses=set())
    assert not claims.check(sent("Does Wednesday at 11:00 work?", ["slot1"]), EV, allowed_addresses=set())


def test_an_invented_source_or_address_is_caught():
    assert claims.check(sent("As we discussed.", ["email99"]), EV, allowed_addresses=set())
    assert claims.check(sent("Write to me at sales@acme.example", ["comment"]), EV, allowed_addresses=set())


# ── ledger ─────────────────────────────────────────────────────────────────────

def test_duplicate_delivery_executes_once(tmp_path):
    led = Ledger(tmp_path / "l.db")
    aid = action_id("send", "john@acme.example")
    assert led.begin(aid, "r1", "send")
    led.done(aid)
    assert not led.begin(aid, "r2", "send")
    assert led.done_count("send") == 1
    led.close()


# ── pipeline, end to end, with the model and the apps stubbed ──────────────────

class Stubs:
    def __init__(self, monkeypatch, *, intent=LEAD, confidence=0.92, records=(), draft=None,
                 meeting=None):
        self.sent = []
        # These scenarios exercise the direct outreach route; introductions have their own suite.
        monkeypatch.setenv("WARMPATH_INTRODUCTIONS", "0")
        monkeypatch.setattr(llm, "classify", lambda text, **k: {
            "intent": intent, "confidence": confidence, "signals": [], "ungrounded_signals": 0})
        monkeypatch.setattr(google, "mail_evidence", lambda d, a=(), years=3: list(records))
        monkeypatch.setattr(google, "meeting_evidence", lambda d, a=(), years=3: [])
        monkeypatch.setattr(google, "composio_ready", lambda: False)
        monkeypatch.setattr(unipile, "get_company", lambda cid: None)
        monkeypatch.setattr(llm, "draft", draft or self.default_draft)
        if meeting:
            monkeypatch.setattr(llm, "read_meeting", lambda body, **k: meeting)

    @staticmethod
    def default_draft(*, channel, comment, evidence, problems=(), temperature=0.3):
        warm = [i for i, e in evidence.items() if e["kind"] == "relationship"]
        s = [{"text": "Thanks for the comment - happy to show you how it works.", "evidence": ["comment"],
              "claims_prior_contact": False}]
        if warm:
            s.insert(0, {"text": "We have prior contact with your team.", "evidence": warm[:1],
                         "claims_prior_contact": True})
        return {"subject": "Your comment", "sentences": s}


def comments():
    return {c["comment_id"]: c for c in json.loads((FIX / "eval_post.json").read_text(encoding="utf-8"))["comments"]}


CONTACTS = json.loads((FIX / "eval_contacts.json").read_text(encoding="utf-8"))
WARM = [rec(1, "david.chen@acme.example", "meeting", name="David Chen"),
        rec(2, "david.chen@acme.example", "inbound", name="David Chen"),
        rec(3, "david.chen@acme.example", "outbound", name="David Chen")]


def go(c, tmp_path, answer="approved"):
    led, leads = Ledger(tmp_path / "w.db"), Leads(tmp_path / "w.db")
    run = pipeline.process_comment(c, post_text="post", approver=lambda r, p: answer,
                                   contacts=CONTACTS, ledger=led, leads=leads, ours=set())
    return run, led, leads


def test_praise_is_ignored_and_nothing_is_written(monkeypatch, tmp_path):
    Stubs(monkeypatch, intent=NOT_LEAD, confidence=0.95)
    run, *_ = go(comments()["c-praise"], tmp_path)
    assert run.state == "IGNORED" and run.reason_code == "NO_COMMERCIAL_INTENT"
    assert not any(s.tool in audit.WRITES for s in run.steps)


def test_warm_lead_gets_one_email_that_cites_the_relationship(monkeypatch, tmp_path):
    Stubs(monkeypatch, records=WARM)
    run, led, leads = go(comments()["c-warm-acme"], tmp_path)
    assert run.state == "AWAITING_REPLY"
    send = next(s for s in run.steps if s.tool == "gmail.send")
    assert send.data["to"] == "john.smith@acme.example" and send.data["simulated"]
    assert any(x["claims_prior_contact"] for x in send.data["sentences"])
    assert audit.audit_run(run) == []


def test_no_address_means_a_linkedin_reply_never_a_guessed_email(monkeypatch, tmp_path):
    Stubs(monkeypatch)
    run, *_ = go(comments()["c-cold-nova"], tmp_path)
    assert run.state == "COMMENT_REPLIED"
    assert not any(s.tool == "gmail.send" for s in run.steps)
    assert audit.audit_run(run) == []


def test_a_public_reply_never_sees_private_relationship_evidence(monkeypatch, tmp_path):
    seen = {}

    def spy(*, channel, comment, evidence, problems=(), temperature=0.3):
        seen[channel] = {e["kind"] for e in evidence.values()}
        return Stubs.default_draft(channel=channel, comment=comment, evidence=evidence)
    Stubs(monkeypatch, records=WARM, draft=spy)
    c = {**comments()["c-warm-acme"], "profile": {**comments()["c-warm-acme"]["profile"], "emails": []}}
    run, *_ = go(c, tmp_path)
    assert run.state == "COMMENT_REPLIED" and "relationship" not in seen["linkedin_reply"]
    assert audit.audit_run(run) == []


def test_same_company_name_different_domain_is_not_warm(monkeypatch, tmp_path):
    Stubs(monkeypatch, records=WARM)
    run, *_ = go(comments()["c-name-collision"], tmp_path)
    rel = next(s for s in run.steps if s.tool == "relationship.resolve")
    assert rel.decision == "cold"
    assert any(s.tool == "relationship.collision" for s in run.steps)


def test_a_draft_that_invents_a_relationship_never_leaves(monkeypatch, tmp_path):
    lying = lambda **k: {"subject": "x", "sentences": [
        {"text": "Great catching up at our call last month.", "evidence": ["comment"], "claims_prior_contact": True}]}
    Stubs(monkeypatch, draft=lying)
    run, *_ = go(comments()["c-warm-acme"], tmp_path)
    assert run.state == "REVIEW_REQUIRED" and run.reason_code == "UNSUPPORTED_CLAIM"
    assert not any(s.tool in audit.WRITES for s in run.steps)


def test_the_same_comment_twice_sends_once(monkeypatch, tmp_path):
    Stubs(monkeypatch, records=WARM)
    first, led, leads = go(comments()["c-warm-acme"], tmp_path)
    second = pipeline.process_comment(comments()["c-warm-acme"], post_text="post",
                                      approver=lambda r, p: "approved", contacts=CONTACTS,
                                      ledger=led, leads=leads, ours=set())
    assert second.state == "BLOCKED_DUPLICATE" and led.done_count("send") == 1


@pytest.mark.parametrize("answer,end", [("rejected", "STOPPED"), ("timeout", "STOPPED"), ("review", "REVIEW_REQUIRED")])
def test_anything_but_send_sends_nothing(monkeypatch, tmp_path, answer, end):
    Stubs(monkeypatch, records=WARM)
    run, led, _ = go(comments()["c-warm-acme"], tmp_path, answer)
    assert run.state == end and led.done_count() == 0


def _lead_with_offer(leads):
    return leads.save("john.smith@acme.example", {
        "email": "john.smith@acme.example", "name": "John Smith", "company": "Acme",
        "offered": {"slot1": {"start_local": "2026-09-16T11:00:00", "label": "Wednesday 16 September at 11:00 Asia/Kolkata"}}})


YES = {"wants_meeting": "yes", "offered_slot_id": "slot1", "day_key": None, "time_24h": None,
       "timezone_stated": None, "ambiguous": False, "signals": []}
REPLY = {"message_id": "<r1@x>", "body": "Wednesday 11 works for me."}


def test_accepted_slot_books_once_and_notifies(monkeypatch, tmp_path):
    Stubs(monkeypatch, meeting=YES)
    led, leads, pings = Ledger(tmp_path / "w.db"), Leads(tmp_path / "w.db"), []
    lead = _lead_with_offer(leads)
    run = pipeline.process_reply(lead, REPLY, approver=lambda r, p: "approved", notify=pings.append,
                                 ledger=led, leads=leads)
    again = pipeline.process_reply(lead, REPLY, approver=lambda r, p: "approved", notify=pings.append,
                                   ledger=led, leads=leads)
    assert run.state == "NOTIFIED" and again.state == "BLOCKED_DUPLICATE"
    assert led.done_count("meeting") == 1 and len(pings) == 1
    assert audit.audit_run(run) == []


def test_not_this_week_means_offers_only_inside_their_window(monkeypatch, tmp_path):
    from datetime import date, timedelta
    today = date.today()
    monday = today + timedelta(days=7 - today.weekday())
    friday = monday + timedelta(days=4)
    Stubs(monkeypatch, meeting={**YES, "offered_slot_id": None, "earliest_day_key": monday.isoformat(),
                                "latest_day_key": friday.isoformat()})
    led, leads = Ledger(tmp_path / "w.db"), Leads(tmp_path / "w.db")
    lead = leads.save("john.smith@acme.example", {"email": "john.smith@acme.example", "name": "John Smith",
                                                  "company": "Acme", "sent_at": "2026-01-01T00:00:00+00:00"})

    def times_draft(*, channel, comment, evidence, problems=(), temperature=0.3):
        slots = [i for i, e in evidence.items() if e["kind"] == "slot"]
        return {"subject": "Times", "sentences": [{"text": "Here are a few times that work next week.",
                                                   "evidence": slots, "claims_prior_contact": False}]}
    monkeypatch.setattr(llm, "draft", times_draft)
    run = pipeline.process_reply(lead, {"message_id": "<r2@x>", "body": "Not available this week, can we do next week?"},
                                 approver=lambda r, p: "approved", notify=lambda t: None, ledger=led, leads=leads)
    offer = next(s for s in run.steps if s.tool == "calendar.offer")
    days = [v["start_local"][:10] for v in offer.data["slots"].values()]
    assert run.state == "AWAITING_REPLY" and days
    assert all(monday.isoformat() <= d <= friday.isoformat() for d in days)
    assert audit.audit_run(run) == []


def test_auditor_catches_an_offer_outside_their_window():
    run = Run.start("m", kind="reply", state="REPLY_RECEIVED")
    run.record("calendar.offer", data={"slots": {"slot1": {"start_local": "2026-09-15T11:00:00"}},
                                       "window": {"earliest": "2026-09-21", "latest": "2026-09-25"}, "calendar_checked": True})
    assert "M4_OFFER_ONLY_TIMES_THEY_CAN_DO" in invariants(run)


def test_a_time_they_never_accepted_is_not_booked(monkeypatch, tmp_path):
    Stubs(monkeypatch, meeting={**YES, "offered_slot_id": None, "day_key": "2026-09-18", "time_24h": "15:00"})
    led, leads = Ledger(tmp_path / "w.db"), Leads(tmp_path / "w.db")
    run = pipeline.process_reply(_lead_with_offer(leads), REPLY, approver=lambda r, p: "approved",
                                 notify=lambda t: None, ledger=led, leads=leads)
    assert run.state == "REVIEW_REQUIRED" and led.done_count("meeting") == 0


def test_calendar_503_resumes_at_booking_only(monkeypatch, tmp_path):
    Stubs(monkeypatch, meeting=YES)
    led, leads = Ledger(tmp_path / "w.db"), Leads(tmp_path / "w.db")
    lead = _lead_with_offer(leads)
    google.INJECT_CALENDAR_FAILURES["remaining"] = 1
    broken = pipeline.process_reply(lead, REPLY, approver=lambda r, p: "approved", notify=lambda t: None,
                                    ledger=led, leads=leads)
    fixed = pipeline.process_reply(lead, REPLY, approver=lambda r, p: "approved", notify=lambda t: None,
                                   ledger=led, leads=leads)
    google.INJECT_CALENDAR_FAILURES["remaining"] = 0
    assert broken.state == "CALENDAR_FAILED" and fixed.state == "NOTIFIED"
    assert not any(s.tool == "meeting.read" for s in fixed.steps)
    assert led.done_count("meeting") == 1
    assert audit.audit_run(broken) == [] and audit.audit_run(fixed) == []


# ── the auditor catches what returned success ──────────────────────────────────

def lead_run(*, approved="approved", verified=True, to="john.smith@acme.example", simulated=True,
             sentences=None, rel="warm"):
    run = Run.start("m")
    sentences = sentences or [{"text": "Thanks for the comment.", "evidence": ["comment"], "claims_prior_contact": False}]
    run.record("intent.classify", decision=LEAD, confidence=0.9)
    run.record("identity.resolve", data={"verified": verified, "email": "john.smith@acme.example" if verified else ""})
    run.record("relationship.resolve", decision=rel, data={"domain": "acme.example", "people": []})
    run.record("claims.check", decision="pass", data={"sentences": sentences, "evidence": EV})
    run.record("slack.approval", decision=approved)
    run.record("gmail.send", data={"simulated": simulated, "to": to, "sentences": sentences, "action_id": "send:x"})
    for s in ("CLASSIFIED", "IDENTITY_RESOLVED", "RELATIONSHIP_RESOLVED", "ACTION_PROPOSED",
              "AWAITING_APPROVAL", "APPROVED", "EMAIL_SENT", "AWAITING_REPLY"):
        run.move(s)
    return run.finish("AWAITING_REPLY")


def invariants(run):
    return {v.invariant for v in audit.audit_run(run)}


def test_auditor_passes_a_correct_run():
    assert invariants(lead_run()) == set()


def test_auditor_catches_send_without_approval():
    assert "C1_SEND_NEEDS_APPROVAL" in invariants(lead_run(approved="timeout"))


def test_auditor_catches_a_guessed_address():
    assert "J1_NEVER_GUESS_AN_ADDRESS" in invariants(lead_run(verified=False, to="john@acme.example"))


def test_auditor_catches_a_real_send_in_dry_run():
    assert "C4_DRY_RUN_TOUCHES_NOTHING" in invariants(lead_run(simulated=False))


def test_auditor_catches_an_unbacked_warm_claim_even_after_it_passed_a_weaker_check():
    s = [{"text": "Great catching up last month.", "evidence": ["rel1"], "claims_prior_contact": True}]
    assert "J2_COMPANY_BY_DOMAIN_NOT_NAME" in invariants(lead_run(sentences=s, rel="cold"))


def test_auditor_catches_a_silent_refusal():
    run = Run.start("m")
    run.move("CLASSIFIED")
    run.move("IGNORED")
    assert "A1_REFUSAL_HAS_A_REASON" in invariants(run.finish("IGNORED"))


def test_auditor_catches_the_same_action_in_two_runs():
    a, b = lead_run(), lead_run()
    assert any(v.invariant == "C3_ONE_OUTREACH_PER_PERSON" for v in audit.audit_duplicates([a, b]))


def test_a_violation_becomes_a_regression_case(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "REGRESSIONS", tmp_path)
    report = audit.audit_all([lead_run(approved="rejected")])
    assert report["violations"] and list(tmp_path.glob("C1_SEND_NEEDS_APPROVAL__*.json"))


# ── posts: a goal becomes something public only with approval and sources ─────

def _post_draft(*, goal, evidence, problems=(), temperature=0.5):
    return {"subject": "", "sentences": [
        {"text": "Warm leads die when you forget who you already know.", "evidence": [], "claims_capability": False},
        {"text": "WarmPath checks Gmail and Calendar for an existing relationship.", "evidence": ["relationship"],
         "claims_capability": True}]}


def test_a_post_is_published_only_after_approval_and_only_once(monkeypatch, tmp_path):
    monkeypatch.setattr(llm, "draft_post", _post_draft)
    led = Ledger(tmp_path / "p.db")
    rejected = pipeline.process_goal("I want to grow", approver=lambda r, p: "rejected", ledger=led)
    assert rejected.state == "STOPPED" and not any(s.tool == "linkedin.post" for s in rejected.steps)
    first = pipeline.process_goal("I want to grow", approver=lambda r, p: "approved", ledger=led)
    again = pipeline.process_goal("I want to grow", approver=lambda r, p: "approved", ledger=led)
    assert first.state == "POST_PUBLISHED" and again.state == "BLOCKED_DUPLICATE"
    assert audit.audit_run(first) == [] and audit.audit_run(rejected) == []


def test_a_post_that_invents_a_result_never_reaches_approval(monkeypatch, tmp_path):
    lying = lambda **k: {"subject": "", "sentences": [
        {"text": "WarmPath booked 40 meetings for our customers last month.", "evidence": ["what"], "claims_capability": True}]}
    monkeypatch.setattr(llm, "draft_post", lying)
    asked = []
    run = pipeline.process_goal("grow", approver=lambda r, p: asked.append(1) or "approved", ledger=Ledger(tmp_path / "p.db"))
    assert run.state == "REVIEW_REQUIRED" and not asked


def test_a_verified_work_email_places_someone_at_that_company(monkeypatch, tmp_path):
    """LinkedIn shows a company typed as plain text (no page, no website); the member shares a work
    address. The domain comes from that verified address - never from the company name."""
    Stubs(monkeypatch, records=[rec(1, "known@ascot.example", "inbound", name="Known"),
                                rec(2, "known@ascot.example", "outbound", name="Known")])
    c = {**comments()["c-warm-acme"], "profile": {"name": "Rahul", "headline": "Engineer at Ascot",
         "emails": ["rahul@ascot.example"], "work": [{"company": "Ascot", "company_id": "", "current": True}]}}
    run, *_ = go(c, tmp_path)
    ident = next(s for s in run.steps if s.tool == "identity.resolve").data
    assert ident["domain"] == "ascot.example" and ident["domain_source"] == "their verified work email"


def test_a_free_mail_address_never_becomes_a_company(monkeypatch, tmp_path):
    Stubs(monkeypatch)
    c = {**comments()["c-warm-acme"], "profile": {"name": "Rahul", "headline": "Engineer at Ascot",
         "emails": ["rahul.personal@gmail.com"], "work": [{"company": "Ascot", "company_id": "", "current": True}]}}
    run, *_ = go(c, tmp_path)
    assert next(s for s in run.steps if s.tool == "identity.resolve").data["domain"] == ""
