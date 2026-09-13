"""Replay the current pipeline with recorded tool responses in a network-disabled child process."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from collections import defaultdict, deque
from copy import deepcopy
from dataclasses import asdict
from unittest.mock import patch


def version():
    h = hashlib.sha256()
    for path in sorted(Path(__file__).parent.rglob('*.py')):
        h.update(str(path.relative_to(Path(__file__).parent)).encode())
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


def capture(leads, kind, inputs):
    from .integrations import google
    from . import llm
    return deepcopy(dict(kind=kind, inputs=inputs, version=version(), model=llm.MODEL,
        calendar_connected=google.composio_ready(),
        environment={k: os.environ.get(k, default) for k, default in (
            ('WARMPATH_MODE', 'dry_run'), ('WARMPATH_TIMEZONE', 'Asia/Kolkata'), ('WARMPATH_INTRODUCTIONS', '1'))},
        storage={t: leads.s.select(t) for t in ('leads', 'settings', 'action_ledger')}))


def execute(original):
    from .core import Run, Step, Leads, Ledger
    from . import pipeline, audit
    from .integrations import google
    source = next((s['data'] for s in original['steps'] if s['tool'] == 'replay.input'), None)
    if not source:
        raise ValueError('This older trace has no replay inputs. Record a new lead or reply run.')
    queues = defaultdict(deque)
    for step in original['steps']:
        if step.get('replay_captured') or not step['ok'] and step.get('error'):
            queues[step['tool']].append(step)
    def recorded(self, tool, fn, **meta):
        if not queues[tool]:
            raise ValueError('Replay diverged: no recorded response for ' + tool)
        step = queues[tool].popleft()
        self.steps.append(Step(tool=tool, ok=step['ok'], ms=0, error=step.get('error', ''), **meta))
        if not step['ok']:
            raise RuntimeError(step['error'])
        return deepcopy(step['replay_output'])
    def blocked(*args, **kwargs):
        raise RuntimeError('Network is disabled during replay')
    with tempfile.TemporaryDirectory(prefix='warmpath-replay-') as folder:
        path = Path(folder) / 'replay.db'
        leads, ledger = Leads(path), Ledger(path)
        for table, items in source['storage'].items():
            for row in items:
                leads.s.upsert(table, row)
        args = deepcopy(source['inputs'])
        if 'ours' in args:
            args['ours'] = set(args['ours'])
        with patch.dict(os.environ, source['environment']), patch.object(Run, 'step', recorded), \
             patch.object(google, 'composio_ready', lambda: source['calendar_connected']), \
             patch('socket.socket.connect', blocked), patch('socket.create_connection', blocked):
            common = dict(leads=leads, ledger=ledger, approver=blocked)
            result = (pipeline.process_comment(**args, **common) if source['kind'] == 'comment'
                      else pipeline.process_reply(**args, **common, notify=blocked))
        def outcome(trace):
            return {'state': trace['state'], 'reason_code': trace['reason_code'],
                    'actions': [{'tool': s['tool'], 'ok': s['ok'], 'to': s.get('data', {}).get('to'),
                                 'sentences': s.get('data', {}).get('sentences')}
                                for s in trace['steps'] if s['tool'] in audit.WRITES]}
        expected, actual = outcome(original), outcome(asdict(result))
        return {'replay': True, 'external_writes': 0, 'model_calls': 0,
                'recorded_version': source['version'], 'current_version': version(),
                'expected': expected, 'actual': actual, 'matches': expected == actual,
                'violations': [asdict(v) for v in audit.audit_run(result)],
                'note': 'Current workflow with recorded tool/model responses; not a fresh model evaluation. Current clock applies.'}


if __name__ == '__main__':
    try:
        print(json.dumps(execute(json.load(sys.stdin))))
    except Exception as exc:
        print(json.dumps({'error': f'{type(exc).__name__}: {exc}'}))
        sys.exit(1)
