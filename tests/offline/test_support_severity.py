"""A one-point deduction must never become a retained grade of one."""

import pytest

from src.evaluation.support_severity import apply_severity_labels


def fixture():
    payload = {'answers': {'1': '正确回答'}, 'requirements': [],
               'trajectory': [{'turn': 1, 'sequence': 3, 'step': 'decision', 'text': '迁移目标错误'}]}
    def review():
        return {'status': 'reviewed', 'basis': '已审查'}
    judgment = {'answers': [{'turn': 1, **{name: review() for name in 'ABC'}}],
                'process': {name: review() for name in 'IET'}, 'checks': [], 'audits': [],
                'safety': {'status': 'not_covered'}, 'review_needed': [],
                'defects': [{'id': 'D1', 'turn': 1, 'summary': '轻微理解错误，答案正确',
                             'stage': 'interpretation', 'answer_dimension': None, 'answer_grade': None,
                             'process_dimension': 'I', 'process_grade': 1,
                             'before_event': None, 'evidence': [{'source': 'event', 'turn': 1, 'sequence': 3, 'quote': '迁移目标错误'}]}]}
    return payload, judgment


def test_explicit_minor_label_maps_to_three_and_keeps_original_untouched():
    payload, judgment = fixture()
    revised, score = apply_severity_labels(payload, judgment, [
        {'defect_id': 'D1', 'family': 'process', 'severity': 'minor', 'basis': '未影响主要取证和正确答案'}])
    assert revised['process']['I']['grade'] == 3
    assert score['P'] == 9.25
    assert judgment['defects'][0]['process_grade'] == 1


def test_missing_or_numeric_severity_is_not_silently_inferred():
    payload, judgment = fixture()
    with pytest.raises(ValueError, match='coverage'):
        apply_severity_labels(payload, judgment, [])
    with pytest.raises(ValueError, match='explicit'):
        apply_severity_labels(payload, judgment, [
            {'defect_id': 'D1', 'family': 'process', 'severity': 1, 'basis': '不能猜数字含义'}])
