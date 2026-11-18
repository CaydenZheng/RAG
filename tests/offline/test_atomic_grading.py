"""Atomic scoring must not hide unknowns, future evidence, or critical failures."""
import copy
import json

import pytest

from src.evaluation.atomic_grading import (
    BUDGETS,
    quote_in_source,
    validate_atomic_judgment,
    validate_spec,
)


def test_json_quotes_allow_real_newlines_but_not_cross_field_fabrication():
    source = json.dumps({'text': 'first line\nsecond line', 'other': 'unrelated'}, ensure_ascii=False)
    assert quote_in_source('first line\nsecond line', source)
    assert quote_in_source('first linesecond line', source)
    assert not quote_in_source('firstlinesecondline', source)
    assert not quote_in_source('second line\nunrelated', source)


def fixture():
    criteria = []
    for family, groups in BUDGETS.items():
        for group, weight in groups.items():
            criteria.append({'id': family + '.' + group, 'family': family, 'group': group,
                             'weight': weight, 'requirement': 'A specific requirement', 'critical': False,
                             'scope': 'final' if family == 'Q' else 'all', 'na_allowed': False,
                             'tool_evidence_required': group == 'E', 'complete_intent_required': False,
                             'evidence_kinds': ['answer', 'tool_done', 'tool_call', 'decision', 'trace_index'],
                             **{k: 'Explicit criterion definition' for k in (
                                 'met_when', 'unmet_when', 'unknown_when', 'applicability', 'equivalence', 'basis')}})
    spec = {'protocol': 'atomic-v1', 'criteria': criteria}
    payload = {'final_turn': 1, 'safety_coverage': 'not_covered', 'sources': {
        'answer:1': {'kind': 'answer', 'turn': 1, 'text': 'Correct answer'},
        'event:1:3': {'kind': 'decision', 'turn': 1, 'sequence': 3, 'text': 'Known intent', 'intent_reported': True},
        'event:1:5': {'kind': 'tool_done', 'turn': 1, 'sequence': 5, 'text': 'Earlier evidence', 'visible_to_answer': True},
        'event:1:9': {'kind': 'tool_call', 'turn': 1, 'sequence': 9, 'text': 'Extra call'},
        'event:1:10': {'kind': 'tool_done', 'turn': 1, 'sequence': 10, 'text': 'Later evidence', 'visible_to_answer': True},
        'trace:1': {'kind': 'trace_index', 'turn': 1, 'text': 'Complete event list'},
    }}
    rows = []
    for c in criteria:
        source = 'event:1:5' if c['group'] == 'E' else 'event:1:3' if c['group'] == 'I' else 'answer:1'
        rows.append({'id': c['id'], 'verdict': 'MET', 'reason': 'Observed matching fact', 'missing_evidence': '',
                     'evidence': [{'source_id': source, 'mode': 'scope', 'quote': ''}], 'before_event': None})
    return spec, payload, {'completion': 'answered', 'verdicts': rows}


def test_all_met_and_safety_not_covered_is_not_safety_pass():
    spec, payload, judgment = fixture()
    result = validate_atomic_judgment(spec, payload, judgment)
    assert result['Q']['score'] == result['P']['score'] == 100
    assert result['business_status'] == 'pass'
    assert result['safety'] == 'not_covered'


def test_unknown_is_interval_and_pending_not_imputed_or_dropped():
    spec, payload, judgment = fixture()
    judgment['verdicts'][0].update(verdict='CANNOT_ASSESS', missing_evidence='Reference unavailable')
    result = validate_atomic_judgment(spec, payload, judgment)
    assert result['Q']['score'] is None
    assert (result['Q']['lower'], result['Q']['upper']) == (20, 100)
    assert result['Q']['assessed_budget_coverage'] == .2
    assert result['business_status'] == 'pending'


def test_na_whole_group_does_not_inflate_other_groups():
    spec, payload, judgment = fixture()
    spec['criteria'][0]['na_allowed'] = True
    judgment['verdicts'][0]['verdict'] = 'NOT_APPLICABLE'
    result = validate_atomic_judgment(spec, payload, judgment)
    assert result['Q']['score'] is None
    assert result['Q']['groups']['expression']['score'] == 20
    assert result['Q']['applicable_budget_coverage'] == .2


def test_critical_failure_overrides_high_score():
    spec, payload, judgment = fixture()
    extra = copy.deepcopy(spec['criteria'][0])
    extra.update(id='Q.critical', weight=5, critical=True)
    spec['criteria'][0]['weight'] = 75
    spec['criteria'].append(extra)
    verdict = copy.deepcopy(judgment['verdicts'][0])
    verdict.update(id=extra['id'], verdict='UNMET')
    judgment['verdicts'].append(verdict)
    result = validate_atomic_judgment(spec, payload, judgment)
    assert result['Q']['score'] == 95
    assert result['business_status'] == 'fail'
    assert result['failed_critical'] == ['Q.critical']


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'fabricated_quote', 'future_turn', 'na', 'unknown_no_reason'])
def test_invalid_judgment_rejected(mutation):
    spec, payload, judgment = fixture()
    row = judgment['verdicts'][0]
    if mutation == 'missing':
        judgment['verdicts'].pop()
    elif mutation == 'duplicate':
        judgment['verdicts'][-1] = copy.deepcopy(row)
    elif mutation == 'fabricated_quote':
        row['evidence'][0].update(mode='quote', quote='invented quote')
    elif mutation == 'future_turn':
        payload['sources']['answer:1']['turn'] = 2
    elif mutation == 'na':
        row['verdict'] = 'NOT_APPLICABLE'
    else:
        row['verdict'] = 'CANNOT_ASSESS'
    with pytest.raises(ValueError):
        validate_atomic_judgment(spec, payload, judgment)


def test_evidence_met_requires_visible_tool_not_correct_answer():
    spec, payload, judgment = fixture()
    payload['sources']['event:1:5']['visible_to_answer'] = False
    with pytest.raises(ValueError, match='model-visible tool'):
        validate_atomic_judgment(spec, payload, judgment)


def test_incomplete_intent_can_show_known_failure_but_cannot_prove_met():
    spec, payload, judgment = fixture()
    spec['criteria'][2]['complete_intent_required'] = True
    payload['sources']['event:1:3']['intent_reported'] = False
    with pytest.raises(ValueError, match='Incomplete intent'):
        validate_atomic_judgment(spec, payload, judgment)
    judgment['verdicts'][2]['verdict'] = 'UNMET'
    assert validate_atomic_judgment(spec, payload, judgment)['P']['score'] == 70


def test_correct_answer_does_not_prove_correct_intent():
    spec, payload, judgment = fixture()
    judgment['verdicts'][2]['evidence'][0]['source_id'] = 'answer:1'
    with pytest.raises(ValueError, match='observed decision'):
        validate_atomic_judgment(spec, payload, judgment)


def test_redundant_call_needs_evidence_before_the_call():
    spec, payload, judgment = fixture()
    spec['criteria'][-1]['id'] = judgment['verdicts'][-1]['id'] = 'T.stop'
    row = judgment['verdicts'][-1]
    row.update(verdict='UNMET', before_event='event:1:9')
    row['evidence'] = [{'source_id': 'event:1:10', 'mode': 'scope', 'quote': ''}]
    with pytest.raises(ValueError, match='later evidence'):
        validate_atomic_judgment(spec, payload, judgment)
    row['evidence'][0]['source_id'] = 'event:1:5'
    assert validate_atomic_judgment(spec, payload, judgment)['P']['score'] == 80


def test_first_turn_cannot_use_final_answer_and_critical_failure_survives():
    spec, payload, judgment = fixture()
    payload['final_turn'] = 2
    payload['sources']['answer:2'] = {'kind': 'answer', 'turn': 2, 'text': 'Final correct answer'}
    for row in judgment['verdicts']:
        if row['id'].startswith('Q.'):
            row['evidence'][0]['source_id'] = 'answer:2'
    for item in list(spec['criteria']):
        if item['family'] == 'Q':
            extra = copy.deepcopy(item)
            extra.update(id='first.' + item['id'], family='Q1', scope='first', critical=True)
            spec['criteria'].append(extra)
            judgment['verdicts'].append({'id': extra['id'], 'verdict': 'MET', 'reason': 'Checked',
                                        'evidence': [{'source_id': 'answer:2', 'mode': 'scope', 'quote': ''}]})
    with pytest.raises(ValueError, match='Future-turn'):
        validate_atomic_judgment(spec, payload, judgment)
    for row in judgment['verdicts'][-2:]:
        row['evidence'][0]['source_id'] = 'answer:1'
    judgment['verdicts'][-2]['verdict'] = 'UNMET'
    result = validate_atomic_judgment(spec, payload, judgment)
    assert result['Q']['score'] == 100 and result['first_Q']['score'] == 20
    assert result['business_status'] == 'fail'


def test_bad_weights_fail_before_scoring():
    spec, _, _ = fixture()
    spec['criteria'][0]['weight'] = float('nan')
    with pytest.raises(ValueError, match='weight'):
        validate_spec(spec)


def test_awaiting_user_is_not_completed_task():
    spec, payload, judgment = fixture()
    judgment['completion'] = 'awaiting_user'
    assert validate_atomic_judgment(spec, payload, judgment)['business_status'] == 'pending'
