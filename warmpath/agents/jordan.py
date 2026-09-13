"""Jordan - Sales Development Rep. Who is this person, and do we already know their company?

Same role and face as Jordan in Zavorik, and the relationship engine is ported from there.
Jordan's whole job is to be right about identity, because every mistake here is silent:
a guessed address sends fine, and a warm opener to the wrong "Acme" reads fine.
"""

from __future__ import annotations

import json
import re

from ..core import Run, verify_contact
from ..integrations import google, unipile
from ..relationships import domain_of, is_company_domain, is_role_mailbox, people_at

NAME = "Jordan"
ROLE = "Sales Development Rep"

INSTRUCTIONS = {
    "J1_NEVER_GUESS_AN_ADDRESS":
        "An email address is used only if it literally appears in a named source.",
    "J2_COMPANY_BY_DOMAIN_NOT_NAME":
        "A relationship counts only when it is tied to the lead's company by domain - a matching "
        "company name is never enough.",
    "J3_NO_DESKS_NO_FREEMAIL_NO_SELF":
        "Shared mailboxes, free-mail domains and our own domain are never a company or a warm path.",
}


def _contact_for(comment: dict, contacts: list[dict]) -> dict | None:
    url = (comment.get("profile_url") or "").rstrip("/").lower()
    return next((c for c in contacts if url and (c.get("linkedin_url") or "").rstrip("/").lower() == url),
                None)


def resolve_identity(run: Run, comment: dict, contacts: list[dict], ours: set[str]) -> dict:
    """Name, company domain and a usable address - each with the source it came from."""
    profile = comment.get("profile")
    if profile is None and comment.get("author_id"):
        profile = run.step("linkedin.profile", lambda: unipile.get_profile(comment["author_id"]),
                           agent=NAME)
    profile = profile or {}
    contact = _contact_for(comment, contacts)

    # company domain: the company's own website on its LinkedIn page, else the founder's
    # contacts file. Never derived from the company's name.
    domain, domain_source, company = "", "", ""
    current = next((w for w in profile.get("work") or [] if w.get("current")), None)
    if current:
        company = current.get("company") or ""
        site = current.get("website") or ""
        if not site and current.get("company_id"):
            info = unipile.get_company(current["company_id"])
            site = (info or {}).get("website", "")
        if site and is_company_domain(site, ours):
            domain, domain_source = domain_of(site), "company LinkedIn page"
    if not domain and contact and is_company_domain(contact.get("domain", ""), ours):
        domain, domain_source = domain_of(contact["domain"]), "contacts file"
        company = company or contact.get("company", "")

    # address: only from somewhere the person or the founder actually wrote it down. Not from
    # the comment text: a comment is public, and anyone can type someone else's address into
    # one ("send the pricing to ceo@competitor.example").
    sources = {
        "LinkedIn contact info": " ".join(profile.get("emails") or []),
        "contacts file": json.dumps(contact) if contact else "",
    }
    candidates = [e for e in (profile.get("emails") or [])]
    candidates += [contact["email"]] if contact and contact.get("email") else []
    email = candidates[0].lower() if candidates else ""
    verified, evidence = verify_contact(email, sources)
    source = next((n for n, t in sources.items() if email and email in (t or "").lower()), "")

    ident = {"name": profile.get("name") or comment.get("author_name", ""),
             "headline": profile.get("headline") or comment.get("headline", ""),
             "company": company, "domain": domain, "domain_source": domain_source,
             "email": email if verified else "", "email_source": source if verified else "",
             "verified": verified, "verify_evidence": evidence,
             "shared_mailbox": bool(email) and is_role_mailbox(email),
             "profile_url": comment.get("profile_url", "")}
    run.record("identity.resolve", agent=NAME, decision="verified" if verified else "no_address",
               data={**ident, "address": ident["email"]})
    return ident


def relationship(run: Run, ident: dict, contacts: list[dict], ours: set[str]) -> dict:
    """Real prior contact at the lead's company, from Gmail and Calendar."""
    domain = ident["domain"]
    known = [c["email"].lower() for c in contacts
             if c.get("email") and domain and domain_of(c.get("domain", "")) == domain]
    if ident["email"]:
        known.append(ident["email"])
    records: list[dict] = []
    if domain or known:
        records += run.step("gmail.search", lambda: google.mail_evidence(domain, known),
                            agent=NAME, input=f"domain={domain or '-'} addresses={len(known)}")
        run.steps[-1].data = {"found": len(records)}
        try:
            meetings = run.step("calendar.search", lambda: google.meeting_evidence(domain, known),
                                agent=NAME)
            run.steps[-1].data = {"found": len(meetings)}
            records += meetings
        except Exception:  # noqa: BLE001 - recorded on the step; no meetings is not a guess
            pass
    address_company = {c["email"].lower(): c.get("domain", "") for c in contacts if c.get("email")}
    found = people_at(domain, records, ours=ours, address_company=address_company)
    # The founder's contacts file names people the way the founder knows them; a mail header
    # name is whatever that mailbox happened to send.
    names = {c["email"].lower(): c.get("name") for c in contacts if c.get("email") and c.get("name")}
    for p in found["people"]:
        p["name"] = names.get(p["email"], p["name"])
    usable = [p for p in found["people"] if p["label"] in ("strong", "warm")]
    by_id = {r["id"]: r for r in records}
    for p in usable:
        subjects = [by_id[i]["subject"] for i in p["evidence_ids"] if by_id.get(i, {}).get("subject")]
        p["last_subject"] = subjects[-1] if subjects else ""
    rel = {"domain": domain, "refused": found["refused"], "warm": bool(usable),
           "people": usable, "weak": [p for p in found["people"] if p not in usable],
           "records": len(records)}
    run.record("relationship.resolve", agent=NAME, decision="warm" if usable else "cold",
               data={"domain": domain, "refused": found["refused"],
                     "people": [{k: p[k] for k in ("name", "email", "via", "label", "score",
                                                   "evidence", "evidence_ids")} for p in usable],
                     "weak": len(rel["weak"])})
    return rel


def company_name_collision(ident: dict, contacts: list[dict]) -> list[str]:
    """Contacts whose company NAME matches the lead but whose domain does not - shown to the
    founder as the trap it is, never used as a relationship."""
    name = re.sub(r"[^a-z0-9]", "", (ident.get("company") or "").lower())
    if not name:
        return []
    return [f"{c.get('name')} <{c.get('email')}> lists '{c.get('company')}' but is at "
            f"{domain_of(c.get('domain', ''))}, not {ident['domain'] or 'an unknown domain'}"
            for c in contacts
            if re.sub(r"[^a-z0-9]", "", (c.get("company") or "").lower()) == name
            and domain_of(c.get("domain", "")) != ident["domain"]]
