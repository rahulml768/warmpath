"""The parts of WarmPath that no model is allowed to influence.

State machine, action ledger, identity rules, policy gates and the run trace. Everything
here is deterministic and testable without a network - which is the point. The model
proposes an intent and a draft; this module decides whether anything may leave the building.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = ROOT / "runs"
DB_PATH = ROOT / "warmpath.db"


def load_env(path: Path = ROOT / ".env") -> None:
    """Read .env into the process environment without overwriting anything already set."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))


def mode() -> str:
    """dry_run unless someone deliberately says otherwise. The mailbox has real clients."""
    return "live" if os.environ.get("WARMPATH_MODE", "dry_run").strip() == "live" else "dry_run"


# ── state machine ──────────────────────────────────────────────────────────────

STATES = {
    # a lead: one LinkedIn comment
    "RECEIVED", "CLASSIFIED", "IDENTITY_RESOLVED", "RELATIONSHIP_RESOLVED", "ACTION_PROPOSED",
    "AWAITING_APPROVAL", "APPROVED", "EMAIL_SENT", "COMMENT_REPLIED", "AWAITING_REPLY",
    # a reply: what they wrote back
    "REPLY_RECEIVED", "MEETING_READ", "SLOT_CHECKED", "MEETING_BOOKED", "CALENDAR_FAILED",
    "NOTIFIED",
    # a post: a goal turned into something public
    "POST_PUBLISHED", "AWAITING_INTRO",
    # exits
    "IGNORED", "REVIEW_REQUIRED", "BLOCKED", "STOPPED", "BLOCKED_DUPLICATE",
}

TERMINAL = {"IGNORED", "REVIEW_REQUIRED", "BLOCKED", "STOPPED", "BLOCKED_DUPLICATE",
            "COMMENT_REPLIED", "AWAITING_REPLY", "NOTIFIED", "POST_PUBLISHED", "AWAITING_INTRO"}

#: Legal moves only. `RECEIVED -> EMAIL_SENT` is not reachable because it is not written
#: down here, which is a stronger guarantee than a comment saying it should not happen.
TRANSITIONS = {
    "RECEIVED": {"CLASSIFIED", "BLOCKED", "ACTION_PROPOSED"},
    "CLASSIFIED": {"IDENTITY_RESOLVED", "IGNORED", "REVIEW_REQUIRED", "BLOCKED"},
    "IDENTITY_RESOLVED": {"RELATIONSHIP_RESOLVED", "BLOCKED", "REVIEW_REQUIRED"},
    "RELATIONSHIP_RESOLVED": {"ACTION_PROPOSED", "BLOCKED", "REVIEW_REQUIRED"},
    "ACTION_PROPOSED": {"AWAITING_APPROVAL", "REVIEW_REQUIRED", "BLOCKED", "BLOCKED_DUPLICATE"},
    "AWAITING_APPROVAL": {"APPROVED", "STOPPED", "REVIEW_REQUIRED"},
    "APPROVED": {"EMAIL_SENT", "COMMENT_REPLIED", "POST_PUBLISHED", "BLOCKED_DUPLICATE", "STOPPED"},
    "EMAIL_SENT": {"AWAITING_REPLY", "AWAITING_INTRO"},
    "REPLY_RECEIVED": {"MEETING_READ", "BLOCKED_DUPLICATE"},
    "MEETING_READ": {"SLOT_CHECKED", "ACTION_PROPOSED", "IGNORED", "REVIEW_REQUIRED", "BLOCKED"},
    "SLOT_CHECKED": {"MEETING_BOOKED", "CALENDAR_FAILED", "REVIEW_REQUIRED", "BLOCKED_DUPLICATE"},
    "CALENDAR_FAILED": {"MEETING_BOOKED", "CALENDAR_FAILED", "BLOCKED_DUPLICATE", "REVIEW_REQUIRED"},
    "MEETING_BOOKED": {"NOTIFIED"},
}


class IllegalTransition(Exception):
    pass


# ── trace ──────────────────────────────────────────────────────────────────────

@dataclass
class Step:
    tool: str
    ok: bool
    ms: int
    agent: str = ""
    input: str = ""
    output: str = ""
    decision: str = ""
    confidence: float | None = None
    error: str = ""
    data: dict = field(default_factory=dict)
    replay_output: object = None
    replay_captured: bool = False
    model_call: dict = field(default_factory=dict)


@dataclass
class Run:
    """One execution, recorded as it happens. Refusals are recorded exactly like actions."""

    run_id: str
    message_id: str
    mode: str
    started: str
    kind: str = "lead"
    subject: str = ""
    state: str = "RECEIVED"
    states: list[str] = field(default_factory=lambda: ["RECEIVED"])
    steps: list[Step] = field(default_factory=list)
    result: str = ""
    reason_code: str = ""
    evidence: list[str] = field(default_factory=list)

    @classmethod
    def start(cls, message_id: str, run_id: str | None = None, *, kind: str = "lead",
              subject: str = "", state: str = "RECEIVED") -> "Run":
        return cls(run_id=run_id or "wp_" + uuid.uuid4().hex[:8], message_id=message_id,
                   mode=mode(), started=datetime.now(timezone.utc).isoformat(), kind=kind,
                   subject=subject, state=state, states=[state])

    def move(self, new: str) -> None:
        if new not in TRANSITIONS.get(self.state, set()):
            raise IllegalTransition(f"{self.state} -> {new}")
        self.state = new
        self.states.append(new)

    def step(self, tool: str, fn, **meta):
        """Time a call and record it whether it succeeds or not."""
        t = time.perf_counter()
        from .llm import CALL_INFO
        token = CALL_INFO.set(None)
        try:
            out = fn()
            self.steps.append(Step(tool=tool, ok=True, ms=int((time.perf_counter() - t) * 1000),
                                   replay_output=out, replay_captured=True, model_call=CALL_INFO.get() or {}, **meta))
            return out
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
            self.steps.append(Step(tool=tool, ok=False, ms=int((time.perf_counter() - t) * 1000),
                                   error=f"{type(exc).__name__}: {exc}"[:300], model_call=CALL_INFO.get() or {}, **meta))
            raise
        finally:
            CALL_INFO.reset(token)

    def record(self, tool: str, ok: bool = True, **meta) -> Step:
        s = Step(tool=tool, ok=ok, ms=0, **meta)
        self.steps.append(s)
        return s

    def finish(self, result: str, reason_code: str = "", evidence: list[str] | None = None):
        self.result, self.reason_code = result, reason_code
        self.evidence = list(evidence or [])
        return self

    def save(self, violations: int = 0) -> Path:
        """Write the trace to the database (the source of truth) and to runs/ (for grepping)."""
        from . import store
        d = asdict(self)
        store.store().upsert("runs", {
            "run_id": self.run_id, "kind": self.kind, "subject": self.subject, "message_id": self.message_id,
            "mode": self.mode, "state": self.state, "states": d["states"], "steps": d["steps"],
            "result": self.result, "reason_code": self.reason_code, "evidence": d["evidence"],
            "started": self.started, "is_eval": self.run_id.startswith("eval_"), "violations": violations,
            "created_at": datetime.now(timezone.utc).isoformat()})
        RUNS_DIR.mkdir(exist_ok=True)
        path = RUNS_DIR / f"{self.run_id}.json"
        path.write_text(json.dumps(d, indent=2, default=str), encoding="utf-8")
        return path

    @classmethod
    def from_dict(cls, d: dict) -> "Run":
        fields = {k: d.get(k) for k in ("run_id", "message_id", "mode", "started", "kind", "subject", "state",
                                        "states", "result", "reason_code", "evidence") if d.get(k) is not None}
        run = cls(**fields)
        run.steps = [Step(**s) for s in (d.get("steps") or [])]
        return run

    @classmethod
    def load(cls, path: Path) -> "Run":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


# ── action ledger ──────────────────────────────────────────────────────────────

def _store_for(path):
    from . import store
    return store.SQLiteStore(path) if path else store.store()


class Ledger:
    """A retry must never become a second real-world action.

    Every external action is written here with a stable action_id BEFORE it is attempted
    and marked done AFTER it succeeds. A second attempt at a done action is refused, which
    is what stops a duplicated webhook or a timed-out retry from emailing someone twice.
    The check-and-claim is atomic in the database (ledger_begin), not two separate calls.
    """

    def __init__(self, path: Path | str | None = None):
        self.s = _store_for(path)

    def close(self) -> None:
        pass

    def status(self, action_id: str) -> str | None:
        row = self.s.get("action_ledger", action_id)
        return row["status"] if row else None

    def begin(self, action_id: str, run_id: str, kind: str) -> bool:
        """True if the action may be attempted; False if it is done or another run holds it."""
        return self.s.ledger_begin(action_id, run_id, kind)

    def done(self, action_id: str, detail: str = "") -> None:
        self.s.update("action_ledger", action_id, {"status": "done", "detail": detail[:500],
                                                   "updated": datetime.now(timezone.utc).isoformat()})

    def failed(self, action_id: str, detail: str = "") -> None:
        self.s.update("action_ledger", action_id, {"status": "failed", "detail": detail[:500],
                                                   "updated": datetime.now(timezone.utc).isoformat()})

    def done_count(self, kind: str | None = None) -> int:
        where = {"status": "done", **({"kind": kind} if kind else {})}
        return len(self.s.select("action_ledger", where))


LEAD_COLUMNS = {"key", "email", "name", "company", "domain", "comment", "intent", "confidence", "warm", "knows",
                "stage", "status", "subject", "sent_at", "offered", "pending_booking", "meeting", "comment_id",
                "lead_run", "data", "updated_at"}


class Leads:
    """Everyone the team is working, and what a later run needs from an earlier one: who we wrote
    to, what we offered, and a booking decided but not yet on the calendar (so a retry resumes)."""

    def __init__(self, path: Path | str | None = None):
        self.s = _store_for(path)

    @staticmethod
    def _flat(row: dict | None) -> dict | None:
        if not row:
            return None
        extra = row.get("data") or {}
        return {**extra, **{k: v for k, v in row.items() if k != "data" and v is not None}}

    def get(self, key: str) -> dict | None:
        direct = self._flat(self.s.get("leads", key))
        if direct or '@' not in key:
            return direct
        from .entities import rows
        person_ids = {p['id'] for p in rows(self.s, f'person:{mode()}:')
                      if 'email:' + key.lower() in p['aliases']}
        return next((row for row in self.all() if row.get('person_id') in person_ids), None)

    def save(self, key: str, data: dict) -> dict:
        merged = {**(self.get(key) or {}), **data}
        row = {k: v for k, v in merged.items() if k in LEAD_COLUMNS and k != "data"}
        row["data"] = {k: v for k, v in merged.items() if k not in LEAD_COLUMNS}
        row.update(key=key, updated_at=datetime.now(timezone.utc).isoformat())
        self.s.upsert("leads", row)
        return merged

    def all(self) -> list[dict]:
        return [self._flat(r) for r in self.s.select("leads", order="updated_at")]

    def close(self) -> None:
        pass


def action_id(kind: str, message_id: str) -> str:
    """Stable per message: the same inbound message always maps to the same action.

    Derived from the message, never from the run - a retry is a new run of the same action,
    and keying on the run would let every retry look like a fresh, allowed action.
    """
    safe = re.sub(r"[^A-Za-z0-9]+", "", message_id)[-24:]
    return f"{kind}:{safe}"


# ── identity ───────────────────────────────────────────────────────────────────

#: A rota, not a person - even when a human name sits in the signature.
SHARED_LOCAL_PARTS = {"info", "sales", "support", "hello", "contact", "billing", "team",
                      "admin", "office", "help", "enquiries", "inquiries", "noreply",
                      "no-reply", "notifications", "hr", "careers", "jobs", "marketing"}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def local_part(address: str) -> str:
    return address.split("@", 1)[0].lower()


def is_shared_mailbox(address: str) -> bool:
    """`sales2@` and `info+web@` are still the sales and info queues."""
    base = re.sub(r"\+.*$", "", local_part(address))
    base = re.sub(r"[\d._\-]+$", "", base)
    return base in SHARED_LOCAL_PARTS


def verify_contact(candidate: str, sources: dict[str, str] | list[str]) -> tuple[bool, list[str]]:
    """An address is usable only if the person actually put it somewhere we can point to.

    Never a pattern like firstname@company.com. `sources` are named raw texts the address must
    literally appear in - LinkedIn contact info the member shares, the founder's contacts file,
    a message the person wrote. A guessed address is indistinguishable from a found one to
    SMTP, which is exactly why it has to be distinguishable here.
    """
    if isinstance(sources, list):
        sources = {f"source #{i}": s for i, s in enumerate(sources)}
    cand = (candidate or "").strip().lower()
    if not EMAIL_RE.fullmatch(cand):
        return False, ["no well-formed email address was found in any source"]
    hits = [name for name, text in sources.items()
            if cand in {address.lower() for address in EMAIL_RE.findall(text or "")}]
    if not hits:
        return False, [f"{cand} does not appear in any source"]
    return True, [f"{cand} appears in {hits[0]}"]


# ── policy ─────────────────────────────────────────────────────────────────────

CONFIDENCE_FLOOR = 0.7

LEAD, NOT_LEAD, UNCERTAIN, OPT_OUT = "LEAD", "NOT_LEAD", "UNCERTAIN", "OPT_OUT"


@dataclass
class Decision:
    allowed: bool
    decision: str
    reason_code: str
    evidence: list[str]


def evaluate(*, intent: str, confidence: float, verified: bool, verify_evidence: list[str],
             shared_mailbox: bool, approved: bool | str | None) -> Decision:
    """The hard gates. The model can argue; it cannot get past these.

    Every refusal returns a reason code and evidence. A refusal with no reason is
    indistinguishable from a bug, and this product's claim is that doing nothing can be a
    successful run - so the nothing has to be provable.
    """
    if intent == OPT_OUT:
        return Decision(False, "BLOCK", "OPT_OUT", ["sender asked not to be contacted"])
    if intent == NOT_LEAD:
        return Decision(False, "IGNORE", "NO_COMMERCIAL_INTENT",
                        [f"classified NOT_LEAD at confidence {confidence:.2f}"])
    if (intent != LEAD or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float)) or not math.isfinite(confidence)
            or not 0 <= confidence <= 1):
        return Decision(False, "HUMAN_REVIEW", "INVALID_CLASSIFICATION",
                        ["expected LEAD with finite numeric confidence between 0 and 1"])
    if confidence < CONFIDENCE_FLOOR:
        return Decision(False, "HUMAN_REVIEW", "LOW_CONFIDENCE",
                        [f"intent {intent} at confidence {confidence:.2f} "
                         f"(floor {CONFIDENCE_FLOOR})"])
    if not verified:
        return Decision(False, "BLOCK", "CONTACT_NOT_VERIFIED", verify_evidence)
    if shared_mailbox:
        return Decision(False, "BLOCK_PERSONALIZATION", "SHARED_MAILBOX",
                        ["sender is a shared mailbox", "person-level identity not verified"])
    if approved is None:
        return Decision(True, "DRAFT_ALLOWED", "SEND_REQUIRES_APPROVAL",
                        ["lead", "verified contact", "awaiting human approval"])
    if approved == "review":
        return Decision(False, "HUMAN_REVIEW", "FOUNDER_WANTS_REVIEW", ["founder chose review"])
    if approved is not True:
        return Decision(False, "STOP", "APPROVAL_REJECTED", ["human rejected the draft"])
    return Decision(True, "SEND_ALLOWED", "APPROVED", ["lead", "verified", "approved"])
