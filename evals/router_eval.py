"""Does Alex understand what the founder means, however they phrase it?

    python evals/router_eval.py [--runs 2]

Real model. Each phrasing is written the way a founder actually types - direct, indirect, with
typos - and has one correct action. The ones that must NOT publish anything matter most: a public
post triggered by "how are replies going?" is the silent failure of routing.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from warmpath.core import load_env  # noqa: E402

load_env()

from warmpath.chat import route  # noqa: E402

CASES = [
    # grow - write a post
    ("I want to grow, get me more leads", "grow"),
    ("I need more customers this month", "grow"),
    ("We need leads, do something", "grow"),
    ("Help me grow the business", "grow"),
    ("Can you write a LinkedIn post about WarmPath?", "grow"),
    ("Pipeline is empty, help me get some inbound", "grow"),
    ("i wnat more leeds pls", "grow"),
    ("Let's get the word out about the product", "grow"),
    ("Post something on LinkedIn so people know we launched", "grow"),
    ("Sales are slow this week", "grow"),
    # scan a post
    ("Check the comments on https://www.linkedin.com/posts/rahul-activity-7332661864792854528-abcd", "scan_post"),
    ("Watch the demo post", "scan_post"),
    ("Go through the comments on the demo post", "scan_post"),
    ("Anyone interested on my post? https://www.linkedin.com/feed/update/urn:li:activity:7332661864792854528", "scan_post"),
    # replies
    ("Any replies?", "check_replies"),
    ("Did John get back to us?", "check_replies"),
    ("Has anyone answered our emails?", "check_replies"),
    ("Book meetings with whoever said yes", "check_replies"),
    # report
    ("How did the team do today?", "report"),
    ("Did any agent break a rule?", "report"),
    ("How many leads came in and what happened to them?", "report"),
    # none - must not act, and must never publish
    ("Hi Alex", "none"),
    ("Who is on the team?", "none"),
    ("Don't post anything today", "none"),
    ("What does Jordan do?", "none"),
    ("Thanks", "none"),
    ("Check the comments on my post", "none"),      # no link given: Alex must ask, not invent one
    ("Delete my last LinkedIn post", "none"),        # not something Alex can do: must not become a new post
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=2)
    args = ap.parse_args()
    jobs = [(text, want) for text, want in CASES for _ in range(args.runs)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda j: (j[0], j[1], route(j[0])), jobs))

    by_case: dict[str, list[str]] = {}
    for text, want, plan in results:
        by_case.setdefault(text, []).append(plan["action"])
    correct = sum(plan["action"] == want for _, want, plan in results)
    unwanted_posts = sum(plan["action"] == "grow" and want != "grow" for _, want, plan in results)
    invented_urls = sum(plan["action"] == "scan_post" and plan.get("source") not in (None, "demo")
                        and plan["source"] not in text for text, _, plan in results)

    print(f"\n{'phrasing':72} {'want':14} got")
    for text, want in CASES:
        got = Counter(by_case[text])
        ok = all(a == want for a in by_case[text])
        print(f"{'PASS' if ok else 'FAIL'} {text[:67]:67} {want:14} {dict(got)}")
    print(f"\ncorrect: {correct}/{len(results)} = {correct / len(results):.0%}")
    print(f"posts triggered by a message that didn't ask for one: {unwanted_posts}")
    print(f"post links Alex acted on that the founder never wrote: {invented_urls}")
    (ROOT / "evals" / "router_report.json").write_text(json.dumps({
        "runs": args.runs, "correct": correct, "total": len(results), "unwanted_posts": unwanted_posts,
        "invented_urls": invented_urls, "cases": {t: by_case[t] for t, _ in CASES}}, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
