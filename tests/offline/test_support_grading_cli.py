"""Permanent provider failures must be actionable and safe to log."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from scripts.score_support_agent_eval import provider_failure


@pytest.mark.parametrize(('status', 'blocked'), [(401, True), (402, True), (403, True), (429, False), (500, False)])
def test_provider_failure_keeps_status_without_raw_sensitive_message(status, blocked):
    error = RuntimeError('private request text must not appear')
    error.status_code = status
    error.code = 'insufficient_balance'
    result = provider_failure(error)
    assert result['provider_http_status'] == status
    assert result['provider_blocked'] is blocked
    assert 'private request' not in str(result)


def test_balance_failure_stops_batch_before_retry_or_next_case(tmp_path, monkeypatch):
    from scripts.score_support_agent_eval import run
    from src.llm import llm_client

    packets = tmp_path / 'packets'
    packets.mkdir()
    annotations = tmp_path / 'annotations.jsonl'
    annotations.write_text(''.join(json.dumps({'number': number, 'suite': 'coverage',
                                               'required_facts': [], 'critical': []}) + '\n'
                                   for number in ('01', '02')), encoding='utf-8')
    for number in ('01', '02'):
        (packets / f'{number}-完整调用与结果.json').write_text(json.dumps({
            'result': {'input': [], 'predictions': [{'text': 'Answer'}]},
            'events': [{'step': 'model_context', 'turn': 1,
                        'observation': {'stage': 'final_generation', 'messages': []}}],
        }), encoding='utf-8')
    rubric = tmp_path / 'rubric.md'
    rubric.write_text('Frozen rubric', encoding='utf-8')
    calls = []

    async def unavailable(*args, **kwargs):
        calls.append(1)
        error = RuntimeError('secret request must not be logged')
        error.status_code = 402
        raise error

    monkeypatch.setattr(llm_client, 'chat_async', unavailable)
    output = tmp_path / 'output'
    with pytest.raises(RuntimeError, match='HTTP 402'):
        asyncio.run(run(SimpleNamespace(packets=packets, annotations=annotations, rubric=rubric,
                                       output=output, judge_thinking=False)))
    assert calls == [1]
    saved = json.loads((output / '01/attempt-001.json').read_text(encoding='utf-8'))
    assert saved['provider_http_status'] == 402
    assert not (output / '01/attempt-002.json').exists()
    assert not (output / '02').exists()
    assert 'secret request' not in json.dumps(saved)
