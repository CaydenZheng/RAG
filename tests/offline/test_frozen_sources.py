"""Guard corpus boundaries and answer isolation for the frozen Agent adapter."""

import asyncio
import json
import re

import pytest

from src.agent.frozen_sources import FrozenSources


def sources():
    return FrozenSources(
        [
            {'_id': 'faq', 'title': '企业群', 'text': '群消息知识库'},
            {'_id': 'official', 'title': '消息存储', 'text': '官方群消息保留规则'},
            {'_id': 'internal', 'title': '迁移', 'text': '内部迁移操作手册'},
        ],
        [
            {'document_id': 'faq', 'source_document_id': 'faq', 'source_type': 'knowledge_base'},
            {'document_id': 'official', 'source_document_id': 'doc-o', 'source_type': 'official'},
            {'document_id': 'internal', 'source_document_id': 'doc-i', 'source_type': 'internal'},
        ],
    )


def test_search_respects_source_directory():
    corpus = sources()
    knowledge = corpus.search_knowledge({'query': '群消息'})
    assert {x['source_type'] for x in knowledge.data['contexts']} <= {'knowledge_base'}
    official = corpus.search_documents({'query': '群消息', 'source_type': 'official'})
    assert {x['source_type'] for x in official.data['contexts']} == {'official'}


def test_read_only_accepts_frozen_identifiers():
    corpus = sources()
    for identifier in ['../../.env', 'https://www.feishu.cn/unknown', 'not-present']:
        assert corpus.read_document({'document_id': identifier}).error_code == 'source_not_in_snapshot'
    result = corpus.read_document({'document_id': 'doc-o'})
    assert result.data['contexts'][0]['document_id'] == 'official'
    assert result.data['next_offset'] is None
    assert corpus.read_document({'document_id': 'doc-o', 'offset': 10}).data['contexts'] == []


def test_registry_validates_input_and_omits_external_tools():
    registry = sources().registry()
    descriptions = registry.get_tool_descriptions()
    assert 'search_documents' in descriptions
    assert 'search_web' not in descriptions
    result = asyncio.run(registry.execute_async(
        'read_document', {'document_id': 'doc-o', 'offset': -1}, session_id='eval-test',
    ))
    assert not result.success


def test_duplicate_or_missing_source_metadata_rejected():
    row = {'_id': 'one', 'title': 'Title', 'text': 'Body'}
    with pytest.raises(ValueError):
        FrozenSources([row, row], [])
    with pytest.raises(ValueError):
        FrozenSources([row], [])


def test_registered_url_reuses_snapshot_and_locator_is_unambiguous():
    corpus = FrozenSources(
        [{'_id': 'one', 'title': 'Document', 'text': 'Saved text'}],
        [{'document_id': 'one', 'source_document_id': 'doc', 'source_type': 'internal',
          'url': 'https://example.feishu.cn/docx/registered'}],
    )
    result = corpus.read_document({'url': 'https://example.feishu.cn/docx/registered#part'})
    assert result.data['contexts'][0]['text'] == 'Saved text'
    assert result.data['retrieval_mode'] == 'local_snapshot'
    assert corpus.read_document({}).error_code == 'document_locator_invalid'
    assert corpus.read_document({'document_id': 'doc', 'url': 'https://example.com'}).error_code == 'document_locator_invalid'


def test_query_read_keeps_unmatched_passages_and_explains_pagination(monkeypatch):
    corpus = FrozenSources(
        [{'_id': str(i), 'title': 'Manual', 'text': f'Step {i}'} for i in range(3)],
        [{'document_id': str(i), 'source_document_id': 'manual', 'source_type': 'internal'}
         for i in range(3)],
    )
    monkeypatch.setattr(corpus.index, 'search', lambda *args, **kwargs: [('2', 1.0)])
    first = corpus.read_document({'document_id': 'manual', 'query': 'checklist', 'limit': 1})
    rest = corpus.read_document({'document_id': 'manual', 'query': 'checklist', 'offset': 1})
    assert [c['document_id'] for c in first.data['contexts'] + rest.data['contexts']] == ['2', '0', '1']
    assert first.data['total_passages'] == 3
    assert first.data['paging_query'] == 'checklist'
    assert 'restart at offset 0' in first.data['pagination']
    assert rest.data['next_offset'] is None


def test_task_projection_excludes_gold_and_preserves_history():
    from scripts.run_frozen_agent_eval import model_messages

    task = {
        'input': [{'speaker': 'user', 'text': 'First'}, {'speaker': 'agent', 'text': 'Previous'},
                  {'speaker': 'user', 'text': 'Follow-up'}],
        'targets': [{'text': 'SECRET GOLD'}], 'answerability': ['ANSWERABLE'],
        'contexts': [{'text': 'GOLD CONTEXT'}],
    }
    assert model_messages(task) == [
        {'role': 'user', 'content': 'First'}, {'role': 'assistant', 'content': 'Previous'},
        {'role': 'user', 'content': 'Follow-up'},
    ]


def test_judge_keeps_multiline_question_and_all_observed_evidence(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from scripts.score_frozen_agent_eval import run
    from src.llm import llm_client

    judge = tmp_path / 'judge.py'
    judge.write_text(
        'def extract_document_texts(rows): return [r["text"] for r in rows]\n'
        'def extract_texts(rows): return rows[0]["text"]\n'
        'def extract_reference(rows): return rows[0]["text"]\n'
        'def extract_conversation(rows): return "\\n".join(r["text"] for r in rows)\n'
        'def format_conversation_radbench(df): return [str(df.iloc[0].to_dict())]\n',
        encoding='utf-8',
    )
    result = {
        'task_id': 'multiline', 'input': [{'speaker': 'user', 'text': 'Question\nhttps://example.com/article'}],
        'contexts': [{'text': 'first'}], 'observed_contexts': [{'text': 'first'}, {'text': 'second'}],
        'observed_context_count': 2, 'predictions': [{'text': 'Answer'}], 'targets': [{'text': 'Reference'}],
    }
    results_file = tmp_path / 'results.jsonl'
    results_file.write_text(json.dumps(result) + '\n', encoding='utf-8')

    async def judge_response(messages, **kwargs):
        assert 'https://example.com/article' in messages[0]['content']
        assert 'second' in messages[0]['content']
        return 'Rating: [[8]]'

    monkeypatch.setattr(llm_client, 'chat_async', judge_response)
    output = tmp_path / 'scores.jsonl'
    assert asyncio.run(run(SimpleNamespace(results=results_file, judge_utils=judge, output=output))) == 0
    assert json.loads(output.read_text(encoding='utf-8'))['score'] == 0.8


def test_live_runner_uses_actual_first_reply_and_hides_future_input(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import tiktoken

    def tokens(text):
        return re.findall(r'[A-Za-z0-9_]+|[^\x00-\x7f]|[^\w\s]', text)

    monkeypatch.setattr(tiktoken, 'get_encoding', lambda _: SimpleNamespace(encode=tokens))
    from src.utils import token_counter

    monkeypatch.setattr(token_counter, 'count_tokens', lambda text: len(tokens(text)))
    from scripts.run_frozen_agent_eval import parser, run
    from src.llm import llm_client

    task = {
        'task_id': 'synthetic-live', 'first_user': 'Which rule applies?',
        'user_supplement': 'SUPPLEMENT_AFTER_FIRST_REPLY',
        'first_turn_expectation': 'GOLD_FIRST_EXPECTATION',
        'final_reference': 'GOLD_FINAL_REFERENCE',
    }
    task_file = tmp_path / 'tasks.jsonl'
    corpus_file = tmp_path / 'corpus.jsonl'
    metadata_file = tmp_path / 'metadata.jsonl'
    task_file.write_text(json.dumps(task) + '\n', encoding='utf-8')
    corpus_file.write_text(json.dumps({'_id': 'rule', 'title': 'Rule', 'text': 'Synthetic fact'}) + '\n', encoding='utf-8')
    metadata_file.write_text(json.dumps({
        'document_id': 'rule', 'source_document_id': 'rule', 'source_type': 'knowledge_base',
    }) + '\n', encoding='utf-8')
    prompts = []
    answers = iter(['Which entry are you using?', 'Actual final reply'])
    plans = iter([
        {'action': 'tool_call', 'tool_name': 'read_document', 'tool_params': {'document_id': 'rule'}},
        {'action': 'final_answer'}, {'action': 'final_answer'},
    ])

    async def plan(messages, **kwargs):
        prompts.append(json.dumps(messages))
        return json.dumps(next(plans))

    async def answer(messages, **kwargs):
        prompts.append(json.dumps(messages))
        yield next(answers)

    monkeypatch.setattr(llm_client, 'chat_async', plan)
    monkeypatch.setattr(llm_client, 'chat_stream_async', answer)
    args = parser().parse_args([
        '--mode', 'live', '--tasks', str(task_file), '--corpus', str(corpus_file),
        '--metadata', str(metadata_file), '--output', str(tmp_path / 'output'),
    ])
    assert asyncio.run(run(args)) == 0
    result = json.loads((tmp_path / 'output/results.jsonl').read_text(encoding='utf-8'))
    assert result['input'][1]['text'] == 'Which entry are you using?'
    assert result['predictions'][0]['text'] == 'Actual final reply'
    assert result['contexts'][0]['text'] == 'Synthetic fact'
    assert len(prompts) == 5
    assert all('SUPPLEMENT_AFTER_FIRST_REPLY' not in prompt for prompt in prompts[:3])
    assert 'SUPPLEMENT_AFTER_FIRST_REPLY' in prompts[3]
    assert all('GOLD_' not in prompt for prompt in prompts)
