"""Canonical identities from exact aliases; changed facts require explicit review."""
import json
import uuid
from .core import Ledger, mode
from .memory import identities


def rows(storage, prefix):
    return [json.loads(r['value']) for r in storage.select('settings') if r['key'].startswith(prefix)]


def put(storage, row):
    storage.upsert('settings', {'key': row['id'], 'value': json.dumps(row)})


def resolve(run, leads, ident, comment):
    prefix = f'person:{run.mode}:'
    aliases = identities({**comment, **ident})
    if not ident.get('verified'):
        aliases.discard('email:' + ident.get('email', ''))
    lock = object.__new__(Ledger)
    lock.s = leads.s
    key = f'identity-lock:{run.mode}'
    if not lock.begin(key, run.run_id, 'internal_lock'):
        return None, 'Identity registry is busy; retry after the current update finishes.'
    try:
        matches = [p for p in rows(leads.s, prefix) if aliases & set(p['aliases'])]
        changed = len(matches) > 1 or matches and any(
            matches[0].get(field) and ident.get(field) and matches[0][field] != ident[field]
            for field in ('domain', 'email'))
        if changed:
            conflict = {'id': f'identity-conflict:{run.mode}:{run.run_id}', 'status': 'pending',
                        'existing': matches, 'proposed': ident, 'aliases': sorted(aliases), 'source': run.message_id}
            put(leads.s, conflict)
            run.record('identity.conflict', agent='Jordan', decision='review', data=conflict)
            return None, 'Exact identity aliases match conflicting company/email records. Review the identity conflict.'
        person = matches[0] if matches else {'id': prefix + uuid.uuid4().hex, 'aliases': [], 'sources': []}
        person.update({k: v for k, v in ident.items() if k in ('email', 'domain', 'name') and v})
        person['aliases'] = sorted(set(person['aliases']) | aliases)
        person['sources'] = list(dict.fromkeys(person['sources'] + [run.message_id]))
        put(leads.s, person)
        run.record('identity.canonical', agent='Jordan', decision='matched' if matches else 'created', data=person)
        return person, ''
    finally:
        lock.failed(key, 'internal lock released')


def review(storage, conflict_id, decision):
    lock = object.__new__(Ledger)
    lock.s = storage
    key = f'identity-lock:{mode()}'
    if not lock.begin(key, 'review:' + uuid.uuid4().hex, 'internal_lock'):
        raise ValueError('Identity registry is busy; retry the review shortly')
    try:
        return _review(storage, conflict_id, decision)
    finally:
        lock.failed(key, 'internal lock released')


def _review(storage, conflict_id, decision):
    if not conflict_id.startswith(f'identity-conflict:{mode()}:') or decision not in ('keep', 'accept'):
        raise ValueError('Invalid identity review')
    row = storage.get('settings', conflict_id)
    conflict = json.loads(row['value']) if row else {}
    if conflict.get('status') != 'pending':
        raise ValueError('Conflict is not pending')
    if decision == 'accept':
        if len(conflict['existing']) != 1:
            raise ValueError('Multiple people require manual reconciliation; automatic merge is blocked')
        person = json.loads(storage.get('settings', conflict['existing'][0]['id'])['value'])
        if person != conflict['existing'][0]:
            raise ValueError('Identity changed; rescan for a fresh review')
        person.update({k: v for k, v in conflict['proposed'].items() if k in ('email', 'domain', 'name') and v})
        person['aliases'] = sorted(set(person['aliases']) | set(conflict['aliases']))
        person['sources'].append('founder review:' + conflict_id)
        put(storage, person)
    conflict['status'] = decision
    put(storage, conflict)
