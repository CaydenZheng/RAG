#!/usr/bin/env python
"""Download a local Wikipedia corpus for RAG experiments.

Source: wikimedia/wikipedia, configuration 20231101.en, train split.
The dataset revision and download time must be captured in the evaluation
catalog when a corpus is published.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from datasets import load_dataset
from loguru import logger

OUT_DIR = Path("data/raw/wiki")
DATASET_NAME = "wikimedia/wikipedia"
DATASET_CONFIGURATION = "20231101.en"
DATASET_SPLIT = "train"


def main(num_articles: int = 300) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    logger.info(
        "Downloading {}/{} {} (streaming)...",
        DATASET_NAME,
        DATASET_CONFIGURATION,
        DATASET_SPLIT,
    )
    wiki = load_dataset(
        DATASET_NAME,
        DATASET_CONFIGURATION,
        split=DATASET_SPLIT,
        streaming=True,
    )

    count = 0
    for article in wiki:
        text = article["text"].strip()
        if not text or len(text) < 500 or text.startswith("This is a list"):
            continue

        title = re.sub(r'[\\/:*?"<>|]', "_", article["title"])[:80]
        output_path = OUT_DIR / f"{title}.txt"
        if output_path.exists():
            continue

        output_path.write_text(text, encoding="utf-8")
        count += 1
        logger.info("[{}/{}] {}", count, num_articles, title)
        if count >= num_articles:
            break

    logger.info("Downloaded {} articles to {}", count, OUT_DIR)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--num", type=int, default=300)
    arguments = parser.parse_args()
    main(arguments.num)
