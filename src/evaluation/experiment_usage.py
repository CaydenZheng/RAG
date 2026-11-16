"""Sum physical attempt files once, including failed generation and judging."""

import json
from pathlib import Path


def experiment_usage(directory: Path) -> dict:
    groups = {
        'generation': sorted(set(directory.glob('**/generation/attempt-*/run/results.jsonl'))
                             | set(directory.glob('**/generation/attempt-*/run/errors.jsonl'))),
        'judging': sorted(directory.glob('**/scoring/**/attempt-*/scores.jsonl')),
    }
    report = {}
    for name, paths in groups.items():
        tokens = calls = missing = records = malformed = 0
        for path in paths:
            for line in path.read_text(encoding='utf-8').splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    malformed += 1
                    continue
                records += 1
                usage = row.get('usage', {})
                tokens += usage.get('total_tokens', 0)
                calls += usage.get('model_calls', 0)
                missing += not usage.get('reported', False)
        report[name] = {
            'attempt_records': records, 'reported_tokens': tokens,
            'logical_model_calls': calls, 'records_with_incomplete_usage': missing,
            'malformed_lines': malformed,
            'files': [str(path.relative_to(directory)) for path in paths],
        }
    report['scope'] = 'All saved attempts; missing provider usage and unfinished processes remain unpriced.'
    return report
