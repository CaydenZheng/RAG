"""Prepare the same isolated index used by the Agent evaluation runner."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agent.frozen_sources import FrozenSources  # noqa: E402
from src.evaluation.frozen_index import prepare_frozen_knowledge  # noqa: E402
from src.infra.chroma_clients import close_chroma_clients  # noqa: E402


def rows(path):
    with path.open(encoding='utf-8') as stream:
        return [json.loads(line) for line in stream if line.strip()]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--metadata', type=Path, required=True)
    parser.add_argument('--index-dir', type=Path, required=True)
    args = parser.parse_args()
    try:
        _, info = prepare_frozen_knowledge(FrozenSources(rows(args.corpus), rows(args.metadata)), args.index_dir)
        print(json.dumps(info, ensure_ascii=False), flush=True)
    finally:
        close_chroma_clients()
