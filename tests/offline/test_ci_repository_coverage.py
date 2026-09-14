from pathlib import Path

WORKFLOW_PATH = Path(__file__).parents[2] / ".github" / "workflows" / "offline-checks.yml"
RUFF_COMMAND = "uv run --no-sync --offline --no-env-file ruff check ."


def test_linux_and_windows_lint_the_repository() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert workflow.count(f"run: {RUFF_COMMAND}") == 2
    assert "ruff check app.py" not in workflow
