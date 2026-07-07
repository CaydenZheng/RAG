"""
P1-1~P1-3: 文档摄入管线

DocLoader         → 递归读取 data/raw/ 下的 .md/.txt 文件
DocDeduplicator   → MD5 去重，幂等上传
Chunker           → 语义分块（Markdown 按标题 / 通用递归字符切分）

所有节点以 PocketFlow Node/BatchNode 实现，通过 shared store 串联。
"""

import hashlib
import re
from pathlib import Path
from typing import List, Dict, Any

from pocketflow import Node, BatchNode
from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)
from loguru import logger

from config.settings import settings

# ================================================================
# 数据结构
# ================================================================

class RawDoc:
    """原始文档"""
    def __init__(self, doc_id: str, path: str, text: str, metadata: dict):
        self.doc_id = doc_id
        self.path = path
        self.text = text
        self.metadata = metadata

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "path": self.path,
            "text": self.text,
            "metadata": self.metadata,
        }


class Chunk:
    """分块后的文档片段"""
    def __init__(self, chunk_id: str, doc_id: str, text: str,
                 chunk_index: int, metadata: dict):
        self.chunk_id = chunk_id
        self.doc_id = doc_id
        self.text = text
        self.chunk_index = chunk_index
        self.metadata = metadata

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "text": self.text,
            "chunk_index": self.chunk_index,
            "metadata": self.metadata,
        }


# ================================================================
# P1-1: DocLoaderNode
# ================================================================

class DocLoaderNode(BatchNode):
    """递归遍历文件目录，读取所有 .md/.txt，输出 RawDoc 列表"""

    def prep(self, shared: dict) -> List[Path]:
        """返回待处理的文件路径列表"""
        raw_dir = settings.raw_dir
        if not raw_dir.exists():
            logger.warning("Raw data directory not found: {}", raw_dir)
            return []

        files = []
        for ext in ["*.md", "*.txt"]:
            files.extend(raw_dir.rglob(ext))
        logger.info("Found {} files in {}", len(files), raw_dir)
        return files

    def exec(self, filepath: Path) -> RawDoc | None:
        """读取单个文件"""
        try:
            text = filepath.read_text(encoding="utf-8")
            if not text.strip():
                return None

            # 用相对路径 + 文件名生成 doc_id
            rel_path = str(filepath.relative_to(settings.raw_dir))
            doc_id = hashlib.md5(rel_path.encode()).hexdigest()[:12]

            # 提取目录类别（core_abstraction / design_pattern / utility_function）
            category = filepath.parent.name if filepath.parent != settings.raw_dir else "root"

            return RawDoc(
                doc_id=doc_id,
                path=rel_path,
                text=text,
                metadata={
                    "source": rel_path,
                    "category": category,
                    "extension": filepath.suffix,
                }
            )
        except Exception as e:
            logger.error("Failed to load {}: {}", filepath, e)
            return None

    def post(self, shared: dict, prep_res, exec_res_list: List[RawDoc | None]) -> str:
        """将解析结果存入 shared store"""
        docs = [d for d in exec_res_list if d is not None]
        shared["raw_docs"] = [d.to_dict() for d in docs]
        logger.info("✅ Loaded {} documents", len(docs))
        return "default"


# ================================================================
# P1-2: DocDeduplicatorNode
# ================================================================

class DocDeduplicatorNode(Node):
    """内容 MD5 去重：相同内容的文档只保留一份"""

    def prep(self, shared: dict) -> List[dict]:
        return shared.get("raw_docs", [])

    def exec(self, docs: List[dict]) -> List[dict]:
        seen_hashes = set()
        unique = []
        duplicates = 0

        for doc in docs:
            text_hash = hashlib.md5(doc["text"].encode()).hexdigest()
            if text_hash in seen_hashes:
                duplicates += 1
                continue
            seen_hashes.add(text_hash)
            unique.append(doc)

        if duplicates:
            logger.info("Removed {} duplicate documents", duplicates)
        return unique

    def post(self, shared: dict, prep_res, exec_res: List[dict]) -> str:
        shared["docs"] = exec_res
        logger.info("✅ {} unique documents after dedup", len(exec_res))
        return "default"


# ================================================================
# P1-3: ChunkerNode
# ================================================================

class ChunkerNode(BatchNode):
    """
    语义分块：
    - Markdown 文件 → 按 ## 和 ### 标题切分，再用递归字符切分子段
    - 普通文本    → 递归字符切分
    目标 ~512 tokens/chunk，overlap ~50 tokens
    """

    # 可调参数
    CHUNK_SIZE = 512       # 目标 token 数（近似字符数 ≈ token * 4）
    CHUNK_OVERLAP = 50

    def prep(self, shared: dict) -> List[dict]:
        return shared.get("docs", [])

    def exec(self, doc: dict) -> List[dict]:
        """对单个文档分块"""
        text = doc["text"]
        ext = doc["metadata"].get("extension", "")

        if ext == ".md":
            chunks = self._chunk_markdown(text, doc)
        else:
            chunks = self._chunk_generic(text, doc)
        return chunks

    def _chunk_markdown(self, text: str, doc: dict) -> List[dict]:
        """Markdown 按标题层级切分"""
        # 步骤1：按 ## 和 ### 标题切分
        md_splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=[
                ("##", "h2"),
                ("###", "h3"),
            ],
            strip_headers=False,
        )
        try:
            sections = md_splitter.split_text(text)
        except Exception:
            # Markdown 解析失败，退化为通用切分
            return self._chunk_generic(text, doc)

        # 步骤2：对每个 section（如果太长）用递归字符切分器再切
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.CHUNK_SIZE,
            chunk_overlap=self.CHUNK_OVERLAP,
            separators=["\n\n", "\n", ". ", " ", ""],
        )

        all_chunks = []
        for section in sections:
            section_text = section.page_content
            # 合并标题和内容
            headers = section.metadata
            header_prefix = ""
            if headers.get("h2"):
                header_prefix += f"## {headers['h2']}\n"
            if headers.get("h3"):
                header_prefix += f"### {headers['h3']}\n"

            full_text = header_prefix + section_text

            if len(full_text) <= self.CHUNK_SIZE:
                all_chunks.append(full_text.strip())
            else:
                sub_chunks = text_splitter.split_text(full_text)
                all_chunks.extend(sub_chunks)

        return self._build_chunk_dicts(all_chunks, doc)

    def _chunk_generic(self, text: str, doc: dict) -> List[dict]:
        """通用递归字符切分"""
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.CHUNK_SIZE,
            chunk_overlap=self.CHUNK_OVERLAP,
            separators=["\n\n", "\n", ". ", " ", ""],
        )
        chunks = text_splitter.split_text(text)
        return self._build_chunk_dicts(chunks, doc)

    def _build_chunk_dicts(self, chunks: List[str], doc: dict) -> List[dict]:
        """统一构建 chunk dict 列表"""
        result = []
        for i, chunk_text in enumerate(chunks):
            chunk_text = chunk_text.strip()
            if not chunk_text:
                continue
            chunk_id = f"{doc['doc_id']}_chunk{i}"
            result.append({
                "chunk_id": chunk_id,
                "doc_id": doc["doc_id"],
                "text": chunk_text,
                "chunk_index": i,
                "metadata": {
                    **doc["metadata"],
                    "chunk_index": i,
                },
            })
        return result

    def post(self, shared: dict, prep_res, exec_res_list: List[List[dict]]) -> str:
        """展平并存储所有 chunk"""
        all_chunks = []
        for chunk_list in exec_res_list:
            all_chunks.extend(chunk_list)

        shared["chunks"] = all_chunks
        logger.info("✅ Created {} chunks from {} documents",
                     len(all_chunks), len(exec_res_list))
        return "default"
