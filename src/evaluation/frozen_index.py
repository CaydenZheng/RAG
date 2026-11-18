"""Isolated evaluation indexes using the application's retrieval components."""

import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path

from config.settings import settings
from src.core.index_versions import IndexVersion
from src.core.knowledge import EvidencePolicy, KnowledgeSystem
from src.core.retrieval import HybridRetrieverNode
from src.infra.chroma_clients import get_chroma_client
from src.infra.index_catalog import IndexCatalog
from src.llm import llm_client


def write_json_atomic(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_frozen_knowledge(sources, root: Path):
    """Build or resume one content-addressed corpus without touching the app index."""
    if not sources.passages:
        raise ValueError('An indexed corpus must contain passages')
    identity = {
        'passages': list(sources.passages.values()), 'metadata': list(sources.metadata.values()),
        'embedding_model': settings.local_embedding_model, 'format': 'frozen-passages-v3',
    }
    fingerprint = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    directory = root / fingerprint
    directory.mkdir(parents=True, exist_ok=True)
    catalog = IndexCatalog(directory / 'catalog')
    database = directory / 'chroma'
    if os.name == 'nt' and not str(database.resolve()).isascii():
        # Windows HNSW silently omits binary files under non-ASCII paths.
        # Rebuildable runtime cache uses ASCII; portable vectors stay with artifacts.
        cache_key = hashlib.sha256(str(directory.resolve()).encode()).hexdigest()
        database = Path(tempfile.gettempdir()) / 'ragrag-eval-chroma' / cache_key
    client = get_chroma_client(database)
    info_path = directory / 'index.json'
    chunks = []
    for pid, passage in sources.passages.items():
        meta = sources.metadata[pid]
        chunks.append({
            'chunk_id': pid, 'doc_id': meta['source_document_id'],
            'text': passage['title'] + '\n' + passage['text'],
            'metadata': {'source_type': meta['source_type'], 'title': passage['title'],
                         'retrieval_eligible': meta.get('retrieval_eligible', True),
                         'source_document_id': meta['source_document_id'], 'source': meta.get('url', '')},
        })
    if info_path.exists():
        info = json.loads(info_path.read_text(encoding='utf-8'))
        if info['fingerprint'] != fingerprint:
            raise ValueError('Evaluation index fingerprint mismatch')
        manifest = catalog.manifest(info['version_id'])
    else:
        dimension = llm_client.embedding_dim
        # Index identity hashes source text and metadata, and records vector dimension.
        dimension_marker = [0.0] * dimension
        manifest = IndexVersion.create(
            ({**chunk, 'embedding': dimension_marker} for chunk in chunks),
            parser='frozen-jsonl-v1', chunker='preserved-passages-v1',
            chunk_size=max(len(c['text']) for c in chunks), chunk_overlap=0,
            embedding_model=settings.local_embedding_model,
        )
    collection = client.get_or_create_collection(
        name=manifest.collection_name,
        metadata={'hnsw:space': 'cosine', 'index_version': manifest.version_id,
                  'content_checksum': manifest.content_checksum},
    )
    present = set(collection.get(include=[])['ids'])
    for start in range(0, len(chunks), 32):
        batch = chunks[start:start + 32]
        if all(c['chunk_id'] in present for c in batch):
            continue
        vector_path = directory / 'embeddings' / f'{start:08d}.json'
        vector_path.parent.mkdir(exist_ok=True)
        ids = [c['chunk_id'] for c in batch]
        if vector_path.exists():
            saved = json.loads(vector_path.read_text(encoding='utf-8'))
            if saved['ids'] != ids:
                raise ValueError('Saved embedding batch membership mismatch')
            vectors = saved['vectors']
        else:
            vectors = llm_client.embed([c['text'] for c in batch])
            write_json_atomic(vector_path, {'ids': ids, 'vectors': vectors})
        collection.upsert(
            ids=ids, embeddings=vectors,
            documents=[c['text'] for c in batch],
            metadatas=[{**c['metadata'], 'index_version': manifest.version_id,
                        'source_version': manifest.source_checksum(c['doc_id'])} for c in batch],
        )
        print(f'Indexed {min(start + 32, len(chunks))}/{len(chunks)} passages', flush=True)
    info = {'fingerprint': fingerprint, 'version_id': manifest.version_id,
            'embedding_model': settings.local_embedding_model, 'reranker': settings.rerank_model,
            'passages': len(chunks), 'directory': str(directory.resolve()),
            'database_directory': str(database.resolve()),
            'portable_vectors': str((directory / 'embeddings').resolve())}
    stored = collection.get(include=['documents', 'metadatas'])
    expected = {c['chunk_id']: c for c in chunks}
    if set(stored['ids']) != set(expected):
        raise ValueError('Evaluation index passage membership mismatch')
    for pid, text, meta in zip(stored['ids'], stored['documents'], stored['metadatas'], strict=True):
        if text != expected[pid]['text'] or meta.get('index_version') != manifest.version_id:
            raise ValueError('Evaluation index content mismatch')
        if any(meta.get(k) != v for k, v in expected[pid]['metadata'].items()):
            raise ValueError('Evaluation index source metadata mismatch')
    catalog.publish(manifest)
    write_json_atomic(info_path, info)
    sources.index.build([c['text'] for c in chunks], [c['chunk_id'] for c in chunks],
                        version_id=manifest.version_id)
    retriever = HybridRetrieverNode(catalog=catalog, runtime_bm25=sources.index,
                                   collection_loader=client.get_collection)
    knowledge = KnowledgeSystem(retriever=retriever, evidence_policy=EvidencePolicy())
    return knowledge, info
