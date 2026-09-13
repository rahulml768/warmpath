import json
import urllib.error

import pytest

from warmpath import llm
from warmpath.core import Run


class Response:
    def __init__(self, content='{"intent":"LEAD","confidence":0.9}'):
        self.content = content
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self):
        return json.dumps({'choices': [{'message': {'content': self.content}, 'finish_reason': 'stop'}]}).encode()


@pytest.fixture
def providers(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-primary')
    monkeypatch.setenv('CEREBRAS_API_KEY', 'test-backup')
    monkeypatch.setenv('WARMPATH_LLM_PRIMARY', 'deepseek')
    monkeypatch.setattr(llm.time, 'sleep', lambda *a: None)


@pytest.mark.parametrize('status', [401, 402, 429, 500, 503])
def test_primary_failure_switches_immediately_and_records_provider(providers, monkeypatch, status):
    calls = []
    def urlopen(req, **kw):
        calls.append(req.full_url)
        if 'deepseek' in req.full_url:
            raise urllib.error.HTTPError(req.full_url, status, 'provider error', {}, None)
        assert req.get_header('Authorization') == 'Bearer test-backup'
        return Response()
    monkeypatch.setattr(llm.urllib.request, 'urlopen', urlopen)
    run = Run.start('test')
    result = run.step('intent.classify', lambda: llm.classify('Interested'))
    assert result['intent'] == 'LEAD' and len(calls) == 2
    assert run.steps[-1].model_call['provider'] == 'cerebras'
    assert run.steps[-1].model_call['attempts'][0]['status'] == f'HTTP {status}'


def test_cerebras_works_without_primary_key(providers, monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY')
    calls = []
    monkeypatch.setattr(llm.urllib.request, 'urlopen', lambda req, **kw: (calls.append(req.full_url) or Response()))
    assert llm._call('JSON', 'test')
    assert calls == ['https://api.cerebras.ai/v1/chat/completions']


@pytest.mark.parametrize('failure', ['timeout', 'bad_json', 'array'])
def test_unusable_primary_response_uses_backup(providers, monkeypatch, failure):
    def urlopen(req, **kw):
        if 'deepseek' in req.full_url:
            if failure == 'timeout':
                raise TimeoutError('connection stalled')
            return Response('not json' if failure == 'bad_json' else '[]')
        return Response()
    monkeypatch.setattr(llm.urllib.request, 'urlopen', urlopen)
    assert llm._call('JSON', 'test')['intent'] == 'LEAD'
    assert llm.CALL_INFO.get()['provider'] == 'cerebras'


def test_all_exhausted_stops_without_secret_leak_or_endless_retry(providers, monkeypatch):
    calls = []
    def fail(req, **kw):
        calls.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 402, 'secret test-primary', {}, None)
    monkeypatch.setattr(llm.urllib.request, 'urlopen', fail)
    with pytest.raises(llm.LLMError) as error:
        llm._call('JSON', 'test')
    assert len(calls) == 2
    assert 'test-primary' not in str(error.value)


def test_primary_success_never_calls_backup(providers, monkeypatch):
    calls = []
    monkeypatch.setattr(llm.urllib.request, 'urlopen', lambda req, **kw: (calls.append(req.full_url) or Response()))
    llm._call('JSON', 'test')
    assert calls == [llm.URL]


def test_cerebras_can_be_selected_as_primary(providers, monkeypatch):
    monkeypatch.setenv('WARMPATH_LLM_PRIMARY', 'cerebras')
    calls = []
    monkeypatch.setattr(llm.urllib.request, 'urlopen', lambda req, **kw: (calls.append(req.full_url) or Response()))
    llm._call('JSON', 'test')
    assert calls == ['https://api.cerebras.ai/v1/chat/completions']
