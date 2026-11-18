"""Shared after-sales assembly for application and evaluation entry points."""

import asyncio
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter

from config.settings import settings
from src.agent.frozen_sources import FrozenSources
from src.agent.harness import AgentConfig, AgentHarness
from src.agent.memory import MemoryConfig, MemoryManager
from src.agent.official_help import OfficialHelp
from src.agent.support_instructions import (
    CONCISE_SUPPORT_INSTRUCTIONS,
    PUBLIC_INSTRUCTIONS,
    SUPPORT_INSTRUCTIONS,
)
from src.core.knowledge import validate_retrieval_mode
from src.core.run_options import SkillSnapshot
from src.infra.errors import LocalModelUnavailable


def read_support_configuration(path: Path) -> dict:
    """Resolve one server-owned profile before serving or evaluating requests."""
    path = path.resolve()
    document = json.loads(path.read_text(encoding='utf-8-sig'))
    for name in ('corpus', 'metadata', 'runtime_dir', 'official_replay'):
        if document.get(name):
            document[name] = path.parent / document[name]
    numeric_settings = {'rerank_timeout_seconds', 'vector_top_k', 'bm25_top_k', 'rrf_k',
                        'rerank_batch_size', 'query_rewrite_enabled', 'local_model_retry_seconds',
                        'rerank_min_score', 'rerank_filter_calibration_id'}
    overrides = document.get('retrieval_settings', {})
    if set(overrides) - numeric_settings:
        raise ValueError('Unsupported support retrieval setting')
    validated = {}
    for name, value in overrides.items():
        definition = type(settings).model_fields[name]
        validated[name] = TypeAdapter(definition.rebuild_annotation()).validate_python(value)
    if validated.get('rerank_min_score') is not None and not validated.get('rerank_filter_calibration_id', '').strip():
        raise ValueError('Per-passage filtering requires a calibration ID in the support profile')
    for name, value in validated.items():
        setattr(settings, name, value)
    return document


def effective_retrieval_settings() -> dict:
    return {
        'embedding_model': settings.local_embedding_model, 'rerank_model': settings.rerank_model,
        'rerank_timeout_seconds': settings.rerank_timeout_seconds,
        'vector_top_k': settings.vector_top_k, 'bm25_top_k': settings.bm25_top_k,
        'rrf_k': settings.rrf_k, 'evidence_threshold': 'uncalibrated_for_frozen_corpus',
        'rerank_batch_size': settings.rerank_batch_size,
        'query_rewrite_enabled': settings.query_rewrite_enabled,
        'rerank_min_score': settings.rerank_min_score,
        'rerank_filter_calibration_id': settings.rerank_filter_calibration_id,
    }


@dataclass(frozen=True)
class SupportProfile:
    planner: Literal['json', 'native'] = 'json'
    timeout: float = 180
    response_style: Literal['original', 'concise'] = 'concise'
    suite: Literal['support', 'public'] = 'support'

    def __post_init__(self):
        if self.planner not in {'json', 'native'} or self.response_style not in {'original', 'concise'}:
            raise ValueError('Invalid support planner or response style')
        if self.suite not in {'support', 'public'}:
            raise ValueError('Invalid corpus suite')

    @property
    def instructions(self) -> str:
        if self.suite == 'public':
            return PUBLIC_INSTRUCTIONS
        return CONCISE_SUPPORT_INSTRUCTIONS if self.response_style == 'concise' else SUPPORT_INSTRUCTIONS

    def config(self) -> AgentConfig:
        return AgentConfig(
            planner_mode=self.planner, timeout_seconds=self.timeout,
            max_iterations=6, max_tool_calls=5, max_token_budget=128000,
            max_tool_result_length=14000, final_max_tokens=1600, verbose=False,
            observe_decisions=True, capture_model_context=True,
            require_understanding=self.suite == 'support' and self.planner == 'json',
        )

    def memory_config(self) -> MemoryConfig:
        return MemoryConfig(compress_trigger_turns=10000, max_history_turns=10000,
                            tool_result_max_tokens=20000)

    def runtime(self, sources: FrozenSources, memory: MemoryManager, official=None) -> AgentHarness:
        return AgentHarness(
            config=self.config(), memory=memory, tools=sources.registry(official),
            skills=(SkillSnapshot('support-profile', 'Support assistant', '4', self.instructions),),
        )


async def prepare_sources(sources: FrozenSources, retrieval_mode: str, index_dir: Path | None):
    validate_retrieval_mode(retrieval_mode)
    sources.retrieval_mode = retrieval_mode
    if retrieval_mode == 'bm25_only':
        return None
    if index_dir is None:
        raise ValueError('Vector/hybrid retrieval requires an index directory')
    from src.evaluation.frozen_index import prepare_frozen_knowledge

    try:
        sources.knowledge, info = await asyncio.to_thread(prepare_frozen_knowledge, sources, index_dir)
    except LocalModelUnavailable:
        sources.retrieval_mode = 'bm25_only'
        sources.warnings = ['dense_unavailable', 'index_preparation_degraded_to_bm25']
        return {'status': 'degraded', 'retrieval_mode': 'bm25_only', 'warnings': sources.warnings}
    if retrieval_mode == 'hybrid+rerank':
        from src.core.retrieval import _get_reranker

        try:
            await asyncio.to_thread(_get_reranker)
        except LocalModelUnavailable:
            info['warmup_warning'] = 'rerank_unavailable'
    return info


async def load_support_runtime(path: Path) -> AgentHarness:
    """Load an explicit server-owned profile; model requests cannot change it."""
    from src.infra.session_store import session_store

    document = read_support_configuration(path)
    profile = SupportProfile(**document.get('profile', {}))
    corpus_path = document['corpus']
    metadata_path = document['metadata']
    corpus = [json.loads(line) for line in corpus_path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    metadata = [json.loads(line) for line in metadata_path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    sources = FrozenSources(corpus, metadata)
    runtime_dir = document['runtime_dir']
    runtime_dir.mkdir(parents=True, exist_ok=True)
    index = await prepare_sources(sources, document.get('retrieval_mode', 'hybrid+rerank'), runtime_dir / 'indexes')
    official_mode = document.get('official', 'live')
    if official_mode not in {'off', 'live', 'replay'}:
        raise ValueError('Invalid official source mode')
    replay = document.get('official_replay')
    if official_mode == 'replay' and not replay:
        raise ValueError('Official replay requires a response directory')
    official = None if official_mode == 'off' else OfficialHelp(
        record_dir=runtime_dir / 'official_http', proxy=document.get('official_proxy'),
        replay_dir=replay if official_mode == 'replay' else None,
    )
    memory = MemoryManager(str(runtime_dir / 'memory'), config=profile.memory_config(), store=session_store)
    runtime = profile.runtime(sources, memory, official)
    runtime.observation_file = runtime_dir / 'trajectory.jsonl'
    (runtime_dir / 'assembly.json').write_text(json.dumps({
        'profile_version': 'support-v2-observable', 'config': asdict(runtime.config),
        'memory': asdict(profile.memory_config()), 'index': index,
        'retrieval_mode': sources.retrieval_mode, 'official_mode': official_mode,
        'retrieval_settings': effective_retrieval_settings(),
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    return runtime
