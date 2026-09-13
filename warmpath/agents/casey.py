"""Casey - Outreach Specialist. Writes the message; proves every sentence before it can go.

Same role and face as Casey in Zavorik. The rule ported from there: the model's judgement
can be wrong, but the consequence that matters - a sent sentence that isn't true - cannot
ship. A draft whose claims fail the check is rewritten once, then handed to a human.
"""

from __future__ import annotations

from .. import claims, llm
from ..core import Run
from ..integrations import google

NAME = "Casey"
ROLE = "Outreach Specialist"

INSTRUCTIONS = {
    "C1_SEND_NEEDS_APPROVAL": "No email or reply goes out without the founder's approval in Slack.",
    "C2_EVERY_CLAIM_HAS_EVIDENCE":
        "Every sentence that went out cites evidence that exists; prior contact is claimed only "
        "with relationship evidence; no figure, time or address that no source contains.",
    "C3_ONE_OUTREACH_PER_PERSON": "A person is emailed at most once for one comment, however often it is retried.",
    "C4_DRY_RUN_TOUCHES_NOTHING": "In dry-run mode no real email, reply or invite is sent.",
}


def evidence_for(comment: dict, ident: dict, rel: dict, slots: dict[str, str] | None = None,
                 post_text: str = "") -> dict:
    ev = {"comment": {"kind": "comment", "text": f"{ident['name']} commented: \"{comment['text']}\""}}
    if post_text:
        ev["post"] = {"kind": "post", "text": f"Rahul's post: \"{post_text[:1200]}\""}
    if ident.get("headline"):
        ev["profile"] = {"kind": "profile", "text": f"{ident['name']} - {ident['headline']}"}
    for i, p in enumerate(rel.get("people") or [], 1):
        who = p["name"] or p["email"]
        subject = f", most recent subject \"{p['last_subject']}\"" if p.get("last_subject") else ""
        where = ident.get("company") or rel["domain"]
        ev[f"rel{i}"] = {"kind": "relationship",
                         "text": f"Rahul has prior contact with {who} at {where}: "
                                 f"{p['evidence']}{subject}."}
    for sid, label in (slots or {}).items():
        ev[sid] = {"kind": "slot", "text": f"Free slot {label}"}
    return ev


def write(run: Run, *, channel: str, comment: dict, evidence: dict, allowed: set[str]) -> tuple[dict, list[str]]:
    problems: list[str] = []
    draft: dict = {}
    for attempt in (1, 2):
        draft = run.step("llm.draft", lambda: llm.draft(channel=channel, comment=comment["text"],
                                                        evidence=evidence, problems=problems),
                         agent=NAME, input=f"channel={channel} attempt={attempt}")
        problems = claims.check(draft, evidence, allowed_addresses=allowed)
        run.record("claims.check", ok=not problems, agent=NAME,
                   decision="pass" if not problems else "fail",
                   data={"attempt": attempt, "problems": problems,
                         "sentences": draft.get("sentences", []), "evidence": evidence})
        if not problems:
            break
    return draft, problems


def send_email(run: Run, *, to: str, subject: str, body: str, live: bool) -> dict:
    return run.step("gmail.send", lambda: google.send(to=to, subject=subject, body=body, live=live),
                    agent=NAME, output=body[:400])
