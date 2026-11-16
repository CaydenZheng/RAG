"""Exercise the real retrieval service with isolated synthetic evaluation data."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.agent.frozen_sources import FrozenSources
from src.evaluation.frozen_index import prepare_frozen_knowledge


@pytest.fixture
def synthetic_sources(monkeypatch):
    from src.llm import llm_client

    monkeypatch.setattr(type(llm_client), 'embedding_dim', property(lambda _: 3))
    monkeypatch.setattr(llm_client, 'embed', lambda texts: [[1.0, 0.0, 0.0] for _ in texts])
    monkeypatch.setattr(llm_client, 'embed_single', lambda _: [1.0, 0.0, 0.0])
    corpus = [
        {'_id': 'kb', 'title': 'Retention', 'text': 'Message retention rule'},
        {'_id': 'doc', 'title': 'Migration', 'text': 'Migration checklist'},
        {'_id': 'public', 'title': 'Attendance', 'text': 'Attendance records'},
    ]
    metadata = [{'document_id': pid, 'source_document_id': pid, 'source_type': kind}
                for pid, kind in [('kb', 'knowledge_base'), ('doc', 'internal'), ('public', 'official')]]
    return FrozenSources(corpus, metadata)


def test_isolated_index_reuses_pipeline_and_preserves_app_catalog(tmp_path, monkeypatch, synthetic_sources):
    from src.core.retrieval import QueryRewriterNode, RerankerNode
    from src.infra.index_catalog import index_catalog

    before = index_catalog.capture()
    knowledge, info = prepare_frozen_knowledge(synthetic_sources, tmp_path / 'evaluation')
    synthetic_sources.knowledge = knowledge
    synthetic_sources.retrieval_mode = 'hybrid+rerank'
    calls = []

    async def rewrite(query):
        calls.append('rewrite')
        return [query]

    def rerank(query, candidates, top_k):
        calls.append('rerank')
        return [{**c, 'rerank_score': 2.0} for c in candidates[:top_k]]

    monkeypatch.setattr(knowledge._rewriter, 'rewrite', rewrite)
    monkeypatch.setattr(knowledge._reranker, 'rerank', rerank)
    assert isinstance(knowledge._rewriter, QueryRewriterNode)
    assert isinstance(knowledge._reranker, RerankerNode)
    result = asyncio.run(synthetic_sources.registry().execute_async(
        'search_knowledge_base', {'query': 'retention'}, session_id='isolated-eval',
    ))
    assert result.success
    assert [c['document_id'] for c in result.data['contexts']] == ['kb']
    assert result.data['index_version'] == info['version_id']
    assert result.data['contexts'][0]['retrieval_scores']['dense_score'] is not None
    assert result.data['contexts'][0]['retrieval_scores']['bm25_score'] is not None
    assert result.data['contexts'][0]['retrieval_scores']['rerank_score'] == 2.0
    assert result.data['warnings'] == []
    assert calls == ['rewrite', 'rerank']
    assert index_catalog.capture() == before
    assert synthetic_sources.search_knowledge({'query': 'retention'}).error_code == 'async_retrieval_required'


def test_completed_index_reloads_without_reembedding_and_detects_corruption(tmp_path, monkeypatch, synthetic_sources):
    from src.infra.chroma_clients import get_chroma_client
    from src.llm import llm_client

    _, info = prepare_frozen_knowledge(synthetic_sources, tmp_path)

    def unexpected_embed(_):
        raise AssertionError('Completed index should reuse saved vectors')

    monkeypatch.setattr(llm_client, 'embed', unexpected_embed)
    _, again = prepare_frozen_knowledge(synthetic_sources, tmp_path)
    assert info == again
    collection = get_chroma_client(info['database_directory']).get_collection(
        'rag_v_' + info['version_id'],
    )
    collection.update(ids=['kb'], documents=['tampered'], embeddings=[[1.0, 0.0, 0.0]])
    with pytest.raises(ValueError, match='content mismatch'):
        prepare_frozen_knowledge(synthetic_sources, tmp_path)


def test_portable_vectors_restore_missing_runtime_cache(tmp_path, monkeypatch, synthetic_sources):
    from src.evaluation import frozen_index
    from src.infra.chroma_clients import get_chroma_client
    from src.llm import llm_client

    _, info = prepare_frozen_knowledge(synthetic_sources, tmp_path / 'index')
    replacement = get_chroma_client(tmp_path / 'replacement')
    monkeypatch.setattr(frozen_index, 'get_chroma_client', lambda _: replacement)

    def unexpected_embed(_):
        raise AssertionError('Saved embedding batches should rebuild the runtime cache')

    monkeypatch.setattr(llm_client, 'embed', unexpected_embed)
    _, rebuilt = prepare_frozen_knowledge(synthetic_sources, tmp_path / 'index')
    assert rebuilt['version_id'] == info['version_id']
    assert replacement.get_collection('rag_v_' + info['version_id']).count() == 3


def test_unicode_index_reopens_in_another_process(tmp_path):
    script = '''
import sys
from pathlib import Path
from src.agent.frozen_sources import FrozenSources
from src.evaluation.frozen_index import prepare_frozen_knowledge
from src.infra.chroma_clients import close_chroma_clients
from src.llm import llm_client
type(llm_client).embedding_dim = property(lambda _: 3)
def embed(texts):
    if sys.argv[2] == 'reload':
        raise AssertionError('Reload must use existing vectors')
    return [[1.0, 0.0, 0.0] for _ in texts]
llm_client.embed = embed
sources = FrozenSources(
    [{'_id': 'a', 'title': 'Title', 'text': 'Evidence'}],
    [{'document_id': 'a', 'source_document_id': 'doc', 'source_type': 'knowledge_base'}],
)
try:
    prepare_frozen_knowledge(sources, Path(sys.argv[1]))
finally:
    close_chroma_clients()
'''
    root = tmp_path / '中文索引'
    for mode in ['build', 'reload']:
        result = subprocess.run(
            [sys.executable, '-c', script, str(root), mode],
            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=90,
        )
        assert result.returncode == 0, result.stderr
    info = json.loads(next(root.glob('*/index.json')).read_text(encoding='utf-8'))
    assert Path(info['portable_vectors']).is_dir()
