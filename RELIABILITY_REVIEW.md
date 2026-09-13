# WarmPath reliability review — 14 September 2026

The workflow is implemented: Alex delegates post creation to Quinn, approved posts are watched,
Quinn classifies comments, Jordan resolves identity and relationships, Casey drafts outreach,
Alex requests approval, and Morgan offers times and books accepted meetings.
The current evidence does not establish complete test coverage or production reliability.

## Verified and fixed

Baseline: `python -m pytest tests -q` produced 47 passed, 1 skipped.
Added adversarial regressions in `tests/test_safety_gaps.py` for:

- Slack decisions scoped to the specific message thread and configured approver, with exact
  commands. `send later` cannot approve, and a reply to another card cannot authorize this one.
  **Usage change:** reply inside each Slack card's thread with `send`, `review`, or `ignore`.
- Whole email address matching: `notjohn@acme.example` and `john@acme.example.evil` cannot
  verify `john@acme.example`.
- Invalid/nonfinite confidence values and truthy non-boolean approvals fail closed.
- Ambiguous meeting acceptance requires review; unknown offered slots do not crash lookup.
- Calendar availability must cover the entire 30-minute meeting, not merely return a nonempty list.
- A competing booking retry can legally exit as BLOCKED_DUPLICATE.

No live messages, posts, invites, migrations, or paid model evaluations were executed in this review.
The existing evaluation report records 24/24 WarmPath scenario passes across eight comments
repeated three times. This is historical evidence, not a fresh evaluation of these changes.

## Remaining priorities

| Priority | Code and failure | Required improvement and acceptance test |
|---|---|---|
| P0 | `pipeline.py`: send exceptions mark actions failed even when the provider may have accepted a write before the response was lost | Introduce unknown-outcome reconciliation and provider idempotency where supported. Simulate accepted write followed by timeout; retry must not duplicate it. |
| P0 | `autopilot.py`: claims use separate read/upsert calls; startup resets every in-progress claim | Atomic claims with owner, lease, heartbeat and bounded workers. Test two processes, restart during approval, and one process restarting while another remains active. |
| P0 | `core.py`: `action_id` strips punctuation and truncates input; dry and live actions use the same keys | Plan a backward-compatible ledger migration with complete identity hashing and environment separation. Test colliding addresses and dry-run-to-live promotion without bypassing existing live ledger entries. |
| P0 | `pipeline.py`: saved pending bookings resume without rechecking availability; normal scheduling allows unchecked calendar results | Recheck availability on retry, require connected calendar for live scheduling, reject expired slots, test DST and conflicting bookings across different leads. |
| P1 | `pipeline.py`: slot-offer send failure lacks the ordinary send error handling; Slack notification failure still moves to NOTIFIED | Persist step outcomes and retry notification independently. Test send failure and notification failure after successful booking. |
| P1 | `integrations/google.py`: reply lookup uses sender/day rather than a verified conversation thread; Composio replies retain quoted text | Match exact sender, timestamp and thread; strip quoted history consistently. Test unrelated messages and quoted acceptance. |
| P1 | `autopilot.py`: failure message says nothing was sent even if a later persistence step failed | Report confirmed versus unknown outcomes from the ledger; inject failures after each external write. |
| P1 | Audit regression test is skipped because no saved cases exist; replay only checks the auditor | Add executable workflow fixtures and rerun the actual pipeline after fixes. Preserve bad traces separately to test detection. |
| P1 | No demonstrated Supabase parity, live connector contract, UI approval or crash-recovery suite | Add isolated staging checks, recorded provider fixtures, and browser approval scenarios before the tournament. |

These changes need a storage/recovery design and provider contract validation; they should not be
represented as solved by the current deterministic suite. In particular, the README's at-most-once
claim is stronger than the implementation can guarantee after ambiguous network failures.

## Comparison with Lemma

Assumption: “Lemma AI” means [uselemma.ai](https://www.uselemma.ai/), the agent monitoring product.
It describes instruction-based trace auditing, grouping recurring issues, Slack alerts and online
evaluations after a fix. WarmPath already has explicit agent instructions, traces and deterministic
audit rules. It does not demonstrate equivalent semantic detection of unexpected failures or a
complete ongoing evaluation loop. Merely having evidence IDs cannot prove semantic truth.

Lemma's [failure taxonomy](https://www.uselemma.ai/blog/a-taxonomy-of-agent-failures?v=1)
provides useful review categories: skipped work, out-of-scope work, instruction violations,
integration failures, retry loops, hallucinations and communication failures. Add scenarios for each,
including valid-looking API successes that produce the wrong business result.

Slack's [thread retrieval documentation](https://docs.slack.dev/reference/methods/conversations.replies/)
supports the new card-specific lookup. Validate the installed bot's DM history permissions in staging;
pagination and aggregate polling/rate limits still need coverage for large approval queues.

## Tournament demonstration

Show a single traceable journey: approved post → real buying-intent comment → sourced relationship
→ supported draft → specific Slack approval → one outreach → accepted free slot → calendar event.
Then show three failures handled visibly: prompt injection/opt-out, duplicate input, and calendar
failure with recovery. Display expected versus actual action, evidence, approval, and final outcome.

Prioritize recovery and observable correctness before adding more agents. A useful next feature is
a recovery inbox listing blocked work, unknown external outcomes and the precise action a human can
take. Report qualified leads, approved outreach, replies and meetings separately from agent activity.
Tournament placing cannot be predicted from code inspection; the judging rubric should guide the demo.
