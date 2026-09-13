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
        return self

    def _loop(self) -> None:
        while True:
            if self.enabled:
                try:
                    self.tick()
                except Exception as exc:  # noqa: BLE001 - a bad tick must never kill the heartbeat
                    self.status["last_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
                    traceback.print_exc()
            self.status["next_tick"] = datetime.fromtimestamp(time.time() + self.interval_s, timezone.utc).isoformat()
            self.wake.wait(self.interval_s)
            self.wake.clear()

    def tick(self) -> None:
        with self.lock:
            self.status.update(last_tick=_now(), ticks=self.status["ticks"] + 1, last_error="")
            demo_post, contacts_file = _source_files()
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
                                               f"{str(exc)[:100]}). Nothing was sent - I'll retry on the next heartbeat.")
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
                "watches": self.watches(), "inflight": list(self.inflight.values())}


AUTOPILOT: Autopilot | None = None
