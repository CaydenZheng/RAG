"""Read-only tools over an explicitly supplied, versioned evaluation corpus."""

import json
from collections import defaultdict
from urllib.parse import urlsplit, urlunsplit

from src.agent.tools import SafetyLevel, ToolDef, ToolParam, ToolRegistry
from src.core.agent_runtime import ToolResult
from src.utils.bm25_store import BM25Store


class FrozenSources:
    """Keep corpus contents separate from task answers and evaluation labels."""

    def __init__(self, corpus: list[dict], metadata: list[dict]):
        self.knowledge = None
        self.retrieval_mode = 'bm25_only'
        self.warnings = []
        self.passages = {row['_id']: dict(row) for row in corpus}
        self.metadata = {row['document_id']: dict(row) for row in metadata}
        if len(self.passages) != len(corpus) or len(self.metadata) != len(metadata):
            raise ValueError('Duplicate frozen source identifiers')
        if set(self.passages) != set(self.metadata):
            raise ValueError('Every frozen passage must have source metadata')
        self.documents: dict[str, list[str]] = defaultdict(list)
        self.urls: dict[str, str] = {}
        for pid, row in self.metadata.items():
            if row['source_type'] not in {'knowledge_base', 'official', 'internal', 'public'}:
                raise ValueError('Unsupported source type')
            self.documents[row['source_document_id']].append(pid)
            if row.get('url'):
                self.urls[self.normalize_url(row['url'])] = row['source_document_id']
        self.index = BM25Store()
        self.index.build(
            [row['title'] + '\n' + row['text'] for row in corpus],
            [row['_id'] for row in corpus],
        )

    def context(self, pid: str, score: float | None = None) -> dict:
        passage = self.passages[pid]
        meta = self.metadata[pid]
        return {
            'document_id': pid, 'title': passage['title'], 'text': passage['text'],
            'score': score, 'source_document_id': meta['source_document_id'],
            'source_type': meta['source_type'], 'url': meta.get('url', ''),
            'scope': meta.get('scope', ''),
            'evidence_limit': meta.get('evidence_limit', ''),
        }

    def search(self, query: str, source_types: set[str], limit: int = 5) -> list[dict]:
        allowed = {pid for pid, meta in self.metadata.items()
                   if meta['source_type'] in source_types and meta.get('retrieval_eligible', True)}
        hits = self.index.search(query, top_k=limit, allowed_chunk_ids=allowed)
        return [self.context(pid, score) for pid, score in hits]

    def search_knowledge(self, params: dict) -> ToolResult:
        if self.knowledge is not None:
            return ToolResult(success=False, error_code='async_retrieval_required',
                              error='Use the async tool entry point for indexed retrieval.')
        contexts = self.fit_contexts(self.search(
            params['query'], {'knowledge_base', 'public'}, params.get('limit', 5),
        ))
        return ToolResult(success=True, data={
            'contexts': contexts, 'retrieval_mode': 'bm25_only', 'warnings': self.warnings,
            'evidence': {'sufficient': None if contexts else False,
                         'reason': 'threshold_not_calibrated' if contexts else 'no_retrieval_results'},
        })

    def search_documents(self, params: dict) -> ToolResult:
        if self.knowledge is not None:
            return ToolResult(success=False, error_code='async_retrieval_required',
                              error='Use the async tool entry point for indexed retrieval.')
        selected = params.get('source_type', 'all')
        source_types = {'official', 'internal'} if selected == 'all' else {selected}
        contexts = self.fit_contexts(self.search(
            params['query'], source_types, params.get('limit', 5),
        ))
        return ToolResult(success=True, data={
            'contexts': contexts, 'retrieval_mode': 'bm25_only', 'warnings': self.warnings,
            'evidence': {'sufficient': None if contexts else False,
                         'reason': 'threshold_not_calibrated' if contexts else 'no_retrieval_results'},
        })

    async def search_indexed(self, params: dict, source_types: set[str]) -> ToolResult:
        if self.knowledge is None:
            raise RuntimeError('Indexed retrieval has not been configured')
        result = await self.knowledge.retrieve(
            params['query'], top_k=params.get('limit', 5), mode=self.retrieval_mode,
            metadata_filter={'$and': [{'source_type': {'$in': sorted(source_types)}},
                                      {'retrieval_eligible': True}]},
        )
        contexts = []
        for chunk in result.chunks:
            pid = chunk['chunk_id']
            if pid not in self.passages or self.metadata[pid]['source_type'] not in source_types:
                raise ValueError('Retrieved passage escaped the evaluation corpus scope')
            context = self.context(pid, chunk.get('rerank_score', chunk.get('rrf_score', 0)))
            context['retrieval_scores'] = {key: chunk.get(key) for key in
                                           ('dense_score', 'bm25_score', 'rrf_score', 'rerank_score')}
            contexts.append(context)
        return ToolResult(success=True, data={
            'contexts': self.fit_contexts(contexts), 'retrieval_mode': self.retrieval_mode,
            'query_variants': result.query_variants, 'index_version': result.index_version,
            'warnings': list(result.warnings), 'evidence': result.evidence.to_dict(),
        }, observation={
            'query_variants': list(result.query_variants),
            'candidate_ids': [chunk['chunk_id'] for chunk in result.candidates],
            'selected_ids': [chunk['chunk_id'] for chunk in result.chunks],
            'returned_ids': [context['document_id'] for context in self.fit_contexts(contexts)],
        })

    async def search_knowledge_async(self, params: dict) -> ToolResult:
        if self.knowledge is None:
            return self.search_knowledge(params)
        return await self.search_indexed(params, {'knowledge_base', 'public'})

    async def search_documents_async(self, params: dict) -> ToolResult:
        if self.knowledge is None:
            return self.search_documents(params)
        selected = params.get('source_type', 'all')
        source_types = {'official', 'internal'} if selected == 'all' else {selected}
        return await self.search_indexed(params, source_types)

    @staticmethod
    def fit_contexts(contexts: list[dict]) -> list[dict]:
        """Keep whole passages within the harness's recorded character allowance."""
        selected = []
        for context in contexts:
            if len(json.dumps(selected + [context], ensure_ascii=False)) > 12000:
                break
            selected.append(context)
        if contexts and not selected:
            raise ValueError('A frozen passage exceeds the tool payload limit; split it before evaluation')
        return selected

    @staticmethod
    def normalize_url(url: str) -> str:
        parsed = urlsplit(url)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip('/'), '', ''))

    def read_document(self, params: dict, official=None) -> ToolResult:
        document_id = params.get('document_id')
        url = params.get('url')
        if bool(document_id) == bool(url):
            return ToolResult(success=False, error_code='document_locator_invalid',
                              error='Provide exactly one document_id or url.')
        if url:
            if official and urlsplit(url).path.startswith('/hc/'):
                return official.read(params)
            document_id = self.urls.get(self.normalize_url(url))
        offset = params.get('offset', 0)
        limit = params.get('limit', 20)
        if document_id in self.passages:
            identifiers = [document_id]
        else:
            identifiers = self.documents.get(document_id, [])
        if not identifiers:
            return ToolResult(success=False, error_code='source_not_in_snapshot',
                              error='The identifier is absent from this corpus snapshot.')
        total_passages = len(identifiers)
        query = params.get('query', '').strip()
        scores = {}
        if query:
            hits = self.index.search(params['query'], top_k=len(identifiers), allowed_chunk_ids=identifiers)
            scores = dict(hits)
            ranked = [pid for pid, _ in hits]
            matched = set(ranked)
            identifiers = ranked + [pid for pid in identifiers if pid not in matched]
        selected = self.fit_contexts([
            self.context(pid, scores.get(pid, 0.0) if query else None)
            for pid in identifiers[offset:offset + limit]
        ])
        next_offset = offset + len(selected)
        return ToolResult(success=True, data={
            'contexts': selected,
            'retrieval_mode': 'local_snapshot',
            'next_offset': next_offset if next_offset < len(identifiers) else None,
            'total_passages': total_passages,
            'paging_query': query,
            'ranking': 'bm25' if query else 'document_order',
            'complete': offset == 0 and next_offset == total_passages,
            'pagination': 'Keep the same query while paging. When changing query, restart at offset 0. '
                          'Query ranks passages first; remaining passages follow in document order.',
        })

    def registry(self, official=None) -> ToolRegistry:
        registry = ToolRegistry()
        query_params = [
            ToolParam('query', 'str', 'Search terms from the user question.', min_length=1),
            ToolParam('limit', 'int', 'Maximum results.', required=False,
                      default=5, minimum=1, maximum=5),
        ]
        registry.register(ToolDef(
            name='search_knowledge_base',
            description='Search reviewed support FAQs and historical cases, or the public benchmark corpus.',
            params=query_params, safety_level=SafetyLevel.WHITELIST,
            execute_fn=self.search_knowledge,
            execute_async_fn=self.search_knowledge_async,
        ))
        if any(row['source_type'] in {'official', 'internal'} for row in self.metadata.values()):
            registry.register(ToolDef(
                name='search_documents',
                description='Search saved official and internal documentation. Returns passages and source IDs. '
                            'Use this for detailed procedures, official rules, or gaps in the FAQ.',
                params=query_params + [ToolParam(
                    'source_type', 'str', 'Source directory filter.', required=False,
                    default='all', choices=('all', 'official', 'internal'),
                )], safety_level=SafetyLevel.WHITELIST, execute_fn=self.search_documents,
                execute_async_fn=self.search_documents_async,
            ))
        if official:
            registry.register(ToolDef(
                name='search_official',
                description='Search Feishu official Help Center when local knowledge or documentation '
                            'is insufficient. Use concise product keywords. Results contain links and '
                            'snippets; read the relevant article for full evidence.',
                params=query_params, safety_level=SafetyLevel.WHITELIST, execute_fn=official.search,
            ))
        registry.register(ToolDef(
            name='read_document',
            description='Read a known local document ID, a registered document URL, or a Feishu '
                        'Help Center article URL. Provide one locator. Optional query finds relevant '
                        'sections inside the document. To continue, keep the same query and use next_offset. '
                        'When changing query, restart at offset 0. Omit query for document-order reading.',
            params=[
                ToolParam('document_id', 'str', 'Known source document or passage identifier.',
                          required=False, min_length=1),
                ToolParam('url', 'str', 'Article or registered document URL.', required=False, min_length=1),
                ToolParam('query', 'str', 'Optional keywords to locate relevant sections.', required=False),
                ToolParam('offset', 'int', 'Offset within the SAME query results. Reset to 0 for a new query.',
                          required=False, default=0, minimum=0),
                ToolParam('limit', 'int', 'Maximum passages within the response budget. Short documents fit in one read.', required=False,
                          default=20, minimum=1, maximum=20),
            ], safety_level=SafetyLevel.WHITELIST,
            execute_fn=lambda params: self.read_document(params, official=official),
        ))
        return registry
