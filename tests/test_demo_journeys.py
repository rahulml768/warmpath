"""Cross-feature scenarios for the tournament demo; isolated storage and stubbed apps."""
import pytest

from warmpath import audit, memory, pipeline
from warmpath.core import Leads, Ledger
from test_reliability import Stubs, YES, REPLY, _lead_with_offer
from test_introductions import setup, run_intro


def test_connector_optout_blocks_intro_even_with_founder_approval(monkeypatch, tmp_path):
    ledger, leads = setup(monkeypatch, tmp_path)
    memory.Memory(leads.s).remember(scope='contact', target='email:david.chen@acme.example',
        kind='do_not_contact', text='David declined further requests', source='reply:david')
    run = run_intro(ledger, leads)
    assert run.reason_code == 'MEMORY_HOLD'
    assert not any(s.tool in audit.WRITES for s in run.steps)
    assert not ledger.done_count()
    assert audit.audit_run(run) == []


@pytest.mark.parametrize('resume', [False, True])
def test_optout_stops_normal_booking_and_failed_booking_resume(monkeypatch, tmp_path, resume):
    Stubs(monkeypatch, meeting=YES)
    monkeypatch.setenv('WARMPATH_MODE', 'dry_run')
    leads, ledger = Leads(tmp_path / 'test.db'), Ledger(tmp_path / 'test.db')
    lead = _lead_with_offer(leads)
    if resume:
        leads.save(lead['email'], {'pending_booking': '2026-09-16T11:00:00'})
    run = pipeline.process_reply(lead, {**REPLY, 'body': 'Please stop contacting me.'},
        approver=lambda *a: 'approved', notify=lambda *a: pytest.fail('No notification expected'),
        leads=leads, ledger=ledger)
    assert run.reason_code == 'MEMORY_HOLD'
    assert not any(s.tool in audit.WRITES for s in run.steps)
    assert audit.audit_run(run) == []


def test_quoted_optout_is_not_mistaken_for_new_optout(monkeypatch, tmp_path):
    Stubs(monkeypatch, meeting=YES)
    monkeypatch.setenv('WARMPATH_MODE', 'dry_run')
    leads, ledger = Leads(tmp_path / 'test.db'), Ledger(tmp_path / 'test.db')
    lead = _lead_with_offer(leads)
    run = pipeline.process_reply(lead, {**REPLY, 'body': 'Wednesday works.\n> To unsubscribe, reply stop.'},
        approver=lambda *a: 'approved', notify=lambda *a: None, leads=leads, ledger=ledger)
    assert run.state == 'NOTIFIED'
    assert not memory.Memory(leads.s).all()


def test_intro_result_exposes_route_without_inventing_connection(monkeypatch, tmp_path):
    from warmpath.chat import result_card
    ledger, leads = setup(monkeypatch, tmp_path)
    run = run_intro(ledger, leads)
    posted = []
    class Chat:
        def post(self, *args, **payload):
            posted.append(payload)
    result_card(Chat(), run)
    assert posted[0]['route']['connector'] == 'david.chen@acme.example'
    assert posted[0]['route']['knows_prospect'] == 'unknown'
