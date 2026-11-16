#!/usr/bin/env python
"""Apply the pinned MTRAG RB_llm prompt through the configured model provider.

Retain raw judgments and reject malformed ratings instead of imputing a score.
This is a configurable judge adapter, not the complete MTRAG metric suite.
"""

import argparse
import asyncio
import hashlib
import importlib.util
import json
import re
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def run(args):
    import pandas as pd

    from config.settings import settings
    from src.infra.tracer import tracer
    from src.infra.usage import usage_from_spans
    from src.llm import llm_client

    spec = importlib.util.spec_from_file_location('pinned_mtrag_judge', args.judge_utils)
    official = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(official)
    results = [json.loads(line) for line in args.results.read_text(encoding='utf-8').splitlines()]
    policy_path = getattr(args, 'business_policy', None)
    policy = policy_path.read_text(encoding='utf-8') if policy_path else ''
    if args.output.exists():
        raise FileExistsError('Choose a new score output path')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    failed = False
    for result in results:
        row = {'task_id': result['task_id'], 'metric': 'internal_RB_llm' if policy else 'RB_llm',
               'judge_model': settings.llm_flash_model,
               'judge_source_sha256': hashlib.sha256(args.judge_utils.read_bytes()).hexdigest(),
               'results_sha256': hashlib.sha256(args.results.read_bytes()).hexdigest(),
               'protocol_note': 'Official prompt, configured provider adapter, strict invalid-rating reporting'}
        if result.get('error_code'):
            row.update(status='generation_failed', score=None)
        else:
            contexts = result.get('model_visible_contexts', result.get('observed_contexts', result['contexts']))
            if 'model_visible_contexts' not in result and len(contexts) < result.get('observed_context_count', len(contexts)):
                raise ValueError('Scoring requires all observed evidence; recover it from the trace first')
            row['evidence_count'] = len(contexts)
            row['evidence_policy'] = result.get('evidence_policy', 'all_observed_tool_evidence')
            row['conversation_projection'] = 'structured_turns_preserving_multiline_text'
            record = {
                'document': official.extract_document_texts(contexts),
                'response': official.extract_texts(result['predictions']),
                'reference_answer': official.extract_reference(result['targets']),
            }
            record['previous_conversation'] = official.extract_conversation(result['input'][:-1])
            record['current_question'] = result['input'][-1]['text']
            prompt = official.format_conversation_radbench(pd.DataFrame([record]))[0]
            if policy:
                prompt += '\n\nInternal business calibration guidance\n' + policy
                row['business_policy_sha256'] = hashlib.sha256(policy.encode('utf-8')).hexdigest()
            row['prompt_sha256'] = hashlib.sha256(prompt.encode('utf-8')).hexdigest()
            trace = tracer.start_trace(uuid.uuid4().hex, 'evaluation_judge')
            token = tracer.bind(trace)
            started = time.perf_counter()
            try:
                with tracer.context(task_id=result['task_id'], generation_trace_id=result.get('trace_id', '')):
                    with tracer.stage('evaluation_judge'):
                        raw = await llm_client.chat_async(
                            [{'role': 'user', 'content': prompt}], model=settings.llm_flash_model,
                            temperature=0, max_tokens=1600, skip_cache=getattr(args, 'fresh_cache', False),
                        )
                matches = re.findall(r'Rating:\s*\[\[(\d+(?:\.\d+)?)\]\]', raw)
                valid = len(matches) == 1 and 1 <= float(matches[0]) <= 10
                row.update(raw_judgment=raw, status='valid' if valid else 'invalid_rating',
                           score=float(matches[0]) / 10 if valid else None)
            except Exception as error:
                row.update(status='judge_failed', score=None, error_type=type(error).__name__)
            finally:
                row.update(trace_id=trace['trace_id'], usage=usage_from_spans(trace['spans']),
                           trace_spans=trace['spans'], elapsed_seconds=time.perf_counter() - started)
                tracer.reset(token)
        with args.output.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
        failed = failed or row['status'] != 'valid'
        print(row['task_id'], row['status'], row['score'], flush=True)
    return 1 if failed else 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results', type=Path, required=True)
    parser.add_argument('--judge-utils', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--business-policy', type=Path)
    parser.add_argument('--fresh-cache', action='store_true')
    sys.exit(asyncio.run(run(parser.parse_args())))
