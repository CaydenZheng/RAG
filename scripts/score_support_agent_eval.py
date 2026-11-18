"""Score saved support executions with frozen, evidence-linked grading rules."""

import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
import uuid
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from src.evaluation.support_grading import (  # noqa: E402
    JUDGE_INSTRUCTIONS,
    derive_dimension_grades,
    prepare_input,
    validate_and_score,
)


def load(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def provider_failure(error):
    """Keep actionable status without serializing provider messages or requests."""
    status = getattr(error, 'status_code', None)
    code = getattr(error, 'code', None)
    return {'error_type': type(error).__name__,
            'provider_http_status': status if type(status) is int else None,
            'provider_error_code': code if isinstance(code, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', code) else None,
            'provider_blocked': status in {401, 402, 403}}


async def run(args):
    from config.settings import settings
    from src.infra.tracer import tracer
    from src.infra.usage import usage_from_spans
    from src.llm import llm_client

    rubric = args.rubric.read_text(encoding='utf-8')
    max_tokens = 16000 if args.judge_thinking else 6500
    protocol = {'instructions': JUDGE_INSTRUCTIONS, 'rubric_sha256': digest(args.rubric),
                'validator_sha256': digest(PROJECT / 'src/evaluation/support_grading.py'),
                'model': settings.llm_flash_model, 'temperature': 0, 'max_tokens': max_tokens,
                'thinking': args.judge_thinking,
                'selection': 'first valid judgment; at most two attempts; retain failures',
                'independence': 'same model family; calibrated development judge, not independent human validation'}
    protocol_path = args.output / 'protocol.json'
    if protocol_path.exists() and load(protocol_path) != protocol:
        raise ValueError('Existing grading protocol differs; use a new output directory')
    save(protocol_path, protocol)
    annotations = [json.loads(line) for line in args.annotations.read_text(encoding='utf-8').splitlines() if line.strip()]
    summary = []
    for annotation in annotations:
        number = annotation['number']
        packet_path = args.packets / f'{number}-完整调用与结果.json'
        if not packet_path.exists():
            raise FileNotFoundError(f'Missing required execution packet: {packet_path}')
        packet = load(packet_path)
        result, events = packet['result'], packet['events']
        target = args.output / number
        selected_path = target / 'selected.json'
        if selected_path.exists():
            existing = load(selected_path)
            if existing.get('packet_sha256') != digest(packet_path) or existing.get('protocol_sha256') != digest(protocol_path):
                raise ValueError('Saved judgment belongs to different execution or protocol; use a new output directory')
            summary.append(existing)
            continue
        if result.get('error_code'):
            record = {'number': number, 'status': 'execution_failed', 'Q': None, 'P': None,
                      'business_status': 'fail', 'error_code': result['error_code'],
                      'packet_sha256': digest(packet_path), 'protocol_sha256': digest(protocol_path)}
            save(selected_path, record)
            summary.append(record)
            continue
        payload = prepare_input(annotation, result, events)
        save(target / 'judge-input.json', payload)
        messages = [{'role': 'system', 'content': JUDGE_INSTRUCTIONS + '\n' + rubric},
                    {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]
        save(target / 'judge-messages.json', messages)
        for attempt in range(1, 3):
            path = target / f'attempt-{attempt:03d}.json'
            if path.exists():
                prior = load(path)
                if prior.get('packet_sha256') != digest(packet_path) or prior.get('protocol_sha256') != digest(protocol_path):
                    raise ValueError('Saved attempt belongs to different execution or protocol')
                if prior['status'] == 'valid':
                    save(selected_path, prior)
                    summary.append(prior)
                    break
                messages.append({'role': 'system', 'content': 'Previous judgment rejected by validator: ' + prior.get('validation_error', 'transport failure') + '. Return a complete corrected judgment.'})
                continue
            trace = tracer.start_trace(uuid.uuid4().hex, 'support_judge_v2')
            token = tracer.bind(trace)
            started = time.perf_counter()
            record = {'number': number, 'status': 'judge_failed', 'input_sha256': digest(target / 'judge-input.json'),
                      'packet_sha256': digest(packet_path), 'protocol_sha256': digest(protocol_path)}
            try:
                raw = await llm_client.chat_async(messages, model=settings.llm_flash_model,
                                                 temperature=0, max_tokens=max_tokens, skip_cache=True,
                                                 enable_thinking=args.judge_thinking)
                record['raw_judgment'] = raw
                value = raw.strip()
                if value.startswith('```json') and value.endswith('```'):
                    value = value[7:-3].strip()
                raw_judgment = json.loads(value)
                record['raw_parsed_judgment'] = raw_judgment
                judgment = derive_dimension_grades(raw_judgment)
                record['judgment'] = judgment
                record.update(validate_and_score(payload, judgment), status='valid')
            except (ValueError, KeyError, TypeError) as error:
                record['validation_error'] = str(error)
            except Exception as error:
                record.update(provider_failure(error))
            finally:
                record.update(usage=usage_from_spans(trace['spans']), elapsed_seconds=time.perf_counter() - started)
                tracer.reset(token)
                save(path, record)
            print(json.dumps({key: record.get(key) for key in ('number', 'status', 'Q', 'P', 'validation_error')}, ensure_ascii=False), flush=True)
            if record.get('provider_blocked'):
                summary.append({'number': number, 'status': 'provider_blocked', 'Q': None, 'P': None,
                                'business_status': 'unscored', 'provider_http_status': record['provider_http_status']})
                save(args.output / 'summary.json', summary)
                raise RuntimeError(f'Grading stopped: provider HTTP {record["provider_http_status"]}; resolve account access or balance before resuming')
            if record['status'] == 'valid':
                save(selected_path, record)
                summary.append(record)
                break
            messages.append({'role': 'system', 'content': 'Previous judgment rejected by validator: ' + record.get('validation_error', 'transport failure') + '. Return a complete corrected judgment.'})
        else:
            summary.append({'number': number, 'status': 'judge_failed', 'Q': None, 'P': None,
                            'business_status': 'uncertain', 'review_needed': 'Judge exhausted allowed attempts'})
        save(args.output / 'summary.json', summary)
    save(args.output / 'summary.json', summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--judge-thinking', action='store_true', help='Enable uncached DeepSeek reasoning for grading only')
    for name in ('packets', 'annotations', 'rubric', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    asyncio.run(run(parser.parse_args()))


if __name__ == '__main__':
    main()
