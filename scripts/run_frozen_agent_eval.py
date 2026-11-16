#!/usr/bin/env python
"""Run MTRAG-shaped tasks or two-turn scenarios through the production harness.

Model-facing inputs are explicit projections. Gold answers stay in result files.
This runner records observations; scoring is a separate, versioned operation.
"""

import argparse
import asyncio
import hashlib
import json
import sys
import time
import uuid
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import settings  # noqa: E402
from src.agent.frozen_sources import FrozenSources  # noqa: E402
from src.agent.memory import MemoryManager  # noqa: E402
from src.agent.official_help import OfficialHelp  # noqa: E402
from src.agent.support import (  # noqa: E402
    SupportProfile,
    effective_retrieval_settings,
    prepare_sources,
    read_support_configuration,
)
from src.core.agent_runtime import AgentEventKind, AgentResponse  # noqa: E402
from src.core.run_options import (  # noqa: E402
    RunOptions,
    scoped_run_options,
)
from src.evaluation.evidence import visible_tool_evidence  # noqa: E402
from src.evaluation.usage import usage_from_spans  # noqa: E402
from src.infra.session_store import SessionStore  # noqa: E402
from src.infra.tracer import tracer  # noqa: E402
from src.llm.cache_context import scoped_cache_identity  # noqa: E402


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def append(path, row):
    with path.open('a', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')


def model_messages(task):
    history = task['input']
    if not history or history[-1]['speaker'] != 'user':
        raise ValueError('A fixed-history task must end with a user message')
    roles = {'user': 'user', 'agent': 'assistant', 'assistant': 'assistant'}
    return [{'role': roles[turn['speaker']], 'content': turn['text']} for turn in history]


async def execute_turn(runtime, session_id, question, trace_file, task_id, turn, visible_evidence=None):
    contexts = {}
    response = None
    with tracer.context(turn=turn):
        async for event in runtime.events(session_id, question):
            record = event.to_record()
            if event.kind == AgentEventKind.TOOL_RESULT and event.tool_result:
                data = event.tool_result.data
                if event.tool_result.success and isinstance(data, dict):
                    for context in data.get('contexts', []):
                        contexts.setdefault(context['document_id'], context)
                    for hit in data.get('results', []):
                        pid = 'search-' + hashlib.sha256(hit['url'].encode('utf-8')).hexdigest()[:16]
                        contexts.setdefault(pid, {'document_id': pid, 'title': hit['title'],
                                                  'text': hit['snippet'], 'score': 1.0,
                                                  'url': hit['url'], 'evidence_kind': 'search_snippet'})
            if event.kind == AgentEventKind.MODEL_CONTEXT and event.observation.get('stage') == 'final_generation' and visible_evidence is not None:
                visible_evidence.clear()
                visible_evidence.extend(visible_tool_evidence(event.observation['messages']))
            if event.response:
                response = event.response
            elif event.kind == AgentEventKind.ERROR:
                response = AgentResponse(session_id=session_id, answer='',
                                         error_code=event.error_code, error=event.message)
            append(trace_file, {'task_id': task_id, 'turn': turn, **record})
    if response is None:
        raise RuntimeError('Agent completed without a terminal response')
    return response, list(contexts.values())


async def run(args):
    if getattr(args, 'support_profile', None):
        document = read_support_configuration(args.support_profile)
        profile = SupportProfile(**document.get('profile', {}))
        args.corpus, args.metadata = document['corpus'], document['metadata']
        args.planner, args.timeout = profile.planner, profile.timeout
        args.response_style, args.suite = profile.response_style, profile.suite
        args.retrieval_mode = document.get('retrieval_mode', 'hybrid+rerank')
        args.index_dir = document['runtime_dir'] / 'indexes'
        args.official = document.get('official', 'live')
        args.official_proxy = document.get('official_proxy')
        args.official_replay = document.get('official_replay')
    if args.limit is not None and args.limit < 1:
        raise ValueError('limit must be positive')
    if args.max_consecutive_errors < 0:
        raise ValueError('max_consecutive_errors must be nonnegative')
    if args.mode == 'live' and args.suite != 'support':
        raise ValueError('Live two-turn scenarios are an internal protocol')
    if args.manifest:
        manifest = json.loads(args.manifest.read_text(encoding='utf-8-sig'))
        for relative, checksum in manifest['file_sha256'].items():
            if digest(args.manifest.parent / relative) != checksum:
                raise ValueError('Frozen manifest checksum mismatch')
    tasks = rows(args.tasks)
    environments = json.loads(args.environments.read_text(encoding='utf-8')) if args.environments else {}
    if args.official == 'replay' and not args.official_replay:
        raise ValueError('Replay requires an official response directory')
    if args.task_id:
        requested = set(args.task_id)
        tasks = [task for task in tasks if task['task_id'] in requested]
        if {task['task_id'] for task in tasks} != requested:
            raise ValueError('Requested task ID absent')
    if args.limit:
        tasks = tasks[:args.limit]
    if not tasks or len({task['task_id'] for task in tasks}) != len(tasks):
        raise ValueError('Expected nonempty tasks with unique identifiers')
    args.output.mkdir(parents=True, exist_ok=False)
    profile = SupportProfile(planner=args.planner, timeout=args.timeout,
                             response_style=args.response_style, suite=args.suite)
    config = profile.config()
    instructions = profile.instructions
    options = RunOptions('flash', settings.llm_flash_model)
    sources = None
    index_info = None
    if args.mode in {'agent', 'live'}:
        if not args.corpus or not args.metadata:
            raise ValueError('Agent modes require corpus and source metadata')
        sources = FrozenSources(rows(args.corpus), rows(args.metadata))
        index_info = await prepare_sources(sources, args.retrieval_mode, args.index_dir)
    empty_sources = FrozenSources([], []) if any(e.get('local_sources') == 'empty' for e in environments.values()) else None
    files = [p for p in [args.tasks, args.corpus, args.metadata, args.manifest, args.environments,
                        getattr(args, 'support_profile', None)] if p]
    run_id = uuid.uuid4().hex
    configuration = {
        'run_id': run_id, 'profile_version': 'support-v2-observable',
        'mode': args.mode, 'suite': args.suite, 'model': options.model,
        'retriever': args.retrieval_mode, 'index': index_info, 'config': asdict(config),
        'response_style': args.response_style if args.suite == 'support' else 'public_original',
        'retrieval_settings': effective_retrieval_settings(),
        'instructions': instructions, 'input_sha256': {str(p): digest(p) for p in files},
        'task_ids': [t['task_id'] for t in tasks], 'status': 'running',
        'scoring': 'not_run', 'protocol': 'internal_live' if args.mode == 'live' else 'mtrag_shaped',
        'official_mode': args.official, 'official_proxy': args.official_proxy,
        'official_replay': str(args.official_replay) if args.official_replay else None,
        'fresh_cache': args.fresh_cache,
        'max_consecutive_errors': args.max_consecutive_errors,
        'code_sha256': {
            str(path): digest(path) for path in [
                Path(__file__),
                Path(__file__).resolve().parents[1] / 'src/agent/support.py',
                Path(__file__).resolve().parents[1] / 'src/agent/support_instructions.py',
                Path(__file__).resolve().parents[1] / 'src/agent/observation.py',
                Path(__file__).resolve().parents[1] / 'src/infra/tracer.py',
                Path(__file__).resolve().parents[1] / 'src/infra/usage.py',
                Path(__file__).resolve().parents[1] / 'src/agent/frozen_sources.py',
                Path(__file__).resolve().parents[1] / 'src/agent/official_help.py',
                Path(__file__).resolve().parents[1] / 'src/agent/harness.py',
                Path(__file__).resolve().parents[1] / 'src/core/retrieval.py',
                Path(__file__).resolve().parents[1] / 'src/core/knowledge.py',
                Path(__file__).resolve().parents[1] / 'src/evaluation/frozen_index.py',
            ]
        },
    }
    config_path = args.output / 'run.json'
    config_path.write_text(json.dumps(configuration, ensure_ascii=False, indent=2), encoding='utf-8')
    completed, failed, consecutive_errors = 0, 0, 0
    for task in tasks:
        started = time.perf_counter()
        session_id = 'eval-' + uuid.uuid4().hex
        trace = tracer.start_trace(session_id, 'frozen_agent_evaluation')
        trace_token = tracer.bind(trace)
        cache_scope = ExitStack()
        cache_scope.enter_context(tracer.context(run_id=run_id, task_id=task['task_id'], attempt_id=str(args.output.resolve())))
        visible_evidence = []
        failure_response = None
        if args.fresh_cache:
            cache_scope.enter_context(scoped_cache_identity(session_id))
        try:
            if args.mode == 'reference':
                from src.llm import llm_client

                messages = model_messages(task)
                evidence = json.dumps(task['contexts'], ensure_ascii=False)
                messages.insert(0, {'role': 'system', 'content': instructions})
                messages.insert(1, {'role': 'user', 'content': '[REFERENCE_DATA]\n' + evidence + '\n[/REFERENCE_DATA]'})
                answer = await llm_client.chat_async(messages, model=options.model,
                                                     temperature=0, max_tokens=1600)
                result = {**task, 'predictions': [{'text': answer}], 'error_code': ''}
            else:
                memory = MemoryManager(
                    str(args.output / 'memory'),
                    config=profile.memory_config(),
                    store=SessionStore(str(args.output / 'sessions.db')),
                )
                environment = environments.get(task['task_id'], {})
                official = None
                if args.official != 'off':
                    official = OfficialHelp(
                        record_dir=args.output / 'official_http', proxy=args.official_proxy,
                        replay_dir=args.official_replay if args.official == 'replay' else None,
                        read_failure=environment.get('official_read_failure'),
                        search_failure=environment.get('official_search_failure'),
                    )
                task_sources = empty_sources if environment.get('local_sources') == 'empty' else sources
                runtime = profile.runtime(task_sources, memory, official)
                if args.mode == 'live':
                    with scoped_run_options(options):
                        first, first_contexts = await execute_turn(
                            runtime, session_id, task['first_user'], args.output / 'trace.jsonl', task['task_id'], 1, visible_evidence,
                        )
                        if first.error_code:
                            failure_response = first
                            raise RuntimeError('First turn failed: ' + first.error_code)
                        final, last_contexts = await execute_turn(
                            runtime, session_id, task['user_supplement'], args.output / 'trace.jsonl', task['task_id'], 2, visible_evidence,
                        )
                    all_contexts = {c['document_id']: c for c in first_contexts + last_contexts}
                    result = {
                        'task_id': task['task_id'], 'Collection': 'support-local-v1',
                        'input': [{'speaker': 'user', 'text': task['first_user']},
                                  {'speaker': 'agent', 'text': first.answer},
                                  {'speaker': 'user', 'text': task['user_supplement']}],
                        'contexts': list(all_contexts.values())[:10],
                        'observed_contexts': list(all_contexts.values()),
                        'first_response': first.to_dict(), 'final_response': final.to_dict(),
                        'targets': [{'speaker': 'agent', 'text': task['final_reference']}],
                        'predictions': [{'text': final.answer}], 'error_code': final.error_code,
                        'observed_context_count': len(all_contexts),
                        'first_turn_expectation': task['first_turn_expectation'],
                    }
                else:
                    messages = model_messages(task)
                    for message in messages[:-1]:
                        memory.add_turn(session_id, message['role'], message['content'])
                    with scoped_run_options(options):
                        response, contexts = await execute_turn(
                            runtime, session_id, messages[-1]['content'], args.output / 'trace.jsonl', task['task_id'], 1, visible_evidence,
                        )
                    result = {**task, 'contexts': contexts[:10], 'observed_context_count': len(contexts),
                              'observed_contexts': contexts,
                              'predictions': [{'text': response.answer}], 'response': response.to_dict(),
                              'error_code': response.error_code}
                result['model_visible_contexts'] = visible_evidence
                result['evidence_policy'] = 'final_model_visible_tool_messages'
                result['context_export_policy'] = 'contexts_first_10; observed_contexts_and_trace_complete'
            result['trace_id'] = trace['trace_id']
            result['run_id'] = run_id
            result['attempt_id'] = str(args.output.resolve())
            result['elapsed_seconds'] = round(time.perf_counter() - started, 3)
            result['usage'] = usage_from_spans(trace['spans'])
            result['trace_spans'] = trace['spans']
            result['dropped_spans'] = trace['dropped_spans']
            append(args.output / 'results.jsonl', result)
            if result['error_code']:
                failed += 1
                consecutive_errors += 1
            else:
                completed += 1
                consecutive_errors = 0
            print(task['task_id'], 'error' if result['error_code'] else 'complete', flush=True)
        except Exception as error:
            failed += 1
            consecutive_errors += 1
            # Provider exceptions can contain URLs or keys. Store stable type only.
            append(args.output / 'errors.jsonl', {
                'task_id': task['task_id'], 'error_type': type(error).__name__,
                'error_code': failure_response.error_code if failure_response else 'evaluation_failed',
                'response': failure_response.to_dict() if failure_response else None,
                'elapsed_seconds': time.perf_counter() - started,
                'trace_id': trace['trace_id'], 'run_id': run_id, 'attempt_id': str(args.output.resolve()),
                'usage': usage_from_spans(trace['spans']), 'trace_spans': trace['spans'],
            })
            print(task['task_id'], type(error).__name__, flush=True)
        finally:
            cache_scope.close()
            tracer.reset(trace_token)
        if args.max_consecutive_errors and consecutive_errors >= args.max_consecutive_errors:
            break
    configuration.update(status='completed' if failed == 0 else 'completed_with_errors',
                         completed=completed, failed=failed)
    config_path.write_text(json.dumps(configuration, ensure_ascii=False, indent=2), encoding='utf-8')
    return 1 if failed else 0


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--tasks', type=Path, required=True)
    result.add_argument('--support-profile', type=Path, help='Shared application profile; source and runtime options come from this file')
    result.add_argument('--corpus', type=Path)
    result.add_argument('--metadata', type=Path)
    result.add_argument('--manifest', type=Path)
    result.add_argument('--output', type=Path, required=True)
    result.add_argument('--mode', choices=['agent', 'reference', 'live'], default='agent')
    result.add_argument('--suite', choices=['support', 'public'], default='support')
    result.add_argument('--planner', choices=['json', 'native'], default='json')
    result.add_argument('--retrieval-mode', choices=['bm25_only', 'vector_only', 'hybrid', 'hybrid+rerank'],
                        default='bm25_only')
    result.add_argument('--index-dir', type=Path, help='Isolated content-addressed evaluation indexes')
    result.add_argument('--response-style', choices=['original', 'concise'], default='concise')
    result.add_argument('--timeout', type=float, default=180)
    result.add_argument('--limit', type=int)
    result.add_argument('--task-id', action='append')
    result.add_argument('--fresh-cache', action='store_true', help='Isolate model cache for each task')
    result.add_argument('--max-consecutive-errors', type=int, default=0,
                        help='Stop an attempt after this many consecutive execution failures; zero disables')
    result.add_argument('--official', choices=['off', 'live', 'replay'], default='off')
    result.add_argument('--official-proxy', help='Optional HTTPX proxy URL, e.g. socks5://127.0.0.1:10808')
    result.add_argument('--official-replay', type=Path)
    result.add_argument('--environments', type=Path, help='Per-task controlled source availability and injected failures')
    return result


if __name__ == '__main__':
    from src.infra.chroma_clients import close_chroma_clients

    try:
        sys.exit(asyncio.run(run(parser().parse_args())))
    finally:
        close_chroma_clients()
