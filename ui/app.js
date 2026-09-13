/* WarmPath console: shared helpers, navigation, pipeline, team, reliability, integrations, trace drawer. */

const AV = {Alex: "alex", Quinn: "quinn", Jordan: "jordan", Casey: "casey", Morgan: "morgan"};
const ROLE = {Alex: "Orchestrator", Quinn: "Social Media Manager", Jordan: "SDR", Casey: "Outreach", Morgan: "Account Executive"};
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const face = (n, cls = "av sm") => AV[n] ? `<img class="${cls}" src="agents/${AV[n]}.webp" alt="${esc(n)}" title="${esc(n)}">` : "";

function statePill(s) {
  const map = {
    AWAITING_REPLY: ["p-ok", "Email sent · awaiting reply"], COMMENT_REPLIED: ["p-blue", "Replied on LinkedIn"],
    POST_PUBLISHED: ["p-ok", "Post published"],
    NOTIFIED: ["p-ok", "Meeting booked"], MEETING_BOOKED: ["p-ok", "Meeting booked"], IGNORED: ["p-mute", "Ignored"],
    REVIEW_REQUIRED: ["p-warn", "Needs you"], BLOCKED: ["p-bad", "Blocked"], STOPPED: ["p-mute", "Stopped"],
    BLOCKED_DUPLICATE: ["p-acc", "Duplicate blocked"], CALENDAR_FAILED: ["p-warn", "Calendar failed · resumable"],
  };
  const [c, l] = map[s] || ["p-mute", s];
  return `<span class="pill ${c}">${esc(l)}</span>`;
}

/* ── navigation ────────────────────────────────────────────────────────── */
const VIEWS = ["chat", "leads", "pipeline", "team", "reliability", "integrations"];
function show(v) {
  if (!VIEWS.includes(v)) v = "chat";
  VIEWS.forEach(x => document.getElementById("view-" + x).classList.toggle("on", x === v));
  document.querySelectorAll(".nav a").forEach(a => a.classList.toggle("on", a.dataset.view === v));
  document.getElementById("side").classList.remove("open");
  document.getElementById("scrim-nav").classList.remove("on");
  if (v === "integrations") loadIntegrations(false);
  if (v === "chat") setTimeout(() => document.getElementById("input").focus(), 0);
}
window.addEventListener("hashchange", () => show(location.hash.slice(1)));
document.getElementById("open-nav").onclick = () => { document.getElementById("side").classList.add("open"); document.getElementById("scrim-nav").classList.add("on"); };
document.getElementById("close-nav").onclick = document.getElementById("scrim-nav").onclick = () => { document.getElementById("side").classList.remove("open"); document.getElementById("scrim-nav").classList.remove("on"); };

/* ── state: pipeline, team, reliability ────────────────────────────────── */
let tab = "all", data = null;

function render() {
  const d = data;
  if (!d) return;
  const live = d.mode === "live";
  document.getElementById("mode").innerHTML = `<b>${live ? "LIVE" : "DRY RUN"}</b>${live ? "Real sends, allow-listed test addresses only" : "Nothing leaves the building"}`;
  const pill = document.getElementById("mode-pill");
  pill.textContent = live ? "Live" : "Dry run";
  pill.classList.toggle("live", live);

  const runs = d.runs, real = runs.filter(r => !r.eval), count = f => runs.filter(f).length, v = d.audit.violations;
  const navViol = document.getElementById("nav-viol");
  navViol.hidden = !v; navViol.textContent = v;

  document.getElementById("stats").innerHTML = [
    [count(r => r.kind === "lead"), "comments judged"],
    [count(r => r.intent === "LEAD"), "buying intent"],
    [count(r => r.warm), "warm accounts found"],
    [count(r => ["AWAITING_REPLY", "COMMENT_REPLIED"].includes(r.state)), "messages approved"],
    [count(r => ["IGNORED", "BLOCKED", "REVIEW_REQUIRED", "STOPPED", "BLOCKED_DUPLICATE"].includes(r.state)), "chose not to act"],
    [count(r => r.state === "NOTIFIED"), "meetings booked"],
    [v, "rules broken", v ? "bad" : "good"],
  ].map(([n, l, c]) => `<div class="stat ${c || ""}"><div class="n">${n}</div><div class="l">${l}</div></div>`).join("");

  const byAgent = {};
  Object.values(d.audit.issues || {}).forEach(i => byAgent[i.agent] = (byAgent[i.agent] || 0) + i.count);
  document.getElementById("agents").innerHTML = d.agents.map(a => {
    const n = byAgent[a.name] || 0, rules = Object.values(a.instructions);
    return `<div class="agent"><div class="top">${face(a.name, "av lg")}<div><div class="name">${esc(a.name)}</div><div class="role">${esc(a.role)}</div></div></div>
      <div class="does">${esc(a.does)}</div>
      <div class="row"><span class="pill p-mute">${rules.length} rules</span><span class="pill ${n ? "p-bad" : "p-ok"}">${n ? n + " broken" : "0 broken"}</span></div>
      <details><summary>What the auditor checks</summary><ul>${rules.map(r => `<li>${esc(r)}</li>`).join("")}</ul></details></div>`;
  }).join("");

  const tabs = [["all", "All", runs.length], ["live", "Real runs", real.length], ["eval", "Evaluation", runs.length - real.length],
                ["act", "Acted", count(r => r.action)], ["held", "Held back", count(r => !r.action)]];
  document.getElementById("tabs").innerHTML = tabs.map(([k, l, n]) => `<button class="tab ${tab === k ? "on" : ""}" data-tab="${k}">${l} · ${n}</button>`).join("");
  const shown = runs.filter(r => tab === "all" || (tab === "live" && !r.eval) || (tab === "eval" && r.eval) || (tab === "act" && r.action) || (tab === "held" && !r.action));
  document.getElementById("runs").innerHTML = shown.length ? shown.slice(0, 100).map(r => `
    <button class="run" data-run="${esc(r.run_id)}">
      <div class="row"><span class="who">${esc(r.subject || "-")}</span><span class="co">${esc(r.company)}${r.domain ? " · " + esc(r.domain) : ""}</span>
        ${r.kind === "reply" ? '<span class="pill p-acc">reply</span>' : ""}${r.warm ? '<span class="pill p-acc">warm account</span>' : ""}${r.eval ? '<span class="pill p-mute">eval</span>' : ""}
        <span class="faces">${r.agents.map(a => face(a)).join("")}</span></div>
      ${r.comment ? `<span class="quote">“${esc(r.comment)}”</span>` : ""}
      <div class="row">${statePill(r.state)}${r.intent ? `<span class="pill p-mute">${esc(r.intent)} ${r.confidence != null ? Number(r.confidence).toFixed(2) : ""}</span>` : ""}
        ${r.reason_code ? `<span class="pill p-mute">${esc(r.reason_code)}</span>` : ""}${r.violations.length ? `<span class="pill p-bad">${r.violations.length} rule broken</span>` : ""}</div>
    </button>`).join("") : `<div class="empty">No runs yet. Ask Alex to check a post.</div>`;

  const issues = Object.entries(d.audit.issues || {});
  document.getElementById("audit").innerHTML = `
    <div class="issue"><div class="row"><b>${d.audit.runs} traces audited</b><span class="pill ${v ? "p-bad" : "p-ok"}">${v} violations</span><span class="pill p-mute">${d.regressions} regression cases</span></div></div>
    ${issues.length ? issues.map(([k, i]) => `<div class="issue">${face(i.agent)} <b>${esc(k)}</b> ×${i.count}<div style="color:var(--soft);font-size:12.5px">${esc(i.instruction)}</div>${i.examples.map(e => `<div class="viol">${esc(e)}</div>`).join("")}</div>`).join("")
      : `<div class="issue" style="color:var(--ok);font-weight:600">No instruction was broken.</div>`}`;

  const e = d.eval;
  document.getElementById("eval").innerHTML = !e ? `<div class="empty">Run <code>python evals/run_evals.py</code></div>` : `
    <table><tr><th>Silent failure</th><th>Naive</th><th>WarmPath</th></tr>
    ${Object.keys(e.silent_failures.naive).map(k => { const a = e.silent_failures.naive[k], b = e.silent_failures.warmpath[k] || 0;
      return `<tr><td>${esc(k.replaceAll("_", " "))}</td><td class="num"><span class="pill ${a ? "p-bad" : "p-ok"}">${a}/${e.attempts.naive}</span></td><td class="num"><span class="pill ${b ? "p-bad" : "p-ok"}">${b}/${e.attempts.warmpath}</span></td></tr>`; }).join("")}
    <tr><td>ended in the right state</td><td class="num">-</td><td class="num"><span class="pill p-ok">${e.scenario_pass[0]}/${e.scenario_pass[1]}</span></td></tr></table>
    <div style="color:var(--muted);font-size:12px;padding:10px 12px">Same comments, same model, same synthetic mailbox · ${e.runs_per_comment} runs per comment · every call returned success in both columns.</div>`;
}

async function loadState() {
  try { data = await (await fetch("/api/state")).json(); render(); } catch (e) { /* server restarting */ }
}

/* ── trace drawer ──────────────────────────────────────────────────────── */
async function openRun(id) {
  const r = await (await fetch("/api/run/" + encodeURIComponent(id))).json();
  const steps = r.steps.map(s => {
    const extra = Object.keys(s.data || {}).length ? `<details><summary>data</summary><pre>${esc(JSON.stringify(s.data, null, 2))}</pre></details>` : "";
    const out = s.output ? `<details><summary>output</summary><pre>${esc(s.output)}</pre></details>` : "";
    return `<div class="step">${face(s.agent) || "<span></span>"}<div>
      <div class="row"><code>${esc(s.tool)}</code>${s.agent ? `<span class="pill p-mute">${esc(s.agent)}</span>` : ""}
        ${s.decision ? `<span class="pill ${s.ok ? "p-acc" : "p-bad"}">${esc(s.decision)}${s.confidence != null ? " " + Number(s.confidence).toFixed(2) : ""}</span>` : ""}
        ${!s.ok ? '<span class="pill p-bad">failed</span>' : ""}<span style="color:var(--muted);font-size:11px;margin-left:auto">${s.ms} ms</span></div>
      ${s.input ? `<div style="color:var(--soft);font-size:12px;margin-top:4px">${esc(s.input)}</div>` : ""}
      ${s.error ? `<div class="viol">${esc(s.error)}</div>` : ""}${out}${extra}</div></div>`;
  }).join("");
  const dr = document.getElementById("drawer");
  dr.innerHTML = `<button class="btn" id="close" style="float:right">Close</button>
    <div style="color:var(--muted);font-size:12px">${esc(r.kind)} run · <code>${esc(r.run_id)}</code> · ${esc(r.mode)}</div>
    <h2>${esc(r.subject || r.message_id)}</h2>
    <div class="row">${statePill(r.state)}${r.reason_code ? `<span class="pill p-mute">${esc(r.reason_code)}</span>` : ""}</div>
    <div class="path">${r.states.map(s => `<span class="pill p-mute">${esc(s)}</span>`).join("→")}</div>
    ${r.evidence && r.evidence.length ? `<div class="say"><b>Why</b><ul style="margin:6px 0 0;padding-left:18px">${r.evidence.map(x => `<li>${esc(x)}</li>`).join("")}</ul></div>` : ""}
    ${r.violations.length ? r.violations.map(x => `<div class="viol"><b>${esc(x.agent)} · ${esc(x.invariant)}</b><br>${esc(x.detail)}</div>`).join("") : `<div class="say" style="color:var(--ok)">Audit: every agent followed its instructions on this run.</div>`}
    ${steps}`;
  dr.classList.add("on");
  document.getElementById("scrim").classList.add("on");
  document.getElementById("close").onclick = closeRun;
}
function closeRun() { document.getElementById("drawer").classList.remove("on"); document.getElementById("scrim").classList.remove("on"); }
document.getElementById("scrim").onclick = closeRun;
document.addEventListener("keydown", e => { if (e.key === "Escape") closeRun(); });

/* ── integrations ──────────────────────────────────────────────────────── */
async function loadIntegrations(force) {
  const box = document.getElementById("integrations");
  if (force || !box.innerHTML) box.innerHTML = `<div class="empty">Checking each app live - a few seconds…</div>`;
  const d = await (await fetch("/api/integrations" + (force ? "?force" : ""))).json();
  box.innerHTML = d.integrations.map(i => `<div class="integ">
    <div class="row" style="justify-content:space-between"><b><span class="dot" style="background:${i.ok ? "var(--ok)" : "var(--bad)"}"></span>${esc(i.name)}</b><span class="pill p-mute">${esc(i.via)}</span></div>
    <div style="color:var(--body);font-size:13px;margin:8px 0">${esc(i.used_for)}</div>
    <div style="font-size:12px;color:${i.ok ? "var(--ok)" : "var(--bad)"}">${esc(i.detail)}</div>
    <div style="font-size:11px;color:var(--muted);margin-top:4px">${i.ms} ms</div></div>`).join("");
}
document.getElementById("recheck").onclick = () => loadIntegrations(true);

document.addEventListener("click", ev => {
  const t = ev.target.closest("[data-tab]");
  if (t) { tab = t.dataset.tab; render(); return; }
  const r = ev.target.closest("[data-run]");
  if (r) openRun(r.dataset.run);
});

/* ── autopilot ─────────────────────────────────────────────────────────── */
const ago = iso => { if (!iso) return "never"; const s = Math.max(0, (Date.now() - new Date(iso)) / 1000); return s < 60 ? `${Math.round(s)}s ago` : `${Math.round(s / 60)}m ago`; };
let pilot = null;
function renderPilot() {
  const p = pilot;
  if (!p) return;
  const busy = (p.inflight || []).length;
  document.getElementById("pilot").innerHTML = `
    <div class="top"><b><span class="pulse ${p.enabled ? "" : "off"}"></span>Autopilot ${p.enabled ? "on" : "paused"}</b>
      <button class="switch" id="pilot-toggle">${p.enabled ? "Pause" : "Resume"}</button></div>
    <div class="line">Every ${p.interval_s}s · last check ${ago(p.last_tick)}</div>
    <div class="line">Watching ${(p.watches || []).length} post${(p.watches || []).length === 1 ? "" : "s"}${busy ? ` · ${busy} in progress` : ""}</div>
    ${p.last_error ? `<div class="line" style="color:#fca5a5" title="${esc(p.last_error)}">${esc(p.last_error)}</div>` : ""}`;
  document.getElementById("pilot-toggle").onclick = async () => {
    pilot = await (await fetch("/api/autopilot", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({enabled: !p.enabled})})).json();
    renderPilot();
  };
}
async function loadPilot() { try { pilot = await (await fetch("/api/autopilot")).json(); renderPilot(); } catch (e) { /* restarting */ } }

/* ── leads ─────────────────────────────────────────────────────────────── */
function stagePill(l) {
  const s = l.live_stage || l.stage || "";
  const cls = /booked/i.test(s) ? "p-ok" : /approval|needs you/i.test(s) ? "p-warn" : /offered|emailed|replied on/i.test(s) ? "p-blue"
            : /not a lead|not interested|stopped|blocked/i.test(s) ? "p-mute" : "p-acc";
  return `<span class="pill ${cls}">${esc(s)}</span>`;
}
async function loadLeads() {
  let rows = [];
  try { rows = (await (await fetch("/api/leads")).json()).leads; } catch (e) { return; }
  const count = f => rows.filter(f).length;
  document.getElementById("lead-stats").innerHTML = [
    [rows.length, "people"], [count(r => r.intent === "LEAD"), "buying intent"], [count(r => r.warm), "warm accounts"],
    [count(r => /emailed|offered/i.test(r.stage || "")), "in conversation"], [count(r => /approval/i.test(r.live_stage || "")), "waiting for you"],
    [count(r => /booked/i.test(r.stage || "")), "meetings booked"],
  ].map(([n, l]) => `<div class="stat"><div class="n">${n}</div><div class="l">${l}</div></div>`).join("");
  document.getElementById("leads").innerHTML = rows.length ? rows.map(r => `<tr>
      <td class="person"><b>${esc(r.name || "-")}</b><span>${esc(r.email || "no verified address")}</span>${r.comment ? `<div class="said">“${esc(r.comment)}”</div>` : ""}</td>
      <td>${esc(r.company || "-")}${r.domain ? `<div style="color:var(--muted);font-size:12px">${esc(r.domain)}</div>` : ""}</td>
      <td>${r.warm ? `<span class="pill p-acc">warm</span><div style="font-size:12px;color:var(--soft);margin-top:3px">knows ${esc(r.knows)}</div>` : r.intent ? '<span class="pill p-mute">cold</span>' : ""}</td>
      <td>${stagePill(r)}</td>
      <td>${r.meeting ? esc(r.meeting.replace("T", " ").slice(0, 16)) : "-"}</td>
      <td style="white-space:nowrap">${ago(r.last)}${(r.runs || []).length ? `<div><button class="btn" data-run="${esc(r.runs[r.runs.length - 1])}" style="padding:2px 8px;font-size:11px;margin-top:4px">Trace</button></div>` : ""}</td>
    </tr>`).join("") : `<tr><td colspan="6"><div class="empty">No leads yet. Give Alex a post to watch.</div></td></tr>`;
}
document.getElementById("tick-now").onclick = async () => { await fetch("/api/autopilot", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({tick: true})}); setTimeout(loadLeads, 1500); };

loadState();
loadPilot();
loadLeads();
setInterval(loadState, 3000);
setInterval(loadPilot, 3000);
setInterval(loadLeads, 4000);
