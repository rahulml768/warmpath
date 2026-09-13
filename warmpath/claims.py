"""Every factual sentence in an outgoing message must point at something we actually read.

Casey's gate. The model is told not to invent; a prompt is an instruction and instructions
fail - the failure IS the model overriding it. What can be checked is arithmetic:

1. The draft comes back as sentences, each tagged with the evidence ids it relies on.
2. A cited id that is not in the evidence set is an invented source.
3. A sentence that promises what the product can do must cite the founder's own post.
4. A sentence that asserts prior contact ("we spoke", "our call", "your colleague") must cite
   a relationship evidence item - otherwise it claims a relationship nobody can show.
5. Every figure, date or time in the draft must appear in the evidence text (ported from
   Zavorik's unconfirmed-claims check: a figure is a commitment).
6. Every email address in the draft must be one we were given.

A draft that fails is not sent. It is regenerated once with the failures named, and if it
still fails it goes to a human with the reasons - never out the door.
"""

from __future__ import annotations

import re

from .core import EMAIL_RE

_FIGURE_RE = re.compile(r"(?:[$€£₹]\s?)?\d[\d,.:]*\s?(?:%|k|am|pm|AM|PM)?")


def _norm(t: str) -> str:
    return re.sub(r"[\s,]", "", (t or "").lower())


def check(draft: dict, evidence: dict[str, dict], *, allowed_addresses: set[str]) -> list[str]:
    """Return the problems with a draft; an empty list means every claim is backed.

    draft:    {"subject", "sentences": [{"text", "evidence": [ids], "claims_prior_contact": bool}]}
    evidence: {id: {"kind": "relationship"|"comment"|"profile"|"calendar"|..., "text": str}}
    """
    problems: list[str] = []
    haystack = _norm(" ".join(e.get("text", "") for e in evidence.values()))
    relationship_ids = {i for i, e in evidence.items() if e.get("kind") == "relationship"}
    post_ids = {i for i, e in evidence.items() if e.get("kind") == "post"}

    for n, s in enumerate(draft.get("sentences") or [], 1):
        text = (s.get("text") or "").strip()
        cited = [str(c) for c in (s.get("evidence") or [])]
        unknown = [c for c in cited if c not in evidence]
        if unknown:
            problems.append(f"sentence {n} cites evidence that does not exist: {', '.join(unknown)}")
        if s.get("claims_capability") and not (set(cited) & post_ids):
            problems.append(f"sentence {n} promises a product capability the founder's post doesn't state: "
                            f"\"{text[:90]}\"")
        if s.get("claims_prior_contact") and not (set(cited) & relationship_ids):
            problems.append(f"sentence {n} claims prior contact with no relationship evidence: "
                            f"\"{text[:90]}\"")
        for fig in _FIGURE_RE.findall(text):
            key = _norm(fig)
            if len(key) >= 1 and any(ch.isdigit() for ch in key) and key not in haystack:
                problems.append(f"sentence {n} states a figure/time not in any source: {fig.strip()}")
        for addr in EMAIL_RE.findall(text):
            if addr.lower() not in allowed_addresses:
                problems.append(f"sentence {n} names an address we were never given: {addr}")
    if not draft.get("sentences"):
        problems.append("draft has no sentences")
    return problems


def render(draft: dict, signoff: str = "Rahul") -> str:
    body = " ".join((s.get("text") or "").strip() for s in draft.get("sentences") or [])
    return f"{body}\n\n{signoff}".strip()
