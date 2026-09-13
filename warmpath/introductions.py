"""Founder-approved requests to known contacts; replies wait for human review."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from . import claims, memory
from .agents import casey
from .core import Run, mode
from .integrations import google
from .relationships import is_role_mailbox


def connector_for(ident, rel):
    # A name at the same company is insufficient: require reciprocal contact.
    if any(p['email'] == ident.get('email') for p in rel.get('people', [])):
        return None
    return next((p for p in rel.get('people', []) if p.get('mutual')
                 and p.get('evidence_ids') and not is_role_mailbox(p['email'])), None)


def request(run, comment, ident, connector, *, approver, ledger, leads):
    lead_key = ident.get('email') or f"comment:{comment['comment_id']}"
    token = hashlib.sha256(lead_key.encode()).hexdigest()[:16]
    key = f"intro:{run.mode}:{token}"
    run.kind = 'introduction'
    run.record('intro.route', agent='Jordan', decision='ask_known_contact', data={
        'lead_key': lead_key, 'connector': connector['email'],
        'evidence_ids': connector['evidence_ids'], 'evidence': connector['evidence'],
        'prospect': ident['name'], 'knows_prospect': 'unknown'})
    # Fixed wording deliberately asks whether they know the prospect; no invented endorsement.
    text = (f"Do you know {ident['name']} at {ident['company'] or ident['domain']}, "
            "and would you be comfortable introducing us? No problem if not.")
    evidence = {'profile': {'kind': 'profile', 'text': f"{ident['name']} at {ident['company'] or ident['domain']}"}}
    draft = {'subject': f'Introduction request [WP-{token}]', 'sentences': [
        {'text': text, 'evidence': ['profile'], 'claims_prior_contact': False}]}
    problems = claims.check(draft, evidence, allowed_addresses={connector['email']})
    run.record('claims.check', agent='Casey', decision='fail' if problems else 'pass',
               ok=not problems, data={'sentences': draft['sentences'], 'evidence': evidence, 'problems': problems})
    run.move('ACTION_PROPOSED')
    if problems:
        run.move('REVIEW_REQUIRED')
        return run.finish('REVIEW_REQUIRED', 'UNSUPPORTED_CLAIM', problems)
    run.move('AWAITING_APPROVAL')
    card = (f"Introduction request · {run.mode} · {run.run_id}\n"
            f"To: {connector['name'] or connector['email']} <{connector['email']}>\n"
            f"Prospect: {ident['name']} · {ident['company']}\n"
            f"Why this contact: {connector['evidence']}\n"
            "Whether they know the prospect is unknown. This sends only the intro request.\n"
            f"Subject: {draft['subject']}\n{claims.render(draft)}\nReply send, review, or ignore.")
    answer = run.step('slack.approval', lambda: approver(run, {'card': card}), agent='Alex', output=card)
    run.steps[-1].decision = answer
    if answer != 'approved':
        run.move('REVIEW_REQUIRED' if answer == 'review' else 'STOPPED')
        return run.finish(run.state, 'APPROVAL_TIMEOUT' if answer == 'timeout' else 'APPROVAL_REJECTED', [f'answer: {answer}'])
    run.move('APPROVED')
    held = memory.check(run, leads, {**comment, **ident}, active=True)
    held += memory.check(run, leads, {'email': connector['email'], 'domain': ident['domain']})
    if held:
        run.move('STOPPED')
        return run.finish(run.state, 'MEMORY_HOLD', held)
    if not ledger.begin(key, run.run_id, 'introduction'):
        run.move('BLOCKED_DUPLICATE')
        return run.finish(run.state, 'ACTION_ALREADY_CLAIMED', [key])
    # Persist before the write. Unknown outcomes remain held, never automatically resent.
    info = {'status': 'sending', 'connector': connector['email'], 'connector_name': connector['name'],
            'subject': draft['subject'], 'token': f'[WP-{token}]', 'mode': run.mode,
            'run_id': run.run_id, 'action_id': key, 'sent_at': datetime.now(timezone.utc).isoformat()}
    leads.save(lead_key, {'introduction': info, 'status': 'intro_pending'})
    try:
        res = casey.send_email(run, to=connector['email'], subject=draft['subject'],
                               body=claims.render(draft), live=run.mode == 'live')
    except Exception as exc:
        leads.save(lead_key, {'introduction': {**info, 'status': 'outcome_unknown'}})
        run.move('STOPPED')
        return run.finish(run.state, 'INTRO_OUTCOME_UNKNOWN', ['Check the sent mailbox before retrying.', str(exc)[:160]])
    run.steps[-1].data = {**res, 'to': connector['email'], 'action_id': key, 'sentences': draft['sentences']}
    run.steps[-1].decision = 'sent'
    ledger.done(key, 'simulated' if res.get('simulated') else 'sent')
    leads.save(lead_key, {'introduction': {**info, 'status': 'simulated' if res.get('simulated') else 'awaiting_reply'},
                          'status': 'intro_pending'})
    run.move('EMAIL_SENT')
    run.move('AWAITING_INTRO')
    return run.finish(run.state, '', ['Introduction request sent to the known contact; prospect not contacted.'])


def check_replies(leads, chat):
    for lead in leads.all():
        info = lead.get('introduction') or {}
        if info.get('status') not in ('awaiting_reply', 'waiting') or info.get('mode') != mode():
            continue
        try:
            replies = google.replies_from(info['connector'], info['sent_at'])
        except Exception:
            # One disconnected mailbox must not prevent other leads being processed.
            chat.post('Alex', 'text', f"Could not check the introduction reply from {info['connector']}; will retry.")
            continue
        for reply in replies:
            if not reply.get('message_id') or reply['message_id'] == info.get('reply_id'):
                continue
            if (reply.get('from', '').lower() != info['connector'].lower()
                    or info['token'] not in reply.get('subject', '')):
                continue
            body = google._strip_quoted(reply.get('body', ''))
            if not body:
                continue
            memory.capture_opt_out(leads.s, {'email': info['connector']}, body,
                                   f"reply:{reply['message_id']}", info['mode'])
            # An affirmative response is not proof that the introduction happened.
            leads.save(lead['key'], {'introduction': {**info, 'status': 'response_received',
                       'reply': body, 'reply_id': reply.get('message_id')},
                       'stage': 'Intro response · needs you'})
            chat.post('Alex', 'reply', f"{info['connector_name'] or info['connector']} replied to the introduction request. "
                      "Review their response before contacting the prospect. No prospect outreach was sent.", body=body[:2000])
            break


def resolve(leads, key, outcome):
    """Record the founder's assessment; never interpret a connector's yes as a meeting yes."""
    labels = {'introduced': 'Introduction confirmed', 'declined': 'Introduction declined',
              'waiting': 'Waiting for introduction'}
    if outcome not in labels:
        raise ValueError('Choose introduced, declined, or waiting')
    lead = leads.get(key) or {}
    info = lead.get('introduction') or {}
    if info.get('status') not in ('response_received', 'waiting'):
        raise ValueError('An introduction response must be reviewed first')
    leads.save(key, {'introduction': {**info, 'status': outcome,
               'reviewed_at': datetime.now(timezone.utc).isoformat(), 'reviewed_by': 'founder'},
               'stage': labels[outcome], 'status': 'intro_' + outcome})
