"""The metrics demonstration must require explicit execution and Ragas opt-in."""

import runpy
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

DEMO_PATH = Path(__file__).resolve().parents[2] / "scripts/demo_eval_metrics.py"


def test_demo_import_has_no_evaluation_side_effects(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(sys.modules, "scripts.run_eval", None)
    original_path = list(sys.path)
    namespace = runpy.run_path(str(DEMO_PATH))
    assert callable(namespace["main"])
    assert sys.path == original_path
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("with_ragas", [False, True])
def test_demo_only_runs_ragas_when_requested(
    with_ragas: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    evaluator = ModuleType("scripts.run_eval")

    def retrieval(results: dict[str, Any]) -> dict[str, dict[str, float]]:
        calls.append("retrieval")
        assert len(results) == 2
        assert all(len(samples) == 3 for samples in results.values())
        return {"synthetic": {"mrr": 0.5}}

    def ragas(results: dict[str, Any]) -> dict[str, dict[str, float]]:
        calls.append("ragas")
        return {"synthetic": {"faithfulness": 0.5}}

    monkeypatch.setattr(evaluator, "compute_retrieval_metrics", retrieval, raising=False)
    monkeypatch.setattr(evaluator, "run_ragas_eval", ragas, raising=False)
    monkeypatch.setitem(sys.modules, "scripts.run_eval", evaluator)
    monkeypatch.syspath_prepend(str(DEMO_PATH.parents[1]))
    namespace = runpy.run_path(str(DEMO_PATH))
    assert namespace["main"](["--with-ragas"] if with_ragas else []) == 0
    assert calls == (["retrieval", "ragas"] if with_ragas else ["retrieval"])
