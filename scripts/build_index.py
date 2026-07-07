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
from flow import get_offline_flow


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
    logger.info("  Fingerprint: {}", info.get("fingerprint", "N/A"))
    logger.info("  ChromaDB:   {}", info.get("chroma_persist_dir", "N/A"))


if __name__ == "__main__":
    from config.settings import settings
    main()
