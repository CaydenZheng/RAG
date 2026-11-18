"""Score saved support answers against versioned atomic criteria; never run the Agent."""
import argparse
import asyncio
import hashlib
import json
import sys
import time
import uuid
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from scripts.score_support_agent_eval import (  # noqa: E402
    digest,
    load,
    provider_failure,
    save,
)
from src.evaluation.atomic_grading import (  # noqa: E402
    JUDGE_INSTRUCTIONS,
    prepare_atomic_input,
    validate_atomic_judgment,
    validate_spec,
)


def content_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def run(args):
    from config.settings import settings
    from src.infra.tracer import tracer
    from src.infra.usage import usage_from_spans
    from src.llm import llm_client

    protocol = {'name': 'atomic-v1.1-citation-contracts', 'instructions': JUDGE_INSTRUCTIONS,
                'module_sha256': digest(PROJECT / 'src/evaluation/atomic_grading.py'),
                'projection_sha256': digest(PROJECT / 'src/evaluation/support_grading.py'),
                'runner_sha256': digest(Path(__file__)), 'model': settings.llm_flash_model,
                'thinking': args.judge_thinking, 'temperature': 0, 'max_tokens': 20000,
                'selection': 'first mechanically valid; at most two attempts; no score selection',
                'calibration': 'exposed development cases; not held-out evaluator accuracy'}
    protocol_path = args.output / 'protocol.json'
    if protocol_path.exists() and load(protocol_path) != protocol:
        raise ValueError('Protocol changed: use a new output directory')
    save(protocol_path, protocol)
    specs = [json.loads(line) for line in args.specs.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    if len({s['number'] for s in specs}) != len(specs):
        raise ValueError('Duplicate case number')
    if args.only:
        requested = set(args.only.split(','))
        if not requested <= {s['number'] for s in specs}:
            raise ValueError('Unknown requested case')
        specs = [s for s in specs if s['number'] in requested]
    summary = []
    for spec in specs:
        validate_spec(spec)
        number = spec['number']
        packet_path = args.packets / f'{number}-完整调用与结果.json'
        packet = load(packet_path)
        target = args.output / number
        binding = {'packet_sha256': digest(packet_path), 'spec_sha256': content_hash(spec),
                   'protocol_sha256': digest(protocol_path)}
        selected = target / 'selected.json'
        if selected.exists():
            record = load(selected)
            if record.get('binding') != binding:
                raise ValueError('Saved result binding changed')
            summary.append(record)
            continue
        if packet['result'].get('error_code') and not packet['result'].get('predictions'):
            record = {'number': number, 'status': 'execution_failed', 'binding': binding,
                      'business_status': 'fail', 'Q': None, 'P': None}
            save(selected, record)
            summary.append(record)
            continue
        payload = prepare_atomic_input(spec, packet)
        input_path = target / 'judge-input.json'
        if input_path.exists() and load(input_path) != payload:
            raise ValueError('Saved judge input changed')
        save(input_path, payload)
        messages = [{'role': 'system', 'content': JUDGE_INSTRUCTIONS},
                    {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]
        if args.prepare_only:
            summary.append({'number': number, 'status': 'prepared', 'binding': binding})
            continue
        for attempt in range(1, 3):
            path = target / f'attempt-{attempt:03d}.json'
            if path.exists():
                record = load(path)
                if record.get('binding') != binding:
                    raise ValueError('Saved attempt binding changed')
            else:
                trace = tracer.start_trace(uuid.uuid4().hex, 'support_atomic_judge')
                token = tracer.bind(trace)
                started = time.perf_counter()
                record = {'number': number, 'status': 'judge_failed', 'binding': binding,
                          'input_sha256': digest(input_path)}
                save(target / f'messages-{attempt:03d}.json', messages)
                try:
                    raw = await llm_client.chat_async(messages, model=settings.llm_flash_model,
                                                     temperature=0, max_tokens=protocol['max_tokens'],
                                                     skip_cache=True, enable_thinking=args.judge_thinking)
                    record['raw_judgment'] = raw
                    text = raw.strip()
                    if text.startswith('```json') and text.endswith('```'):
                        text = text[7:-3].strip()
                    judgment = json.loads(text)
                    record['judgment'] = judgment
                    record.update(validate_atomic_judgment(spec, payload, judgment), status='valid')
                except (ValueError, KeyError, TypeError) as error:
                    record['validation_error'] = str(error)
                except Exception as error:
                    record.update(provider_failure(error))
                    if getattr(error, 'failure_kind', None) in {
                        'incomplete_output', 'filtered_output', 'empty_output',
                        'unconfirmed_output', 'unexpected_tool_delta',
                    }:
                        record['completion_failure_kind'] = error.failure_kind
                finally:
                    record.update(usage=usage_from_spans(trace['spans']), elapsed_seconds=time.perf_counter() - started)
                    tracer.reset(token)
                    save(path, record)
            print(json.dumps({'number': number, 'attempt': attempt, 'status': record['status'],
                              'error': record.get('validation_error') or record.get('completion_failure_kind')
                              or record.get('error_type'),
                              'Q': (record.get('Q') or {}).get('score'),
                              'P': (record.get('P') or {}).get('score')}, ensure_ascii=False), flush=True)
            if record.get('provider_blocked'):
                summary.append(record)
                save(args.output / 'summary.json', summary)
                raise RuntimeError(f'Provider HTTP {record["provider_http_status"]}; batch stopped')
            if record['status'] == 'valid':
                save(selected, record)
                summary.append(record)
                break
            if record.get('raw_judgment'):
                messages.append({'role': 'assistant', 'content': record['raw_judgment']})
            messages.append({'role': 'system', 'content': '机械校验失败：' + record.get('validation_error', 'transport error') +
                             '。请重新返回完整判定；不要为了通过而修改语义判断，没有证据用CANNOT_ASSESS。'})
        else:
            summary.append({'number': number, 'status': 'judge_failed', 'binding': binding,
                            'Q': None, 'P': None, 'business_status': 'pending'})
        save(args.output / 'summary.json', summary)
    save(args.output / 'summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('packets', 'specs', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--judge-thinking', action='store_true')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--only', default='')
    asyncio.run(run(parser.parse_args()))


if __name__ == '__main__':
    main()
