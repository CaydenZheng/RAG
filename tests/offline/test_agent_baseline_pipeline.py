"""Regression checks for resumable runs and unbiased attempt selection."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/run_agent_baseline.py'
spec = importlib.util.spec_from_file_location('baseline_pipeline', SCRIPT)
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


def test_attempt_selection_keeps_first_success_and_recovers_final_partial_line(tmp_path):
    first, second = tmp_path / 'attempt-001', tmp_path / 'attempt-002'
    pipeline.atomic_rows(first / 'results.jsonl', [
        {'task_id': 'a', 'predictions': [{'text': 'First answer'}], 'score': 0.4},
        {'task_id': 'b', 'predictions': [], 'error_code': 'timeout'},
    ])
    pipeline.atomic_rows(second / 'results.jsonl', [
        {'task_id': 'a', 'predictions': [{'text': 'Different answer'}], 'score': 1.0},
        {'task_id': 'b', 'predictions': [{'text': 'Recovered'}]},
    ])
    with (second / 'results.jsonl').open('a', encoding='utf-8') as stream:
        stream.write('{"task_id":')
    selected, _, provenance = pipeline.collect_attempts(
        [second, first], 'results.jsonl', pipeline.generation_succeeded,
    )
    assert selected['a']['score'] == 0.4
    assert selected['b']['predictions'][0]['text'] == 'Recovered'
    assert provenance['a'] == str(first)


def test_generate_retries_only_failed_tasks_and_resume_skips_completed(tmp_path, monkeypatch):
    pipeline.atomic_rows(tmp_path / 'tasks.jsonl', [{'task_id': 'a'}, {'task_id': 'b'}])
    spec = {'tasks': 'tasks.jsonl', 'corpus': 'corpus.jsonl', 'metadata': 'metadata.jsonl', 'suite': 'public'}
    config = {'max_attempts': 2, 'retrieval_mode': 'hybrid+rerank', 'index_dir': 'index'}
    requests = []

    def invoke(command, log_path, environment):
        task_ids = [command[i + 1] for i, arg in enumerate(command) if arg == '--task-id']
        requests.append(task_ids)
        rows = [{'task_id': task_id, 'predictions': [{'text': 'Answer'}],
                 'error_code': 'timeout' if task_id == 'b' and len(requests) == 1 else ''}
                for task_id in task_ids]
        pipeline.atomic_rows(Path(command[command.index('--output') + 1]) / 'results.jsonl', rows)
        return 0

    monkeypatch.setattr(pipeline, 'invoke', invoke)
    for _ in range(2):
        results = pipeline.generate_suite('test', spec, config, tmp_path / 'manifest.json', tmp_path / 'out', {})
        assert len(results) == 2
        assert all(not row['error_code'] for row in results)
    assert requests == [['a', 'b'], ['b']]


def test_scoring_retries_invalid_rating_and_preserves_valid_score(tmp_path, monkeypatch):
    requests = []

    def invoke(command, log_path, environment):
        records = pipeline.read_rows(Path(command[command.index('--results') + 1]))
        requests.append([r['task_id'] for r in records])
        scores = [{'task_id': r['task_id'],
                   'status': 'invalid_rating' if r['task_id'] == 'b' and len(requests) == 1 else 'valid',
                   'score': 0.8} for r in records]
        pipeline.atomic_rows(Path(command[command.index('--output') + 1]), scores)
        return 0

    monkeypatch.setattr(pipeline, 'invoke', invoke)
    config = {'max_attempts': 2, 'judge_utils': 'judge.py'}
    pipeline.score_suite(tmp_path / 'out', [{'task_id': 'a'}, {'task_id': 'b'}], config,
                         tmp_path / 'manifest.json', {})
    assert requests == [['a', 'b'], ['b']]
    assert len(pipeline.read_rows(tmp_path / 'out/RB_llm.jsonl')) == 2


def test_manifest_drift_is_rejected(tmp_path):
    (tmp_path / 'task.json').write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='changed'):
        pipeline.verify_manifest(tmp_path / 'manifest.json', {
            'file_sha256': {'task.json': 'incorrect'}, 'code_sha256': {},
        })
    (tmp_path / 'bad.jsonl').write_text('{bad}\n' + json.dumps({'task_id': 'a'}), encoding='utf-8')
    with pytest.raises(json.JSONDecodeError):
        pipeline.read_rows(tmp_path / 'bad.jsonl')
