"""Morgan - Account Executive. Turns "yes, let's talk" into a meeting that actually exists.

Same role and face as Morgan in Zavorik, and the rule ported from there: every reason an
invite did not happen is returned, never swallowed - a pipeline that says "booked" while
nobody's calendar has the event is the silent failure of scheduling.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .. import llm
from ..core import Run
from ..integrations import google

NAME = "Morgan"
ROLE = "Account Executive"

INSTRUCTIONS = {
    "M1_BOOK_ONLY_AN_ACCEPTED_TIME":
        "A meeting is created only for a specific time the prospect accepted in writing, with "
        "the day looked up from a supplied calendar and a timezone that is known.",
    "M2_ONE_MEETING_PER_PERSON": "A person gets at most one meeting from one thread, however many replies arrive.",
    "M3_RESUME_NOT_RESTART":
        "If the calendar write fails after the time was agreed, the retry books the same time and "
        "does not re-read, re-ask or re-send anything.",
    "M4_OFFER_ONLY_TIMES_THEY_CAN_DO":
        "Times offered to a prospect fall inside the window they gave and were free on the founder's calendar.",
}


def tz() -> str:
    return os.environ.get("WARMPATH_TIMEZONE", "Asia/Kolkata")


#: Preferred start hours, in order - late morning first, then afternoon.
_HOURS = (11, 15, 10, 14, 16)


def offer_slots(run: Run, n: int = 3, *, earliest: str | None = None, latest: str | None = None) -> dict[str, dict]:
    """Up to n free half-hours on different business days, inside the window they gave.

    "Not this week, next week" is a constraint, and ignoring it is silent: an email offering this
    week's times sends fine and is useless. The window comes from their reply (day keys looked up
    in a supplied calendar); the times come from the founder's real free/busy.
    """
    zone = ZoneInfo(tz())
    now = datetime.now(zone)
    first = now.date() + timedelta(days=1)
    if earliest:
        first = max(first, date.fromisoformat(earliest))
    last = date.fromisoformat(latest) if latest else first + timedelta(days=14)
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)
            if (first + timedelta(days=i)).weekday() < 5]
    checked = google.composio_ready()
    spans: list[tuple[datetime, datetime]] = []
    if checked and days:
        free = run.step("calendar.free_slots", lambda: google.free_slots(
            start=datetime(first.year, first.month, first.day, 9, 0, tzinfo=zone),
            end=datetime(last.year, last.month, last.day, 18, 0, tzinfo=zone), tz=tz()), agent=NAME)
        spans = [(datetime.fromisoformat(f["start"].replace("Z", "+00:00")),
                  datetime.fromisoformat(f["end"].replace("Z", "+00:00"))) for f in free]
    picked: list[datetime] = []
    for d in days:
        for hour in _HOURS:
            s = datetime(d.year, d.month, d.day, hour, 0, tzinfo=zone)
            if s > now and (not checked or any(a <= s and s + timedelta(minutes=30) <= b for a, b in spans)):
                picked.append(s)
                break
        if len(picked) == n:
            break
    slots = {f"slot{i}": {"start_local": w.strftime("%Y-%m-%dT%H:%M:%S"),
                          "label": f"{w.strftime('%A %d %B')} at {w.strftime('%H:%M')} {tz()}",
                          "checked": checked}
             for i, w in enumerate(picked, 1)}
    run.record("calendar.offer", agent=NAME, decision="checked" if checked else "unchecked",
               data={"slots": slots, "calendar_checked": checked,
                     "window": {"earliest": first.isoformat(), "latest": last.isoformat(),
                                "from_their_reply": bool(earliest or latest)}})
    return slots


def read_reply(run: Run, reply: dict, offered: dict[str, dict]) -> dict:
    today = datetime.now(ZoneInfo(tz()))
    m = run.step("meeting.read", lambda: llm.read_meeting(
        reply["body"], today=today, offered={k: v["label"] for k, v in offered.items()}),
        agent=NAME, input=reply["body"][:300])
    run.steps[-1].decision = m["wants_meeting"]
    run.steps[-1].data = m
    return m


def agreed_time(m: dict, offered: dict[str, dict]) -> tuple[str, str]:
    """(start_local, "") when the time is settled, else ("", reason for a human)."""
    if m.get("ambiguous", True):
        return "", "their acceptance is ambiguous; ask for clarification"
    if m["offered_slot_id"]:
        slot = offered.get(m["offered_slot_id"])
        if not slot:
            return "", "they selected an unknown slot"
        return slot["start_local"], ""
    if not (m["day_key"] and m["time_24h"]):
        return "", "they want to meet but did not accept a specific day and time"
    if not m["timezone_stated"]:
        return "", "they named a time that is not one we offered, with no timezone"
    return "", f"they proposed {m['day_key']} {m['time_24h']} {m['timezone_stated']} - confirm the timezone before booking"


def slot_is_free(run: Run, start_local: str) -> tuple[bool, bool]:
    """(free, checked). An unconnected calendar can't say a slot is free - it says so."""
    if not google.composio_ready():
        run.record("calendar.check", agent=NAME, decision="unchecked",
                   data={"start_local": start_local, "reason": "calendar not connected"})
        return True, False
    zone = ZoneInfo(tz())
    start = datetime.fromisoformat(start_local).replace(tzinfo=zone)
    free = run.step("calendar.check", lambda: google.free_slots(
        start=start, end=start + timedelta(minutes=30), tz=tz()), agent=NAME)
    ok = any(datetime.fromisoformat(f["start"].replace("Z", "+00:00")) <= start
             and start + timedelta(minutes=30) <= datetime.fromisoformat(f["end"].replace("Z", "+00:00"))
             for f in free)
    run.steps[-1].decision = "free" if ok else "busy"
    return ok, True


def book(run: Run, *, title: str, start_local: str, attendee: str, description: str, live: bool) -> dict:
    return run.step("calendar.create", lambda: google.create_event(
        title=title, start_local=start_local, minutes=30, tz=tz(), attendees=[attendee],
        description=description, live=live), agent=NAME, input=f"{start_local} {tz()}")
