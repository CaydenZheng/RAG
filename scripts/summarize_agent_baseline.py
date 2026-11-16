"""Report each baseline suite separately with evidence coverage and review candidates."""

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_agent_baseline import atomic_json, atomic_rows, read_rows  # noqa: E402

from src.evaluation.experiment_usage import experiment_usage  # noqa: E402
from src.evaluation.metrics import retrieval_metrics  # noqa: E402
from src.evaluation.percentiles import linear_percentile  # noqa: E402


def mean(values):
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else None


def aggregate(records):
    latencies = [r['elapsed_seconds'] for r in records if r['elapsed_seconds'] is not None]
    return {
        'tasks': len(records), 'execution_success': sum(not r['error_code'] for r in records),
        'RB_llm_scored': sum(r['RB_llm'] is not None for r in records),
        'RB_llm_mean_10': mean(r['RB_llm'] for r in records),
        'business_scored': sum(r['business_score'] is not None for r in records),
        'business_mean_10': mean(r['business_score'] for r in records),
        'mean_answer_characters': mean(r['answer_characters'] for r in records),
        'p50_seconds': linear_percentile(latencies, 0.5) if latencies else None,
        'p95_seconds': linear_percentile(latencies, 0.95) if latencies else None,
        'reported_tokens': sum(r['usage'].get('total_tokens', 0) for r in records),
        'model_calls': sum(r['usage'].get('model_calls', 0) for r in records),
        'usage_reported_tasks': sum(bool(r['usage'].get('reported')) for r in records),
        'gold_evidence_tasks': sum(r['first_search_metrics'] is not None for r in records),
        'first_search_metrics': {key: mean(r['first_search_metrics'][key] for r in records
                                         if r['first_search_metrics'] is not None)
                                 for key in ['recall@1', 'recall@3', 'recall@5', 'ndcg@1', 'ndcg@3', 'ndcg@5']},
        'observed_evidence_recall': mean(r['observed_evidence_recall'] for r in records),
    }


def summarize(manifest, output, report):
    config = json.loads(manifest.read_text(encoding='utf-8'))
    base = manifest.parent
    report.mkdir(parents=True, exist_ok=True)
    atomic_json(report / 'all-attempt-usage.json', experiment_usage(output))
    annotations = {r['task_id']: r for r in read_rows(base / '数据/internal/annotations.jsonl')}
    qrels = defaultdict(set)
    with (base / '数据/public/qrels.tsv').open(encoding='utf-8') as stream:
        for row in csv.DictReader(stream, delimiter='\t'):
            if int(row['score']) > 0:
                qrels[row['query-id']].add(row['corpus-id'])
    summaries, detail, candidates = {}, [], []
    for name, spec in config['suites'].items():
        directory = output / name
        if not (directory / 'results.jsonl').exists():
            summaries[name] = {'status': 'pending'}
            continue
        tasks = {r['task_id']: r for r in read_rows(base / spec['tasks'])}
        results = read_rows(directory / 'results.jsonl')
        selection = json.loads((directory / 'selection.json').read_text(encoding='utf-8'))
        scores = {r['task_id']: r for r in read_rows(directory / 'RB_llm.jsonl')}
        business = {r['task_id']: r for r in read_rows(directory / 'business.jsonl')}
        traces = {p: read_rows(Path(p) / 'trace.jsonl') for p in set(selection['selected_attempt'].values())}
        group_rows = defaultdict(list)
        for result in results:
            task_id = result['task_id']
            task = tasks[task_id]
            annotation = annotations.get(task_id, {})
            gold = qrels[task_id] if spec['suite'] == 'public' else {
                c['document_id'] for c in task.get('contexts', [])}
            events = traces.get(selection['selected_attempt'].get(task_id, ''), [])
            events = [e for e in events if e['task_id'] == task_id]
            first_search = next((e['tool_result']['data'].get('contexts', []) for e in events
                                 if e.get('tool') in {'search_knowledge_base', 'search_documents'}
                                 and e.get('tool_result', {}).get('success')), [])
            metrics = retrieval_metrics(
                {'source_documents': [{'path': pid} for pid in sorted(gold)]},
                [{'metadata': {'source': c['document_id']}} for c in first_search], k_values=(1, 3, 5),
            ) if gold else None
            observed = {c['document_id'] for c in result.get('observed_contexts', [])}
            raw_score = scores.get(task_id, {})
            business_score = business.get(task_id, {})
            record = {
                'suite': name, 'task_id': task_id, 'split': annotation.get('split', 'coverage' if name != 'public' else 'public'),
                'source_family': annotation.get('source_family', task.get('source_family')),
                'difficulty': annotation.get('difficulty'),
                'answerability': task.get('Answerability', task.get('answerability')),
                'error_code': result.get('error_code', ''),
                'RB_llm': raw_score['score'] * 10 if raw_score.get('status') == 'valid' else None,
                'business_score': business_score['score'] * 10 if business_score.get('status') == 'valid' else None,
                'judge_status': raw_score.get('status', 'pending'),
                'elapsed_seconds': result.get('elapsed_seconds'),
                'answer_characters': sum(len(p.get('text', '')) for p in result.get('predictions', [])),
                'usage': result.get('usage', {}), 'first_search_metrics': metrics,
                'observed_evidence_recall': len(gold & observed) / len(gold) if gold else None,
                'tool_counts': dict(Counter(e['tool'] for e in events if e.get('step') == 'tool_call')),
                'retrieval_warnings': [warning for e in events for warning in
                                       (e.get('tool_result', {}).get('data') or {}).get('warnings', [])],
            }
            detail.append(record)
            group_rows[record['split']].append(record)
            flags = []
            if record['error_code']:
                flags.append('执行失败')
            if record['RB_llm'] is not None and record['RB_llm'] <= 7:
                flags.append('自动总分不高于7分')
            if record['business_score'] is not None and record['business_score'] <= 7:
                flags.append('业务评分不高于7分')
            if record['RB_llm'] is not None and record['business_score'] is not None:
                if abs(record['RB_llm'] - record['business_score']) >= 2:
                    flags.append('两种评分相差至少2分')
            if flags:
                candidates.append({'suite': name, 'task_id': task_id, 'flags': flags,
                                   'input': result.get('input', []), 'targets': result.get('targets', []),
                                   'predictions': result.get('predictions', []),
                                   'observed_contexts': result.get('observed_contexts', []),
                                   'official_judgment': raw_score, 'business_judgment': business_score})
        summaries[name] = {'status': 'measured', 'all': aggregate([r for r in detail if r['suite'] == name]),
                           'by_split': {split: aggregate(records) for split, records in group_rows.items()}}
        first = read_rows(directory / 'first_turn/business.jsonl')
        if first:
            summaries[name]['first_turn_business_scores'] = {
                r['task_id']: r['score'] * 10 if r.get('status') == 'valid' else None for r in first}
    atomic_json(report / 'summary.json', summaries)
    atomic_rows(report / '逐题指标.jsonl', detail)
    atomic_rows(report / '复核候选.jsonl', candidates)
    lines = ['# 冻结配置的双基线运行报告', '',
             '各集合分别汇总。均分基于有效评分，执行成功数、评分覆盖数与失败项同时报告。', '',
             '| 集合与划分 | 题数 | 执行成功 | 官方提示词均分 | 业务校准均分 |',
             '|---|---:|---:|---:|---:|']
    for name, summary in summaries.items():
        if summary['status'] == 'pending':
            lines.append(f'| {name} | 待运行 | — | — | — |')
            continue
        for split, values in summary['by_split'].items():
            raw = values['RB_llm_mean_10']
            calibrated = values['business_mean_10']
            lines.append(f"| {name} / {split} | {values['tasks']} | {values['execution_success']} | "
                         f"{raw if raw is not None else '待评分'} | {calibrated if calibrated is not None else '—'} |")
    lines += ['', '## 指标口径', '',
              '生成评分使用固定MTRAG RB_llm原提示词与DeepSeek判分。内部同时报告业务校准提示词的独立结果。', '',
              '检索指标基于Agent首次成功的本地检索调用，计算Recall与nDCG的1、3、5档。公开集使用固定提交的官方qrels。另记录全部实际观察证据对已标注正例的覆盖率。工具调用及证据路径差异保留在逐题记录。', '',
              '公开报告覆盖当前Agent协议下的Cloud子集。完整MTRAG指标套件、官方不同模型配置的排名对齐仍需额外验证。当前预留14题的模型输出用于集中复核，开发集和工具变体单独报告。', '',
              'Token和调用数来自模型调用轨迹，金额需与对应服务商计费记录核对。失败重试的原始记录保留在各attempt目录。', '',
              '## 人工复核候选', '',
              f'按固定筛选条件共标记{len(candidates)}条。7分及2分差异用于优先安排审阅，业务通过线仍需结合审阅确定。', '']
    for case in candidates:
        lines.append(f"- {case['suite']} / {case['task_id']}，{'、'.join(case['flags'])}")
    (report / '报告.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(json.dumps({name: value['status'] for name, value in summaries.items()}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    summarize(args.manifest.resolve(), args.output, args.report)
