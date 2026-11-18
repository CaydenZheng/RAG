"""Planning guidance must not replace final answer format or change ordinary skills."""
from src.core.run_options import RunOptions, SkillSnapshot, skill_instructions


def test_stage_specific_guidance_keeps_final_answer_format_out_of_planning():
    skill = SkillSnapshot('support', 'Support', '1', 'FINAL: write concise steps',
                          planning_body='PLAN: retain unresolved applicability conditions')
    options = RunOptions('flash', 'test-model', skills=(skill,))
    planning = skill_instructions(options, planning=True)
    final = skill_instructions(options)
    assert 'retain unresolved' in planning
    assert 'FINAL: write' not in planning
    assert 'FINAL: write' in final
    assert 'PLAN: retain' not in final
    assert 'exact JSON decision' in planning


def test_existing_skill_body_is_preserved_in_both_stages():
    options = RunOptions('flash', 'test-model', skills=(SkillSnapshot('normal', 'Normal', '1', 'User guidance'),))
    assert 'User guidance' in skill_instructions(options, planning=True)
    assert 'User guidance' in skill_instructions(options)


def test_support_assembly_does_not_change_public_or_original_skill_protocol(monkeypatch):
    from types import SimpleNamespace

    from src.agent import support
    monkeypatch.setattr(support, 'AgentHarness', lambda **kwargs: kwargs)
    sources = SimpleNamespace(registry=lambda official: object())
    internal = support.SupportProfile().runtime(sources, object())['skills'][0]
    public = support.SupportProfile(suite='public').runtime(sources, object())['skills'][0]
    original = support.SupportProfile(response_style='original').runtime(sources, object())['skills'][0]
    assert internal.planning_body and internal.planning_body != internal.body
    assert internal.version == '5'
    assert public.planning_body is original.planning_body is None
    assert public.version == original.version == '4'
    assert public.body == support.PUBLIC_INSTRUCTIONS
