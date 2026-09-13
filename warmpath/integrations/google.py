"""Gmail and Google Calendar.

Through Composio when COMPOSIO_USER_ID names a connected account; Gmail falls back to IMAP
with an app password so the pipeline runs before that connection exists.

Two hard rules live here rather than in the caller, because a caller can forget:
- A free-mail domain is never searched as a company. "from:gmail.com" is everyone.
- A live send or invite goes only to an address on WARMPATH_LIVE_ALLOWLIST. The demo mailbox
  and calendar are real; nothing here may reach a person who did not agree to be a test.
"""

from __future__ import annotations

import email
import imaplib
import json
import os
import smtplib
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from ..relationships import FREEMAIL, domain_of

UA = {"User-Agent": "warmpath/0.1"}


class NotAllowed(RuntimeError):
    pass


class NotConnected(RuntimeError):
    pass


def _allowlist() -> set[str]:
    return {a.strip().lower() for a in os.environ.get("WARMPATH_LIVE_ALLOWLIST", "").split(",")
            if a.strip()}


def guard_live_recipients(addresses: list[str]) -> None:
    allowed = _allowlist()
    outside = [a for a in addresses if a.lower() not in allowed]
    if outside:
        raise NotAllowed(f"live write refused - not on WARMPATH_LIVE_ALLOWLIST: {', '.join(outside)}")


def composio_ready() -> bool:
    """Calendar goes through Composio whenever a connected account is configured."""
    return bool(os.environ.get("COMPOSIO_USER_ID", "").strip())


def gmail_via_composio() -> bool:
    """Gmail has its own switch: the Composio account may hold only Calendar, while the IMAP app
    password already reaches the same mailbox."""
    return composio_ready() and os.environ.get("GMAIL_VIA_COMPOSIO", "0") == "1"


def _composio(tool: str, arguments: dict, *, user_id: str = "", account_id: str = "") -> dict:
    user = user_id or os.environ.get("COMPOSIO_USER_ID", "").strip()
    if not user:
        raise NotConnected("COMPOSIO_USER_ID is not set - connect Gmail/Calendar in Composio")
    body = {"arguments": arguments, "user_id": user}
    if account_id:
        body["connected_account_id"] = account_id
    if os.environ.get("COMPOSIO_TOOL_VERSION"):
        body["version"] = os.environ["COMPOSIO_TOOL_VERSION"]
    req = urllib.request.Request(
        f"https://backend.composio.dev/api/v3/tools/execute/{tool}",
        data=json.dumps(body).encode(), method="POST",
        headers={**UA, "Content-Type": "application/json",
                 "x-api-key": os.environ["COMPOSIO_API_KEY"].strip()})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"composio {tool}: HTTP {exc.code} "
                           f"{exc.read().decode(errors='replace')[:300]}") from exc
    if not d.get("successful", True):
        raise RuntimeError(f"composio {tool}: {str(d.get('error'))[:300]}")
    data = d.get("data") or {}
    # Some tools answer in data, some in data.response_data. Callers read one shape.
    if isinstance(data, dict) and isinstance(data.get("response_data"), dict):
        data = {**data, **data["response_data"]}
    return data


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


# ── Gmail: relationship evidence ───────────────────────────────────────────────

def _search_terms(domain: str, addresses: list[str]) -> list[str]:
    terms = [a.lower() for a in addresses if a]
    d = domain_of(domain)
    if d and d not in FREEMAIL:
        terms.append(d)
    return sorted(set(terms))


def mail_evidence(domain: str, addresses: list[str] = (), years: int = 3) -> list[dict]:
    """Mail received from and sent to a company domain (or specific known addresses)."""
    terms = _search_terms(domain, list(addresses))
    if not terms:
        return []
    return _mail_composio(terms, years) if gmail_via_composio() else _mail_imap(terms, years)


def _mail_composio(terms: list[str], years: int) -> list[dict]:
    since = (datetime.now() - timedelta(days=365 * years)).strftime("%Y/%m/%d")
    out: list[dict] = []
    for direction, op in (("inbound", "from"), ("outbound", "to")):
        q = " OR ".join(f"{op}:{t}" for t in terms)
        data = _composio("GMAIL_FETCH_EMAILS", {"query": f"({q}) after:{since}",
                                                "max_results": 25, "include_payload": False})
        for m in data.get("messages") or []:
            header = m.get("sender") if direction == "inbound" else m.get("to")
            for name, addr in getaddresses([header or ""]):
                addr = addr.lower()
                if not any(addr == t or domain_of(addr) == t for t in terms):
                    continue
                ts = m.get("messageTimestamp") or ""
                out.append({"id": f"gmail:{m.get('messageId')}:{direction}", "channel": direction,
                            "address": addr, "name": name, "at": ts,
                            "subject": m.get("subject") or "", "thread_id": m.get("threadId")})
    return out


def _imap() -> imaplib.IMAP4_SSL:
    # A mailbox that stops answering must fail this call, not hang it: the heartbeat waits on it.
    box = imaplib.IMAP4_SSL("imap.gmail.com", timeout=int(os.environ.get("WARMPATH_IMAP_TIMEOUT_S", "25")))
    box.login(os.environ["GMAIL_USER"], os.environ["GMAIL_APP_PASSWORD"])
    return box


def _dec(v: str | None) -> str:
    return str(make_header(decode_header(v or "")))


def _mail_imap(terms: list[str], years: int) -> list[dict]:
    since = (datetime.now() - timedelta(days=365 * years)).strftime("%d-%b-%Y")
    out: list[dict] = []
    box = _imap()
    try:
        for direction, folder, field in (("inbound", "INBOX", "FROM"),
                                         ("outbound", '"[Gmail]/Sent Mail"', "TO")):
            box.select(folder, readonly=True)
            for t in terms:
                _, data = box.search(None, "SINCE", since, field, f'"{t}"')
                for i in data[0].split()[-25:]:
                    _, raw = box.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)])")
                    h = email.message_from_bytes(raw[0][1])
                    for name, addr in getaddresses([_dec(h.get(field.title()))]):
                        addr = addr.lower()
                        if addr != t and domain_of(addr) != t:
                            continue
                        try:
                            at = _iso(parsedate_to_datetime(h.get("Date")))
                        except (TypeError, ValueError):
                            at = ""
                        out.append({"id": f"gmail:{(h.get('Message-ID') or i.decode()).strip('<> ')}:{direction}",
                                    "channel": direction, "address": addr, "name": name, "at": at,
                                    "subject": _dec(h.get("Subject"))})
    finally:
        box.logout()
    seen, unique = set(), []
    for r in out:
        if r["id"] not in seen:
            seen.add(r["id"])
            unique.append(r)
    return unique


# ── Gmail: send and replies ────────────────────────────────────────────────────

def send(*, to: str, subject: str, body: str, live: bool) -> dict:
    if not live:
        return {"simulated": True, "to": to, "subject": subject}
    guard_live_recipients([to])
    # Sending over HTTPS through a Composio Gmail connection. Some hosts block outbound SMTP entirely
    # (Render's free tier answers "Network is unreachable" on port 465), and reading over IMAP still works.
    send_user = os.environ.get("COMPOSIO_GMAIL_USER_ID", "").strip()
    if os.environ.get("GMAIL_SEND_VIA", "").strip() == "composio" and send_user:
        args = {"recipient_email": to, "subject": subject, "body": body}
        name = os.environ.get("WARMPATH_SENDER_NAME", "").strip()
        sender = os.environ.get("GMAIL_USER", "").strip()
        if name and sender:
            args["from_email"] = f"{name} <{sender}>"
        try:
            d = _composio("GMAIL_SEND_EMAIL", args, user_id=send_user,
                          account_id=os.environ.get("COMPOSIO_GMAIL_ACCOUNT_ID", "").strip())
        except RuntimeError as exc:
            if "from_email" not in args or "from" not in str(exc).lower():
                raise
            args.pop("from_email")               # the provider refused the display name: send without it
            d = _composio("GMAIL_SEND_EMAIL", args, user_id=send_user,
                          account_id=os.environ.get("COMPOSIO_GMAIL_ACCOUNT_ID", "").strip())
        rd = d.get("response_data") if isinstance(d.get("response_data"), dict) else d
        return {"simulated": False, "to": to, "subject": subject, "via": "composio",
                "message_id": rd.get("id", ""), "thread_id": rd.get("threadId", "")}
    if gmail_via_composio():
        d = _composio("GMAIL_SEND_EMAIL", {"recipient_email": to, "subject": subject, "body": body})
        return {"simulated": False, "to": to, "subject": subject,
                "message_id": d.get("id") or (d.get("response_data") or {}).get("id", ""),
                "thread_id": d.get("threadId") or (d.get("response_data") or {}).get("threadId", "")}
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = os.environ["GMAIL_USER"], to, subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(os.environ["GMAIL_USER"], os.environ["GMAIL_APP_PASSWORD"])
        s.send_message(msg)
    return {"simulated": False, "to": to, "subject": subject, "message_id": msg.get("Message-ID", "")}


def replies_from(address: str, since_iso: str) -> list[dict]:
    """Messages this person sent us after our email - the input to meeting intent.

    Mail search is by day, so a message they sent earlier the same day would come back too and be
    read as a reply to an email they had not yet received. Filtered by the actual timestamp.
    """
    since = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
    return [m for m in _replies_from(address, since) if _after(m.get("at"), since)]


def _after(stamp, since: datetime) -> bool:
    try:
        if isinstance(stamp, (int, float)) or str(stamp).isdigit():
            when = datetime.fromtimestamp(int(stamp) / (1000 if int(stamp) > 10**11 else 1), timezone.utc)
        else:
            try:
                when = parsedate_to_datetime(str(stamp))
            except (TypeError, ValueError):
                when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when > since
    except (TypeError, ValueError):
        return False                      # an undatable message is not treated as a reply


def _replies_from(address: str, since: datetime) -> list[dict]:
    if gmail_via_composio():
        data = _composio("GMAIL_FETCH_EMAILS", {
            "query": f"from:{address} after:{since.strftime('%Y/%m/%d')}",
            "max_results": 10, "include_payload": True})
        return [{"message_id": m.get("messageId"), "from": address,
                 "subject": m.get("subject") or "", "at": m.get("messageTimestamp") or "",
                 "body": (m.get("messageText") or (m.get("preview") or {}).get("body") or "")[:4000]}
                for m in data.get("messages") or []]
    box = _imap()
    out = []
    try:
        box.select("INBOX", readonly=True)
        _, data = box.search(None, "SINCE", since.strftime("%d-%b-%Y"), "FROM", f'"{address}"')
        for i in data[0].split()[-10:]:
            _, raw = box.fetch(i, "(BODY.PEEK[])")
            m = email.message_from_bytes(raw[0][1])
            body = ""
            for part in (m.walk() if m.is_multipart() else [m]):
                if part.get_content_type() == "text/plain" and not part.get_filename():
                    body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8",
                                                                "replace")
                    break
            out.append({"message_id": (m.get("Message-ID") or f"imap-{i.decode()}").strip(),
                        "from": parseaddr(_dec(m.get("From")))[1].lower(),
                        "subject": _dec(m.get("Subject")), "at": m.get("Date", ""),
                        "body": _strip_quoted(body)[:4000]})
    finally:
        box.logout()
    return out


def _strip_quoted(body: str) -> str:
    """Their words only - the quoted copy of our own email is not something they said.

    Gmail wraps "On Sun, 14 Sep 2026 at 00:10, Rahul <...> wrote:" across two lines, so the
    marker is matched across line breaks, not per line.
    """
    import re
    cut = re.search(r"(^|\n)(On [^\n]{0,200}(\n[^\n]{0,200})?wrote:|>)", body)
    return (body[:cut.start()] if cut else body).strip()


# ── Calendar ───────────────────────────────────────────────────────────────────

def meeting_evidence(domain: str, addresses: list[str] = (), years: int = 3) -> list[dict]:
    terms = _search_terms(domain, list(addresses))
    if not terms or not composio_ready():
        return []
    now = datetime.now(timezone.utc)
    out = []
    for t in terms:
        data = _composio("GOOGLECALENDAR_EVENTS_LIST", {
            "calendarId": "primary", "q": t, "singleEvents": True, "maxResults": 25,
            "timeMin": _iso(now - timedelta(days=365 * years)), "timeMax": _iso(now)})
        for e in data.get("items") or data.get("events") or []:
            for a in e.get("attendees") or []:
                addr = (a.get("email") or "").lower()
                if addr == t or domain_of(addr) == t:
                    start = (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date", "")
                    out.append({"id": f"calendar:{e.get('id')}:{addr}", "channel": "meeting",
                                "address": addr, "name": a.get("displayName") or "",
                                "at": start, "subject": e.get("summary") or ""})
    return out


def free_slots(*, start: datetime, end: datetime, tz: str) -> list[dict]:
    data = _composio("GOOGLECALENDAR_FIND_FREE_SLOTS", {
        "items": ["primary"], "time_min": start.isoformat(), "time_max": end.isoformat(),
        "timezone": tz})
    cal = ((data.get("calendars") or {}).get("primary") or {})
    return [{"start": s.get("start"), "end": s.get("end")} for s in cal.get("free") or []]


#: Set to N to make the next N calendar writes fail with a 503. Used by the partial-failure
#: test and the demo - the failure is injected, the recovery is real.
INJECT_CALENDAR_FAILURES = {"remaining": 0}


def create_event(*, title: str, start_local: str, minutes: int, tz: str, attendees: list[str],
                 description: str, live: bool) -> dict:
    if INJECT_CALENDAR_FAILURES["remaining"] > 0:
        INJECT_CALENDAR_FAILURES["remaining"] -= 1
        raise RuntimeError("calendar: HTTP 503 Service Unavailable (injected)")
    if not live:
        return {"simulated": True, "title": title, "start": start_local, "tz": tz,
                "attendees": attendees}
    guard_live_recipients(attendees)
    args = {"calendar_id": "primary", "summary": title, "description": description,
            "start_datetime": start_local, "timezone": tz, "attendees": attendees,
            "create_meeting_room": True}
    if minutes >= 60:
        args["event_duration_hour"], args["event_duration_minutes"] = minutes // 60, minutes % 60
    else:
        args["event_duration_minutes"] = minutes
    d = _composio("GOOGLECALENDAR_CREATE_EVENT", args)
    ev = d.get("response_data") or d
    return {"simulated": False, "title": title, "start": start_local, "tz": tz,
            "attendees": attendees, "event_id": ev.get("id", ""), "link": ev.get("htmlLink", "")}
