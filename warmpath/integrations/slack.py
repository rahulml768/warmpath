"""Slack - the founder's control center: evidence cards, approvals and notifications.

Approval is a reply in a DM ("send" / "review" / "ignore"). Buttons would need a public
request URL or Socket Mode; a DM reply works from a phone and the bot can read DM history.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request

UA = {"User-Agent": "warmpath/0.1"}


def _slack(method: str, params: dict | None = None, *, post: bool = False) -> dict:
    headers = {**UA, "Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN'].strip()}"}
    url = f"https://slack.com/api/{method}"
    if post:
        req = urllib.request.Request(url, data=json.dumps(params or {}).encode(), method="POST",
                                     headers={**headers,
                                              "Content-Type": "application/json; charset=utf-8"})
    else:
        req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params or {}),
                                     headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read())
    if not d.get("ok"):
        raise RuntimeError(f"slack {method}: {d.get('error')} {d.get('needed', '')}".strip())
    return d


def post(channel: str, text: str) -> dict:
    d = _slack("chat.postMessage", {"channel": channel, "text": text, "unfurl_links": False},
               post=True)
    return {"channel": d["channel"], "ts": d["ts"]}


APPROVE = ("send", "approve", "yes", "ok")
REJECT = ("ignore", "reject", "no", "stop", "cancel")
REVIEW = ("review", "hold", "wait")


def check_decision(*, channel: str, ts: str) -> str | None:
    """One look at the DM thread: approved / rejected / review, or None if nobody has answered."""
    approver = os.environ.get("SLACK_APPROVER_ID", "").strip()
    if not approver:
        return None
    d = _slack("conversations.replies", {"channel": channel, "ts": ts, "limit": 100})
    for m in d.get("messages", []):
        if (m.get("bot_id") or m.get("subtype") or m.get("ts") == ts
                or m.get("thread_ts") != ts or m.get("user") != approver):
            continue
        word = (m.get("text") or "").strip().lower()
        if word in APPROVE:
            return "approved"
        if word in REJECT:
            return "rejected"
        if word in REVIEW:
            return "review"
    return None


def wait_for_decision(*, channel: str, ts: str, timeout_s: int = 600, poll_s: int = 4) -> str:
    """approved / rejected / review / timeout. Silence is never approval."""
    end = time.time() + timeout_s
    while time.time() < end:
        got = check_decision(channel=channel, ts=ts)
        if got:
            return got
        time.sleep(poll_s)
    return "timeout"


def approver_from_slack(timeout_s: int = 600):
    """An approver for the pipeline that asks the founder in a Slack DM."""
    def ask(run, payload: dict) -> str:
        where = post(os.environ["SLACK_APPROVER_ID"].strip(), payload["card"] +
                     "\nReply in this message's thread with exactly: send, review, or ignore.")
        return wait_for_decision(channel=where["channel"], ts=where["ts"], timeout_s=timeout_s)
    return ask
