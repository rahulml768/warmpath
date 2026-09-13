# WarmPath

**Live app:** https://warmpath-xzfg.onrender.com · **Access key:** `Etg1rcYCY9qT0iWGxo4dzXO_` · **Demo video:**(https://www.loom.com/share/f59694a6b64841ef8627e8f0dee77680)


**Someone shows buying intent on LinkedIn. WarmPath checks whether you already know their company, writes a message where every sentence cites what it read, asks you in Slack, and books the meeting only when they say yes.**

WarmPath doesn't automate outreach. It automates the judgment around outreach - and it proves that judgment, run by run.

```
LinkedIn comment ─► Quinn: buying intent? ─► Jordan: who, which company, do we already know them?
                                                        │ (Gmail + Calendar, matched by domain)
                         ┌──────────────────────────────┴───────────────────────────┐
                 existing relationship                                       no relationship
                         └──────────────► Casey: draft, every sentence cited ◄───────┘
                                                        │ claim check (deterministic)
                                            Alex: evidence card in Slack → you approve
                                                        │ ledger: at most once
                                    email (verified address)  or  LinkedIn reply (no address)
                                                        │
                            their reply ─► Morgan: accepted a time? free? ─► Calendar event ─► Slack ✅
```

Doing nothing is a successful run. Every run that doesn't act says why, with evidence.

## The silent failures

The failures that matter here never raise. Every API call returns success and the result is still wrong:

| What a naive agent does | Why nothing notices | What WarmPath does |
|---|---|---|
| Emails `john.smith@acme.com` - an address it pattern-guessed | SMTP accepts it | Emails only an address that appears in a named source (LinkedIn contact info, your contacts file). Otherwise it replies on LinkedIn and asks. |
| Opens with "I spoke with someone at your company" to **Acme Logistics** because Gmail had a thread with **Acme** | The email sends, reads well | Matches companies by **domain**, never by name. Free-mail, shared desks, relays and your own domain are never a "company". |
| Writes "great catching up last month" with no meeting on record | The draft looks personal | Every sentence cites evidence ids; prior contact must cite a relationship record; any figure, time or address must appear in a source. A failing draft never reaches you. |
| Emails `sales@contoso.com` as if it were a person | Delivered | A shared desk is a rota, not a relationship. |
| Books "Wednesday 11" in the wrong week or timezone | The event is created | Books only a slot the prospect accepted from real free times, with the day looked up from a supplied calendar. |
| Sends twice when a webhook is retried, or re-sends the email when only the calendar write failed | Each call succeeds | An action ledger keyed on the person/message: at most once. A retry resumes at the failed step. |

## The team

Five agents, each with written instructions. The instructions are not decoration - they are exactly what the auditor checks on every trace.

| | Agent | Job | Instructions the auditor enforces |
|---|---|---|---|
| ![](ui/agents/alex.webp) | **Alex** · Orchestrator | Moves each lead through the state machine; asks you in Slack | Every refusal has a reason and evidence · only legal state moves |
| ![](ui/agents/quinn.webp) | **Quinn** · Social Media Manager | Reads LinkedIn comments (Unipile), scores buying intent, posts approved replies | Outreach only on LEAD ≥ floor · public reply needs approval |
| ![](ui/agents/jordan.webp) | **Jordan** · SDR | Identity, company domain, relationship strength from Gmail + Calendar | Never guess an address · company by domain not name · no desks, free-mail or self |
| ![](ui/agents/casey.webp) | **Casey** · Outreach | Writes the message, proves every sentence, sends through the ledger | Send needs approval · every claim has evidence · one outreach per person · dry run touches nothing |
| ![](ui/agents/morgan.webp) | **Morgan** · Account Executive | Reads replies for meeting intent, offers real free slots, books | Book only an accepted time · one meeting per person · resume, never restart |

## Apps

### Persistent account and contact memory

Open **Leads → Account and contact memory** to record a do-not-contact rule, follow-up date,
active conversation, relationship note or writing preference. Use a verified email/LinkedIn
identity for contact scope, a company domain for account scope, or `founder` for founder scope.
Each record retains its source, creation date, expiry and whether it was inferred. Archive a
record and add a replacement to correct it; archiving preserves the original evidence.

New lead outreach and introduction requests recheck restrictions after approval, before sending.
Scheduling emails and calendar writes also check contact/account restrictions. Explicit opt-out
phrases in comments and reply bodies become persistent restrictions; model-classified comment
opt-outs are also retained as inferred restrictions. Detection is conservative, not exhaustive.
Active introductions hold additional outreach at the same company. Confirmed founder writing
preferences are supplied to Casey as instructions, not product facts.

Follow-up dates hold outreach until the date expires; they do not schedule an automatic send.
Expired records remain visible for review and any subsequent send still needs approval.
Relationship outcomes remain on the lead's introduction record. Identity matching uses known
email, profile URL and author ID; memory cannot link identities that have no shared identifier.
Dry-run and live records are isolated. Data uses the existing `settings` table in SQLite or
Supabase, so no migration is needed. This is a persistent pre-action check, not a distributed
transaction covering an external provider write.

### Approved introductions

For new leads, Jordan prefers a known company contact with reciprocal relationship evidence
when the founder does not already know the prospect. Alex shows the recipient, evidence and
exact introduction request in the app and Slack. Only approval sends the request; the prospect
is not emailed. Set `WARMPATH_INTRODUCTIONS=0` to use the existing direct outreach route.

Autopilot and the chat's reply check look for a response from that contact with the request's
unique subject marker. The Leads view shows the response for review: choose **Introduction
happened**, **Still waiting**, or **Declined**. These choices record the outcome; they do not send
prospect outreach or book a meeting. A connector agreeing to help is not prospect consent.
Subject matching is conservative and can miss a new thread without the marker.

Dry runs simulate the request and do not poll for its reply. Live requests retain the email
allowlist requirement: the connector must be allowed. Uncertain send outcomes are held for
manual mailbox reconciliation, not automatically retried. Introduction state uses the existing
lead data field in SQLite and Supabase; no migration is required.

| App | Through | Used for |
|---|---|---|
| LinkedIn | [Unipile](https://www.unipile.com/) | Read comments on a post, profile → current company → company website (domain), reply under a comment |
| Gmail | Composio (IMAP/SMTP fallback) | Relationship evidence (mail from/to a domain), send, read replies |
| Google Calendar | Composio | Past meetings as relationship evidence, free slots, create the event |
| Slack | Web API | Evidence card + approval by DM reply (`send` / `review` / `ignore`), booking notification |

Unipile reaches LinkedIn through the connected member's own session, not LinkedIn's official API (which has no endpoint for reading comments on a member's post). Volume is one post's comments.

## Reliability: three layers

1. **Deterministic tests** (`tests/`, 38, no network, no model): policy gates, identity rules, relationship engine, claim check, ledger idempotency, and the full pipeline end to end with the model stubbed - including a calendar 503 that resumes at booking only.
2. **Measured evaluation** (`evals/run_evals.py`, real model): the same comments through a naive first-version agent and through WarmPath, counting the silent failures above. Plus adversarial comments: opt-out, prompt injection, a shared desk, a company-name collision.
3. **Trace audit** (`warmpath/audit.py`): every run - live or eval - is checked after it finishes against every agent's instructions. Violations are grouped into issues per agent and each becomes a regression case in `evals/regressions/`.

Results: see [BRIEF.md](BRIEF.md) and `evals/last_report.json`.

## Run it

### Model failover

Set `CEREBRAS_API_KEY` in your local `.env` or deployment environment to enable Cerebras backup.
DeepSeek is tried first by default; set `WARMPATH_LLM_PRIMARY=cerebras` to reverse the order.
`CEREBRAS_MODEL` defaults to `gpt-oss-120b`. The adapter uses the official
[Cerebras chat-completions API](https://inference-docs.cerebras.ai/api-reference/chat-completions).

Missing credentials skip that provider. Quota, authentication and rate-limit errors switch immediately;
timeouts, server errors and malformed JSON also try the backup before a bounded retry round.
Each request has a 30-second timeout. Failed 4xx providers are skipped for the remainder of that call.
All configured providers failing produces an explicit error; it never invents an answer or bypasses
approval. The trace includes provider/model and sanitized attempt statuses. Replay uses recorded
responses and does not consume fallback credit.

Restart the process after updating `.env`. For Render, add the same Cerebras environment variables
to the deployed service; editing the local `.env` does not change a running remote deployment.

See [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) for canonical identity review, recovery,
isolated workflow replay, and the required Supabase `002_safe_ledger.sql` upgrade.

Python 3.11+, standard library only.

```bash
cp .env.example .env      # DeepSeek, Slack, Unipile, Composio (or Gmail app password)
python -m pytest tests -q
python evals/run_evals.py --runs 3

# dry run (default): nothing leaves the building
python run.py comments fixtures/eval_post.json --contacts fixtures/eval_contacts.json --approve auto
python run.py comments https://www.linkedin.com/posts/...-activity-7xxxxxxxxxxxxxxxxxx --contacts contacts.local.json
python run.py replies
python run.py audit
python run.py serve        # console on http://localhost:8765
```

`WARMPATH_MODE=live` enables real writes, and even then an email or invite goes only to addresses on `WARMPATH_LIVE_ALLOWLIST`. `--approve auto` is refused in live mode.

## Honest limits

- The evaluation mailbox is synthetic (`fixtures/`, reserved `.example` domains). The live demo uses test accounts with a seeded prior conversation - it is labelled as such.
- Intent and drafting are probabilistic and reported as measured rates, never as guarantees. The gates, ledger and audit are deterministic.
- Relationship strength weights (meeting > inbound > outbound, recency decay, reciprocity cap) are a judgment, ported from the founder's product Zavorik, and shown to you with the evidence behind them.
