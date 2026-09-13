"""Quinn - Social Media Manager. Reads the comments; decides which ones are buying intent.

Same role and face as Quinn in Zavorik. Quinn never writes to anyone on her own judgement:
a comment she scores as a lead goes to Jordan, and a public reply she drafts goes to the
founder first.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import llm
from ..core import Run
from ..integrations import unipile

NAME = "Quinn"
ROLE = "Social Media Manager"

INSTRUCTIONS = {
    "Q1_OUTREACH_ONLY_ON_BUYING_INTENT":
        "Nobody is contacted unless their comment was scored LEAD at or above the confidence floor.",
    "Q2_PUBLIC_REPLY_NEEDS_APPROVAL":
        "A LinkedIn reply is public and posted as the founder - it goes out only after approval.",
    "Q3_NO_PRIVATE_FACTS_IN_PUBLIC":
        "A public reply never uses private evidence - nothing from the founder's Gmail or Calendar.",
    "Q4_POSTS_ARE_APPROVED_AND_SOURCED":
        "A LinkedIn post is published only after approval, only once, and says nothing the product brief doesn't.",
}


def read_post(source: str) -> tuple[dict, list[dict]]:
    """A live post URL/id through Unipile, or a fixture file - labelled as such in every trace."""
    path = Path(source)
    if path.suffix == ".json" and path.exists():
        d = json.loads(path.read_text(encoding="utf-8"))
        post = {**d["post"], "source": f"fixture:{path.name}"}
        return post, [{**c, "source": f"fixture:{path.name}"} for c in d["comments"]]
    post = unipile.get_post(source)
    return {**post, "source": "unipile"}, unipile.list_comments(post["social_id"])


def classify(run: Run, comment: dict, post_text: str, rubric: str = "v2") -> dict:
    cls = run.step("intent.classify", lambda: llm.classify(comment["text"], post=post_text,
                                                           rubric=rubric), agent=NAME,
                   input=comment["text"][:300])
    s = run.steps[-1]
    s.decision, s.confidence = cls["intent"], cls["confidence"]
    s.data = {"signals": cls["signals"], "ungrounded_signals": cls["ungrounded_signals"],
              "rubric": rubric, "source": comment.get("source", "")}
    return cls


def brief() -> dict[str, dict]:
    """The founder's product brief as evidence - the only facts a post may state."""
    folder = Path(__file__).resolve().parent.parent.parent / "fixtures"
    src = folder / "product.local.json" if (folder / "product.local.json").exists() else folder / "product.json"
    facts = json.loads(src.read_text(encoding="utf-8"))["facts"]
    return {k: {"kind": "post", "text": v} for k, v in facts.items()}


def write_post(run: Run, goal: str) -> tuple[dict, list[str], dict]:
    from .. import claims
    evidence = brief()
    problems: list[str] = []
    draft: dict = {}
    for attempt in (1, 2):
        draft = run.step("llm.post", lambda: llm.draft_post(goal=goal, evidence=evidence, problems=problems),
                         agent=NAME, input=f"goal={goal[:200]} attempt={attempt}")
        problems = claims.check(draft, evidence, allowed_addresses=set())
        run.record("claims.check", ok=not problems, agent=NAME, decision="pass" if not problems else "fail",
                   data={"attempt": attempt, "problems": problems, "sentences": draft.get("sentences", []),
                         "evidence": evidence})
        if not problems:
            break
    return draft, problems, evidence


def publish_post(run: Run, text: str, live: bool) -> dict:
    return run.step("linkedin.post", lambda: unipile.create_post(text=text, live=live), agent=NAME, output=text[:600])


def reply_on_linkedin(run: Run, comment: dict, text: str, live: bool) -> dict:
    res = run.step("linkedin.reply", lambda: unipile.reply_to_comment(
        social_id=comment["post_social_id"], comment_id=comment["comment_id"], text=text,
        live=live and not str(comment.get("source", "")).startswith("fixture")), agent=NAME,
        output=text[:300])
    return res
