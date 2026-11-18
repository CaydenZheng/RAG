"""The per-passage gate filters noise without treating fusion as rerank scores."""

import asyncio

import pytest


@pytest.mark.parametrize('degraded', [False, True])
def test_filter_operates_on_each_reranked_passage_only(monkeypatch, degraded):
    from config.settings import settings
    from src.core.knowledge import EvidencePolicy, KnowledgeSystem

    monkeypatch.setattr(settings, 'query_rewrite_enabled', False)
    monkeypatch.setattr(settings, 'rerank_min_score', 0.1)
    monkeypatch.setattr(settings, 'rerank_filter_calibration_id', 'offline-test')

    class Retriever:
        def search(self, *args):
            return [{'chunk_id': 'relevant', 'rerank_score': 0.8, 'rrf_score': 0.03},
                    {'chunk_id': 'noise', 'rerank_score': 0.02, 'rrf_score': 0.02}]

    class Reranker:
        def rerank(self, query, candidates, top_k):
            if degraded:
                raise RuntimeError('offline injected model failure')
            return candidates

    knowledge = KnowledgeSystem(retriever=Retriever(), reranker=Reranker(), evidence_policy=EvidencePolicy())
    result = asyncio.run(knowledge.retrieve('question'))
    assert [c['chunk_id'] for c in result.chunks] == (['relevant', 'noise'] if degraded else ['relevant'])
    assert len(result.candidates) == 2
    assert ('low_relevance_passages_filtered' in result.warnings) is not degraded


def test_filter_requires_identifiable_calibration(tmp_path):
    from src.agent.support import read_support_configuration

    path = tmp_path / 'profile.json'
    path.write_text('{"retrieval_settings":{"rerank_min_score":0.1}}', encoding='utf-8')
    with pytest.raises(ValueError, match='calibration ID'):
        read_support_configuration(path)
