/* WarmPath console: chat with Alex. Every agent reports back as a card drawn from the run's trace. */

let lastSig = "", stickBottom = true, workingSince = null;
const CHIPS = ["I want to grow - get me more leads", "Watch the demo post", "Any replies? Book meetings if they said yes",
               "How did the team do? Any rules broken?", "Who's on the team?"];
document.getElementById("chips").innerHTML = CHIPS.map(c => `<button type="button" class="chip">${esc(c)}</button>`).join("");

const scroller = document.getElementById("chat-scroll");
scroller.addEventListener("scroll", () => { stickBottom = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 120; });

/** The Slack card text, rendered the way Slack renders it instead of as raw *stars* and ```fences```. */
function slackMarkdown(text) {
  const parts = String(text).split("```");
  return parts.map((chunk, i) => {
    const safe = esc(chunk.trim());
    if (i % 2 === 1) return `<div class="sc-quote">${safe.replace(/\n/g, "<br>")}</div>`;
    return safe.replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*([^*\n]+)\*/g, "<b>$1</b>").replace(/\n/g, "<br>");
  }).join("");
}

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
      const isPost = p.run_kind === "post";
      const title = isPost ? `Publish this post on LinkedIn${p.page_author ? ` as ${esc(p.page_author)}` : ""}?` : "Send it?";
      return `<div class="card ${done ? "" : "approve"}"><h4>${title}</h4>
        ${isPost ? `<div style="color:var(--body);font-size:13.5px;margin-bottom:10px">The preview above is exactly what goes out. It's public, so it waits for you.</div>`
                 : `<div class="slackcard">${slackMarkdown(p.card || "")}</div>`}
        <div style="color:var(--soft);font-size:13px">Approve here, or reply <b>send</b> in the Slack card's thread. First answer wins; silence sends nothing.</div>
        ${done ? `<div class="btns"><span class="pill ${p.status === "approved" ? "p-ok" : "p-mute"}">${label}${p.via ? " · via " + esc(p.via) : ""}</span></div>`
               : `<div class="btns"><button class="btn primary" data-approve="send" data-run="${esc(p.run_id)}">${isPost ? "Publish" : "Send"}</button>
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
    case "post_draft": {
      const who = p.page_author || "Rahul Mittal";
      const failed = (p.problems || []).length && !(p.sentences || []).length;
      return `<div class="card"><h4>LinkedIn post preview ${failed ? '<span class="pill p-bad">claims failed · not sent</span>' : '<span class="pill p-ok">every product claim cites your brief</span>'}</h4>
        <div class="li-post">
          <div class="li-head">
            ${p.as_page ? `<div class="li-logo">${esc(who.charAt(0))}</div>` : `<img class="li-av" src="agents/human-founder.webp" alt="">`}
            <div><div class="li-name">${esc(who)}</div>
              <div class="li-sub">${p.as_page ? "Company page" : "Founder"} · Just now · 🌐</div></div>
            <span class="li-follow">${p.as_page ? "+ Follow" : ""}</span>
          </div>
          <div class="li-body">${(p.sentences || []).map(s => `<p>${esc(s.text)}${(s.evidence || []).map(id =>
              `<span class="cite" title="${esc((p.evidence || {})[id] || "missing evidence")}">${esc(id)}</span>`).join("")}</p>`).join("")}</div>
          <div class="li-bar"><span>👍 Like</span><span>💬 Comment</span><span>🔁 Repost</span><span>➤ Send</span></div>
        </div>
        <div style="font-size:11.5px;color:var(--muted);margin-top:8px">Hover a tag to see the brief fact behind that sentence. Tags are not published.</div>
        ${(p.problems || []).length ? `<details><summary>${p.problems.length} problem(s) caught and rewritten before you saw it</summary><ul>${p.problems.map(x => `<li>${esc(x)}</li>`).join("")}</ul></details>` : ""}</div>`;
    }
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

/* ── the relay: who has the work right now ─────────────────────────────── */
const seenIds = new Set();
let firstPaint = true;

/** The stages a run goes through, and which chat cards prove each one happened. */
function relayStages(runMsgs) {
  const kinds = new Set(runMsgs.map(m => m.kind));
  const approval = runMsgs.filter(m => m.kind === "approval").pop();
  const result = runMsgs.filter(m => m.kind === "result").pop();
  const decided = approval && approval.payload.status && approval.payload.status !== "pending";
  const you = {who: "you", label: "You approve", done: !!decided || (!!result && !approval), waiting: !!approval && !decided};
  const finish = {who: "done", label: result ? (result.payload.action ? "Sent" : "Held back") : "Done", done: !!result};
  if (kinds.has("post_draft")) return [{who: "Quinn", label: "Writes the post", done: true}, you, finish];
  if (kinds.has("reply") || kinds.has("meeting_read") || kinds.has("offer"))
    return [{who: "Morgan", label: "Reads the reply", done: kinds.has("meeting_read") || kinds.has("offer") || !!result},
            {who: "Casey", label: "Writes it", done: kinds.has("draft") || !!result}, you, finish];
  return [{who: "Quinn", label: "Buying intent?", done: kinds.has("intent")},
          {who: "Jordan", label: "Who do we know?", done: kinds.has("relationship") || (!!result && !kinds.has("relationship"))},
          {who: "Casey", label: "Proves every line", done: kinds.has("draft") || (!!result && !kinds.has("draft"))},
          you, finish];
}

function relayHtml(runMsgs) {
  const stages = relayStages(runMsgs);
  const active = stages.findIndex(s => !s.done);
  return `<div class="relay">${stages.map((s, i) => {
    const state = s.done ? "done" : s.waiting ? "waiting" : i === active ? "active" : "todo";
    const avatar = s.who === "you" ? `<img class="av" src="agents/human-founder.webp" alt="">`
                 : s.who === "done" ? `<span class="relay-end">${s.done ? "✓" : "•"}</span>` : face(s.who, "av");
    return `${i ? `<span class="relay-line ${stages[i - 1].done ? "lit" : ""}"></span>` : ""}
      <div class="relay-step ${state}">${avatar}<div class="relay-label"><b>${s.who === "you" ? "You" : s.who === "done" ? s.label : esc(s.who)}</b>
      <span>${s.who === "done" ? "" : esc(s.label)}</span></div>${s.done && s.who !== "done" ? '<span class="relay-tick">✓</span>' : ""}</div>`;
  }).join("")}</div>`;
}

function renderChat(msgs, busy) {
  const waitingOnYou = msgs.some(m => m.kind === "approval" && m.payload.status === "pending");
  const sig = JSON.stringify([msgs.map(m => [m.id, m.payload && m.payload.status]), busy && !waitingOnYou]);
  const nav = document.getElementById("nav-pending");
  nav.hidden = !waitingOnYou; nav.textContent = "1";
  if (sig === lastSig) return;
  lastSig = sig;
  const byRun = {};
  msgs.forEach(m => { const r = m.payload && m.payload.run_id; if (r) (byRun[r] = byRun[r] || []).push(m); });
  const relayShown = new Set();
  const html = msgs.map(m => {
    const fresh = !firstPaint && !seenIds.has(m.id);
    const run = m.payload && m.payload.run_id;
    let relay = "";
    if (run && !relayShown.has(run) && byRun[run].length > 1) {
      relayShown.add(run);
      relay = relayHtml(byRun[run]);
    }
    const body = m.author === "you"
      ? `<div class="msg you ${fresh ? "enter" : ""}"><div class="bubble">${esc(m.text)}</div></div>`
      : `<div class="msg ${fresh ? "enter" : ""}">${face(m.author, "av") || "<span></span>"}<div class="msg-body"><div class="by"><b>${esc(m.author)}</b> · ${esc(ROLE[m.author] || "")}</div>${cardHtml(m)}</div></div>`;
    return relay + body;
  }).join("");
  msgs.forEach(m => seenIds.add(m.id));
  firstPaint = false;
  document.getElementById("thread").innerHTML = (msgs.length ? html : hero()) + typing(busy && !waitingOnYou);
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
