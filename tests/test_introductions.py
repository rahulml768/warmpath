"""Introduction workflow contract: correct recipient, approval, replay, response isolation."""
import pytest

from warmpath import audit, introductions, pipeline
from warmpath.core import Leads, Ledger
from warmpath.integrations import google
from test_reliability import Stubs, WARM, CONTACTS, comments


def setup(monkeypatch, tmp_path):
    Stubs(monkeypatch, records=WARM)
    monkeypatch.setenv('WARMPATH_MODE', 'dry_run')
    monkeypatch.setenv('WARMPATH_INTRODUCTIONS', '1')
    return Ledger(tmp_path / 'intro.db'), Leads(tmp_path / 'intro.db')


def run_intro(ledger, leads, answer='approved'):
    return pipeline.process_comment(comments()['c-warm-acme'], post_text='post', contacts=CONTACTS,
                                    approver=lambda r, p: answer, ledger=ledger, leads=leads, ours=set())


def test_approved_intro_only_emails_connector_and_replay_does_not_contact_prospect(monkeypatch, tmp_path):
    ledger, leads = setup(monkeypatch, tmp_path)
    run = run_intro(ledger, leads)
    assert run.state == 'AWAITING_INTRO'
    sends = [s for s in run.steps if s.tool == 'gmail.send']
    assert len(sends) == 1 and sends[0].data['to'] == 'david.chen@acme.example'
    assert sends[0].data['simulated'] is True
    assert audit.audit_run(run) == []
    assert leads.get('john.smith@acme.example')['introduction']['status'] == 'simulated'
    assert run_intro(ledger, leads).state == 'BLOCKED_DUPLICATE'
    assert ledger.done_count('introduction') == 1


@pytest.mark.parametrize('answer,state', [('rejected', 'STOPPED'), ('timeout', 'STOPPED'), ('review', 'REVIEW_REQUIRED')])
def test_intro_requires_specific_approval(monkeypatch, tmp_path, answer, state):
    ledger, leads = setup(monkeypatch, tmp_path)
    run = run_intro(ledger, leads, answer)
    assert run.state == state and not any(s.tool == 'gmail.send' for s in run.steps)


def test_uncertain_send_is_held_not_retried(monkeypatch, tmp_path):
    ledger, leads = setup(monkeypatch, tmp_path)
    def timeout(**kw):
        raise TimeoutError('response lost')
    monkeypatch.setattr(google, 'send', timeout)
    run = run_intro(ledger, leads)
    assert run.reason_code == 'INTRO_OUTCOME_UNKNOWN'
    assert leads.get('john.smith@acme.example')['introduction']['status'] == 'outcome_unknown'
    assert run_intro(ledger, leads).state == 'BLOCKED_DUPLICATE'


def test_one_way_or_prospect_relationship_is_not_an_intro():
    person = {'email': 'david@acme.example', 'mutual': False, 'evidence_ids': ['mail']}
    assert introductions.connector_for({'email': 'john@acme.example'}, {'people': [person]}) is None
    assert introductions.connector_for({'email': person['email']}, {'people': [{**person, 'mutual': True}]}) is None


@pytest.mark.parametrize('outcome', ['introduced', 'declined', 'waiting'])
def test_founder_can_record_outcome_without_prospect_outreach(monkeypatch, tmp_path, outcome):
    _, leads = setup(monkeypatch, tmp_path)
    leads.save('john', {'introduction': {'status': 'response_received', 'reply': 'I can help.'}})
    introductions.resolve(leads, 'john', outcome)
    assert leads.get('john')['introduction']['status'] == outcome
    assert leads.get('john')['introduction']['reviewed_by'] == 'founder'


def test_cannot_confirm_intro_before_response(monkeypatch, tmp_path):
    _, leads = setup(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        introductions.resolve(leads, 'missing', 'introduced')


@pytest.mark.parametrize('sender,subject,accepted', [
    ('david@acme.example', 'Re: Introduction request [WP-token]', True),
    ('stranger@acme.example', 'Re: Introduction request [WP-token]', False),
    ('david@acme.example', 'Re: another conversation', False),
])
def test_reply_matches_connector_and_request_and_never_sends(monkeypatch, tmp_path, sender, subject, accepted):
    _, leads = setup(monkeypatch, tmp_path)
    leads.save('john@acme.example', {'introduction': {'status': 'awaiting_reply', 'mode': 'dry_run',
        'connector': 'david@acme.example', 'connector_name': 'David', 'sent_at': '2026-09-14T00:00:00+00:00',
        'token': '[WP-token]'}})
    monkeypatch.setattr(google, 'replies_from', lambda *a: [
        {'from': sender, 'subject': subject, 'body': 'Happy to introduce you.', 'message_id': 'r1'}])
    messages = []
    class Chat:
        def post(self, *args, **kw):
            messages.append((args, kw))
    introductions.check_replies(leads, Chat())
    introductions.check_replies(leads, Chat())
    assert len(messages) == int(accepted)
    assert leads.get('john@acme.example')['introduction']['status'] == ('response_received' if accepted else 'awaiting_reply')
