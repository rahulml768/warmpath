"""Who do we already know at this company, and how well - Jordan's engine.

Ported from the relationship service in Zavorik (the founder's product), minus its database:
here the evidence is read live from Gmail and Calendar and handed in as plain records.

WHAT COUNTS AS KNOWING SOMEBODY. Not "an address exists" - every newsletter that ever reached
the inbox satisfies that. There has to be evidence of contact, and the channels are not the
same evidence:

    meeting   somebody accepted an invitation and we were in a room together
    inbound   they wrote to us - the first sign the relationship is not one-sided
    outbound  we wrote to them, which a cold email also satisfies

So they are weighted in that order, and the newest contact decides how much any of it still
counts: a warm path that was warm three years ago is a cold email with a name attached.

WHAT IS NEVER A COMPANY. Free mail providers (gmail.com employs nobody), shared desks
(sales@ reaches whoever is on the rota), relays (one person holding several addresses on a
domain means the domain mints addresses), and ourselves (an introduction to yourself proves
nothing behind it knows who you are).

MATCHING IS BY DOMAIN, NEVER BY NAME. "Acme" in a signature and "Acme" in a LinkedIn headline
can be two different businesses. Opening an email with "I spoke with someone at your company"
to the wrong Acme sends fine, every step reports success, and the sentence is false.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

FREEMAIL = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "outlook.com",
    "hotmail.com", "live.com", "msn.com", "icloud.com", "me.com", "aol.com",
    "proton.me", "protonmail.com", "gmx.com", "mail.com", "rediffmail.com",
    "yandex.com", "qq.com", "163.com",
})

#: Shared mailboxes, not people. RFC 2142 plus the commercial ones every company runs.
#: A warm path is somebody you can address by name: a real person answering from info@ one
#: day is still a desk, and "as we discussed" to a desk claims a relationship in front of a
#: stranger.
ROLE_MAILBOXES = frozenset({
    "postmaster", "hostmaster", "webmaster", "abuse", "noc", "security", "usenet",
    "news", "uucp", "ftp", "www", "marketing", "sales", "support", "info",
    "hello", "hi", "contact", "team", "admin", "office", "help", "helpdesk",
    "service", "enquiries", "enquiry", "inquiries", "careers", "jobs", "hr",
    "billing", "accounts", "accounting", "finance", "invoice", "invoices",
    "legal", "privacy", "press", "media", "partners", "orders", "bookings",
    "noreply", "no-reply", "donotreply", "do-not-reply", "notifications",
    "notification", "alerts", "mailer", "mail", "bounce", "bounces",
})

_PER_CHANNEL_CAP = 8                       # a mailing list must not outweigh a real client
_WEIGHTS = {"meeting": 3, "inbound": 2, "outbound": 1}
_RECENCY = ((90, 1.0), (365, 0.6), (1095, 0.3))
_STALE = 0.1
_STRONG, _WARM = 6.0, 2.0
_PHRASING = {
    "meeting": ("met once", "met {n}x"),
    "inbound": ("wrote to us once", "replied {n}x"),
    "outbound": ("we emailed once", "we emailed {n}x"),
}


def domain_of(value: str | None) -> str:
    """"https://www.Acme.com/about", "acme.com" and "jo@acme.com" are one company."""
    v = (value or "").strip().lower()
    if not v:
        return ""
    if "@" in v:
        v = v.rsplit("@", 1)[-1]
    v = re.sub(r"^[a-z]+://", "", v).split("/")[0].split("?")[0]
    return v.removeprefix("www.").strip(". >")


def is_role_mailbox(address: str) -> bool:
    """`sales2@`, `support-1@` and `info+web@` are still the same desk."""
    local = (address or "").strip().lower().rsplit("@", 1)[0].split("+", 1)[0]
    return local.rstrip("0123456789-_.") in ROLE_MAILBOXES


def is_company_domain(domain: str, ours: set[str] = frozenset()) -> bool:
    d = domain_of(domain)
    return bool(d) and d not in FREEMAIL and d not in ours


def _days_since(stamp: str | None) -> int | None:
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - when).days)


def strength(counts: dict[str, dict]) -> dict:
    """How well we know this person, with the evidence that says so.

    `counts` is {"meeting"|"inbound"|"outbound": {"count": int, "last_at": iso}}. Returns
    score, label and a line a founder can check - a number nobody can audit is exactly how a
    warm path becomes a confident guess.
    """
    raw, parts, newest = 0.0, [], None
    for channel, weight in _WEIGHTS.items():
        rec = counts.get(channel) or {}
        n = int(rec.get("count") or 0)
        if n <= 0:
            continue
        raw += weight * min(n, _PER_CHANNEL_CAP)
        days = _days_since(rec.get("last_at"))
        if days is not None and (newest is None or days < newest):
            newest = days
        one, many = _PHRASING[channel]
        parts.append(one if n == 1 else many.format(n=n))

    factor = _STALE
    if newest is not None:
        factor = next((m for limit, m in _RECENCY if newest <= limit), _STALE)
    score = round(raw * factor, 1)

    # A one-way exchange is not a relationship however much of it there is: eight emails out
    # and no reply is somebody ignoring us. Reciprocity - either side answering, or a meeting
    # somebody had to accept - is the signal, not volume.
    n = {c: int((counts.get(c) or {}).get("count") or 0) for c in _WEIGHTS}
    mutual = n["meeting"] > 0 or (n["inbound"] > 0 and n["outbound"] > 0)
    if not mutual and score >= _STRONG:
        score = _STRONG - 0.1
        parts.append("one-way so far")

    label = "strong" if score >= _STRONG else "warm" if score >= _WARM else "weak"
    if newest is not None:
        parts.append(f"last contact {newest}d ago")
    return {"score": score, "label": label, "mutual": mutual,
            "evidence": ", ".join(parts) or "no contact on file"}


def people_at(domain: str, records: list[dict], *, ours: set[str] = frozenset(),
              address_company: dict[str, str] | None = None) -> dict:
    """Everyone we have real contact with at this company, strongest first.

    `records` are evidence items: {"id", "channel", "address", "name", "at", "subject"}.
    `address_company` maps an address to a company domain from an explicit source (the
    founder's contacts file) - the only way a person on a free-mail address can belong to a
    company, and it is recorded as such.

    Returns {"people": [...], "refused": reason or ""}.
    """
    want = domain_of(domain)
    if not want:
        return {"people": [], "refused": "no company domain"}
    if want in FREEMAIL:
        return {"people": [], "refused": f"{want} is a free mail provider, not a company"}
    if want in ours:
        return {"people": [], "refused": f"{want} is our own domain"}
    address_company = {k.lower(): domain_of(v) for k, v in (address_company or {}).items()}

    by_person: dict[str, dict] = {}
    for r in records:
        addr = (r.get("address") or "").strip().lower()
        if not addr or addr in ours:
            continue
        via = "email domain" if domain_of(addr) == want else (
            "contacts file" if address_company.get(addr) == want else "")
        if not via:
            continue
        p = by_person.setdefault(addr, {"email": addr, "name": r.get("name") or "",
                                        "via": via, "counts": {}, "evidence_ids": []})
        c = p["counts"].setdefault(r["channel"], {"count": 0, "last_at": ""})
        c["count"] += 1
        c["last_at"] = max(c["last_at"], r.get("at") or "")
        p["evidence_ids"].append(r["id"])
        p["name"] = p["name"] or r.get("name") or ""

    names: dict[str, set[str]] = {}
    people = []
    for addr, p in by_person.items():
        if is_role_mailbox(addr):
            continue                          # a desk is never an introduction
        key = (p["name"] or addr).strip().lower()
        names.setdefault(key, set()).add(addr)
        people.append({**p, **strength(p["counts"])})

    # A relay, not an employer: one person holding several addresses on this domain means
    # the domain mints them, and every card on it is equally untrustworthy.
    if any(len(v) > 1 and all(domain_of(a) == want for a in v) for v in names.values()):
        return {"people": [], "refused": f"{want} mints several addresses per person - a relay"}

    people.sort(key=lambda h: -h["score"])
    return {"people": people, "refused": ""}
