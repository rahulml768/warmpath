"""LinkedIn, through Unipile.

Unipile reaches LinkedIn through the connected member's own session, not LinkedIn's official
API (which offers no way to read comments on a member's post). That is stated in the README
and the brief. Reads are low volume - one post's comments - and the one write, a reply under
a comment, is public and posted under the founder's name, so it goes through the same
approval, ledger and dry-run gates as an email.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

UA = "warmpath/0.1"


class UnipileError(RuntimeError):
    pass


def _call(method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
    base = "https://" + os.environ["UNIPILE_DSN"].strip()
    q = {"account_id": os.environ["UNIPILE_ACCOUNT_ID"].strip(), **(params or {})}
    url = f"{base}/api/v1/{path}?{urllib.parse.urlencode(q, doseq=True)}"
    headers = {"X-API-KEY": os.environ["UNIPILE_API_KEY"].strip(), "accept": "application/json",
               "User-Agent": UA}
    data = None
    if body is not None:
        data = json.dumps({"account_id": q["account_id"], **body}).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raise UnipileError(f"unipile {method} {path}: HTTP {exc.code} "
                           f"{exc.read().decode(errors='replace')[:300]}") from exc


def post_id_from(url_or_id: str) -> str:
    """`https://www.linkedin.com/posts/...-activity-7332661864792854528-abcd` -> the number."""
    m = re.search(r"(?:activity|ugcPost|share)[-:](\d{15,})", url_or_id or "")
    return m.group(1) if m else (url_or_id or "").strip()


def get_post(url_or_id: str) -> dict:
    p = _call("GET", f"posts/{urllib.parse.quote(post_id_from(url_or_id), safe=':')}")
    return {"id": p.get("id"), "social_id": p.get("social_id"), "text": p.get("text") or "",
            "share_url": p.get("share_url") or "", "comments": p.get("comment_counter") or 0}


def list_comments(social_id: str, limit: int = 50) -> list[dict]:
    d = _call("GET", f"posts/{urllib.parse.quote(social_id, safe='')}/comments",
              {"limit": limit, "sort_by": "MOST_RECENT"})
    out = []
    for c in d.get("items", []):
        a = c.get("author_details") or {}
        author = c.get("author")
        name = author if isinstance(author, str) else (author or {}).get("name", "")
        out.append({
            "source": "linkedin",
            "comment_id": str(c.get("id") or ""),
            "post_social_id": social_id,
            "text": c.get("text") or "",
            "date": c.get("date") or "",
            "author_name": name or a.get("name") or "",
            "author_id": a.get("id") or c.get("author_id") or "",
            "headline": a.get("headline") or "",
            "profile_url": a.get("profile_url") or "",
        })
    return out


def get_profile(identifier: str) -> dict:
    """Name, headline, work history and - only if the member shares it with us - an email."""
    p = _call("GET", f"users/{urllib.parse.quote(identifier, safe='')}",
              {"linkedin_sections": "experience"})
    contact = p.get("contact_info") or {}
    return {
        "name": " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x),
        "headline": p.get("headline") or "",
        "emails": [e.lower() for e in (contact.get("emails") or []) if isinstance(e, str)],
        "websites": p.get("websites") or [],
        "network_distance": p.get("network_distance") or "",
        "work": [{"company": w.get("company") or "", "company_id": w.get("company_id") or "",
                  "position": w.get("position") or "", "current": not w.get("end")}
                 for w in (p.get("work_experience") or [])],
    }


def get_company(company_id: str) -> dict | None:
    """The company's own website, if LinkedIn has one on its page. None when unavailable."""
    try:
        c = _call("GET", f"linkedin/company/{urllib.parse.quote(str(company_id), safe='')}")
    except UnipileError:
        return None
    return {"name": c.get("name") or "", "website": c.get("website") or ""}


def create_post(*, text: str, live: bool) -> dict:
    """Publish a post on the founder's profile. Public and under their name - callers gate it."""
    if not live:
        return {"simulated": True, "post_id": "", "text": text}
    import uuid
    boundary = "warmpath" + uuid.uuid4().hex
    fields = {"account_id": os.environ["UNIPILE_ACCOUNT_ID"].strip(), "text": text}
    body = b"".join(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode("utf-8")
        for k, v in fields.items()) + f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        "https://" + os.environ["UNIPILE_DSN"].strip() + "/api/v1/posts", data=body, method="POST",
        headers={"X-API-KEY": os.environ["UNIPILE_API_KEY"].strip(), "accept": "application/json",
                 "User-Agent": UA, "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raise UnipileError(f"unipile create post: HTTP {exc.code} {exc.read().decode(errors='replace')[:300]}") from exc
    return {"simulated": False, "post_id": str(d.get("post_id") or ""), "text": text}


def reply_to_comment(*, social_id: str, comment_id: str, text: str, live: bool) -> dict:
    if not live:
        return {"simulated": True, "comment_id": comment_id, "text": text}
    d = _call("POST", f"posts/{urllib.parse.quote(social_id, safe='')}/comments",
              body={"text": text, "comment_id": comment_id})
    return {"simulated": False, "comment_id": comment_id, "result": d.get("object") or "ok"}
