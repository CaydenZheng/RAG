#!/usr/bin/env python
"""
下载 Wikipedia 文章作为 RAG 知识库。

来源: Wikipedia 20220301 英文快照
数量: 300 篇（覆盖科学/历史/技术/生物/地理等多领域）
输出: data/raw/wiki/*.txt

用法:
    python scripts/download_wiki.py
    python scripts/download_wiki.py --num 500
"""

import sys
import re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from datasets import load_dataset
from loguru import logger

OUT_DIR = Path("data/raw/wiki")


def main(num_articles: int = 300):
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("Downloading Wikipedia 20231101.en (streaming)...")
    wiki = load_dataset("wikimedia/wikipedia", "20231101.en", split="train", streaming=True)

    count = 0
    for article in wiki:
        text = article["text"].strip()
        # 跳过太短或明显是列表页的文章
        if not text or len(text) < 500:
            continue
        if text.startswith("This is a list"):
            continue

        title = re.sub(r'[\\/:*?"<>|]', "_", article["title"])[:80]
        path = OUT_DIR / f"{title}.txt"

        if path.exists():
            continue

        path.write_text(text, encoding="utf-8")
        count += 1
        logger.info("[{}/{}] {}", count, num_articles, title)

        if count >= num_articles:
            break

    logger.info("✅ Downloaded {} articles to {}", count, OUT_DIR)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--num", type=int, default=300)
    args = parser.parse_args()
    main(args.num)
