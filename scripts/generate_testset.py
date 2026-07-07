#!/usr/bin/env python
"""
LLM 自动生成评测集 — 从知识库文章抽取 chunk，让 DeepSeek 生成问答对。

来源: data/raw/wiki/ 中的 Wikipedia 文章
数量: 50 条（自动生成 + 建议人工快速审阅）
输出: data/testset/generated_test.json

用法:
    python scripts/generate_testset.py
    python scripts/generate_testset.py --num 50
"""

import sys
import json
import random
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
from loguru import logger

from config.settings import settings
from src.llm import llm_client

OUT_FILE = Path("data/testset/generated_test.json")
WIKI_DIR = Path("data/raw/wiki")


def main(num_questions: int = 50):
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    # 读取 Wikipedia 文章
    articles = list(WIKI_DIR.glob("*.txt"))
    if not articles:
        logger.error("No Wikipedia articles found in {}. Run download_wiki.py first.", WIKI_DIR)
        return

    logger.info("Found {} articles, generating {} QA pairs...", len(articles), num_questions)

    # 随机抽样文章，每篇文章生成几条
    random.shuffle(articles)
    testset = []
    articles_used = 0

    for article_path in articles:
        if len(testset) >= num_questions:
            break

        text = article_path.read_text(encoding="utf-8")
        if len(text) < 500:
            continue

        title = article_path.stem
        # 取文章中间段落（避免 intro 模板和尾部模板）
        paragraphs = [p.strip() for p in text.split("\n\n") if len(p.strip()) > 100]
        if len(paragraphs) < 3:
            continue

        # 随机选 2 个段落
        sample = random.sample(paragraphs[1:-1], min(2, len(paragraphs) - 2))
        context = "\n\n".join(sample)[:1500]

        # 让 DeepSeek 基于段落生成 2 条问答
        prompt = f"""You are generating evaluation questions for a RAG system. The knowledge base contains Wikipedia articles.

Here is a passage from the article "{title}":

---
{context}
---

Generate 2 question-answer pairs that test whether a RAG system can retrieve and use this passage correctly.

Rules:
- Questions should be answerable ONLY from the given passage
- Answers should be concise but complete (1-2 sentences)
- Include at least one question that requires combining information from different parts of the passage

Return ONLY valid YAML:
```yaml
qa_pairs:
  - question: "first question here"
    answer: "first answer here"
  - question: "second question here"
    answer: "second answer here"
```"""

        try:
            resp = llm_client.chat([{"role": "user", "content": prompt}], skip_cache=True)
            if "```yaml" in resp:
                yaml_str = resp.split("```yaml")[1].split("```")[0].strip()
            else:
                yaml_str = resp.strip()
            parsed = yaml.safe_load(yaml_str)

            for pair in parsed.get("qa_pairs", []):
                q = pair.get("question", "").strip()
                a = pair.get("answer", "").strip()
                if q and a:
                    testset.append({"question": q, "ground_truth": a})
                    logger.info("[{}/{}] {}", len(testset), num_questions, q[:80])

        except Exception as e:
            logger.warning("Generation failed for {}: {}", title, e)
            continue

        articles_used += 1

    random.shuffle(testset)
    testset = testset[:num_questions]

    OUT_FILE.write_text(
        json.dumps(testset, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("✅ Generated {} QA pairs from {} articles → {}", len(testset), articles_used, OUT_FILE)
    logger.info("💡 Tip: skim the file for quality, edit any bad questions in 10 minutes")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--num", type=int, default=50)
    args = parser.parse_args()
    main(args.num)
