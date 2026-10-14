from pathlib import Path

WORKFLOW_PATH = Path(__file__).parents[2] / ".github" / "workflows" / "offline-checks.yml"
PRODUCTION_PLAYWRIGHT_CONFIG_PATH = (
    Path(__file__).parents[2] / "frontend" / "playwright.production.config.ts"
)
RUFF_COMMAND = "uv run --no-sync --offline --no-env-file ruff check ."


def test_linux_and_windows_lint_the_repository() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert workflow.count(f"run: {RUFF_COMMAND}") == 2
    assert "ruff check app.py" not in workflow


def test_frontend_browser_tests_run_in_an_independent_job() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "frontend-e2e:" in workflow
    assert "run: npx playwright install --with-deps chromium" in workflow
    assert "run: npm run test:e2e" in workflow
    assert "run: npm run test:e2e:production" in workflow


def test_production_browser_server_has_isolated_required_settings() -> None:
    config = PRODUCTION_PLAYWRIGHT_CONFIG_PATH.read_text(encoding="utf-8")

    assert 'OPENAI_API_KEY: "playwright-production-fixture-key"' in config
