"""Adversarial regression cases found during the reliability review. No app writes."""
import pytest

from warmpath.core import Run, evaluate, verify_contact
from warmpath.agents import morgan
from warmpath.integrations import google, slack
from warmpath import llm


@pytest.mark.parametrize('confidence', ['NaN', 'Infinity', 2, True])
def test_model_confidence_cannot_be_clamped_into_approval(monkeypatch, confidence):
    monkeypatch.setattr(llm, '_call', lambda *a, **kw: {'intent': 'LEAD', 'confidence': confidence})
    assert llm.classify('Interested')['confidence'] == 0


@pytest.mark.parametrize('source', ['notjohn@acme.example', 'john@acme.example.evil'])
def test_verification_requires_whole_address(source):
    assert not verify_contact('john@acme.example', {'profile': source})[0]


def test_verification_accepts_named_address():
    assert verify_contact('john@acme.example', {'profile': 'John <JOHN@acme.example>'})[0]


@pytest.mark.parametrize('confidence', [float('nan'), float('inf'), -1, 2, True, '0.9', None])
def test_invalid_confidence_cannot_authorize(confidence):
    assert not evaluate(intent='LEAD', confidence=confidence, verified=True,
                        verify_evidence=['profile'], shared_mailbox=False, approved=True).allowed


@pytest.mark.parametrize('approval', ['false', 'send later', 1])
def test_policy_requires_boolean_approval(approval):
    assert not evaluate(intent='LEAD', confidence=.9, verified=True,
                        verify_evidence=['profile'], shared_mailbox=False, approved=approval).allowed


@pytest.mark.parametrize('text,user,thread,expected', [
    ('send', 'founder', '1', 'approved'),
    ('review', 'founder', '1', 'review'),
    ('ignore', 'founder', '1', 'rejected'),
    ('send later', 'founder', '1', None),
    ('okay do not send', 'founder', '1', None),
    ('send', 'stranger', '1', None),
    ('send', 'founder', 'other-card', None),
    ('send', 'founder', None, None),
])
def test_slack_approval_is_exact_authorized_and_card_specific(monkeypatch, text, user, thread, expected):
    monkeypatch.setenv('SLACK_APPROVER_ID', 'founder')
    def fake(method, params):
        assert method == 'conversations.replies' and params['ts'] == '1'
        return {'messages': [{'ts': '2', 'thread_ts': thread, 'user': user, 'text': text}]}
    monkeypatch.setattr(slack, '_slack', fake)
    assert slack.check_decision(channel='dm', ts='1') == expected


def test_ambiguous_acceptance_cannot_book():
    assert not morgan.agreed_time({'ambiguous': True, 'offered_slot_id': 'slot1'},
                                 {'slot1': {'start_local': '2026-09-16T11:00:00'}})[0]


@pytest.mark.parametrize('end,expected', [('11:15:00', False), ('11:30:00', True)])
def test_calendar_requires_entire_meeting_free(monkeypatch, end, expected):
    monkeypatch.setenv('WARMPATH_TIMEZONE', 'Asia/Kolkata')
    monkeypatch.setattr(google, 'composio_ready', lambda: True)
    monkeypatch.setattr(google, 'free_slots', lambda **kw: [
        {'start': '2026-09-16T11:00:00+05:30', 'end': f'2026-09-16T{end}+05:30'}])
    assert morgan.slot_is_free(Run.start('slot-test'), '2026-09-16T11:00:00') == (expected, True)


def test_competing_booking_resume_has_legal_exit():
    run = Run.start('retry', state='CALENDAR_FAILED')
    run.move('BLOCKED_DUPLICATE')
