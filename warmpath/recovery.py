"""Durable recovery records. Unknown provider outcomes never auto-retry."""
from datetime import datetime, timedelta, timezone
from .entities import put, rows
from .core import mode


def record(ledger, run, action, kind, error, *, retryable=False, payload=None):
    ident = f'recovery:{run.mode}:{action}'
    old = next((r for r in rows(ledger.s, f'recovery:{run.mode}:') if r['id'] == ident), {})
    attempts = old.get('retry_count', 0) + 1
    retryable = retryable and attempts < 5
    row = dict(id=ident, action_id=action, run_id=run.run_id, kind=kind, mode=run.mode,
               status='retry' if retryable else 'review', retry_count=attempts,
               next_retry_at=(datetime.now(timezone.utc) + timedelta(seconds=min(60 * 2 ** (attempts-1), 3600))).isoformat(),
               error=str(error)[:300], payload=payload or {}, created_at=datetime.now(timezone.utc).isoformat())
    ledger.s.update('action_ledger', action, {'status': 'failed' if retryable else 'unknown', 'detail': str(error)[:500]})
    put(ledger.s, row)
    return row


def complete(storage, action):
    for row in rows(storage, f'recovery:{mode()}:'):
        if row['action_id'] == action:
            row['status'] = 'resolved'
            put(storage, row)


def due(storage, action):
    row = next((r for r in rows(storage, f'recovery:{mode()}:') if r['action_id'] == action), None)
    return not row or row['status'] == 'retry' and datetime.fromisoformat(row['next_retry_at']) <= datetime.now(timezone.utc)


def reconcile(storage, ident, outcome, event_id=''):
    if outcome not in ('performed', 'not_performed'):
        raise ValueError('Confirm performed or not_performed after checking the provider')
    row = next((r for r in rows(storage, f'recovery:{mode()}:') if r['id'] == ident), None)
    if not row or row['status'] != 'review':
        raise ValueError('Recovery item is not awaiting review')
    if outcome == 'performed' and row['kind'] == 'meeting':
        if not event_id.strip() or not row['payload'].get('lead_key'):
            raise ValueError('Calendar reconciliation requires the verified event ID and saved lead context')
        from .core import Leads
        leads = object.__new__(Leads)
        leads.s = storage
        leads.save(row['payload']['lead_key'], {'pending_booking': '', 'status': 'meeting_booked',
                   'meeting': row['payload']['start_local'], 'stage': 'Meeting verified in calendar',
                   'verified_event_id': event_id.strip()})
    storage.update('action_ledger', row['action_id'], {'status': 'done' if outcome == 'performed' else 'failed',
                                                     'detail': 'Founder verified ' + outcome + ' ' + event_id[:200]})
    row.update(status='resolved' if outcome == 'performed' else 'retry', next_retry_at=datetime.now(timezone.utc).isoformat(), reviewed_by='founder')
    put(storage, row)


def retry_notifications(ledger, notify):
    for row in rows(ledger.s, f'recovery:{mode()}:'):
        if row['kind'] != 'notification' or row['status'] != 'retry' or not due(ledger.s, row['action_id']):
            continue
        if not ledger.begin(row['action_id'], row['run_id'], 'notification'):
            continue
        from .core import Run
        run = Run.start(row['action_id'], kind='notification')
        try:
            notify(row['payload']['text'])
        except Exception as exc:
            record(ledger, run, row['action_id'], 'notification', exc, retryable=mode() != 'live', payload=row['payload'])
        else:
            ledger.done(row['action_id'])
            complete(ledger.s, row['action_id'])
