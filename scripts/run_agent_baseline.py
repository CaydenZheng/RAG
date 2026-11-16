"""Run frozen Agent suites with checkpoint recovery and auditable retries."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def read_rows(path):
    """Recover complete records after interruption, retaining the original file."""
    if not path.exists():
        return []
    lines = path.read_text(encoding='utf-8-sig').splitlines()
    records = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            if index != len(lines) - 1:
                raise
            print(f'Incomplete final record retained in {path}', flush=True)
    return records


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def atomic_rows(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in records), encoding='utf-8')
    os.replace(temporary, path)


@contextmanager
def output_lock(path):
    """OS-owned lock releases automatically if the driver is interrupted."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        stream.write(b'0')
        stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def verify_manifest(path, config):
    for base, entries in [(path.parent, config['file_sha256']), (ROOT, config['code_sha256'])]:
        for relative, checksum in entries.items():
            actual = hashlib.sha256((base / relative).read_bytes()).hexdigest()
            if actual != checksum:
                raise ValueError(f'Frozen input or code changed: {relative}')


def collect_attempts(paths, filename, successful):
    """Select the earliest successful execution, independent of its quality score."""
    selected, latest, provenance = {}, {}, {}
    for attempt in sorted(paths):
        seen = set()
        for row in read_rows(attempt / filename):
            task_id = row['task_id']
            if task_id in seen:
                raise ValueError(f'Duplicate task in attempt: {task_id}')
            seen.add(task_id)
            latest[task_id] = row
            if successful(row) and task_id not in selected:
                selected[task_id] = row
                provenance[task_id] = str(attempt)
    return selected, latest, provenance


def generation_succeeded(row):
    return not row.get('error_code') and any(p.get('text', '').strip() for p in row.get('predictions', []))


def score_finished(row):
    return row.get('status') in {'valid', 'generation_failed'}


def invoke(command, log_path, environment):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    print(f'Running {log_path.parent.name}/{log_path.stem}', flush=True)
    with log_path.open('w', encoding='utf-8') as stream:
        process = subprocess.run(command, cwd=ROOT, env=environment, stdout=stream, stderr=subprocess.STDOUT)
    atomic_json(log_path.with_suffix('.exit.json'), {'exit_code': process.returncode})
    return process.returncode


def generate_suite(name, spec, config, manifest, output, environment):
    base = manifest.parent
    directory = output / name
    attempts = directory / 'generation'
    attempts.mkdir(parents=True, exist_ok=True)
    tasks = read_rows(base / spec['tasks'])
    identifiers = [r['task_id'] for r in tasks]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError('Task IDs must be unique')
    for _ in range(config['max_attempts']):
        prior = sorted(attempts.glob('attempt-*'))
        selected, _, _ = collect_attempts([p / 'run' for p in prior], 'results.jsonl', generation_succeeded)
        pending = [task_id for task_id in identifiers if task_id not in selected]
        if not pending or len(prior) >= config['max_attempts']:
            break
        attempt = attempts / f'attempt-{len(prior) + 1:03d}'
        attempt.mkdir()
        command = [sys.executable, '-X', 'utf8', str(ROOT / 'scripts/run_frozen_agent_eval.py'),
                   '--tasks', str(base / spec['tasks']), '--corpus', str(base / spec['corpus']),
                   '--metadata', str(base / spec['metadata']), '--manifest', str(manifest),
                   '--output', str(attempt / 'run'), '--suite', spec['suite'],
                   '--mode', spec.get('mode', 'agent'), '--retrieval-mode', config['retrieval_mode'],
                   '--index-dir', str(base / config['index_dir']), '--response-style', 'concise',
                   '--fresh-cache', '--max-consecutive-errors', '5']
        if spec.get('official'):
            command += ['--official', 'live', '--official-proxy', config['official_proxy']]
        if spec.get('environments'):
            command += ['--environments', str(base / spec['environments'])]
        for task_id in pending:
            command += ['--task-id', task_id]
        atomic_json(attempt / 'request.json', {'task_ids': pending, 'command': command})
        invoke(command, attempt / 'generation.log', environment)
        # The runner owns its run directory. Attempts retain logs and request metadata.
        selected, _, _ = collect_attempts(attempts.glob('attempt-*/run'), 'results.jsonl', generation_succeeded)
        if len(selected) == len(identifiers):
            break
    selected, latest, provenance = collect_attempts(attempts.glob('attempt-*/run'), 'results.jsonl', generation_succeeded)
    results = []
    for task in tasks:
        task_id = task['task_id']
        row = selected.get(task_id) or latest.get(task_id)
        if row is None:
            row = {**task, 'predictions': [{'text': ''}], 'error_code': 'generation_unavailable',
                   'contexts': [], 'observed_contexts': []}
        results.append(row)
    atomic_rows(directory / 'results.jsonl', results)
    atomic_json(directory / 'selection.json', {
        'policy': 'earliest successful execution; successful answers are never retried for a higher score',
        'selected_attempt': provenance, 'expected': len(tasks), 'successful': len(selected),
        'failed': [task_id for task_id in identifiers if task_id not in selected],
    })
    return results


def score_suite(directory, results, config, manifest, environment, business=False):
    metric = 'business' if business else 'RB_llm'
    attempts = directory / 'scoring' / metric
    attempts.mkdir(parents=True, exist_ok=True)
    for _ in range(config['max_attempts']):
        prior = sorted(attempts.glob('attempt-*'))
        selected, _, _ = collect_attempts(prior, 'scores.jsonl', score_finished)
        pending = [r for r in results if r['task_id'] not in selected]
        if not pending or len(prior) >= config['max_attempts']:
            break
        attempt = attempts / f'attempt-{len(prior) + 1:03d}'
        attempt.mkdir()
        atomic_rows(attempt / 'input.jsonl', pending)
        command = [sys.executable, '-X', 'utf8', str(ROOT / 'scripts/score_frozen_agent_eval.py'),
                   '--results', str(attempt / 'input.jsonl'),
                   '--judge-utils', str(manifest.parent / config['judge_utils']),
                   '--output', str(attempt / 'scores.jsonl'), '--fresh-cache']
        if business:
            command += ['--business-policy', str(manifest.parent / config['business_policy'])]
        invoke(command, attempt / 'scoring.log', environment)
    selected, latest, provenance = collect_attempts(attempts.glob('attempt-*'), 'scores.jsonl', score_finished)
    scores = [selected.get(r['task_id']) or latest.get(r['task_id']) or
              {'task_id': r['task_id'], 'status': 'judge_unavailable', 'score': None} for r in results]
    atomic_rows(directory / f'{metric}.jsonl', scores)
    atomic_json(attempts / 'selection.json', provenance)


def first_turn_results(directory, results):
    from src.evaluation.evidence import visible_tool_evidence

    selection = json.loads((directory / 'selection.json').read_text(encoding='utf-8'))
    projections = []
    for result in results:
        if 'first_response' not in result:
            continue
        contexts = {}
        visible_contexts = None
        attempt = Path(selection['selected_attempt'][result['task_id']])
        for event in read_rows(attempt / 'trace.jsonl'):
            if event['task_id'] != result['task_id'] or event['turn'] != 1:
                continue
            observation = event.get('observation', {})
            if observation.get('stage') == 'final_generation':
                visible_contexts = visible_tool_evidence(observation['messages'])
            tool = event.get('tool_result', {})
            if not tool.get('success'):
                continue
            data = tool.get('data') or {}
            for context in data.get('contexts', []):
                contexts.setdefault(context['document_id'], context)
            for hit in data.get('results', []):
                pid = 'search-' + hashlib.sha256(hit['url'].encode()).hexdigest()[:16]
                contexts.setdefault(pid, {'document_id': pid, 'title': hit['title'], 'text': hit['snippet'],
                                          'score': 1.0, 'url': hit['url'], 'evidence_kind': 'search_snippet'})
        projections.append({
            'task_id': result['task_id'] + '::first', 'input': result['input'][:1],
            'targets': [{'speaker': 'agent', 'text': result['first_turn_expectation']}],
            'predictions': [{'text': result['first_response']['answer']}],
            'error_code': result['first_response']['error_code'], 'contexts': list(contexts.values())[:10],
            'observed_contexts': list(contexts.values()), 'observed_context_count': len(contexts),
            'protocol': 'first turn only; evidence collected before user supplement',
        })
        if visible_contexts is not None:
            projections[-1]['model_visible_contexts'] = visible_contexts
            projections[-1]['evidence_policy'] = 'final_model_visible_tool_messages'
        projections[-1]['generation_trace_id'] = result['first_response'].get('trace_id', '')
    return projections


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--suite', action='append')
    args = parser.parse_args()
    manifest = args.manifest.resolve()
    config = json.loads(manifest.read_text(encoding='utf-8'))
    verify_manifest(manifest, config)
    selected = args.suite or list(config['suites'])
    if set(selected) - set(config['suites']):
        raise ValueError('Unknown suite')
    environment = {**os.environ, **config['environment']}
    with output_lock(args.output / '.pipeline.lock'):
        identity_path = args.output / 'identity.json'
        identity = {'manifest_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest()}
        if identity_path.exists() and json.loads(identity_path.read_text(encoding='utf-8')) != identity:
            raise ValueError('Output belongs to a different frozen configuration')
        atomic_json(identity_path, identity)
        for name in selected:
            spec = config['suites'][name]
            results = generate_suite(name, spec, config, manifest, args.output, environment)
            score_suite(args.output / name, results, config, manifest, environment)
            if spec['suite'] == 'support':
                score_suite(args.output / name, results, config, manifest, environment, business=True)
            if spec.get('mode') == 'live':
                first = first_turn_results(args.output / name, results)
                directory = args.output / name / 'first_turn'
                atomic_rows(directory / 'results.jsonl', first)
                score_suite(directory, first, config, manifest, environment)
                score_suite(directory, first, config, manifest, environment, business=True)
            invoke([sys.executable, '-X', 'utf8', str(ROOT / 'scripts/summarize_agent_baseline.py'),
                    '--manifest', str(manifest), '--output', str(args.output.resolve()),
                    '--report', str((args.output / 'report').resolve())],
                   args.output / name / 'report.log', environment)
            print(f'{name} complete; execution and scoring status recorded', flush=True)


if __name__ == '__main__':
    main()
