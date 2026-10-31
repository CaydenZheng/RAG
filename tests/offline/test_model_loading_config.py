"""Formal startup exports the validated setting before model loading."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("dotenv", "environment", "expected"),
    [(None, None, "1"), ("false", None, "0"), ("false", "true", "1")],
)
def test_weight_loading_configuration(
    tmp_path: Path, dotenv: str | None, environment: str | None, expected: str,
) -> None:
    if dotenv is not None:
        (tmp_path / ".env").write_text(f"HF_DEACTIVATE_ASYNC_LOAD={dotenv}\n", encoding="utf8")
    env: dict[str, str] = dict(os.environ)
    env.pop("HF_DEACTIVATE_ASYNC_LOAD", None)
    if environment is not None:
        env["HF_DEACTIVATE_ASYNC_LOAD"] = environment
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    env["OPENAI_API_KEY"] = "synthetic-test-key"
    result: subprocess.CompletedProcess[str] = subprocess.run(
        [sys.executable, "-c", "import json, os; from config.settings import settings; "
         "print(json.dumps(os.environ.get('HF_DEACTIVATE_ASYNC_LOAD')))"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == expected
