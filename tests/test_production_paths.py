import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pytest

from warmpath import entities, memory, pipeline, recovery, replay
from warmpath.core import Leads, Ledger, Run
from test_reliability import Stubs, YES, REPLY, _lead_with_offer, CONTACTS, comments


def identity(email='john@acme.example', domain='acme.example'):
    return dict(email=email, domain=domain, name='John', verified=True, profile_url='https://linkedin.com/in/john')


def test_identity_links_aliases_and_holds_conflicting_company(monkeypatch, tmp_path):
    monkeypatch.setenv('WARMPATH_MODE', 'dry_run')
    leads = Leads(tmp_path / 'db')
    first, error = entities.resolve(Run.start('c1'), leads, identity(), {})
    second, error = entities.resolve(Run.start('c2'), leads, identity(), {})
    assert first['id'] == second['id'] and not error
    mem = memory.Memory(leads.s)
    mem.remember(scope='contact', target='profile_url:https://linkedin.com/in/john', kind='do_not_contact', text='Stop', source='comment:c1')
    assert mem.matching({'email': 'john@acme.example'})
    run = Run.start('changed')
    person, error = entities.resolve(run, leads, identity(domain='nova.example'), {})
    assert person is None and error
    conflict = entities.rows(leads.s, 'identity-conflict:')[0]
    assert entities.rows(leads.s, 'person:')[0]['domain'] == 'acme.example'
    entities.review(leads.s, conflict['id'], 'accept')
    assert entities.rows(leads.s, 'person:')[0]['domain'] == 'nova.example'


def test_same_name_and_company_are_not_enough_to_merge(tmp_path):
    leads = Leads(tmp_path / 'db')
    p, _ = entities.resolve(Run.start('1'), leads, identity(), {})
    q, _ = entities.resolve(Run.start('2'), leads, {**identity('other@acme.example'), 'profile_url': 'https://linkedin.com/in/other'}, {})
    assert p['id'] != q['id']


def test_pending_action_cannot_expire_or_reenter(tmp_path):
    led = Ledger(tmp_path / 'db')
    assert led.begin('send:one', 'r1', 'send')
    led.s.update('action_ledger', 'send:one', {'updated': '2000-01-01T00:00:00+00:00'})
    assert not led.begin('send:one', 'r1', 'send')
    assert not led.begin('send:one', 'r2', 'send')
    led.s.update('action_ledger', 'send:one', {'status': 'unknown'})
    assert not led.begin('send:one', 'r3', 'send')


def test_notification_failure_is_durable_and_does_not_claim_notified(monkeypatch, tmp_path):
    Stubs(monkeypatch, meeting=YES)
    monkeypatch.setenv('WARMPATH_MODE', 'dry_run')
    leads, led = Leads(tmp_path / 'db'), Ledger(tmp_path / 'db')
    lead = _lead_with_offer(leads)
    def fail(text):
        raise RuntimeError('Slack unavailable')
    run = pipeline.process_reply(lead, REPLY, approver=lambda *a: 'approved', notify=fail, ledger=led, leads=leads)
    assert run.state == 'MEETING_BOOKED' and run.reason_code == 'NOTIFICATION_PENDING'
    assert led.done_count('meeting') == 1
    task = entities.rows(led.s, 'recovery:')[0]
    task['next_retry_at'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    entities.put(led.s, task)
    sent = []
    recovery.retry_notifications(Ledger(tmp_path / 'db'), sent.append)
    recovery.retry_notifications(Ledger(tmp_path / 'db'), sent.append)
    assert len(sent) == 1 and led.done_count('meeting') == 1


def test_replay_executes_pipeline_without_external_writes(monkeypatch, tmp_path):
    Stubs(monkeypatch)
    monkeypatch.setenv('WARMPATH_MODE', 'dry_run')
    leads, led = Leads(tmp_path / 'db'), Ledger(tmp_path / 'db')
    run = pipeline.process_comment(comments()['c-warm-acme'], post_text='post', contacts=CONTACTS,
        approver=lambda *a: 'approved', ledger=led, leads=leads, ours=set())
    before = leads.all()
    report = replay.execute(asdict(run))
    assert report['matches'] and report['external_writes'] == 0 and report['model_calls'] == 0
    assert leads.all() == before


def test_old_trace_replay_fails_explicitly():
    with pytest.raises(ValueError, match='older trace'):
        replay.execute(asdict(Run.start('old')))


def test_replay_child_process_has_no_live_state_dependency(monkeypatch, tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    Stubs(monkeypatch)
    monkeypatch.setenv('WARMPATH_MODE', 'dry_run')
    leads, led = Leads(tmp_path / 'db'), Ledger(tmp_path / 'db')
    run = pipeline.process_comment(comments()['c-warm-acme'], post_text='post', contacts=CONTACTS,
        approver=lambda *a: 'approved', ledger=led, leads=leads, ours=set())
    env = {k:v for k,v in os.environ.items() if k.upper() in ('SYSTEMROOT','PATH','TEMP','TMP','WINDIR')}
    env['PYTHONIOENCODING'] = 'utf-8'
    child = subprocess.run([sys.executable, '-m', 'warmpath.replay'], input=json.dumps(asdict(run)),
        text=True, capture_output=True, encoding='utf-8', timeout=20, env=env,
        cwd=Path(__file__).resolve().parent.parent)
    assert child.returncode == 0, child.stdout + child.stderr
    assert json.loads(child.stdout)['matches']


def test_live_unknown_outcome_requires_manual_reconciliation(monkeypatch, tmp_path):
    monkeypatch.setenv('WARMPATH_MODE', 'live')
    led = Ledger(tmp_path / 'db')
    run = Run.start('message')
    assert led.begin('send:test', run.run_id, 'send')
    row = recovery.record(led, run, 'send:test', 'send', TimeoutError('response lost'))
    assert row['status'] == 'review'
    assert not led.begin('send:test', 'another-run', 'send')
    recovery.reconcile(led.s, row['id'], 'not_performed')
    assert led.begin('send:test', 'fresh-run', 'send')
