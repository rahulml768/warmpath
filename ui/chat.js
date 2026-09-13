/* WarmPath console: chat with Alex. Every agent reports back as a card drawn from the run's trace. */

let lastSig = "", stickBottom = true, workingSince = null;
const CHIPS = ["I want to grow - get me more leads", "Watch the demo post", "Any replies? Book meetings if they said yes",
               "How did the team do? Any rules broken?", "Who's on the team?"];
document.getElementById("chips").innerHTML = CHIPS.map(c => `<button type="button" class="chip">${esc(c)}</button>`).join("");

const scroller = document.getElementById("chat-scroll");
scroller.addEventListener("scroll", () => { stickBottom = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 120; });

function sentencesHtml(p) {
  return (p.sentences || []).map(s => `<div class="sent">${esc(s.text)}${(s.evidence || []).map(id =>
    `<span class="cite" title="${esc((p.evidence || {})[id] || "missing evidence")}">${esc(id)}</span>`).join("")}</div>`).join("");
}

function cardHtml(m) {
  const p = m.payload || {};
  switch (m.kind) {
    case "text":
      return `<div class="bubble">${esc(m.text)}</div>`;
    case "delegation":
      return `<div class="deleg">${face("Alex")}<span>${esc(m.text)}</span>${p.to ? `<span class="arrow">→</span>${face(p.to)}` : ""}${p.heartbeat ? '<span class="heartbeat">autopilot</span>' : ""}</div>`;
    case "comments":
      return `<div class="card"><h4>${esc(m.text)} <span class="pill p-mute">${esc(p.source)}</span></h4>${(p.comments || []).map(c =>
        `<div class="comment"><b>${esc(c.name)}</b> <span class="sub" style="font-size:12px;color:var(--muted)">${esc(c.headline || "")}</span><div>“${esc(c.text)}”</div></div>`).join("")}</div>`;
    case "intent": {
      const cls = p.intent === "LEAD" ? "p-ok" : p.intent === "UNCERTAIN" ? "p-warn" : p.intent === "OPT_OUT" ? "p-bad" : "p-mute";
      const label = {LEAD: "Buying intent", NOT_LEAD: "Not a lead", UNCERTAIN: "Unclear", OPT_OUT: "Opted out"}[p.intent] || p.intent;
      return `<div class="card"><h4>${esc(p.name)} <span class="sub">${esc(p.headline || "")}</span></h4>
        <div>“${esc(p.comment)}”</div>
        <div class="btns"><span class="pill ${cls}">${esc(label)} · ${p.confidence != null ? Number(p.confidence).toFixed(2) : ""}</span>
        ${(p.signals || []).map(x => `<span class="pill p-acc">“${esc(x)}”</span>`).join("")}</div></div>`;
    }
    case "relationship": {
      const warm = (p.people || [])[0];
      const kind = {inbound: "↘ they wrote to you", outbound: "↗ you wrote to them", meeting: "📅 you met"};
      const when = s => { const d = new Date(s); return isNaN(d) ? "" : d.toLocaleDateString(undefined, {day: "numeric", month: "short", year: "numeric"}); };
      const network = {FIRST_DEGREE: "1st-degree connection", SECOND_DEGREE: "2nd-degree", THIRD_DEGREE: "3rd-degree", OUT_OF_NETWORK: "not connected"}[p.network] || "";
      const step = (label, value, source) => `<div class="mem-step"><span class="mem-k">${label}</span><span>${value}</span>${source ? `<span class="pill p-mute">${esc(source)}</span>` : ""}</div>`;
      const notUsed = [
        p.weak ? `${p.weak} one-way or stale contact(s) - one-way isn't a relationship` : "",
        p.desks_ignored ? `${p.desks_ignored} shared inbox(es) like hr@ or sales@ - a desk isn't a person` : "",
        p.refused ? esc(p.refused) : "",
        ...(p.collisions || []).map(c => `name trap: ${esc(c)}`),
        "Company matched by domain, never by name",
      ].filter(Boolean);
      return `<div class="card mem"><h4>${p.warm ? "You already know this company" : "No prior relationship"}
          ${p.warm ? '<span class="pill p-acc">warm account</span>' : '<span class="pill p-mute">cold lead</span>'}</h4>

        <div class="mem-h">How I know who this is</div>
        ${step("LinkedIn", `${esc(p.name || "")}${network ? ` · ${network}` : ""}`, "")}
        ${step("Email", p.verified ? `<b>${esc(p.email)}</b>` : `<span class="pill p-warn">none verified</span> I won't guess one`, p.verified ? p.email_source : "")}
        ${step("Company", p.domain ? `${esc(p.company || "")} · <b>${esc(p.domain)}</b>` : esc(p.company || "unknown"), p.domain ? p.domain_source : "")}

        <div class="mem-h">Your history with ${esc(p.domain || "them")}</div>
        ${warm ? `${(p.timeline || []).map(t => `<div class="tl"><span class="tl-k">${kind[t.channel] || esc(t.channel)}</span><span class="tl-s">${esc(t.subject || "(no subject)")}</span><span class="tl-d">${when(t.at)}</span></div>`).join("")}
            <div class="mem-why"><b>${esc(warm.name || warm.email)}</b> · ${esc(warm.evidence)} · <span class="pill p-acc">${esc(warm.label)}</span>
            ${warm.mutual ? "<span class=\"pill p-ok\">two-way, so it counts</span>" : ""}</div>`
          : `<div class="mem-why">Searched Gmail and Calendar${p.domain ? ` for ${esc(p.domain)}` : ""}: ${p.records_found || 0} message(s), no two-way contact. Casey will write cold and never imply you've spoken.</div>`}

        <div class="mem-h">Account memory</div>
        ${(p.holds || []).length ? p.holds.map(h => `<div class="viol">Hold: ${esc(h)}</div>`).join("") : ""}
        ${(p.memory || []).length ? p.memory.map(m => `<div class="mem-step"><span class="pill p-acc">${esc((m.kind || "").replaceAll("_", " "))}</span><span>${esc(m.text)}</span><span class="pill p-mute">${esc(m.scope)} · ${esc(m.source)}</span></div>`).join("")
          : `<div class="mem-why">No do-not-contact rule, follow-up hold or note for this person or company.</div>`}

        <details><summary>Not used, and why</summary><ul>${notUsed.map(x => `<li>${x}</li>`).join("")}</ul></details>
      </div>`;
    }
    case "draft":
      return `<div class="card"><h4>${p.channel === "introduction" ? "Introduction request to your known contact" : p.channel === "email" ? "Email draft" : "LinkedIn reply draft"}
          ${p.passed ? `<span class="pill p-ok">every claim backed${p.attempts > 1 ? " · rewrote once" : ""}</span>` : '<span class="pill p-bad">claims failed · not sent</span>'}</h4>
        ${sentencesHtml(p)}
        <div style="font-size:11.5px;color:var(--muted);margin-top:6px">Hover a tag to see the evidence behind each sentence.</div>
        ${(p.problems || []).length ? `<details><summary>${p.problems.length} problem(s) caught before you saw it</summary><ul>${p.problems.map(x => `<li>${esc(x)}</li>`).join("")}</ul></details>` : ""}</div>`;
    case "approval": {
      const done = p.status && p.status !== "pending";
      const label = {approved: "Approved", rejected: "Ignored", review: "Held for your review", timeout: "No answer · nothing sent",
                     expired: "Expired · a fresh card was posted below"}[p.status] || "";
      return `<div class="card ${done ? "" : "approve"}"><h4>Send it?</h4>
        <div style="white-space:pre-wrap;margin-bottom:12px">${esc(p.card || "")}</div>
        <div style="color:var(--soft);font-size:13px">Approve here or reply <b>send</b> in Slack. First answer wins; silence sends nothing.</div>
        ${done ? `<div class="btns"><span class="pill ${p.status === "approved" ? "p-ok" : "p-mute"}">${label}${p.via ? " · via " + esc(p.via) : ""}</span></div>`
               : `<div class="btns"><button class="btn primary" data-approve="send" data-run="${esc(p.run_id)}">Send</button>
                   <button class="btn" data-approve="review" data-run="${esc(p.run_id)}">Review</button>
                   <button class="btn" data-approve="ignore" data-run="${esc(p.run_id)}">Ignore</button></div>`}</div>`;
    }
    case "result": {
      const what = {"gmail.send": `Email ${p.simulated ? "sent (dry run)" : "sent"} to ${esc(p.to)}`,
                    "linkedin.reply": `Replied under the comment${p.simulated ? " (dry run)" : ""}`,
                    "linkedin.post": `Post ${p.simulated ? "ready (dry run - not published)" : "published on LinkedIn"}`,
                    "calendar.create": `Meeting on the calendar${p.simulated ? " (dry run)" : ""}`}[p.action];
      return `<div class="card ${p.action ? "success" : ""}"><div class="row">${statePill(p.state)}
          ${p.reason_code ? `<span class="pill p-mute">${esc(p.reason_code)}</span>` : ""}
          ${(p.violations || []).length ? `<span class="pill p-bad">${p.violations.length} rule(s) broken</span>` : '<span class="pill p-ok">audit clean</span>'}
          <button class="btn" data-trace="${esc(p.run_id)}" style="margin-left:auto;padding:3px 10px">Trace</button></div>
        ${what ? `<div style="margin-top:6px;color:var(--ink)">${what}</div>` : ""}
        ${p.route && p.route.connector ? `<div class="kv" style="margin-top:12px"><div>Chosen path</div><div>You → ${esc(p.route.connector)} → ${esc(p.route.prospect)}<br><small>The contact-to-prospect connection is unconfirmed; the request asks whether they can help.</small></div><div>Why this contact</div><div>${esc(p.route.evidence)}</div></div>` : ""}
        ${(p.memory_holds || []).length ? `<div style="margin-top:12px"><b>Remembered before acting</b>${p.memory_holds.map(h => `<div class="sent">${esc(h.text)}<br><small>Source: ${esc(h.source)}</small></div>`).join('')}</div>` : ""}
        ${!p.action && (p.evidence || []).length ? `<div style="color:var(--body);font-size:13px;margin-top:6px">Why: ${p.evidence.map(esc).join(" · ")}</div>` : ""}</div>`;
    }
    case "reply":
      return `<div class="card"><h4>${esc(m.text)}</h4><div>“${esc(p.body)}”</div></div>`;
    case "meeting_read":
      return `<div class="card"><h4>Wants a meeting: ${esc(p.wants)}</h4><div class="kv">
        <div>Accepted</div><div>${esc(p.offered_slot_id || "none of the times we offered")}</div>
        <div>They said</div><div>${esc(p.day_key || "-")} ${esc(p.time_24h || "")} ${esc(p.timezone_stated || "")}</div></div></div>`;
    case "post_draft":
      return `<div class="card"><h4>LinkedIn post draft ${(p.problems || []).length && !(p.sentences || []).length ? '<span class="pill p-bad">claims failed</span>' : '<span class="pill p-ok">every product claim cites your brief</span>'}</h4>
        ${sentencesHtml(p)}
        <div style="font-size:11.5px;color:var(--muted);margin-top:6px">Public, under your name - it waits for your approval. Hover a tag to see the brief fact.</div>
        ${(p.problems || []).length ? `<details><summary>${p.problems.length} problem(s) caught and rewritten</summary><ul>${p.problems.map(x => `<li>${esc(x)}</li>`).join("")}</ul></details>` : ""}</div>`;
    case "offer": {
      const w = p.window || {};
      return `<div class="card"><h4>Free times on your calendar ${p.checked ? '<span class="pill p-ok">checked live</span>' : '<span class="pill p-warn">calendar not checked</span>'}</h4>
        ${w.from_their_reply ? `<div style="color:var(--soft);font-size:13px;margin-bottom:6px">Inside the window they gave: ${esc(w.earliest)} to ${esc(w.latest)}</div>` : ""}
        ${Object.values(p.slots || {}).map(s => `<div class="sent">${esc(s.label)}</div>`).join("") || '<div class="viol">No free half-hour in that window.</div>'}</div>`;
    }
    case "meeting":
      return `<div class="card success"><div style="white-space:pre-line">${esc(m.text).replace(/\*/g, "")}</div></div>`;
    case "audit": {
      const issues = Object.entries(p.issues || {});
      return `<div class="bubble">${esc(m.text)}</div>${issues.length ? `<div class="card" style="margin-top:8px">${issues.map(([k, i]) =>
        `<div>${face(i.agent)} <b>${esc(k)}</b> ×${i.count}<div class="viol">${(i.examples || []).map(esc).join("<br>")}</div></div>`).join("")}</div>` : ""}`;
    }
    default:
      return `<div class="bubble">${esc(m.text || m.kind)}</div>`;
  }
}

function hero() {
  return `<div class="hero"><img src="agents/alex.webp" alt="">
    <h1>Chat with Alex</h1><p>Orchestrator · runs your outreach team</p>
    <p class="hint">Send a LinkedIn post. Quinn reads the comments, Jordan checks who you already know there, Casey writes follow-ups where every sentence cites its source, and Morgan books the meeting when they say yes. Nothing goes out until you approve it.</p>
    <div class="team">${["Quinn", "Jordan", "Casey", "Morgan"].map(n => face(n, "av")).join("")}</div></div>`;
}

function typing(busy) {
  if (!busy) { workingSince = null; return ""; }
  workingSince = workingSince || Date.now();
  return `<div class="typing" id="typing">${face("Alex", "av")}<div class="bubble"><span class="dots"><i></i><i></i><i></i></span><small>The team is working…</small><small class="t" id="elapsed">0.0s</small></div></div>`;
}
setInterval(() => { const el = document.getElementById("elapsed"); if (el && workingSince) el.textContent = ((Date.now() - workingSince) / 1000).toFixed(1) + "s"; }, 100);

function renderChat(msgs, busy) {
  const waitingOnYou = msgs.some(m => m.kind === "approval" && m.payload.status === "pending");
  const sig = JSON.stringify([msgs.map(m => [m.id, m.payload && m.payload.status]), busy && !waitingOnYou]);
  const nav = document.getElementById("nav-pending");
  nav.hidden = !waitingOnYou; nav.textContent = "1";
  if (sig === lastSig) return;
  lastSig = sig;
  document.getElementById("thread").innerHTML = (msgs.length ? msgs.map(m => m.author === "you"
    ? `<div class="msg you"><div class="bubble">${esc(m.text)}</div></div>`
    : `<div class="msg">${face(m.author, "av") || "<span></span>"}<div class="msg-body"><div class="by"><b>${esc(m.author)}</b> · ${esc(ROLE[m.author] || "")}</div>${cardHtml(m)}</div></div>`).join("")
    : hero()) + typing(busy && !waitingOnYou);
  document.getElementById("chips").hidden = msgs.length > 0 && busy;
  if (stickBottom) scroller.scrollTop = scroller.scrollHeight;
}

async function loadChat() {
  try { const d = await (await fetch("/api/chat")).json(); renderChat(d.messages, d.busy); } catch (e) { /* server restarting */ }
}
async function sendChat(text) {
  if (!text.trim()) return;
  stickBottom = true;
  await fetch("/api/chat", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({text})});
  loadChat();
}

const input = document.getElementById("input");
input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 200) + "px"; });
input.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); document.getElementById("composer").requestSubmit(); } });
document.getElementById("composer").addEventListener("submit", e => { e.preventDefault(); sendChat(input.value); input.value = ""; input.style.height = "auto"; });
document.getElementById("clear").onclick = async () => { await fetch("/api/chat/clear", {method: "POST", body: "{}"}); lastSig = ""; loadChat(); };

document.addEventListener("click", async ev => {
  const chip = ev.target.closest(".chip");
  if (chip) { sendChat(chip.textContent); return; }
  const a = ev.target.closest("[data-approve]");
  if (a) {
    a.parentElement.querySelectorAll("button").forEach(b => b.disabled = true);
    await fetch("/api/approve", {method: "POST", headers: {"Content-Type": "application/json"},
                                 body: JSON.stringify({run_id: a.dataset.run, decision: a.dataset.approve})});
    loadChat();
    return;
  }
  const tr = ev.target.closest("[data-trace]");
  if (tr) openRun(tr.dataset.trace);
});

show(location.hash.slice(1));
loadChat();
setInterval(() => { if (!document.hidden) loadChat(); }, 2500);
