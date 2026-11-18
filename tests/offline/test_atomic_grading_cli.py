"""Atomic runner must stop for billing failures and bind cached judgments to inputs."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from scripts.score_atomic_support_eval import run
from tests.offline.test_atomic_grading import fixture


def setup(tmp_path):
    spec, _, _ = fixture()
    spec.update(number='01', task='Test task', reference_facts=[])
    specs = tmp_path / 'specs.jsonl'
    specs.write_text(json.dumps(spec), encoding='utf-8')
    packet = {'result': {'input': [{'speaker': 'user', 'text': 'Question'}], 'predictions': [{'text': 'Answer'}]},
              'events': [{'step': 'model_context', 'turn': 1, 'sequence': 3,
                          'observation': {'stage': 'final_generation', 'messages': []}}]}
    packets = tmp_path / 'packets'
    packets.mkdir()
    (packets / '01-完整调用与结果.json').write_text(json.dumps(packet), encoding='utf-8')
    return SimpleNamespace(packets=packets, specs=specs, output=tmp_path/'scores', only='',
                           judge_thinking=False, prepare_only=False)


def test_billing_failure_stops_without_retry(tmp_path, monkeypatch):
    from src.llm import llm_client
    args = setup(tmp_path)
    calls = []

    async def fail(*a, **k):
        calls.append(1)
        error = RuntimeError('must not log provider private payload')
        error.status_code = 402
        raise error

    monkeypatch.setattr(llm_client, 'chat_async', fail)
    with pytest.raises(RuntimeError, match='HTTP 402'):
        asyncio.run(run(args))
    assert len(calls) == 1
    record = (args.output/'01/attempt-001.json').read_text(encoding='utf-8')
    assert 'provider private' not in record
    assert not (args.output/'01/attempt-002.json').exists()


def test_prepare_only_does_not_call_provider_and_rejects_changed_input(tmp_path, monkeypatch):
    from src.llm import llm_client
    args = setup(tmp_path)
    args.prepare_only = True

    async def forbidden(*a, **k):
        pytest.fail('Preparation called provider')

    monkeypatch.setattr(llm_client, 'chat_async', forbidden)
    assert asyncio.run(run(args))[0]['status'] == 'prepared'
    packet_path = args.packets/'01-完整调用与结果.json'
    packet = json.loads(packet_path.read_text(encoding='utf-8'))
    packet['result']['predictions'][0]['text'] = 'Changed answer'
    packet_path.write_text(json.dumps(packet), encoding='utf-8')
    with pytest.raises(ValueError, match='judge input changed'):
        asyncio.run(run(args))


def test_retry_preserves_failed_response_and_success_is_reused(tmp_path, monkeypatch):
    import copy

    from scripts import score_atomic_support_eval as scorer
    from src.llm import llm_client
    args = setup(tmp_path)
    _, payload, valid = fixture()
    invalid = copy.deepcopy(valid)
    invalid['verdicts'][0]['evidence'][0]['source_id'] = 'does-not-exist'
    calls = []

    async def respond(messages, **kwargs):
        calls.append(copy.deepcopy(messages))
        return json.dumps(invalid if len(calls) == 1 else valid)

    monkeypatch.setattr(scorer, 'prepare_atomic_input', lambda *a: payload)
    monkeypatch.setattr(llm_client, 'chat_async', respond)
    assert asyncio.run(run(args))[0]['status'] == 'valid'
    assert calls[1][-2] == {'role': 'assistant', 'content': json.dumps(invalid)}
    assert 'Unavailable evidence source' in calls[1][-1]['content']
    assert asyncio.run(run(args))[0]['status'] == 'valid'
    assert len(calls) == 2


def test_citation_contracts_exclude_future_and_wrong_answer_turn(tmp_path):
    from src.evaluation.atomic_grading import prepare_atomic_input
    args = setup(tmp_path)
    spec = json.loads(args.specs.read_text(encoding='utf-8'))
    packet = json.loads((args.packets/'01-完整调用与结果.json').read_text(encoding='utf-8'))
    packet['result']['first_response'] = {'answer': 'First conditional answer'}
    packet['result']['input'].append({'speaker': 'user', 'text': 'Supplement'})
    packet['events'].append({'step': 'model_context', 'turn': 2, 'sequence': 3,
                             'observation': {'stage': 'final_generation', 'messages': []}})
    spec['criteria'][2]['scope'] = 'first'
    spec['criteria'][2]['evidence_kinds'] += ['user']
    payload = prepare_atomic_input(spec, packet)
    final = payload['citation_contracts']['Q.business']['allowed_source_ids']
    first = payload['citation_contracts']['P.I']['allowed_source_ids']
    assert 'answer:2' in final and 'answer:1' not in final
    assert 'user:1' in first and 'user:2' not in first


@pytest.mark.parametrize('kind', ['incomplete_output', 'empty_output'])
def test_completion_failure_records_safe_category(tmp_path, monkeypatch, kind):
    from src.llm import llm_client
    from src.llm.client import LLMCompletionError
    args = setup(tmp_path)

    async def fail(*a, **k):
        raise LLMCompletionError(kind)

    monkeypatch.setattr(llm_client, 'chat_async', fail)
    result = asyncio.run(run(args))
    assert result[0]['status'] == 'judge_failed'
    record = json.loads((args.output/'01/attempt-001.json').read_text(encoding='utf-8'))
    assert record['completion_failure_kind'] == kind
