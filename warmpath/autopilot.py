"""Autopilot - the heartbeat that keeps the team working without being asked.

Once a post is being watched, nobody has to type anything: every tick Quinn looks for new
comments and Morgan looks for replies, and each piece of work goes through the same pipeline,
gates, ledger and audit as a request typed into chat. The founder is interrupted only for what
needs a human - an approval card, here and in Slack.

Two things make a heartbeat safe to run forever:
- A claim table. A comment or a reply is claimed once before any work starts, so a tick that
  fires while an earlier one is still waiting for approval never picks the same item up again.
  A claim that failed (network down, model unreachable) is released so the next tick retries.
- One worker thread per item. An approval can take minutes; it must not stop the heartbeat from
  noticing the next comment.
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from . import pipeline
from .agents import quinn
from .chat import ROOT, Chat, ChatApprover, _source_files, _step, cards_so_far, result_card
from .core import Leads, Ledger, action_id, mode
from .integrations import google


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Autopilot:
    def __init__(self, chat: Chat, interval_s: int | None = None):
        from . import store
        self.chat, self.s = chat, store.store()
        self.interval_s = interval_s or int(os.environ.get("WARMPATH_HEARTBEAT_S", "60"))
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.inflight: dict[str, dict] = {}
        self.status = {"last_tick": "", "next_tick": "", "ticks": 0, "last_error": "", "last_found": ""}
        # A founder away from the desk is not a "no". Autopilot cards wait a day; silence still sends nothing.
        self.approval_timeout_s = int(os.environ.get("WARMPATH_APPROVAL_TIMEOUT_S", str(24 * 3600)))
        self.tick_started = 0.0
        self.alerted: set[str] = set()
        self.status.update(health="ok", issues=[], alerts=[])

    # ── storage ────────────────────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        row = self.s.get("settings", "autopilot")
        return (row["value"] if row else os.environ.get("WARMPATH_AUTOPILOT", "1")) == "1"

    def set_enabled(self, on: bool) -> None:
        self.s.upsert("settings", {"key": "autopilot", "value": "1" if on else "0"})
        if on:
            self.poke()

    def watch(self, source: str, label: str) -> None:
        self.s.upsert("watches", {"source": source, "label": label, "added": _now(), "active": True})
        self.poke()

    def watches(self) -> list[dict]:
        return [{"source": r["source"], "label": r["label"], "added": r["added"]}
                for r in self.s.select("watches", {"active": True}, order="added")]

    def claim(self, key: str, kind: str) -> bool:
        """True if this item is ours to process now; False if it's done or someone else has it."""
        row = self.s.get("claims", key)
        if row and row["status"] in ("in_progress", "done"):
            return False
        self.s.upsert("claims", {"key": key, "kind": kind, "status": "in_progress", "updated": _now()})
        return True

    def release(self, key: str, status: str) -> None:
        self.s.update("claims", key, {"status": status, "updated": _now()})

    # ── the loop ───────────────────────────────────────────────────────────────
    def poke(self) -> None:
        self.wake.set()

    def start(self) -> "Autopilot":
        # Anything left in_progress by a previous process was interrupted, not finished.
        for row in self.s.select("claims", {"status": "in_progress"}):
            self.release(row["key"], "failed")
        threading.Thread(target=self._loop, daemon=True, name="autopilot").start()
        threading.Thread(target=self._watchdog, daemon=True, name="autopilot-watchdog").start()
        return self

    # ── watching the watcher ───────────────────────────────────────────────────
    #: A tick that runs longer than this is stuck on something (a mailbox, the database, a provider).
    TICK_STUCK_S = int(os.environ.get("WARMPATH_TICK_STUCK_S", "90"))
    #: Work that isn't waiting on the founder and hasn't finished in this long is stuck.
    WORK_STUCK_S = int(os.environ.get("WARMPATH_WORK_STUCK_S", "600"))
    #: A claim nobody in this process is working on, left in_progress this long, was abandoned.
    ORPHAN_CLAIM_S = int(os.environ.get("WARMPATH_ORPHAN_CLAIM_S", "300"))

    def alert(self, key: str, text: str) -> None:
        """Say it once, where the founder already looks: the chat and Slack."""
        if key in self.alerted:
            return
        self.alerted.add(key)
        self.status.setdefault("alerts", []).append({"at": _now(), "text": text})
        self.status["alerts"] = self.status["alerts"][-20:]
        try:
            self.chat.post("Alex", "text", f"⚠️ {text}")
        except Exception:  # noqa: BLE001 - the database may be the thing that is down
            pass
        try:
            pipeline.slack_notifier(f"⚠️ WarmPath: {text}")
        except Exception:  # noqa: BLE001
            pass

    def health(self) -> list[tuple[str, str]]:
        """What is wrong right now, as (stable key, message). Empty means healthy."""
        issues = []
        now = time.time()
        if self.enabled and self.tick_started and now - self.tick_started > self.TICK_STUCK_S:
            issues.append(("heartbeat-stuck", f"heartbeat stuck: one check has been running for {int(now - self.tick_started)}s"))
        last = self.status.get("last_tick")
        if self.enabled and last and not self.tick_started:
            age = now - datetime.fromisoformat(last).timestamp()
            if age > max(60, self.interval_s * 6):
                issues.append(("heartbeat-stopped", f"heartbeat stopped: no check for {int(age)}s"))
        for item in list(self.inflight.values()):
            if item.get("stage") == "Waiting for your approval":
                continue
            age = now - datetime.fromisoformat(item["since"]).timestamp()
            if age > self.WORK_STUCK_S:
                issues.append((f"work-stuck:{item['key']}", f"{item['who'] or item['key']} stuck at '{item['stage']}' for {int(age // 60)} min"))
        return issues

    def _watchdog(self) -> None:
        while True:
            time.sleep(10)
            self.sweep()

    def sweep(self) -> None:
        """One watchdog pass: alert on what is stuck, and release work nobody is doing any more."""
        try:
            issues = self.health()
            for key, text in issues:
                self.alert(key, text)
            # Claims left in_progress with no worker behind them (a crashed thread, a failed database
            # write on release) would block that comment or reply forever. Release them as failed so
            # the next heartbeat retries - the ledger still stops a repeat send.
            for row in self.s.select("claims", {"status": "in_progress"}):
                if row["key"] in self.inflight:
                    continue
                age = time.time() - datetime.fromisoformat(row["updated"]).timestamp()
                if age > self.ORPHAN_CLAIM_S:
                    self.release(row["key"], "failed")
                    self.alert(f"orphan:{row['key']}", f"released abandoned work '{row['key'][:60]}' after "
                                                       f"{int(age // 60)} min - it will be retried")
            self.status["health"] = "ok" if not issues else "degraded"
            self.status["issues"] = [text for _, text in issues]
            live = {key for key, _ in issues}
            # Once a problem clears, it may alert again if it comes back.
            self.alerted = {k for k in self.alerted if k in live or k.startswith(("orphan:", "audit:"))}
        except Exception as exc:  # noqa: BLE001 - the watchdog must outlive whatever broke
            self.status["health"] = "degraded"
            self.status["issues"] = [f"watchdog could not check: {type(exc).__name__}: {str(exc)[:120]}"]

    def audit_now(self, run) -> None:
        """Lemma's loop, online: every run is audited the moment it ends, and a broken rule is an alert,
        a stored issue and a regression case - not something found later on a dashboard."""
        from . import audit
        try:
            report = audit.audit_all([run])
        except Exception as exc:  # noqa: BLE001
            self.alert(f"audit-failed:{run.run_id}", f"could not audit run {run.run_id}: {str(exc)[:120]}")
            return
        if report["violations"]:
            broken = "; ".join(f"{i['agent']} broke {k}" for k, i in report["issues"].items())
            self.alert(f"audit:{run.run_id}", f"run {run.run_id} broke a rule - {broken}. Saved as a regression case.")

    def _loop(self) -> None:
        while True:
            self.beat()
            self.status["next_tick"] = datetime.fromtimestamp(time.time() + self.interval_s, timezone.utc).isoformat()
            self.wake.wait(self.interval_s)
            self.wake.clear()

    def beat(self) -> None:
        """One heartbeat. Whatever fails inside is recorded; the heartbeat itself never dies."""
        try:
            if self.enabled:
                self.tick()
        except Exception as exc:  # noqa: BLE001
            self.status["last_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            traceback.print_exc()

    def tick(self) -> None:
        self.tick_started = time.time()
        try:
            self._tick()
        finally:
            self.tick_started = 0.0

    def _tick(self) -> None:
        with self.lock:
            self.status.update(last_tick=_now(), ticks=self.status["ticks"] + 1, last_error="")
            demo_post, contacts_file = _source_files()
            from . import introductions
            from . import recovery
            recovery.retry_notifications(Ledger(), pipeline.slack_notifier)
            introductions.check_replies(Leads(), self.chat)
            contacts = json.loads(Path(contacts_file).read_text(encoding="utf-8"))
            for w in self.watches():
                path = demo_post if w["source"] == "demo" else w["source"]
                try:
                    post, comments = quinn.read_post(path)
                except Exception as exc:  # noqa: BLE001
                    self.status["last_error"] = f"Quinn couldn't read {w['label']}: {str(exc)[:120]}"
                    continue
                for c in comments:
                    key = f"comment:{c['comment_id']}"
                    if self.claim(key, "comment"):
                        self.status["last_found"] = f"comment from {c.get('author_name')}"
                        self._spawn(key, c.get("author_name", ""), "Reading the comment",
                                    self._comment, post, c, contacts, w["label"])
            for lead in Leads().all():
                if not lead.get("email"):
                    continue
                if lead.get("pending_booking"):
                    # Only a booking that FAILED is resumed. One that is still being written by
                    # another worker is pending in the ledger, and resuming it would book twice.
                    if Ledger().status(action_id("meeting", lead["email"])) != "failed":
                        continue
                    if not recovery.due(self.s, action_id('meeting', lead['email'])):
                        continue
                    key = f"resume:{lead['email']}:{lead['pending_booking']}:{time.strftime('%H%M')}"
                    if self.claim(key, "resume"):
                        self._spawn(key, lead.get("name", ""), "Retrying the calendar booking",
                                    self._reply, lead, {"message_id": key, "body": ""})
                    continue
                if lead.get("status") not in ("awaiting_reply", None) or not lead.get("sent_at"):
                    continue
                try:
                    replies = google.replies_from(lead["email"], lead["sent_at"])
                except Exception as exc:  # noqa: BLE001
                    self.status["last_error"] = f"Morgan couldn't read the inbox: {str(exc)[:120]}"
                    continue
                for reply in replies:
                    key = f"reply:{reply['message_id']}"
                    if self.claim(key, "reply"):
                        self.status["last_found"] = f"reply from {lead.get('name') or lead['email']}"
                        self._spawn(key, lead.get("name", ""), "Reading their reply", self._reply, lead, reply)

    def _spawn(self, key: str, who: str, stage: str, fn, *args) -> None:
        self.inflight[key] = {"key": key, "who": who, "stage": stage, "since": _now()}

        def work():
            try:
                fn(key, *args)
                self.release(key, "done")
            except Exception as exc:  # noqa: BLE001 - released as failed, so the next tick retries it
                self.release(key, "failed")
                self.chat.post("Alex", "text", f"I couldn't finish {who or 'an item'} ({type(exc).__name__}: "
                                               f"{str(exc)[:100]}). Check the trace and Recovery for confirmed and uncertain actions.")
                traceback.print_exc()
            finally:
                self.inflight.pop(key, None)
        threading.Thread(target=work, daemon=True, name=f"autopilot:{key}").start()

    # ── the work ───────────────────────────────────────────────────────────────
    def _comment(self, key: str, post: dict, c: dict, contacts: list[dict], label: str) -> None:
        chat, sent = self.chat, set()
        chat.post("Alex", "delegation", f"New comment from {c.get('author_name')} on {label}. Quinn, take it.", to="Quinn",
                  heartbeat=True)

        class Tracking(ChatApprover):
            def __call__(inner, run, payload):  # noqa: N805
                self.inflight.get(key, {}).update(stage="Waiting for your approval", run_id=run.run_id)
                return super().__call__(run, payload)

        run = pipeline.process_comment(c, post_text=post.get("text", ""), approver=Tracking(chat, c, sent, timeout_s=self.approval_timeout_s),
                                       contacts=contacts, ledger=Ledger(), leads=Leads())
        run.save()
        self.audit_now(run)
        cards_so_far(chat, run, c, sent)
        result_card(chat, run)

    def _reply(self, key: str, lead: dict, reply: dict) -> None:
        chat = self.chat
        if reply.get("body"):
            chat.post("Morgan", "reply", f"{lead.get('name') or lead['email']} replied.", body=reply["body"][:600], heartbeat=True)

        def notify(msg: str) -> None:
            chat.post("Morgan", "meeting", msg)
            pipeline.slack_notifier(msg)

        class Tracking(ChatApprover):
            def __call__(inner, run, payload):  # noqa: N805
                self.inflight.get(key, {}).update(stage="Waiting for your approval", run_id=run.run_id)
                return super().__call__(run, payload)

        run = pipeline.process_reply(lead, reply, approver=Tracking(chat, {"text": reply.get("body", "")}, {"intent", "jordan"}, timeout_s=self.approval_timeout_s),
                                     notify=notify, ledger=Ledger(), leads=Leads())
        run.save()
        self.audit_now(run)
        m = _step(run, "meeting.read")
        if m:
            chat.post("Morgan", "meeting_read", run_id=run.run_id, wants=m.decision,
                      **{k: (m.data or {}).get(k) for k in ("offered_slot_id", "day_key", "time_24h", "timezone_stated",
                                                            "earliest_day_key", "latest_day_key", "signals")})
        offer = _step(run, "calendar.offer")
        if offer:
            chat.post("Morgan", "offer", run_id=run.run_id, slots=(offer.data or {}).get("slots", {}),
                      window=(offer.data or {}).get("window", {}), checked=(offer.data or {}).get("calendar_checked"))
        result_card(chat, run)

    def snapshot(self) -> dict:
        return {"enabled": self.enabled, "interval_s": self.interval_s, "mode": mode(), **self.status,
                "tick_running_s": int(time.time() - self.tick_started) if self.tick_started else 0,
                "watches": self.watches(), "inflight": list(self.inflight.values())}


AUTOPILOT: Autopilot | None = None
