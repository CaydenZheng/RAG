#!/usr/bin/env python
"""
离线索引构建脚本。

用法:
    python scripts/build_index.py
    python scripts/build_index.py --data-dir ./data/raw
"""

import sys
from pathlib import Path

# 确保项目根目录在 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger

from src.orchestration.rag import get_offline_flow


def main():
    logger.info("=" * 50)
    logger.info("Building RAG index...")
    logger.info("=" * 50)

    flow = get_offline_flow()

    shared = {}
    flow.run(shared)

    # 输出统计
    info = shared.get("index_info", {})
    logger.info("=" * 50)
    logger.info("Build complete!")
    logger.info("  Documents:  {} (after dedup)", len(shared.get("docs", [])))
    logger.info("  Chunks:     {}", info.get("chunks_count", 0))
    logger.info("  Version:    {}", info.get("version_id", "N/A"))
    logger.info("  Checksum:   {}", info.get("content_checksum", "N/A"))
    logger.info("  Collection: {}", info.get("collection_name", "N/A"))
    logger.info("  Published:  {}", info.get("published", False))


if __name__ == "__main__":
    main()
