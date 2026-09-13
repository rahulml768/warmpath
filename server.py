"""The WarmPath console: chat with Alex, the pipeline, traces, agents, integrations and the audit.

Stdlib only, bound to localhost. The chat can start a run and answer an approval card; it
cannot skip a gate - every write still goes through the same pipeline, ledger and dry-run mode.
"""

from __future__ import annotations

import json
import mimetypes
import sys
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from warmpath.core import DB_PATH, RUNS_DIR, Leads, Run, load_env, mode  # noqa: E402

load_env()

from warmpath import audit  # noqa: E402
from warmpath.agents import casey, jordan, morgan, quinn  # noqa: E402
from warmpath.chat import Chat  # noqa: E402
from warmpath.chat import handle as chat_handle  # noqa: E402

UI = ROOT / "ui"
AGENTS = [
    {"name": "Alex", "role": "Orchestrator", "avatar": "alex", "does": "Moves each lead through the gates and asks you in Slack.",
     "instructions": audit.ALEX},
    {"name": quinn.NAME, "role": quinn.ROLE, "avatar": "quinn", "does": "Reads LinkedIn comments and scores buying intent.",
     "instructions": quinn.INSTRUCTIONS},
    {"name": jordan.NAME, "role": jordan.ROLE, "avatar": "jordan", "does": "Resolves who they are and who you already know there.",
     "instructions": jordan.INSTRUCTIONS},
    {"name": casey.NAME, "role": casey.ROLE, "avatar": "casey", "does": "Writes the message and proves every sentence.",
     "instructions": casey.INSTRUCTIONS},
    {"name": morgan.NAME, "role": morgan.ROLE, "avatar": "morgan", "does": "Turns a yes into a meeting that actually exists.",
     "instructions": morgan.INSTRUCTIONS},
]


def _runs() -> list[Run]:
    return audit.load_runs(limit=500)


def _summary(run: Run) -> dict:
    get = lambda tool: next((s for s in run.steps if s.tool == tool), None)  # noqa: E731
    cls, ident, rel = get("intent.classify"), get("identity.resolve"), get("relationship.resolve")
    send = next((s for s in run.steps if s.tool in ("gmail.send", "linkedin.reply", "calendar.create") and s.ok), None)
    return {
        "run_id": run.run_id, "kind": run.kind, "subject": run.subject, "state": run.state,
        "reason_code": run.reason_code, "evidence": run.evidence, "started": run.started, "mode": run.mode,
        "comment": (cls.input if cls else "") or "",
        "intent": cls.decision if cls else "", "confidence": cls.confidence if cls else None,
        "company": ((ident.data or {}).get("company") if ident else "") or "",
        "domain": ((ident.data or {}).get("domain") if ident else "") or "",
        "warm": bool(rel and rel.decision == "warm"),
        "action": send.tool if send else "", "eval": run.run_id.startswith("eval_"),
        "agents": list(dict.fromkeys(s.agent for s in run.steps if s.agent)),
        "violations": [asdict(v) | {"agent": v.agent} for v in audit.audit_run(run)],
    }


def _audit(runs: list[Run]) -> dict:
    """Real runs share one world (one ledger), so duplicates are checked across all of them. Each
    evaluation repetition is its own world with a fresh ledger, so it is audited on its own."""
    import re
    from collections import Counter
    groups: dict[str, list[Run]] = {}
    for r in runs:
        m = re.match(r"eval_.+_(\d+)$", r.run_id)
        groups.setdefault(f"eval{m.group(1)}" if m else "real", []).append(r)
    reports = [audit.audit_all(g, write_regressions=False) for g in groups.values()]
    issues: dict = {}
    for rep in reports:
        for k, v in rep["issues"].items():
            it = issues.setdefault(k, {**v, "count": 0, "runs": [], "examples": []})
            it["count"] += v["count"]
            it["runs"] += v["runs"]
            it["examples"] = (it["examples"] + v["examples"])[:3]
    return {"runs": sum(r["runs"] for r in reports), "violations": sum(r["violations"] for r in reports),
            "issues": issues, "results": dict(sum((Counter(r["results"]) for r in reports), Counter()))}


def state() -> dict:
    runs = _runs()
    report = _audit(runs)
    ev = ROOT / "evals" / "last_report.json"
    from warmpath import store
    latest = store.store().select("eval_results", order="id", desc=True, limit=1)
    evaluation = latest[0]["report"] if latest else (json.loads(ev.read_text(encoding="utf-8")) if ev.exists() else None)
    return {"mode": mode(), "agents": AGENTS, "runs": [_summary(r) for r in runs[:200]],
            "audit": {k: report[k] for k in ("runs", "violations", "issues", "results")},
            "eval": evaluation, "store": store.store().backend,
            "regressions": len(list((ROOT / "evals" / "regressions").glob("*.json")))
            if (ROOT / "evals" / "regressions").exists() else 0,
            }


CHAT = Chat()
_integrations: dict = {"at": 0.0, "data": []}
AUTOPILOT_REF = None

LEAD_STAGE = {"AWAITING_REPLY": "Emailed · waiting for reply", "COMMENT_REPLIED": "Replied on LinkedIn",
              "IGNORED": "Not a lead", "BLOCKED": "Blocked", "REVIEW_REQUIRED": "Needs you", "STOPPED": "Stopped"}
REPLY_STAGE = {"AWAITING_REPLY": "Times offered · waiting", "NOTIFIED": "Meeting booked", "MEETING_BOOKED": "Meeting booked",
               "IGNORED": "Not interested", "REVIEW_REQUIRED": "Needs you", "CALENDAR_FAILED": "Calendar failed · retrying",
               "STOPPED": "Stopped"}


def leads_view() -> list[dict]:
    """The leads table, with anything a worker is doing right now laid over it."""
    rows = []
    for lead in Leads().all():
        rows.append({**lead, "last": lead.get("updated_at", ""), "runs": [lead["lead_run"]] if lead.get("lead_run") else []})
    if AUTOPILOT_REF:
        for item in list(AUTOPILOT_REF.inflight.values()):
            match = next((r for r in rows if r.get("name") == item["who"]), None)
            if match:
                match["live_stage"] = item["stage"]
            else:
                rows.append({"key": item["key"], "name": item["who"], "stage": item["stage"],
                             "live_stage": item["stage"], "last": item["since"], "runs": []})
    return sorted(rows, key=lambda r: r.get("last") or "", reverse=True)


def integrations(force: bool = False) -> list[dict]:
    """A real call to each app, cached for a minute - a green dot that was never checked is a lie."""
    if not force and time.time() - _integrations["at"] < 60:
        return _integrations["data"]
    import os

    from warmpath import llm
    from warmpath.integrations import google, slack, unipile

    def check(name, app, via, used_for, fn):
        t = time.perf_counter()
        try:
            detail = fn()
            return {"name": name, "app": app, "via": via, "used_for": used_for, "ok": True,
                    "detail": detail, "ms": int((time.perf_counter() - t) * 1000)}
        except Exception as exc:  # noqa: BLE001
            return {"name": name, "app": app, "via": via, "used_for": used_for, "ok": False,
                    "detail": f"{type(exc).__name__}: {str(exc)[:140]}", "ms": int((time.perf_counter() - t) * 1000)}

    def linkedin():
        me = unipile._call("GET", "users/me")
        return f"connected as {me.get('first_name', '')} {me.get('last_name', '')}".strip()

    def gmail():
        if google.gmail_via_composio():
            google._composio("GMAIL_FETCH_EMAILS", {"query": "in:inbox", "max_results": 1, "ids_only": True})
            return f"Composio · {os.environ.get('COMPOSIO_USER_ID')}"
        box = google._imap()
        box.logout()
        return f"IMAP · {os.environ.get('GMAIL_USER')}"

    def calendar():
        if not google.composio_ready():
            raise RuntimeError("not connected - connect Google Calendar in Composio and set COMPOSIO_USER_ID")
        google._composio("GOOGLECALENDAR_LIST_CALENDARS", {})
        return f"Composio · {os.environ.get('COMPOSIO_USER_ID')}"

    def slack_check():
        d = slack._slack("auth.test")
        return f"bot @{d.get('user')} in {d.get('team')}"

    def database():
        from warmpath import store
        st = store.store()
        st.select("leads", limit=1)
        return f"{st.backend} · {len(st.select('runs', limit=1000))} runs stored"

    def model():
        llm._call('Reply with the JSON object {"ok": true}.', "ping - answer in json")
        return llm.MODEL

    specs = [
        ("LinkedIn", "linkedin", "Unipile", "Read post comments, profiles, company sites; reply under a comment", linkedin),
        ("Gmail", "gmail", "Composio / IMAP", "Relationship evidence, send, read replies", gmail),
        ("Google Calendar", "calendar", "Composio", "Past meetings, free slots, create the event", calendar),
        ("Slack", "slack", "Web API", "Evidence cards, approvals, booking alerts", slack_check),
        ("Model", "model", "DeepSeek", "Intent, drafts, reading replies, Alex's routing", model),
        ("Database", "database", "Supabase / SQLite", "Leads, traces, ledger, chat, audit issues, eval results", database),
    ]
    # All at once, each with a time limit - one slow app must not make the page look dead.
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
    pool = ThreadPoolExecutor(max_workers=len(specs))
    futures = [(spec, pool.submit(check, *spec)) for spec in specs]
    data = []
    for (name, app, via, used_for, _), fut in futures:
        try:
            data.append(fut.result(timeout=12))
        except FutureTimeout:
            data.append({"name": name, "app": app, "via": via, "used_for": used_for, "ok": False,
                         "detail": "no answer within 12s", "ms": 12000})
    pool.shutdown(wait=False)
    _integrations.update(at=time.time(), data=data)
    return data


def _access_key() -> str:
    import os
    return os.environ.get("WARMPATH_ACCESS_KEY", "").strip()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        """With WARMPATH_ACCESS_KEY set, every page and API call needs it - this console can email people
        and post on LinkedIn, so a public URL without a key would hand both to anyone who finds it."""
        key = _access_key()
        if not key:
            return True
        import hmac
        from http.cookies import SimpleCookie
        cookie = SimpleCookie(self.headers.get("Cookie") or "")
        given = (cookie["wp_key"].value if "wp_key" in cookie else "") or self.headers.get("X-Access-Key", "")
        return hmac.compare_digest(given, key)

    def _login_page(self, error: str = "") -> None:
        html = (UI / "login.html").read_text(encoding="utf-8").replace("{{error}}", error)
        self._send(401 if error else 200, html.encode(), "text/html; charset=utf-8")

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, default=str).encode(), "application/json")

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) or b"{}"
        path = self.path.split("?")[0]
        if path == "/login":
            import hmac
            from urllib.parse import parse_qs
            given = (parse_qs(raw.decode(errors="replace")).get("key") or [""])[0].strip()
            if _access_key() and hmac.compare_digest(given, _access_key()):
                return self._send(303, b"", "text/plain", {
                    "Location": "/", "Set-Cookie": f"wp_key={given}; Path=/; HttpOnly; SameSite=Lax; Secure; Max-Age=604800"})
            return self._login_page("That key did not match.")
        if not self._authorized():
            return self._json({"error": "access key required"}, 401)
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            return self._json({"error": "bad json"}, 400)
        if path == "/api/chat":
            text = str(body.get("text") or "").strip()[:2000]
            if text:
                chat_handle(CHAT, text)
            return self._json({"ok": True})
        if path == "/api/approve":
            decision = {"send": "approved", "review": "review", "ignore": "rejected"}.get(body.get("decision"))
            if not decision or not body.get("run_id"):
                return self._json({"error": "run_id and decision (send|review|ignore) required"}, 400)
            CHAT.decide(str(body["run_id"]), decision)
            return self._json({"ok": True})
        if path == "/api/chat/clear":
            CHAT.clear()
            return self._json({"ok": True})
        if path == "/api/autopilot" and AUTOPILOT_REF:
            if "enabled" in body:
                AUTOPILOT_REF.set_enabled(bool(body["enabled"]))
            if body.get("tick"):
                AUTOPILOT_REF.poke()
            return self._json(AUTOPILOT_REF.snapshot())
        self._json({"error": "not found"}, 404)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/health":
            return self._json({"ok": True})
        if not self._authorized():
            if path.startswith("/api/"):
                return self._json({"error": "access key required"}, 401)
            if not path.startswith("/agents/"):
                return self._login_page()
        if path == "/api/chat":
            from warmpath import chat as chat_mod
            return self._json({"messages": CHAT.messages(), "mode": mode(), "busy": chat_mod._busy.locked()})
        if path == "/api/autopilot":
            return self._json(AUTOPILOT_REF.snapshot() if AUTOPILOT_REF else {"enabled": False})
        if path == "/api/leads":
            return self._json({"leads": leads_view()})
        if path == "/api/integrations":
            return self._json({"integrations": integrations("force" in self.path)})
        if path == "/api/state":
            return self._send(200, json.dumps(state(), default=str).encode(), "application/json")
        if path.startswith("/api/run/"):
            from warmpath import store
            row = store.store().get("runs", Path(path).name)
            if row:
                run = Run.from_dict(row)
                body = asdict(run) | {"violations": [asdict(v) | {"agent": v.agent} for v in audit.audit_run(run)]}
                return self._send(200, json.dumps(body, default=str).encode(), "application/json")
            return self._send(404, b"{}", "application/json")
        target = UI / ("index.html" if path in ("/", "") else path.lstrip("/"))
        if target.resolve().is_relative_to(UI.resolve()) and target.is_file():
            ctype = {".js": "application/javascript", ".css": "text/css", ".webp": "image/webp",
                     ".html": "text/html; charset=utf-8"}.get(target.suffix) or                 mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            return self._send(200, target.read_bytes(), ctype)
        self._send(404, b"not found", "text/plain")

    def log_message(self, *args) -> None:
        pass


def serve(port: int | None = None) -> None:
    """Locally: 127.0.0.1:8765. On a host (Render sets PORT): 0.0.0.0:$PORT, and only with an access key.

    Exactly one process should run the heartbeat against a shared database. WARMPATH_AUTOPILOT_RUN=0
    makes this process a console only - it still shows cards and takes approvals, because those
    live in the database, while the deployed service does the watching.
    """
    import os
    global AUTOPILOT_REF
    from warmpath import autopilot
    port = port or int(os.environ.get("PORT", "8765"))
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    if host != "127.0.0.1" and not _access_key():
        raise SystemExit("Refusing to serve publicly without WARMPATH_ACCESS_KEY: this console can send email and post on LinkedIn.")
    if os.environ.get("WARMPATH_AUTOPILOT_RUN", "1") == "1":
        # A card still "pending" belongs to a worker that died with the previous process - nothing
        # is waiting on it any more, so it must not keep a live-looking Send button.
        for msg in CHAT.s.select("chat_messages", {"kind": "approval"}, order="id", desc=True, limit=50):
            if (msg.get("payload") or {}).get("status") == "pending":
                CHAT.update(msg["id"], status="expired")
        AUTOPILOT_REF = autopilot.AUTOPILOT = autopilot.Autopilot(CHAT).start()
    beat = f"every {AUTOPILOT_REF.interval_s}s" if AUTOPILOT_REF else "off (console only)"
    print(f"WarmPath console on http://{host}:{port} · autopilot {beat} · mode {mode()}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else None)
