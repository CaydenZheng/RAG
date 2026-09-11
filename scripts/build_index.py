#!/usr/bin/env python
"""
离线索引构建脚本。

用法:
    uv run --no-sync python scripts/build_index.py

语料目录固定为 Settings.raw_dir（默认 data/raw/）；当前命令不接受参数。
"""

import argparse
import sys
from pathlib import Path

# 确保项目根目录在 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger


def main(argv: list[str] | None = None) -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Build the versioned RAG index."
    )
    parser.parse_args(argv)

    from src.orchestration.rag import get_offline_flow

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
