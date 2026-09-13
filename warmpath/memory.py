"""Sourced, inspectable memory stored in the shared settings table.

Independent records avoid overwriting other agents' lead updates. Dry and live memories
are separate. Inferred restrictions can block actions; they never grant permission.
"""
import json
import re
import uuid
from datetime import datetime, timezone

from .core import mode
from .relationships import domain_of, FREEMAIL

KINDS = {'do_not_contact', 'follow_up', 'active_conversation', 'relationship', 'preference'}


def identities(data):
    values = set()
    for key in ('email', 'profile_url', 'author_id'):
        value = str(data.get(key) or '').strip().lower().rstrip('/')
        if value:
            values.add(f'{key}:{value}')
    return values


def expanded_ids(storage, environment, data):
    ids = identities(data)
    for row in storage.select('settings'):
        if row['key'].startswith(f'person:{environment}:'):
            person = json.loads(row['value'])
            if ids & set(person['aliases']):
                ids.update(person['aliases'])
    return ids


class Memory:
    def __init__(self, storage, environment=None):
        self.s = storage
        self.mode = environment or mode()

    def all(self):
        return [json.loads(r['value']) for r in self.s.select('settings')
                if r['key'].startswith(f'memory:{self.mode}:')]

    def remember(self, *, scope, target, kind, text, source, expires='', inferred=False):
        if scope not in ('contact', 'account', 'founder') or kind not in KINDS:
            raise ValueError('Invalid memory scope or kind')
        target = str(target).strip().lower().rstrip('/')
        if scope == 'account':
            target = domain_of(target)
            if not target or target in FREEMAIL or '.' not in target:
                raise ValueError('Account memory requires a company domain')
        if not target or not str(text).strip() or not str(source).strip():
            raise ValueError('Target, fact and source are required')
        if scope == 'contact' and not target.startswith(('email:', 'profile_url:', 'author_id:')):
            raise ValueError('Contact target must be an email or LinkedIn identity')
        if expires:
            expiry = datetime.fromisoformat(expires.replace('Z', '+00:00'))
            if expiry.tzinfo is None:
                raise ValueError('Expiry needs a timezone')
            expires = expiry.isoformat()
        if kind == 'follow_up' and not expires:
            raise ValueError('Follow-up memory requires a resume date')
        record = dict(id=f'memory:{self.mode}:{uuid.uuid4().hex}', scope=scope, target=target,
                      kind=kind, text=str(text)[:2000], source=str(source)[:500], expires=expires,
                      inferred=bool(inferred), created_at=datetime.now(timezone.utc).isoformat(), active=True)
        self.s.upsert('settings', {'key': record['id'], 'value': json.dumps(record)})
        return record

    def archive(self, key):
        if not key.startswith(f'memory:{self.mode}:'):
            raise ValueError('Memory belongs to a different environment')
        row = self.s.get('settings', key)
        if not row:
            raise ValueError('Memory not found')
        record = json.loads(row['value'])
        record.update(active=False, archived_at=datetime.now(timezone.utc).isoformat())
        self.s.update('settings', key, {'value': json.dumps(record)})

    def matching(self, data):
        ids = expanded_ids(self.s, self.mode, data)
        domain = domain_of(data.get('domain', ''))
        now = datetime.now(timezone.utc)
        return [r for r in self.all() if r['active']
                and (not r['expires'] or datetime.fromisoformat(r['expires']) > now)
                and (r['scope'] == 'founder' or r['scope'] == 'contact' and r['target'] in ids
                     or r['scope'] == 'account' and r['target'] == domain)]


def check(run, leads, data, *, active=False):
    records = Memory(leads.s, run.mode).matching(data)
    blocked = [r for r in records if r['kind'] in ('do_not_contact', 'follow_up')
               or active and r['kind'] == 'active_conversation']
    if active:
        ids = expanded_ids(leads.s, run.mode, data)
        for lead in leads.all():
            same = bool(ids & identities(lead))
            info = lead.get('introduction') or {}
            if info.get('mode') == run.mode and info.get('status') in ('sending', 'awaiting_reply', 'waiting', 'response_received', 'outcome_unknown'):
                if same or data.get('domain') and data['domain'] == lead.get('domain'):
                    blocked.append({'text': 'An introduction is already active at this account', 'source': info.get('run_id', 'lead record')})
            elif same and lead.get('status') == 'awaiting_reply' and lead.get('memory_mode') == run.mode:
                blocked.append({'text': 'An outreach conversation is already active', 'source': lead.get('lead_run', 'lead record')})
    run.record('memory.check', agent='Alex', decision='hold' if blocked else 'clear',
               data={'records': records, 'holds': blocked})
    if blocked:
        return [f"{r['text']} (source: {r['source']})" for r in blocked]
    return []


def capture_opt_out(storage, data, text, source, environment=None):
    # Conservative phrase detection on the sender's own words, never quoted history.
    if not re.search(r"\b(unsubscribe|do not contact|don't contact|stop (?:emailing|contacting|messaging))\b", text, re.I):
        return False
    for identity in identities(data):
        Memory(storage, environment).remember(scope='contact', target=identity, kind='do_not_contact',
            text=text[:2000], source=source, inferred=True)
    return True


def preferences(leads, run, data):
    return {f'preference{i}': {'kind': 'instruction',
            'text': 'Founder writing preference (not a product fact): ' + r['text']}
            for i, r in enumerate(Memory(leads.s, run.mode).matching(data))
            if r['kind'] == 'preference' and not r['inferred']}
