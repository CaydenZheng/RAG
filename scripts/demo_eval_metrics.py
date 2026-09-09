"""Synthetic evaluation demonstration; does not run on import.

Usage:
    uv run --locked --group eval python scripts/demo_eval_metrics.py
    uv run --locked --group eval python scripts/demo_eval_metrics.py --with-ragas

The optional Ragas step calls a real LLM. Samples are handcrafted, not retrieval results.
"""

import argparse
import sys
from pathlib import Path

ALL_RESULTS = {
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Demonstrate evaluation metrics using synthetic answers and contexts."
    )
    parser.add_argument(
        "--with-ragas",
        action="store_true",
        help="Also run Ragas with the configured real LLM (requires API access and models).",
    )
    args = parser.parse_args(argv)

    project_root = str(Path(__file__).resolve().parents[1])
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    from scripts.run_eval import compute_retrieval_metrics

    print("SYNTHETIC METRICS DEMO — not a real retrieval comparison")
    print("RETRIEVAL METRICS (rule-based)")
    for group_name, metrics in compute_retrieval_metrics(ALL_RESULTS).items():
        print(f"\n{group_name}:")
        for name, value in metrics.items():
            print(f"  {name}: {value:.4f}")

    if args.with_ragas:
        from scripts.run_eval import run_ragas_eval

        print("\nRAGAS METRICS (real LLM judge)")
        for group_name, metrics in run_ragas_eval(ALL_RESULTS).items():
            if metrics:
                print(f"\n{group_name}:")
                for name, value in metrics.items():
                    print(f"  {name}: {value:.4f}")
    else:
        print("\nRagas skipped; use --with-ragas to call the configured real LLM.")

    print("\nAnswers and contexts are handcrafted; scores do not prove pipeline quality.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
