"""Reject the scoring failures reproduced in the six-case audit."""

import copy

import pytest

from src.evaluation.support_grading import (
    derive_dimension_grades,
    prepare_input,
    validate_and_score,
)


def sample():
    payload = {'answers': {'1': '需要管理员权限。'}, 'requirements': [{'id': 'F1'}],
               'trajectory': [{'turn': 1, 'sequence': 3, 'step': 'tool_done', 'text': '支持权限设置'},
                              {'turn': 1, 'sequence': 5, 'step': 'tool_call'},
                              {'turn': 1, 'sequence': 6, 'step': 'tool_done', 'text': '后续证据'}]}
    full = {'grade': 4, 'defect_ids': [], 'basis': '已覆盖'}
    judgment = {'answers': [{'turn': 1, **{d: copy.deepcopy(full) for d in 'ABC'}}],
                'process': {d: copy.deepcopy(full) for d in 'IET'},
                'checks': [{'id': 'F1', 'status': 'pass', 'basis': '有依据', 'defect_ids': []}],
                'defects': [], 'safety': {'status': 'not_covered', 'basis': '未测安全专项'},
                'review_needed': []}
    return payload, judgment


def defect(judgment):
    item = {'id': 'D1', 'turn': 1, 'summary': '条件错误', 'answer_dimension': 'A',
            'answer_grade': 2, 'process_dimension': None, 'process_grade': None,
            'evidence': [{'source': 'answer', 'turn': 1, 'quote': '需要管理员权限。'}],
            'before_event': None}
    judgment['defects'].append(item)
    judgment['answers'][0]['A'].update(grade=2, defect_ids=['D1'])
    return item


def test_weighted_score_and_known_condition_anchor():
    payload, judgment = sample()
    defect(judgment)
    score = validate_and_score(payload, judgment)
    assert score['Q'] == 7.5
    assert score['P'] == 10
    assert score['business_status'] == 'fail'


@pytest.mark.parametrize('mutation', ['double', 'missing', 'event', 'quote', 'future', 'final'])
def test_invalid_deductions_and_evidence_are_rejected(mutation):
    payload, judgment = sample()
    item = defect(judgment)
    if mutation == 'double':
        judgment['answers'][0]['B'].update(grade=2, defect_ids=['D1'])
    elif mutation == 'missing':
        judgment['defects'] = []
    elif mutation == 'event':
        item['evidence'] = [{'source': 'event', 'turn': 1, 'sequence': 999999, 'quote': '不存在'}]
    elif mutation == 'quote':
        item['evidence'][0]['quote'] = '伪造证据'
    elif mutation == 'future':
        item['before_event'] = {'turn': 1, 'sequence': 5}
        item['evidence'] = [{'source': 'event', 'turn': 1, 'sequence': 6, 'quote': '后续证据'}]
    else:
        item['before_event'] = {'turn': 1, 'sequence': 5}
    with pytest.raises(ValueError):
        validate_and_score(payload, judgment)


def test_valid_pre_call_evidence_and_unknown_observation():
    payload, judgment = sample()
    item = defect(judgment)
    item['before_event'] = {'turn': 1, 'sequence': 5}
    item['evidence'] = [{'source': 'event', 'turn': 1, 'sequence': 3, 'quote': '支持权限设置'}]
    judgment['process']['I']['grade'] = None
    assert validate_and_score(payload, judgment)['P'] is None


def test_input_uses_one_authoritative_fact_list_and_preserves_intent_target():
    annotation = {'required_facts': [{'id': 'F1', 'text': '等价入口可接受'}],
                  'facts': [{'id': 'F1', 'text': '必须使用唯一入口'}], 'critical': [],
                  'suite': 'coverage', 'goal': '核实实际时间', 'known': ['管理员']}
    result = {'input': [{'role': 'user', 'text': '哪里查看？'}], 'predictions': [{'text': '在后台查看。'}]}
    events = [{'turn': 1, 'step': 'model_context',
               'observation': {'stage': 'final_generation', 'messages': [{'role': 'user', 'content': '证据'}]}}]
    payload = prepare_input(annotation, result, events)
    assert payload['requirements'] == [{'id': 'F1', 'requirement': '等价入口可接受'}]
    assert 'facts' not in payload and 'criteria' not in payload
    assert payload['task_criteria']['goal'] == '核实实际时间'


def test_defect_attribution_derives_grades_without_second_model_deduction():
    payload, judgment = sample()
    defect(judgment)
    for record, names in [(judgment['answers'][0], 'ABC'), (judgment['process'], 'IET')]:
        for name in names:
            record[name] = {'status': 'reviewed', 'basis': '已核对'}
    derived = derive_dimension_grades(judgment)
    assert derived['answers'][0]['A']['grade'] == 2
    assert derived['answers'][0]['B']['grade'] == 4
    assert validate_and_score(payload, derived)['Q'] == 7.5


def test_atomic_audits_cannot_fail_without_a_defect():
    payload, judgment = sample()
    payload['evaluation_audits'] = [{'id': 'I_DIRECTION'}]
    judgment['audits'] = [{'id': 'I_DIRECTION', 'status': 'fail', 'basis': '目标错误', 'defect_ids': []}]
    with pytest.raises(ValueError, match='audit lacks'):
        validate_and_score(payload, judgment)


def test_compact_input_uses_visible_tool_payload_not_hidden_raw_result():
    annotation = {'required_facts': [], 'critical': [], 'suite': 'coverage'}
    visible = '[UNTRUSTED_TOOL_RESULT name="read_document"]\n{"data":{"text":"可见证据"}}\n[/UNTRUSTED_TOOL_RESULT]'
    events = [{'turn': 1, 'sequence': 5, 'step': 'tool_done',
               'observation': {'model_content': visible, 'truncated': False},
               'tool_result': {'data': {'text': '未送给模型的原始大文本'}}},
              {'turn': 1, 'step': 'model_context',
               'observation': {'stage': 'final_generation', 'messages': [{'content': visible}]}}]
    payload = prepare_input(annotation, {'input': [], 'predictions': [{'text': '答复'}]}, events)
    assert payload['trajectory'][0]['model_visible_result']['data']['text'] == '可见证据'
    assert payload['trajectory'][0]['visible_to_final_answer'] is True
    assert 'tool_result' not in payload['trajectory'][0]
    assert 'final_model_inputs' not in payload


def test_pre_call_proof_can_also_locate_the_action_but_cannot_be_action_only():
    payload, judgment = sample()
    payload['trajectory'][1]['params'] = {'query': '重复检索'}
    item = defect(judgment)
    item['before_event'] = {'turn': 1, 'sequence': 5}
    item['evidence'] = [{'source': 'event', 'turn': 1, 'sequence': 3, 'quote': '支持权限设置'},
                        {'source': 'event', 'turn': 1, 'sequence': 5, 'quote': '重复检索'}]
    assert validate_and_score(payload, judgment)['Q'] == 7.5
    item['evidence'].pop(0)
    with pytest.raises(ValueError, match='earlier supporting'):
        validate_and_score(payload, judgment)


def test_generation_omission_does_not_imply_retrieval_failure():
    payload, judgment = sample()
    item = defect(judgment)
    item.update(stage='generation', process_dimension='E', process_grade=2)
    with pytest.raises(ValueError, match='cannot infer a process'):
        validate_and_score(payload, judgment)
