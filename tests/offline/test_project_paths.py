"""Regression tests for project-root paths and deterministic ingestion."""

from pathlib import Path

import pytest


def test_repository_defaults_ignore_the_current_working_directory(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import config.settings as settings_module
    from config.settings import Settings

    project_root = isolated_runtime / "project"
    unrelated_cwd = isolated_runtime / "elsewhere"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)
    monkeypatch.setattr(settings_module, "PROJECT_ROOT", project_root)
    monkeypatch.delenv("CHROMA_PERSIST_DIR")
    monkeypatch.delenv("CACHE_DB_PATH")

    configured = Settings(_env_file=None)

    assert configured.data_dir == project_root / "data"
    assert configured.raw_dir == project_root / "data/raw"
    assert configured.prompt_dir == project_root / "prompts/v1"
    assert configured.log_dir == project_root / "logs"
    assert configured.memory_dir == project_root / "memory"
    assert configured.chroma_path == project_root / "data/chroma"
    assert configured.cache_db_path_resolved == project_root / "data/cache.db"


def test_absolute_storage_paths_are_not_rebased(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import config.settings as settings_module
    from config.settings import Settings

    project_root = isolated_runtime / "project"
    absolute_chroma = isolated_runtime / "external/chroma"
    absolute_cache = isolated_runtime / "external/cache.db"
    monkeypatch.setattr(settings_module, "PROJECT_ROOT", project_root)

    configured = Settings(
        _env_file=None,
        chroma_persist_dir=str(absolute_chroma),
        cache_db_path=str(absolute_cache),
    )

    assert configured.chroma_path == absolute_chroma
    assert configured.cache_db_path_resolved == absolute_cache


def test_prompt_manager_uses_the_project_prompt_root(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import config.settings as settings_module
    from src.infra.prompt_manager import PromptManager

    project_root = isolated_runtime / "project"
    monkeypatch.setattr(settings_module, "PROJECT_ROOT", project_root)

    manager = PromptManager()

    assert manager.prompt_dir == project_root / "prompts/v1"


def test_runtime_default_directories_use_the_project_root(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import config.settings as settings_module
    from src.agent import hooks
    from src.agent.memory import MemoryManager
    from src.infra.tracer import TraceLogger

    project_root = isolated_runtime / "project"
    monkeypatch.setattr(settings_module, "PROJECT_ROOT", project_root)
    context = hooks.HookContext(
        event=hooks.HookEvent.PRE_TOOL_USE,
        session_id="session",
        data={"tool_name": "search", "tool_params": {}},
    )

    memory = MemoryManager(store=object())
    hooks.create_logging_hook()(context)
    hooks.create_audit_hook()(context)

    assert memory.memory_dir == project_root / "memory"
    assert TraceLogger().trace_file == project_root / "logs/traces.jsonl"
    assert (project_root / "logs/agent_events.jsonl").is_file()
    assert (project_root / "logs/audit.jsonl").is_file()


def test_doc_loader_returns_files_in_stable_relative_order(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import config.settings as settings_module
    from src.core.ingestion import DocLoaderNode

    project_root = isolated_runtime / "project"
    raw_dir = project_root / "data/raw"
    (raw_dir / "nested").mkdir(parents=True)
    (raw_dir / "z.txt").write_text("z", encoding="utf-8")
    (raw_dir / "a.txt").write_text("a", encoding="utf-8")
    (raw_dir / "nested/m.md").write_text("m", encoding="utf-8")
    monkeypatch.setattr(settings_module, "PROJECT_ROOT", project_root)

    files = DocLoaderNode().prep({})

    assert [path.relative_to(raw_dir).as_posix() for path in files] == [
        "a.txt",
        "nested/m.md",
        "z.txt",
    ]
