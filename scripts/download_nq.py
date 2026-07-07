#!/usr/bin/env python
"""
下载 Natural Questions 评测集。

来源: Google Natural Questions（真实用户搜索 + Wikipedia 段落答案）
数量: 50 条（含 long_answer 的问题）
输出: data/testset/nq_test.json

格式:
[
  {"question": "...", "ground_truth": "..."},
  ...
]

用法:
    python scripts/download_nq.py
    python scripts/download_nq.py --num 100
"""

import sys
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from datasets import load_dataset
from loguru import logger

OUT_FILE = Path("data/testset/nq_test.json")


def main(num_questions: int = 50):
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    logger.info("Downloading Natural Questions (validation split, streaming)...")
    nq = load_dataset("google/natural_questions", split="validation", streaming=True)

    testset = []
    for item in nq:
        # 只取有 long_answer 的问题（段落级答案，适合 RAG 评测）
        long_answer = item["annotations"]["long_answer"]
        if not long_answer or not long_answer[0]["start_token"]:
            continue

        question = item["question"]["text"].strip()
        if not question:
            continue

        # 获取 short_answer（精确答案文本）作为 ground truth
        short_answers = item["annotations"]["short_answers"]
        ground_truth = ""
        if short_answers and short_answers[0]["text"]:
            ground_truth = short_answers[0]["text"][0]
        else:
            # fallback: 用 long_answer 片段
            ground_truth = question

        testset.append({
            "question": question,
            "ground_truth": ground_truth,
        })

        if len(testset) >= num_questions:
            break

    OUT_FILE.write_text(
        json.dumps(testset, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("✅ Saved {} questions to {}", len(testset), OUT_FILE)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--num", type=int, default=50)
    args = parser.parse_args()
    main(args.num)
