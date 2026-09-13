# WarmPath

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
