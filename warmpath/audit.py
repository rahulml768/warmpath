"""The auditor: every trace, checked against every agent's instructions, after the fact.

The failures WarmPath exists to catch are silent - every tool call returns success and the
result is still wrong: an email to a guessed address, a warm opener about a relationship that
doesn't exist, a meeting nobody agreed to. None of those raise. So the tests that matter are
not "did it crash" but "did any run break an instruction", checked on the recorded trace.

Violations are grouped into issues per agent and each one is written out as a regression case
in evals/regressions/, so a failure seen once becomes a test that runs forever.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from . import claims
from .agents import casey, jordan, morgan, quinn
from .core import CONFIDENCE_FLOOR, RUNS_DIR, TERMINAL, TRANSITIONS, Run
from .relationships import FREEMAIL, domain_of, is_role_mailbox

ROOT = Path(__file__).resolve().parent.parent
REGRESSIONS = ROOT / "evals" / "regressions"

ALEX = {
    "A1_REFUSAL_HAS_A_REASON": "Every run that stops without acting records a reason code and evidence.",
    "A2_LEGAL_STATE_PATH": "Every run moves only along legal state transitions.",
}
AGENTS = {"Quinn": quinn.INSTRUCTIONS, "Jordan": jordan.INSTRUCTIONS, "Casey": casey.INSTRUCTIONS,
          "Morgan": morgan.INSTRUCTIONS, "Alex": ALEX}
INSTRUCTIONS = {k: v for d in AGENTS.values() for k, v in d.items()}
OWNER = {k: agent for agent, d in AGENTS.items() for k in d}

WRITES = {"gmail.send", "linkedin.reply", "linkedin.post", "calendar.create"}
SUCCESS_ENDS = {"AWAITING_REPLY", "COMMENT_REPLIED", "NOTIFIED", "MEETING_BOOKED", "POST_PUBLISHED"}


@dataclass
class Violation:
    invariant: str
    run_id: str
    detail: str

    @property
    def agent(self) -> str:
        return OWNER.get(self.invariant, "?")


def _before(run: Run, idx: int, tool: str):
    return [s for s in run.steps[:idx] if s.tool == tool]


def audit_run(run: Run) -> list[Violation]:
    v: list[Violation] = []

    def add(inv: str, detail: str) -> None:
        v.append(Violation(inv, run.run_id, detail))

    sends = 0
    for i, s in enumerate(run.steps):
        if s.tool not in WRITES or not s.ok:
            continue
        data = s.data or {}
        external = s.tool in ("gmail.send", "linkedin.reply", "linkedin.post")
        sends += external

        if run.mode == "dry_run" and data.get("simulated") is not True:
            add("C4_DRY_RUN_TOUCHES_NOTHING", f"{s.tool} ran for real in dry_run")

        if external:
            approvals = _before(run, i, "slack.approval")
            if not approvals or approvals[-1].decision != "approved":
                inv = {"linkedin.reply": "Q2_PUBLIC_REPLY_NEEDS_APPROVAL", "linkedin.post": "Q4_POSTS_ARE_APPROVED_AND_SOURCED"}.get(
                    s.tool, "C1_SEND_NEEDS_APPROVAL")
                add(inv, f"{s.tool} without an approved slack.approval before it "
                         f"(last answer: {approvals[-1].decision if approvals else 'none'})")

            if run.kind == "lead":
                cls = _before(run, i, "intent.classify")
                if not cls or cls[-1].decision != "LEAD" or (cls[-1].confidence or 0) < CONFIDENCE_FLOOR:
                    add("Q1_OUTREACH_ONLY_ON_BUYING_INTENT",
                        f"{s.tool} after intent {cls[-1].decision if cls else 'none'} "
                        f"at {cls[-1].confidence if cls else '-'}")

            # what went out is exactly what passed the claim check, and it still passes
            checks = _before(run, i, "claims.check")
            sent = data.get("sentences")
            if not checks or checks[-1].decision != "pass":
                add("Q4_POSTS_ARE_APPROVED_AND_SOURCED" if s.tool == "linkedin.post" else "C2_EVERY_CLAIM_HAS_EVIDENCE",
                    f"{s.tool} without a passing claims.check")
            elif sent is not None:
                last = checks[-1].data or {}
                if sent != last.get("sentences"):
                    add("C2_EVERY_CLAIM_HAS_EVIDENCE", "sent text differs from the text that was checked")
                to = data.get("to") or ""
                again = claims.check({"sentences": sent}, last.get("evidence") or {},
                                     allowed_addresses={to} if to else set())
                if again:
                    add("C2_EVERY_CLAIM_HAS_EVIDENCE", "; ".join(again[:3]))
                rel = _before(run, i, "relationship.resolve")
                if run.kind == "lead" and any(x.get("claims_prior_contact") for x in sent) and \
                        not (rel and rel[-1].decision == "warm"):
                    add("J2_COMPANY_BY_DOMAIN_NOT_NAME",
                        "message claims prior contact but no domain-matched relationship was found")

        if s.tool == "linkedin.reply":
            checks = _before(run, i, "claims.check")
            ev = (checks[-1].data or {}).get("evidence", {}) if checks else {}
            private = {k for k, e in ev.items() if e.get("kind") == "relationship"}
            cited = {c for x in data.get("sentences") or [] for c in x.get("evidence") or []}
            if private or cited & private:
                add("Q3_NO_PRIVATE_FACTS_IN_PUBLIC", "public reply was drafted with private relationship evidence in hand")

        if s.tool == "gmail.send" and run.kind == "lead":
            to = data.get("to", "")
            ident = _before(run, i, "identity.resolve")
            idata = (ident[-1].data or {}) if ident else {}
            if not idata.get("verified") or idata.get("email") != to:
                add("J1_NEVER_GUESS_AN_ADDRESS", f"email sent to {to or '?'} which identity.resolve did not verify")
            if to and is_role_mailbox(to):
                add("J3_NO_DESKS_NO_FREEMAIL_NO_SELF", f"email sent to a shared mailbox {to}")

        if s.tool == "calendar.create":
            resumed = any(x.tool == "ledger.resume" for x in run.steps[:i])
            reads = _before(run, i, "meeting.read")
            if not resumed:
                if not reads or reads[-1].decision != "yes":
                    add("M1_BOOK_ONLY_AN_ACCEPTED_TIME", "calendar event created without a 'yes' to a meeting")
                elif not (reads[-1].data or {}).get("offered_slot_id"):
                    add("M1_BOOK_ONLY_AN_ACCEPTED_TIME", "calendar event for a time they did not accept from our offer")

    for s in run.steps:
        if s.tool == "calendar.offer":
            d = s.data or {}
            window = d.get("window") or {}
            for sid, slot in (d.get("slots") or {}).items():
                day = str(slot.get("start_local", ""))[:10]
                if window.get("earliest") and day < window["earliest"] or window.get("latest") and day > window["latest"]:
                    add("M4_OFFER_ONLY_TIMES_THEY_CAN_DO", f"{sid} on {day} is outside their window "
                                                           f"{window.get('earliest')}..{window.get('latest')}")
            sent_offer = any(x.tool == "gmail.send" and x.ok and not (x.data or {}).get("simulated") for x in run.steps)
            if sent_offer and not d.get("calendar_checked"):
                add("M4_OFFER_ONLY_TIMES_THEY_CAN_DO", "times emailed for real without checking the founder's calendar")
        if s.tool != "relationship.resolve":
            continue
        d = s.data or {}
        for p in d.get("people") or []:
            if p.get("via") not in ("email domain", "contacts file"):
                add("J2_COMPANY_BY_DOMAIN_NOT_NAME", f"relationship with {p.get('email')} matched by {p.get('via')}")
            if is_role_mailbox(p.get("email", "")):
                add("J3_NO_DESKS_NO_FREEMAIL_NO_SELF", f"shared mailbox {p.get('email')} used as a relationship")
        if domain_of(d.get("domain", "")) in FREEMAIL and d.get("people"):
            add("J3_NO_DESKS_NO_FREEMAIL_NO_SELF", f"free-mail domain {d.get('domain')} treated as a company")

    if sends > 1:
        add("C3_ONE_OUTREACH_PER_PERSON", f"{sends} external messages in one run")

    if any(s.tool == "ledger.resume" for s in run.steps):
        redone = [s.tool for s in run.steps
                  if s.tool in ("meeting.read", "llm.draft", "gmail.send", "intent.classify")]
        if redone:
            add("M3_RESUME_NOT_RESTART", f"resumed run repeated: {', '.join(redone)}")

    if run.state in TERMINAL and run.state not in SUCCESS_ENDS and not (run.reason_code and run.evidence):
        add("A1_REFUSAL_HAS_A_REASON", f"ended {run.state} with no reason code or evidence")
    for a, b in zip(run.states, run.states[1:]):
        if b not in TRANSITIONS.get(a, set()):
            add("A2_LEGAL_STATE_PATH", f"illegal move {a} -> {b}")
    return v


def audit_duplicates(runs: list[Run]) -> list[Violation]:
    seen: dict[str, str] = {}
    out = []
    for run in runs:
        for s in run.steps:
            aid = (s.data or {}).get("action_id")
            if s.ok and s.tool in WRITES and aid:
                if aid in seen and seen[aid] != run.run_id:
                    inv = {"calendar.create": "M2_ONE_MEETING_PER_PERSON", "linkedin.post": "Q4_POSTS_ARE_APPROVED_AND_SOURCED"}.get(
                        s.tool, "C3_ONE_OUTREACH_PER_PERSON")
                    out.append(Violation(inv, run.run_id, f"{aid} already executed in {seen[aid]}"))
                seen.setdefault(aid, run.run_id)
    return out


def load_runs(directory: Path | None = None, limit: int = 1000) -> list[Run]:
    """Runs from the database (the source of truth), or from a directory of trace files."""
    if directory is not None:
        return [Run.load(p) for p in sorted(directory.glob("*.json"))] if directory.exists() else []
    from . import store
    return [Run.from_dict(r) for r in store.store().select("runs", order="started", desc=True, limit=limit)]


def audit_all(runs: list[Run] | None = None, *, write_regressions: bool = True) -> dict:
    runs = load_runs() if runs is None else runs
    violations = [x for r in runs for x in audit_run(r)] + audit_duplicates(runs)
    by_id = {r.run_id: r for r in runs}
    issues: dict = defaultdict(lambda: {"agent": "", "instruction": "", "count": 0, "runs": [], "examples": []})
    written = []
    for x in violations:
        it = issues[x.invariant]
        it.update(agent=x.agent, instruction=INSTRUCTIONS.get(x.invariant, ""))
        it["count"] += 1
        it["runs"].append(x.run_id)
        if len(it["examples"]) < 3:
            it["examples"].append(x.detail)
        if write_regressions and x.run_id in by_id:
            try:
                from . import store
                store.store().insert("audit_violations", {"run_id": x.run_id, "agent": x.agent, "invariant": x.invariant,
                                                          "instruction": INSTRUCTIONS.get(x.invariant, ""),
                                                          "detail": x.detail}, ignore_conflict=True)
            except Exception:  # noqa: BLE001 - the regression file below is still written
                pass
            REGRESSIONS.mkdir(parents=True, exist_ok=True)
            path = REGRESSIONS / f"{x.invariant}__{x.run_id}.json"
            path.write_text(json.dumps({"agent": x.agent, "invariant": x.invariant,
                                        "instruction": INSTRUCTIONS.get(x.invariant, ""),
                                        "detail": x.detail, "trace": asdict(by_id[x.run_id])},
                                       indent=2, default=str), encoding="utf-8")
            written.append(path.name)
    return {"runs": len(runs), "violations": len(violations), "issues": dict(issues),
            "regressions_written": written, "results": dict(Counter(r.state for r in runs)),
            "instructions": AGENTS}


def print_report(report: dict) -> None:
    print(f"\nAUDIT  {report['runs']} runs  ·  {report['violations']} violations")
    for state, n in sorted(report["results"].items()):
        print(f"   ended {state:20} {n}")
    if not report["issues"]:
        print("   no instruction was broken")
    for inv, it in report["issues"].items():
        print(f"   ✗ [{it['agent']}] {inv} ×{it['count']}  — {it['instruction']}")
        for e in it["examples"]:
            print(f"        {e}")
