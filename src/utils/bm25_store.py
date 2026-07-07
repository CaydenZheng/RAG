"""
BM25 关键词索引封装。

特性：
- 运行时同步：文档增删时同步更新 BM25，保证与 ChromaDB 一致
- 冷启动降级：启动时异步重建，重建期间自动降级为纯向量检索
- 中文分词：用 jieba 做分词后构建 BM25
"""

import threading
from typing import List, Tuple, Optional
from rank_bm25 import BM25Okapi
import jieba
from loguru import logger


class BM25Store:
    """BM25 索引管理器"""

    def __init__(self):
        self._bm25: Optional[BM25Okapi] = None
        self._chunk_ids: List[str] = []       # 与 corpus 一一对应
        self._corpus: List[List[str]] = []    # 分词后的文档
        self._ready = False                    # 索引是否可用
        self._lock = threading.Lock()

    # ----------------------------------------------------------------
    # 构建/重建
    # ----------------------------------------------------------------

    def build(self, texts: List[str], chunk_ids: List[str]):
        """全量构建 BM25 索引（离线索引用）"""
        corpus = [self._tokenize(t) for t in texts]
        with self._lock:
            self._bm25 = BM25Okapi(corpus)
            self._corpus = corpus
            self._chunk_ids = list(chunk_ids)
            self._ready = True
        logger.info("BM25 built: {} docs ready", len(corpus))

    def rebuild_async(self, texts: List[str], chunk_ids: List[str]):
        """异步重建（冷启动用），不阻塞服务启动"""
        self._ready = False  # 重建期间不可用，检索自动降级
        thread = threading.Thread(
            target=self.build, args=(texts, chunk_ids), daemon=True
        )
        thread.start()
        logger.info("BM25 async rebuild started ({} docs)", len(texts))

    # ----------------------------------------------------------------
    # 运行时同步增删
    # ----------------------------------------------------------------

    def add(self, text: str, chunk_id: str) -> bool:
        """新增文档（与 ChromaDB 同步调用）"""
        tokens = self._tokenize(text)
        with self._lock:
            if not self._ready:
                return False
            self._corpus.append(tokens)
            self._chunk_ids.append(chunk_id)
            # 重新构建 BM25（rank-bm25 不支持增量，但小规模重建极快）
            self._bm25 = BM25Okapi(self._corpus)
        return True

    def delete(self, chunk_id: str) -> bool:
        """删除文档（与 ChromaDB 同步调用）"""
        with self._lock:
            if chunk_id not in self._chunk_ids:
                return False
            idx = self._chunk_ids.index(chunk_id)
            del self._corpus[idx]
            del self._chunk_ids[idx]
            if self._corpus:
                self._bm25 = BM25Okapi(self._corpus)
            else:
                self._bm25 = None
                self._ready = False
        return True

    # ----------------------------------------------------------------
    # 检索
    # ----------------------------------------------------------------

    @property
    def is_ready(self) -> bool:
        return self._ready and self._bm25 is not None

    def search(self, query: str, top_k: int = 20) -> List[Tuple[str, float]]:
        """
        检索并返回 [(chunk_id, score), ...]，按分数降序。
        若索引未就绪，返回空列表（调用方自动降级为纯向量检索）。
        """
        with self._lock:
            if not self.is_ready:
                return []
            tokens = self._tokenize(query)
            scores = self._bm25.get_scores(tokens)
            # 按分数降序取 top_k
            indexed = list(enumerate(scores))
            indexed.sort(key=lambda x: x[1], reverse=True)
            results = []
            for idx, score in indexed[:top_k]:
                if score > 0:
                    results.append((self._chunk_ids[idx], float(score)))
            return results

    # ----------------------------------------------------------------
    # 工具
    # ----------------------------------------------------------------

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """中文分词 + 英文小写化 + 空白切分"""
        # jieba 分词（对英文兼容，主要处理中文）
        tokens = list(jieba.cut(text))
        # 过滤空 token，英文转小写
        return [t.strip().lower() for t in tokens if t.strip()]


# 全局单例
bm25_store = BM25Store()
