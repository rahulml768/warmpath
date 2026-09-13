# Identity, recovery and replay

## What changed

Canonical people are stored separately from lead activity, using exact verified email and LinkedIn
profile/author aliases. Names and company names do not auto-merge people. Matching aliases with changed
email or company facts produce a review item. Accepting updated facts retains the old aliases and source
history; multi-person collisions remain blocked rather than silently merging. The memory checker expands
canonical aliases, so a profile opt-out also applies when that person is later identified by email.
Lead records that acquire the same canonical identity are consolidated after processing.

The ledger only retries rows explicitly marked failed. Pending, done and unknown rows cannot be reclaimed
by the same run or after a timeout. Calendar failures retain the agreed time and a durable recovery record.
The heartbeat respects exponential backoff (starting at 60 seconds, capped at one hour) and stops automatic
retries after the configured five-attempt threshold in code. Retried bookings recheck availability. Live
write exceptions are treated conservatively as uncertain outcomes; a founder must check the provider.

Meeting creation and Slack notification have separate ledger entries. A Slack error returns MEETING_BOOKED
with NOTIFICATION_PENDING, and notification recovery does not repeat booking or outreach. The recovery panel
accepts explicit provider-outcome reconciliation. Confirming a calendar write requires its event ID; this
is a founder assertion, not an automatic provider verification.

New comment and reply runs capture inputs, starting lead/memory/ledger state, tool/model responses, model
name and a source fingerprint. Open a trace and choose **Replay with recorded responses**. A child process
with no provider credentials and blocked socket connections runs the current pipeline with a temporary
database. The report compares expected and actual state, reason, recipients and message sentences, and
lists current audit violations. Original live state is not modified. Old traces without captured inputs
fail explicitly. This is workflow replay, not model re-evaluation or execution of historical code; the
original version is represented by its recorded outcome. Time-dependent rules use the current clock.

## Deployment

SQLite uses the updated ledger rule immediately. For Supabase, apply
`supabase/migrations/002_safe_ledger.sql` after migration 001 **before using these guarantees in live mode**.
The new migration fixes both pending-action reclamation and the concurrent first-insert race in the old RPC.
The migration has been written but has not been applied to a remote database by this change.

Restart the app after updating. Under **Leads → Identity review and recovery**, inspect conflicts and
recovery tasks. Re-scan after resolving an identity; resolution does not send outreach. For a send marked
not performed, explicitly rerun the original workflow and approve it again. Meeting retries and notification
retries are handled by the heartbeat where their recovery status permits it.

## Remaining limits

- A process crash during a pending write stays held; automatic provider reconciliation is not implemented.
  Internal identity locks also fail closed after a crash and need operator inspection before release.
- Canonical registry updates are serialized, but legacy lead consolidation is not one cross-table transaction.
  Stable verified aliases are needed; name-only matches require manual investigation.
- The recovery panel does not reconstruct missing historical lead context from a provider after a crash.
- Live timeouts require review, even when they might actually be safe to retry. No exactly-once delivery claim
  is made for external providers, including Slack.
- Tests use isolated SQLite and mocked providers. Supabase RPC integration, real connector permissions and
  browser interaction still need staging validation. Existing action-key collision and broad account-worker
  concurrency limitations are not fully resolved by this change.
- Replay snapshots contain private inputs and retrieved evidence; they use the same protected run storage
  as normal traces and currently have no automatic retention policy.
