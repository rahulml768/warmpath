"""Fault injection: break one dependency at a time and check what the system does about it.

Every case asserts the same four things a Lemma-style reviewer asks about an agent in production:
  1. nothing happens twice       - no second email, post or meeting
  2. nothing is silently dropped - the work is retried, or someone is told
  3. every stop has a reason     - a reason code, an alert, or a recorded error
  4. it recovers                 - once the dependency is back, the work completes
No network: the dependency failures are injected.
"""

import time
from datetime import date, datetime, timedelta, timezone

import pytest

from warmpath import autopilot as autopilot_mod
from warmpath import audit, llm, pipeline, store
from warmpath.agents import quinn
from warmpath.chat import Chat, ChatApprover
from warmpath.core import Leads, Ledger
from warmpath.integrations import google, slack
from test_reliability import CONTACTS, REPLY, YES, Stubs, _lead_with_offer, comments, lead_run


@pytest.fixture
def world(monkeypatch, tmp_path):
    """A private database, a chat, an autopilot that never starts its threads, and captured alerts."""
    monkeypatch.setenv("WARMPATH_MODE", "dry_run")
    db = store.SQLiteStore(tmp_path / "chaos.db")
    monkeypatch.setattr(store, "_STORE", db)
    alerts = []
    monkeypatch.setattr(pipeline, "slack_notifier", lambda text: alerts.append(text))
    chat = Chat(tmp_path / "chaos.db")
    pilot = autopilot_mod.Autopilot(chat, interval_s=5)
    return {"db": db, "chat": chat, "pilot": pilot, "alerts": alerts, "path": tmp_path / "chaos.db"}


def _wait_idle(pilot, seconds=10):
    end = time.time() + seconds
    while pilot.inflight and time.time() < end:
        time.sleep(0.05)
    assert not pilot.inflight, "worker did not finish"


# ── the heartbeat itself ───────────────────────────────────────────────────────

def test_a_stuck_heartbeat_is_reported_once(world):
    pilot = world["pilot"]
    pilot.tick_started = time.time() - 500
    pilot.sweep()
    pilot.sweep()
    assert pilot.status["health"] == "degraded"
    assert sum("heartbeat stuck" in a for a in world["alerts"]) == 1


def test_a_heartbeat_that_stopped_is_reported(world):
    pilot = world["pilot"]
    pilot.status["last_tick"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    pilot.sweep()
    assert any("heartbeat stopped" in a for a in world["alerts"])


def test_the_mailbox_connection_cannot_hang_forever(monkeypatch):
    seen = {}

    def fake(host, **kwargs):
        seen.update(kwargs)
        raise OSError("stop here")
    monkeypatch.setattr(google.imaplib, "IMAP4_SSL", fake)
    with pytest.raises(OSError):
        google._imap()
    assert seen.get("timeout") and seen["timeout"] <= 60


def test_a_database_outage_during_a_heartbeat_is_recorded_not_fatal(world, monkeypatch):
    pilot = world["pilot"]

    def down():
        raise store.StoreError("supabase GET watches: HTTP 503")
    monkeypatch.setattr(pilot, "watches", down)
    pilot.beat()
    assert "503" in pilot.status["last_error"]


# ── work that gets stuck or abandoned ──────────────────────────────────────────

def test_abandoned_work_is_released_for_retry(world):
    pilot, db = world["pilot"], world["db"]
    old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    db.upsert("claims", {"key": "reply:<lost@x>", "kind": "reply", "status": "in_progress", "updated": old})
    pilot.sweep()
    assert db.get("claims", "reply:<lost@x>")["status"] == "failed"
    assert any("abandoned" in a for a in world["alerts"])


def test_work_that_never_finishes_is_reported(world):
    pilot = world["pilot"]
    pilot.inflight["reply:<slow@x>"] = {"key": "reply:<slow@x>", "who": "John", "stage": "Reading their reply",
                                        "since": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()}
    pilot.sweep()
    assert any("John stuck" in a for a in world["alerts"])


def test_waiting_for_the_founder_is_not_stuck(world):
    pilot = world["pilot"]
    pilot.inflight["reply:<wait@x>"] = {"key": "reply:<wait@x>", "who": "John", "stage": "Waiting for your approval",
                                        "since": (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()}
    pilot.sweep()
    assert not world["alerts"]


# ── the model is down ──────────────────────────────────────────────────────────

def test_model_down_drops_nothing_and_recovers(world, monkeypatch):
    pilot, db = world["pilot"], world["db"]
    Stubs(monkeypatch, intent="NOT_LEAD", confidence=0.95)

    def down(*a, **k):
        raise llm.LLMError("All configured model providers failed: deepseek: HTTP 402; cerebras: HTTP 429")
    monkeypatch.setattr(llm, "classify", down)
    c = comments()["c-praise"]
    key = f"comment:{c['comment_id']}"
    assert pilot.claim(key, "comment")
    pilot._spawn(key, c["author_name"], "Reading the comment", pilot._comment, {"text": "post"}, c, CONTACTS, "the post")
    _wait_idle(pilot)
    assert db.get("claims", key)["status"] == "failed"                      # released: the next tick retries
    assert any("couldn't finish" in m["text"] for m in world["chat"].messages())  # and the founder is told
    assert Ledger(world["path"]).done_count() == 0                            # nothing went out

    monkeypatch.setattr(llm, "classify", lambda text, **k: {"intent": "NOT_LEAD", "confidence": 0.95,
                                                            "signals": [], "ungrounded_signals": 0})
    assert pilot.claim(key, "comment")
    pilot._spawn(key, c["author_name"], "Reading the comment", pilot._comment, {"text": "post"}, c, CONTACTS, "the post")
    _wait_idle(pilot)
    assert db.get("claims", key)["status"] == "done"


# ── LinkedIn is down ───────────────────────────────────────────────────────────

def test_linkedin_error_does_not_stop_reply_checks(world, monkeypatch):
    pilot, db = world["pilot"], world["db"]
    db.upsert("watches", {"source": "https://www.linkedin.com/posts/x-activity-7332661864792854528", "label": "your post",
                          "added": datetime.now(timezone.utc).isoformat(), "active": True})
    Leads(world["path"]).save("john@acme.example", {"email": "john@acme.example", "status": "awaiting_reply",
                                                    "sent_at": datetime.now(timezone.utc).isoformat()})

    def unipile_down(source):
        raise RuntimeError("unipile GET posts: HTTP 429 rate limited")
    checked = []
    monkeypatch.setattr(quinn, "read_post", unipile_down)
    monkeypatch.setattr(google, "replies_from", lambda addr, since: checked.append(addr) or [])
    pilot.beat()
    assert "429" in pilot.status["last_error"] or "couldn't read" in pilot.status["last_error"]
    assert checked == ["john@acme.example"]


# ── a restart in the middle ────────────────────────────────────────────────────

def test_restart_after_sending_but_before_bookkeeping_sends_once(monkeypatch, tmp_path):
    """The worst crash: the email went out, the process died before marking the claim done.
    The retry must end as a blocked duplicate - never a second email."""
    from test_reliability import WARM
    monkeypatch.setenv("WARMPATH_MODE", "dry_run")
    Stubs(monkeypatch, records=WARM)
    led, leads = Ledger(tmp_path / "r.db"), Leads(tmp_path / "r.db")
    c = comments()["c-warm-acme"]
    first = pipeline.process_comment(c, post_text="post", approver=lambda r, p: "approved", contacts=CONTACTS,
                                     ledger=led, leads=leads, ours=set())
    retry = pipeline.process_comment(c, post_text="post", approver=lambda r, p: "approved", contacts=CONTACTS,
                                     ledger=led, leads=leads, ours=set())
    assert first.state == "AWAITING_REPLY" and retry.state == "BLOCKED_DUPLICATE"
    assert led.done_count("send") == 1


# ── Slack is down ──────────────────────────────────────────────────────────────

def test_slack_down_still_lets_the_founder_approve_in_the_app(world, monkeypatch):
    from warmpath.core import Run
    monkeypatch.setenv("SLACK_APPROVER_ID", "U123")

    def slack_down(channel, text):
        raise RuntimeError("slack chat.postMessage: service_unavailable")
    monkeypatch.setattr(slack, "post", slack_down)
    run = Run.start("m")
    world["chat"].decide(run.run_id, "approved")
    answer = ChatApprover(world["chat"], {"text": "hi"}, {"intent", "jordan", "casey"}, timeout_s=5)(run, {"card": "card"})
    assert answer == "approved"
    assert any("Slack didn't take the card" in m["text"] for m in world["chat"].messages())


# ── the rules, checked live ────────────────────────────────────────────────────

def test_a_broken_rule_alerts_the_moment_the_run_ends(world, monkeypatch, tmp_path):
    monkeypatch.setattr(audit, "REGRESSIONS", tmp_path / "regressions")
    bad = lead_run(approved="timeout")                 # an email went out without approval
    world["pilot"].audit_now(bad)
    assert any("C1_SEND_NEEDS_APPROVAL" in a for a in world["alerts"])
    assert world["db"].select("audit_violations")
    assert list((tmp_path / "regressions").glob("C1_SEND_NEEDS_APPROVAL__*.json"))


def test_a_clean_run_raises_no_alert(world, monkeypatch, tmp_path):
    monkeypatch.setattr(audit, "REGRESSIONS", tmp_path / "regressions")
    world["pilot"].audit_now(lead_run())
    assert not world["alerts"]


# ── scheduling under an unclear reply ──────────────────────────────────────────

def test_not_this_week_is_ambiguous_but_still_gets_real_times(monkeypatch, tmp_path):
    """The model marks "not this week, next week?" ambiguous (no day, no hour). That must lead to
    offered times for approval, not a dead end in review."""
    monkeypatch.setenv("WARMPATH_MODE", "dry_run")
    today = date.today()
    monday = today + timedelta(days=7 - today.weekday())
    Stubs(monkeypatch, meeting={**YES, "offered_slot_id": None, "ambiguous": True,
                                "earliest_day_key": monday.isoformat(), "latest_day_key": (monday + timedelta(days=4)).isoformat()})
    monkeypatch.setattr(llm, "draft", lambda **k: {"subject": "Times", "sentences": [
        {"text": "Here are a few times next week.", "evidence": [i for i, e in k["evidence"].items() if e["kind"] == "slot"],
         "claims_prior_contact": False}]})
    led, leads = Ledger(tmp_path / "w.db"), Leads(tmp_path / "w.db")
    lead = leads.save("john.smith@acme.example", {"email": "john.smith@acme.example", "name": "John Smith",
                                                  "sent_at": "2026-01-01T00:00:00+00:00"})
    run = pipeline.process_reply(lead, {"message_id": "<nw@x>", "body": "I'm not available this week, can we do next week?"},
                                 approver=lambda r, p: "approved", notify=lambda t: None, ledger=led, leads=leads)
    assert run.state == "AWAITING_REPLY"
    assert any(s.tool == "calendar.offer" for s in run.steps)


def test_an_ambiguous_yes_to_a_slot_is_never_booked(monkeypatch, tmp_path):
    monkeypatch.setenv("WARMPATH_MODE", "dry_run")
    Stubs(monkeypatch, meeting={**YES, "ambiguous": True})
    led, leads = Ledger(tmp_path / "w.db"), Leads(tmp_path / "w.db")
    run = pipeline.process_reply(_lead_with_offer(leads), REPLY, approver=lambda r, p: "approved",
                                 notify=lambda t: None, ledger=led, leads=leads)
    assert run.state == "REVIEW_REQUIRED" and led.done_count("meeting") == 0
