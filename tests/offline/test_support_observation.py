"""Regressions for support assembly, visible evidence and execution telemetry."""

import asyncio
import json
import os
import re
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def tokenizer(monkeypatch):
    import tiktoken

    def tokens(text):
        return re.findall(r'[A-Za-z0-9_]+|[^\x00-\x7f]|[^\w\s]', text)

    monkeypatch.setattr(tiktoken, 'get_encoding', lambda _: SimpleNamespace(encode=tokens))
    from src.utils import token_counter

    monkeypatch.setattr(token_counter, 'count_tokens', lambda text: len(tokens(text)))


@pytest.fixture(autouse=True)
def shared_trace_instance(tokenizer, monkeypatch):
    """HTTP tests reload the tracer module; bind all participants to this instance."""
    import importlib

    from src.infra.tracer import tracer

    for name in ('src.agent.harness', 'src.agent.tools', 'src.llm.client', 'src.core.knowledge',
                 'src.api.observability', 'scripts.run_frozen_agent_eval'):
        module = importlib.import_module(name)
        monkeypatch.setattr(module, 'tracer', tracer)


def corpus():
    from src.agent.frozen_sources import FrozenSources

    return FrozenSources(
        [{'_id': 'faq', 'title': '权限', 'text': '管理员可以设置权限。'},
         {'_id': 'manual', 'title': '迁移', 'text': '提供原表样例和目标多维表。'}],
        [{'document_id': 'faq', 'source_document_id': 'faq', 'source_type': 'knowledge_base'},
         {'document_id': 'manual', 'source_document_id': 'manual', 'source_type': 'internal',
          'url': 'https://example.feishu.cn/docx/manual'}],
    )


def runtime(tmp_path):
    from src.agent.memory import MemoryManager
    from src.agent.support import SupportProfile
    from src.infra.session_store import SessionStore

    profile = SupportProfile()
    memory = MemoryManager(str(tmp_path / 'memory'), config=profile.memory_config(),
                           store=SessionStore(str(tmp_path / 'session.db')))
    return profile.runtime(corpus(), memory)


def understanding(route='faq', mode='answer'):
    return {'goal': '确认权限设置', 'known_conditions': ['管理员'], 'missing_conditions': [],
            'route': route, 'answer_mode': mode}


def scripted_model(monkeypatch, plans, answers=('管理员可以设置权限。',)):
    from src.llm import llm_client

    plans = iter(plans)
    answers = iter(answers)
    seen = []
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)

    async def plan(messages, **kwargs):
        llm_client._record_chat_span(time.perf_counter(), 'offline', cache_hit=False, usage=usage)
        return json.dumps(next(plans), ensure_ascii=False)

    async def answer(messages, **kwargs):
        seen.append(messages)
        try:
            yield next(answers)
        finally:
            llm_client._record_chat_span(time.perf_counter(), 'offline', cache_hit=False, usage=usage,
                                         first_token_ms=0.1)

    monkeypatch.setattr(llm_client, 'chat_async', plan)
    monkeypatch.setattr(llm_client, 'chat_stream_async', answer)
    return seen


def tool_plan(name='search_knowledge_base', **params):
    return {'action': 'tool_call', 'tool_name': name, 'tool_params': params,
            'understanding': understanding()}


def final_plan(mode='answer'):
    return {'action': 'final_answer', 'understanding': understanding(mode=mode)}


def test_observation_has_linked_events_intent_visible_evidence_and_usage(tmp_path, monkeypatch):
    from src.core.agent_runtime import AgentEventKind
    from src.infra.tracer import tracer

    agent = runtime(tmp_path)
    seen = scripted_model(monkeypatch, [tool_plan(query='权限'), final_plan()])
    trace = tracer.start_trace('test', 'acceptance')

    async def run():
        token = tracer.bind(trace)
        try:
            with tracer.context(task_id='task', turn=1, iteration=99):
                return [event async for event in agent.events('one', '管理员能设置权限吗？')]
        finally:
            tracer.reset(token)

    events = asyncio.run(run())
    response = events[-1].response
    assert response.usage['total_tokens'] == 45
    assert response.usage['reported']
    assert response.first_answer_token_ms is not None
    assert response.tool_calls[0].attempts == 1
    assert response.tool_calls[0].status == 'succeeded'
    assert {event.telemetry['execution_id'] for event in events} == {response.execution_id}
    assert {event.telemetry['trace_id'] for event in events} == {trace['trace_id']}
    assert all(event.telemetry['turn'] == 1 for event in events)
    assert [event.telemetry['sequence'] for event in events] == list(range(1, len(events) + 1))
    times = [event.telemetry['elapsed_ms'] for event in events]
    assert times == sorted(times)
    decision = next(event for event in events if event.kind is AgentEventKind.DECISION)
    assert decision.to_dict()['iteration'] == 1
    assert decision.observation['understanding'] == understanding()
    assert decision.observation['understanding_errors'] == []
    assert json.loads(decision.observation['raw_response'])['understanding'] == understanding()
    tool = next(event for event in events if event.kind is AgentEventKind.TOOL_RESULT)
    assert tool.observation['model_content'] in [message['content'] for message in seen[-1]]
    assert 'observation' not in tool.to_dict()
    ids = {span['span_id'] for span in trace['spans']}
    assert len(ids) == len(trace['spans'])
    assert all(span['parent_span_id'] is None or span['parent_span_id'] in ids for span in trace['spans'])
    assert all(span['started_at_unix_ms'] <= span['ended_at_unix_ms'] for span in trace['spans'])


def test_budget_failure_closes_proposal_and_keeps_usage(tmp_path, monkeypatch):
    agent = runtime(tmp_path)
    agent.config = replace(agent.config, max_tool_calls=0)
    scripted_model(monkeypatch, [tool_plan(query='权限')])
    response = asyncio.run(agent.execute('budget', '权限'))
    assert response.error_code == response.termination_reason == 'agent_tool_budget_exceeded'
    assert response.iterations == 1
    assert response.usage['total_tokens'] == 15
    assert response.tool_calls[0].status == 'aborted'
    assert response.tool_calls[0].attempts == 0
    assert response.total_latency_ms > 0


def test_truncated_evidence_matches_actual_final_input(tmp_path, monkeypatch):
    from src.core.agent_runtime import AgentEventKind

    agent = runtime(tmp_path)
    agent.config = replace(agent.config, max_tool_result_length=32)
    seen = scripted_model(monkeypatch, [tool_plan(query='权限'), final_plan()])

    async def run():
        return [event async for event in agent.events('truncated', '权限')]

    events = asyncio.run(run())
    result = next(event for event in events if event.kind is AgentEventKind.TOOL_RESULT)
    assert result.observation['truncated']
    assert result.observation['visible_chars'] <= 32
    assert result.observation['model_content'] in [message['content'] for message in seen[-1]]


def test_turn_usage_is_separate_and_clarification_history_is_preserved(tmp_path, monkeypatch):
    from scripts.run_frozen_agent_eval import execute_turn
    from src.infra.tracer import tracer

    agent = runtime(tmp_path)
    seen = scripted_model(monkeypatch, [final_plan('clarify'), tool_plan(query='权限'), final_plan()],
                          ('请补充使用的入口。', '管理员可以设置权限。'))
    trace = tracer.start_trace('turns', 'test')

    async def run():
        token = tracer.bind(trace)
        try:
            first, _ = await execute_turn(agent, 'turns', '权限不生效', tmp_path / 'events.jsonl', 't', 1)
            second, _ = await execute_turn(agent, 'turns', '我是管理员，在设置入口', tmp_path / 'events.jsonl', 't', 2)
            return first, second
        finally:
            tracer.reset(token)

    first, second = asyncio.run(run())
    assert first.usage['total_tokens'] == 30
    assert second.usage['total_tokens'] == 45
    assert first.execution_id != second.execution_id
    assert '请补充使用的入口。' in json.dumps(seen[-1], ensure_ascii=False)
    assert {span['turn'] for span in trace['spans'] if 'model' in span['attributes']} == {1, 2}


@pytest.mark.parametrize('endpoint', ['/agent/chat', '/agent/chat/stream'])
def test_application_entry_uses_shared_support_assembly(tmp_path, monkeypatch, temporary_sessions, endpoint):
    from fastapi.testclient import TestClient

    import app as api
    from config.settings import settings
    from src.agent.support import SupportProfile

    sources = corpus()
    (tmp_path / 'corpus.jsonl').write_text('\n'.join(json.dumps(row) for row in sources.passages.values()), encoding='utf-8')
    (tmp_path / 'metadata.jsonl').write_text('\n'.join(json.dumps(row) for row in sources.metadata.values()), encoding='utf-8')
    profile = tmp_path / 'profile.json'
    profile.write_text(json.dumps({'corpus': 'corpus.jsonl', 'metadata': 'metadata.jsonl',
                                  'runtime_dir': 'support-runtime', 'retrieval_mode': 'bm25_only',
                                  'official': 'off'}), encoding='utf-8')
    monkeypatch.setattr(settings, 'agent_support_profile', profile)
    monkeypatch.setattr(api, 'agent_runtime', api.agent_runtime)
    monkeypatch.setattr(api.app, 'middleware_stack', None)
    monkeypatch.setattr(api.app.router, 'on_startup', [
        handler for handler in api.app.router.on_startup if handler is not api.warm_up_runtime
    ])
    asyncio.run(api.open_agent_runtime())
    assert api.agent_runtime.config == SupportProfile().config()
    scripted_model(monkeypatch, [tool_plan(query='权限'), final_plan()])
    with TestClient(api.app) as client:
        result = client.post(endpoint, json={'message': '管理员可以设置权限吗？', 'session_id': 'support-http'})
    assert result.status_code == 200
    if endpoint.endswith('/stream'):
        events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith('data: ')]
        response = next(event for event in events if event.get('done'))
    else:
        response = result.json()
    assert response['answer'] == '管理员可以设置权限。'
    assert response['trace_id'] == result.headers['x-trace-id']
    assert response['usage']['total_tokens'] == 45
    assert response['tool_calls'][0]['status'] == 'succeeded'
    assert (tmp_path / 'support-runtime/trajectory.jsonl').exists()


def test_all_attempt_usage_counts_failed_attempts_and_judging_once(tmp_path):
    from src.evaluation.experiment_usage import experiment_usage

    paths = ['internal/generation/attempt-001/run/errors.jsonl',
             'internal/generation/attempt-002/run/results.jsonl',
             'internal/scoring/business/attempt-001/scores.jsonl', 'internal/results.jsonl']
    for name in paths:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'usage': {'total_tokens': 15, 'model_calls': 1, 'reported': True}}) + '\n')
    usage = experiment_usage(tmp_path)
    assert usage['generation']['reported_tokens'] == 30
    assert usage['judging']['reported_tokens'] == 15


def test_shared_profile_drives_evaluation_and_application_identically(tmp_path, monkeypatch, temporary_sessions):
    from dataclasses import asdict

    from scripts.run_frozen_agent_eval import parser, run
    from src.agent.support import load_support_runtime

    sources = corpus()
    for name, values in [('corpus', sources.passages.values()), ('metadata', sources.metadata.values())]:
        (tmp_path / f'{name}.jsonl').write_text('\n'.join(json.dumps(value) for value in values), encoding='utf-8')
    profile = tmp_path / 'profile.json'
    profile.write_text(json.dumps({
        'profile': {'timeout': 90}, 'corpus': 'corpus.jsonl', 'metadata': 'metadata.jsonl',
        'runtime_dir': 'runtime', 'retrieval_mode': 'bm25_only', 'official': 'off',
        'retrieval_settings': {'rerank_timeout_seconds': 30},
    }), encoding='utf-8')
    task_file = tmp_path / 'tasks.jsonl'
    task_file.write_text(json.dumps({
        'task_id': 'profile', 'input': [{'speaker': 'user', 'text': '权限'}],
        'targets': [{'text': 'GOLD_NOT_A_MODEL_INPUT'}], 'contexts': [],
    }) + '\n', encoding='utf-8')
    agent = asyncio.run(load_support_runtime(profile))
    seen = scripted_model(monkeypatch, [tool_plan(query='权限'), final_plan()])
    args = parser().parse_args(['--tasks', str(task_file), '--support-profile', str(profile),
                                '--output', str(tmp_path / 'evaluation')])
    assert asyncio.run(run(args)) == 0
    configuration = json.loads((tmp_path / 'evaluation/run.json').read_text(encoding='utf-8'))
    assembly = json.loads((tmp_path / 'runtime/assembly.json').read_text(encoding='utf-8'))
    assert configuration['config'] == asdict(agent.config)
    assert configuration['retrieval_settings'] == assembly['retrieval_settings']
    assert configuration['retrieval_settings']['rerank_timeout_seconds'] == 30
    assert 'GOLD_NOT_A_MODEL_INPUT' not in json.dumps(seen)


def test_tool_retry_and_rejection_have_distinct_statuses(tmp_path, monkeypatch):
    from src.core.agent_runtime import ToolResult

    agent = runtime(tmp_path)
    attempts = []

    async def retry_once(params):
        attempts.append(params)
        if len(attempts) == 1:
            raise RuntimeError('synthetic transient failure')
        return ToolResult(success=True, data={'contexts': []})

    monkeypatch.setattr(agent.tools.get_tool('search_knowledge_base'), 'execute_async_fn', retry_once)
    scripted_model(monkeypatch, [tool_plan(query='权限'), tool_plan('unavailable_tool'), final_plan()])
    response = asyncio.run(agent.execute('retry-and-reject', '权限'))
    assert response.tool_calls[0].attempts == 2
    assert response.tool_calls[0].status == 'succeeded'
    assert response.tool_calls[1].attempts == 0
    assert response.tool_calls[1].status == 'rejected'


def test_native_planner_retains_structured_understanding(tmp_path, monkeypatch):
    from src.core.agent_runtime import AgentEventKind
    from src.llm import llm_client
    from src.llm.client import NativeChatResponse

    agent = runtime(tmp_path)
    agent.config = replace(agent.config, planner_mode='native')
    scripted_model(monkeypatch, [])

    async def native(*args, **kwargs):
        return NativeChatResponse(content=json.dumps(final_plan('clarify')), tool_calls=())

    monkeypatch.setattr(llm_client, 'chat_with_tools_async', native)

    async def run():
        return [event async for event in agent.events('native-observation', '权限')]

    events = asyncio.run(run())
    assert not events[-1].response.error_code
    decision = next(event for event in events if event.kind is AgentEventKind.DECISION)
    assert decision.observation['understanding']['answer_mode'] == 'clarify'
    context = next(event for event in events if event.kind is AgentEventKind.MODEL_CONTEXT)
    assert context.observation['tools']


def test_missing_usage_is_explicit():
    from src.infra.usage import usage_from_spans

    usage = usage_from_spans([{'attributes': {'model': 'test'}}])
    assert not usage['reported']
    assert usage['missing_usage_calls'] == 1
    assert usage['provider_request_attempts'] is None


def test_shared_profile_validates_boolean_and_numeric_retrieval_settings(tmp_path, monkeypatch):
    from config.settings import settings
    from src.agent.support import read_support_configuration

    monkeypatch.setattr(settings, 'query_rewrite_enabled', True)
    monkeypatch.setattr(settings, 'rerank_batch_size', 1)
    profile = tmp_path / 'profile.json'
    profile.write_text(json.dumps({'retrieval_settings': {'query_rewrite_enabled': False,
                                                         'rerank_batch_size': 2}}), encoding='utf-8')
    read_support_configuration(profile)
    assert settings.query_rewrite_enabled is False
    assert settings.rerank_batch_size == 2


def test_missing_intent_fields_are_observable_without_fabricating_them(tmp_path, monkeypatch):
    from src.core.agent_runtime import AgentEventKind

    agent = runtime(tmp_path)
    agent.config = replace(agent.config, require_understanding=False)
    scripted_model(monkeypatch, [{'action': 'final_answer'}])

    async def run():
        return [event async for event in agent.events('missing-intent', '权限')]

    events = asyncio.run(run())
    observation = next(event.observation for event in events if event.kind is AgentEventKind.DECISION)
    assert observation['understanding'] == {}
    assert set(observation['understanding_errors']) == {'goal', 'known_conditions', 'missing_conditions', 'route', 'answer_mode'}
    assert json.loads(observation['raw_response']) == {'action': 'final_answer'}
    assert not events[-1].response.error_code


def test_required_intent_repairs_once_and_reaches_final_input(tmp_path, monkeypatch):
    agent = runtime(tmp_path)
    seen = scripted_model(monkeypatch, [{'action': 'final_answer'}, final_plan()])
    response = asyncio.run(agent.execute('repair', '权限'))
    assert not response.error_code
    assert response.iterations == 2
    assert response.usage['total_tokens'] == 45
    intent = next(m['content'] for m in seen[-1] if 'Provisional planner understanding' in m['content'])
    assert '确认权限设置' in intent


def test_required_intent_blocks_mismatched_action_without_tool_execution(tmp_path, monkeypatch):
    agent = runtime(tmp_path)
    bad = tool_plan(query='权限')
    bad['understanding']['route'] = 'official'
    scripted_model(monkeypatch, [bad, bad])
    response = asyncio.run(agent.execute('mismatch', '权限'))
    assert response.error_code == 'invalid_agent_understanding'
    assert not response.tool_calls
    assert response.iterations == 2


def test_required_intent_repair_cannot_escape_iteration_budget(tmp_path, monkeypatch):
    agent = runtime(tmp_path)
    agent.config = replace(agent.config, max_iterations=1)
    scripted_model(monkeypatch, [{'action': 'final_answer'}])
    response = asyncio.run(agent.execute('limited', '权限'))
    assert response.error_code == 'invalid_agent_understanding'
    assert response.iterations == 1


def test_public_and_native_profiles_preserve_existing_contract():
    from src.agent.support import SupportProfile

    assert not SupportProfile(suite='public').config().require_understanding
    assert not SupportProfile(planner='native').config().require_understanding


def test_tool_failure_trace_links_call_and_omits_private_exception_text(tmp_path, monkeypatch):
    from src.infra.tracer import tracer

    agent = runtime(tmp_path)

    async def broken(params):
        raise OSError(12, 'PRIVATE_ERROR_PAYLOAD')

    monkeypatch.setattr(agent.tools.get_tool('search_knowledge_base'), 'execute_async_fn', broken)
    scripted_model(monkeypatch, [tool_plan(query='权限'), final_plan()])
    trace = tracer.start_trace('diagnostics', 'test')

    async def run():
        token = tracer.bind(trace)
        try:
            return await agent.execute('diagnostics', '权限')
        finally:
            tracer.reset(token)

    response = asyncio.run(run())
    assert response.tool_calls[0].error_code == 'tool_io_failed'
    failures = [span for span in trace['spans'] if span['name'] == 'tool_exception']
    assert failures
    assert all(span['attributes']['call_id'] == response.tool_calls[0].call_id for span in failures)
    assert failures[0]['attributes']['errno'] == 12
    assert failures[0]['attributes']['frames'][-1]['function'] == 'broken'
    assert 'PRIVATE_ERROR_PAYLOAD' not in json.dumps(trace)


def test_sdk_retry_attempts_are_observed_without_request_data(monkeypatch):
    import httpx
    from openai import AsyncOpenAI

    from src.infra.tracer import tracer
    from src.infra.usage import usage_from_spans
    from src.llm import llm_client
    from src.llm.client import record_provider_request_async

    attempts = []

    def provider(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(503, json={'error': {'message': 'temporary'}})
        return httpx.Response(200, json={
            'id': 'offline', 'object': 'chat.completion', 'created': 0, 'model': 'offline',
            'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'ok'}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12},
        })

    async def run():
        trace = tracer.start_trace('retry', 'offline')
        token = tracer.bind(trace)
        client = AsyncOpenAI(api_key='offline', base_url='https://offline.example/v1', max_retries=1,
                             http_client=httpx.AsyncClient(transport=httpx.MockTransport(provider),
                                                          event_hooks={'request': [record_provider_request_async]}))
        monkeypatch.setattr(llm_client, '_async_chat_client', client)
        try:
            await llm_client.chat_async([{'role': 'user', 'content': 'private-marker'}], skip_cache=True)
            return trace
        finally:
            tracer.reset(token)
            await client.close()

    trace = asyncio.run(run())
    usage = usage_from_spans(trace['spans'])
    assert usage['provider_request_attempts'] == 2
    assert usage['provider_retries'] == 1
    assert usage['total_tokens'] == 12
    assert 'private-marker' not in json.dumps(trace)
    assert 'offline.example' not in json.dumps(trace)


def test_recorded_business_scenarios(tmp_path, monkeypatch):
    """Optional local acceptance over saved decisions and tool responses."""
    source_name = os.environ.get('SUPPORT_REPLAY_SOURCE')
    output_name = os.environ.get('SUPPORT_REPLAY_OUTPUT')
    if not source_name or not output_name:
        pytest.skip('Saved internal runs are supplied only for local acceptance')
    from collections import deque

    from scripts.run_frozen_agent_eval import execute_turn
    from src.agent.frozen_sources import FrozenSources
    from src.agent.memory import MemoryManager
    from src.agent.support import SupportProfile
    from src.core.agent_runtime import ToolResult
    from src.infra.session_store import SessionStore
    from src.infra.tracer import tracer

    source = Path(source_name)
    output = Path(output_name)
    output.mkdir(parents=True, exist_ok=False)
    project = Path(__file__).resolve().parents[2]

    def rows(path):
        return [json.loads(line) for line in path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]

    sources = FrozenSources(rows(source / '数据/internal/corpus.jsonl'), rows(source / '数据/internal/metadata.jsonl'))
    profile = SupportProfile()
    summary = []
    for suite in ('coverage', 'live', 'internal'):
        directory = source / '执行记录/baseline-v1-serial' / suite
        selection = json.loads((directory / 'selection.json').read_text(encoding='utf-8'))['selected_attempt']
        selected_results = rows(directory / 'results.jsonl')
        if suite == 'internal':
            selected_results = [row for row in selected_results if row['task_id'] in {
                'as-combined-onboard-seat::1', 'as-migration-history::2',
            }]
        for index, row in enumerate(selected_results):
            task_id = row['task_id']
            session_id = f'replay-{suite}-{index}'
            attempt = Path(selection.get(task_id, directory / 'generation/attempt-001/run'))
            if not attempt.is_absolute():
                attempt = project / attempt
            recorded = [event for event in rows(attempt / 'trace.jsonl') if event['task_id'] == task_id]
            results = {event['call_id']: event['tool_result'] for event in recorded if 'tool_result' in event}
            expected_calls = deque(event for event in recorded if event.get('step') == 'tool_call'
                                   and event['call_id'] in results
                                   and results[event['call_id']].get('error_code') != 'duplicate_tool_call')
            case_dir = output / f'{suite}-{index + 1:02d}'
            case_dir.mkdir()
            memory = MemoryManager(str(tmp_path / suite / str(index)), config=profile.memory_config(),
                                   store=SessionStore(str(tmp_path / f'{suite}-{index}.db')))
            official = SimpleNamespace(search=lambda _: None, read=lambda _: None)
            agent = profile.runtime(sources, memory, official)
            for item in agent.tools.status_snapshot():
                name = item['name']

                async def replay(params, name=name):
                    event = expected_calls.popleft()
                    assert event['tool'] == name
                    assert all(params.get(key) == value for key, value in event['params'].items())
                    original = results[event['call_id']]
                    return ToolResult(success=original['success'], data=original.get('data'),
                                      error=original.get('error', ''), error_code=original.get('error_code', ''))

                monkeypatch.setattr(agent.tools.get_tool(name), 'execute_async_fn', replay)
            plans = []
            answers = []
            for turn in sorted({event['turn'] for event in recorded}):
                plans.extend({'action': 'tool_call', 'tool_name': event['tool'], 'tool_params': event['params']}
                             for event in recorded if event['turn'] == turn and event.get('step') == 'tool_call')
                if any(event.get('done') for event in recorded if event['turn'] == turn):
                    plans.append({'action': 'final_answer'})
                    answers.append(''.join(event.get('chunk', '') for event in recorded if event['turn'] == turn))
            scripted_model(monkeypatch, plans, answers)
            trace = tracer.start_trace(task_id, 'recorded_acceptance')

            async def run_case():
                token = tracer.bind(trace)
                responses = []
                try:
                    with tracer.context(task_id=task_id, run_id='recorded-acceptance'):
                        if suite == 'live':
                            questions = [row['input'][0]['text'], row['input'][2]['text']]
                        else:
                            history = row.get('input', [])
                            if not history:
                                tasks = rows(source / '数据/internal/tasks.jsonl')
                                history = next(task['input'] for task in tasks if task['task_id'] == task_id)
                            for message in history[:-1]:
                                memory.add_turn(session_id, 'assistant' if message['speaker'] == 'agent' else message['speaker'], message['text'])
                            questions = [history[-1]['text']]
                        for turn, question in enumerate(questions, 1):
                            response, _ = await execute_turn(agent, session_id, question, case_dir / 'trajectory.jsonl', task_id, turn)
                            responses.append(response)
                        return responses
                finally:
                    tracer.reset(token)

            responses = asyncio.run(run_case())
            assert responses[-1].error_code == row.get('error_code', '')
            assert not expected_calls
            events = rows(case_dir / 'trajectory.jsonl')
            proposals = {event['call_id'] for event in events if event.get('step') == 'tool_call'}
            outcomes = {event['call_id'] for event in events if event.get('step') == 'tool_done'}
            assert proposals == outcomes
            assert all(event.get('trace_id') and event.get('execution_id') and event.get('timestamp_unix_ms') for event in events)
            report = {'task_id': task_id, 'suite': suite, 'source_attempt': str(attempt),
                      'mode': 'recorded_model_and_tool_replay',
                      'usage_note': 'Synthetic provider usage validates accounting only.',
                      'responses': [response.to_dict() for response in responses], 'spans': trace['spans']}
            (case_dir / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            summary.append({'task_id': task_id, 'turns': len(responses), 'tool_proposals': len(proposals),
                            'tool_outcomes': len(outcomes), 'error_code': responses[-1].error_code,
                            'intent_observation': 'historical_records_have_no_structured_intent',
                            'acceptance': 'passed'})
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    assert len(summary) == 14
