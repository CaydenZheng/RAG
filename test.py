"""
端到端指标验证脚本 — 验证 RAGAS（LLM judge）和检索层指标（rule-based）均正常工作。

用 3 条模拟数据模拟 2 个消融组的上下文质量差异：
  - "Vector Only"     → 检索质量一般（部分题目上下文不匹配）
  - "Hybrid + Rerank" → 检索质量更好（上下文更精准）

运行后会输出两组对比，验证：
  1. retrieval_metrics: Hit Rate@K / MRR 能区分上下文质量
  2. RAGAS: Context Precision / Faithfulness 能反映生成质量

用法:
    python test.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from scripts.run_eval import compute_retrieval_metrics, run_ragas_eval
from loguru import logger

# ================================================================
# 构造 3 条有区分度的测试数据
# ================================================================

all_results = {
    "Vector Only": [
        {
            "question": "What is gradient descent and why is learning rate important?",
            "answer": "Gradient descent is an optimization algorithm that adjusts model parameters to minimize the loss function. The learning rate controls the step size and is important because too large causes divergence, too small causes slow convergence.",
            "contexts": [
                "Gradient descent is an optimization algorithm used in machine learning to minimize loss functions by iteratively adjusting parameters.",
                "The weather in London is often rainy and overcast during winter months.",
                "Neural networks consist of layers of interconnected neurons that process input data.",
                "Learning rate determines the step size in gradient descent. A high learning rate may cause overshooting.",
                "Python is a high-level programming language popular for data science.",
            ],
            "ground_truth": "Gradient descent minimizes the loss function by updating parameters in the direction of steepest descent. The learning rate is a crucial hyperparameter: if too large the algorithm diverges, if too small convergence is prohibitively slow."
        },
        {
            "question": "How does the Transformer's self-attention mechanism work?",
            "answer": "Self-attention computes attention scores between all pairs of tokens using Query, Key, and Value projections, allowing each token to attend to every other token.",
            "contexts": [
                "Transformers are used primarily in computer vision for image classification tasks.",
                "Self-attention computes Query, Key, and Value matrices from input embeddings and calculates attention weights via scaled dot-product.",
                "The Eiffel Tower was completed in 1889 and stands 330 meters tall.",
                "CNNs use convolutional filters to extract spatial features from images.",
                "Multi-head attention runs multiple self-attention operations in parallel to capture different relationship types.",
            ],
            "ground_truth": "The self-attention mechanism projects each token into Query (Q), Key (K), and Value (V) vectors. Attention scores are computed as softmax(QK^T / sqrt(d_k)), and these weights are used to create a weighted sum of Value vectors. Multi-head attention runs several such operations in parallel."
        },
        {
            "question": "What are the key differences between SQL and NoSQL databases?",
            "answer": "SQL databases use structured schemas with tables and ACID transactions, while NoSQL databases support flexible schemas and horizontal scaling with eventual consistency.",
            "contexts": [
                "Beethoven's Symphony No. 5 is one of the most famous classical music compositions.",
                "The Amazon rainforest produces approximately 20% of the world's oxygen supply.",
                "SQL databases enforce rigid schemas with predefined tables and support ACID transactions for data integrity.",
                "NoSQL databases offer flexible document or key-value storage models designed for horizontal scaling.",
                "Relational databases excel at complex joins while NoSQL trades consistency for availability per the CAP theorem.",
            ],
            "ground_truth": "SQL databases are relational, schema-based, and prioritize ACID compliance (Atomicity, Consistency, Isolation, Durability). NoSQL databases are non-relational, schema-flexible, and typically sacrifice strong consistency for availability and partition tolerance under the CAP theorem, making them better suited for distributed, high-volume applications."
        },
    ],

    "Hybrid + Rerank": [
        {
            "question": "What is gradient descent and why is learning rate important?",
            "answer": "Gradient descent minimizes the loss by updating parameters along the negative gradient direction. The learning rate is critical because it scales each update: too large overshoots the minimum, too small stalls training.",
            "contexts": [
                "Gradient descent is an optimization algorithm used in machine learning to minimize loss functions by iteratively adjusting parameters in the direction of steepest descent.",
                "The learning rate hyperparameter controls the magnitude of each parameter update in gradient descent. Values that are too high cause divergence, while values that are too low result in slow convergence and potential trapping in local minima.",
                "Stochastic gradient descent (SGD) uses random subsets of data to compute gradients, trading variance for computational efficiency.",
                "Modern optimizers like Adam combine momentum and adaptive learning rates to improve upon vanilla gradient descent.",
                "Backpropagation computes gradients of the loss with respect to each parameter using the chain rule.",
            ],
            "ground_truth": "Gradient descent minimizes the loss function by updating parameters in the direction of steepest descent. The learning rate is a crucial hyperparameter: if too large the algorithm diverges, if too small convergence is prohibitively slow."
        },
        {
            "question": "How does the Transformer's self-attention mechanism work?",
            "answer": "Self-attention projects tokens into Q/K/V matrices, computes attention scores via scaled dot-product, and produces weighted value sums. Multi-head attention runs this in parallel across multiple subspaces.",
            "contexts": [
                "Self-attention is the core mechanism of the Transformer architecture, computing pairwise token relationships by projecting each input token into Query (Q), Key (K), and Value (V) vectors.",
                "Attention scores are calculated as softmax(QK^T / sqrt(d_k)) where d_k is the key dimension, with the scaling factor preventing gradient vanishing in high dimensions.",
                "Multi-head attention runs h parallel self-attention 'heads', each with its own learned Q/K/V projections, and concatenates their outputs to capture diverse token relationships.",
                "Positional encodings are added to input embeddings before self-attention since the mechanism itself is permutation-invariant and lacks sequence order awareness.",
                "The Transformer encoder stacks multiple self-attention and feed-forward layers, enabling hierarchical feature extraction from text sequences.",
            ],
            "ground_truth": "The self-attention mechanism projects each token into Query (Q), Key (K), and Value (V) vectors. Attention scores are computed as softmax(QK^T / sqrt(d_k)), and these weights are used to create a weighted sum of Value vectors. Multi-head attention runs several such operations in parallel."
        },
        {
            "question": "What are the key differences between SQL and NoSQL databases?",
            "answer": "SQL databases are relational with rigid schemas and ACID guarantees, while NoSQL databases are non-relational, schema-flexible, and horizontally scalable with eventual consistency models.",
            "contexts": [
                "SQL (Structured Query Language) databases use a relational model with predefined schemas, tables, rows, and columns, and support complex JOIN operations across tables.",
                "ACID transactions (Atomicity, Consistency, Isolation, Durability) are a cornerstone of SQL databases, ensuring reliable data integrity even during system failures.",
                "NoSQL databases encompass document stores (MongoDB), key-value stores (Redis), column-family stores (Cassandra), and graph databases (Neo4j), each optimized for specific data access patterns.",
                "The CAP theorem states that a distributed database can only simultaneously guarantee two of Consistency, Availability, and Partition Tolerance; NoSQL systems often favor AP over CP.",
                "Horizontal scaling in NoSQL is achieved through sharding and replication across commodity hardware, while SQL databases traditionally scale vertically by adding resources to a single server.",
            ],
            "ground_truth": "SQL databases are relational, schema-based, and prioritize ACID compliance (Atomicity, Consistency, Isolation, Durability). NoSQL databases are non-relational, schema-flexible, and typically sacrifice strong consistency for availability and partition tolerance under the CAP theorem, making them better suited for distributed, high-volume applications."
        },
    ],
}


# ================================================================
# 1. 检索层指标（rule-based，秒出，不调 LLM）
# ================================================================

print("=" * 60)
print("RETRIEVAL METRICS (rule-based)")
print("=" * 60)
retrieval_metrics = compute_retrieval_metrics(all_results)
for group_name, metrics in retrieval_metrics.items():
    print(f"\n{group_name}:")
    for k, v in metrics.items():
        print(f"  {k}: {v:.4f}")

# 预期: Hybrid+Rerank 组的 Hit Rate 和 MRR 应高于 Vector Only 组
# 因为 Vector Only 的 context 混入了无关噪音（天气、艾菲尔铁塔、贝多芬等）


# ================================================================
# 2. RAGAS 评估（LLM judge，需调用 DeepSeek API，约 30s）
# ================================================================

print("\n" + "=" * 60)
print("RAGAS METRICS (LLM judge)")
print("=" * 60)

ragas_metrics = run_ragas_eval(all_results)
for group_name, metrics in ragas_metrics.items():
    if metrics:
        print(f"\n{group_name}:")
        for k, v in metrics.items():
            print(f"  {k}: {v:.4f}")

# 预期: Hybrid+Rerank 的 Context Precision 和 Answer Relevancy 应更高


# ================================================================
# 3. 对比总结
# ================================================================

print("\n" + "=" * 60)
print("EXPECTED: Hybrid+Rerank > Vector Only")
print("  - Hit Rate@5:   more relevant chunks in top positions")
print("  - MRR:          first relevant chunk ranks higher")
print("  - Context Precision: LLM judges context as more on-topic")
print("  - Answer Relevancy:  answers better match the questions")
print("=" * 60)
