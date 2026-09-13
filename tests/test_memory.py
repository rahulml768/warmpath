from datetime import datetime, timedelta, timezone

import pytest

from warmpath import memory, pipeline
from warmpath.core import Leads, Ledger, Run
from test_reliability import Stubs, CONTACTS, comments


def test_memory_survives_reopening_and_is_environment_scoped(tmp_path):
    path = tmp_path / 'state.db'
    first = memory.Memory(Leads(path).s, 'live')
    first.remember(scope='contact', target='email:john@acme.example', kind='do_not_contact', text='Stop', source='reply:r1')
    assert memory.Memory(Leads(path).s, 'live').matching({'email': 'JOHN@acme.example'})
    assert not memory.Memory(Leads(path).s, 'dry_run').all()


def test_expiry_and_archive(tmp_path):
    mem = memory.Memory(Leads(tmp_path / 'state.db').s)
    record = mem.remember(scope='account', target='acme.example', kind='follow_up', text='Later', source='founder',
                         expires=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
    assert mem.matching({'domain': 'acme.example'})
    mem.archive(record['id'])
    assert not mem.matching({'domain': 'acme.example'})
    mem.remember(scope='account', target='acme.example', kind='follow_up', text='Past', source='founder',
                 expires=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat())
    assert not mem.matching({'domain': 'acme.example'})


def test_optout_matches_new_comment_same_profile(tmp_path):
    leads = Leads(tmp_path / 'state.db')
    data = {'profile_url': 'https://linkedin.com/in/john'}
    memory.capture_opt_out(leads.s, data, 'Please do not contact me', 'comment:first', 'dry_run')
    assert memory.check(Run.start('second'), leads, {**data, 'comment_id': 'second'})


def test_restriction_added_during_approval_blocks_send(monkeypatch, tmp_path):
    Stubs(monkeypatch)
    monkeypatch.setenv('WARMPATH_MODE', 'dry_run')
    leads, ledger = Leads(tmp_path / 'state.db'), Ledger(tmp_path / 'state.db')
    def approve(run, payload):
        memory.Memory(leads.s).remember(scope='contact', target='email:john.smith@acme.example',
            kind='do_not_contact', text='Founder stopped outreach', source='founder')
        return 'approved'
    run = pipeline.process_comment(comments()['c-warm-acme'], post_text='post', contacts=CONTACTS,
                                   approver=approve, ledger=ledger, leads=leads, ours=set())
    assert run.reason_code == 'MEMORY_HOLD'
    assert not any(s.tool == 'gmail.send' for s in run.steps)


def test_active_introduction_holds_another_contact_at_same_account(tmp_path):
    leads = Leads(tmp_path / 'state.db')
    leads.save('john', {'domain': 'acme.example', 'introduction': {'mode': 'dry_run', 'status': 'awaiting_reply'}})
    assert memory.check(Run.start('jane'), leads, {'email': 'jane@acme.example', 'domain': 'acme.example'}, active=True)
    assert not memory.check(Run.start('other'), leads, {'domain': 'other.example'}, active=True)


def test_followup_requires_date_and_company_scope_rejects_freemail(tmp_path):
    mem = memory.Memory(Leads(tmp_path / 'state.db').s)
    with pytest.raises(ValueError):
        mem.remember(scope='contact', target='email:a@acme.example', kind='follow_up', text='later', source='founder')
    with pytest.raises(ValueError):
        mem.remember(scope='account', target='gmail.com', kind='do_not_contact', text='stop', source='founder')
