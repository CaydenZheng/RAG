#!/usr/bin/env python
"""Validate the published evaluation catalog without network or model access."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.evaluation import load_dataset_catalog  # noqa: E402

DEFAULT_MANIFEST = PROJECT_ROOT / "data/testset/manifest.json"


def main(manifest: Path = DEFAULT_MANIFEST) -> int:
    catalog = load_dataset_catalog(manifest, repository_root=PROJECT_ROOT)
    development = catalog.records_for("development")
    final = catalog.records_for("final")
    print(f"catalog_version={catalog.version}")
    print(f"development_samples={len(development)}")
    print(f"final_samples={len(final)}")
    print("status=valid")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    arguments = parser.parse_args()
    raise SystemExit(main(arguments.manifest))
