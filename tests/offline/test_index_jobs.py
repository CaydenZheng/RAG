"""Contract tests for asynchronous index jobs and document mutations."""

import hashlib
import time
from pathlib import Path

import pytest


def _wait_for_job(jobs, job_id: str):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = jobs.status(job_id)
        if job is not None and job.state in {"succeeded", "failed"}:
            return job
        time.sleep(0.01)
    raise AssertionError("index job did not finish")


def _snapshot_version(raw_dir: Path) -> str:
    content = b"".join(
        path.name.encode("utf-8") + b"\0" + path.read_bytes()
        for path in sorted(raw_dir.glob("*"))
        if path.is_file()
    )
    return hashlib.sha256(content).hexdigest()[:24]


def test_submit_is_idempotent_and_records_failure(
    isolated_runtime: Path,
) -> None:
    from src.core.index_jobs import IndexCommand, IndexJobConflictError
    from src.infra.index_jobs import LocalIndexJobs

    calls: list[str] = []

    def build() -> dict:
        calls.append("build")
        return {"version_id": "1" * 24}

    rollback_calls: list[str] = []

    def rollback(version_id: str) -> dict:
        rollback_calls.append(version_id)
        return {"version_id": version_id}

    jobs = LocalIndexJobs(
        db_path=isolated_runtime / "jobs.db",
        raw_dir=isolated_runtime / "raw",
        build=build,
        rollback=rollback,
    )
    try:
        command = IndexCommand.upload("guide.md", b"first")
        first = jobs.submit(command, idempotency_key="upload-guide-v1")
        duplicate = jobs.submit(command, idempotency_key="upload-guide-v1")
        done = _wait_for_job(jobs, first.job_id)

        assert duplicate.job_id == first.job_id
        assert done.state == "succeeded"
        assert done.index_version == "1" * 24
        assert calls == ["build"]
        rollback_job = jobs.submit(
            IndexCommand.rollback("2" * 24),
            idempotency_key="rollback-v2",
        )
        rollback_done = _wait_for_job(jobs, rollback_job.job_id)
        assert rollback_done.state == "succeeded"
        assert rollback_done.index_version == "2" * 24
        assert rollback_calls == ["2" * 24]
        with pytest.raises(IndexJobConflictError):
            jobs.submit(
                IndexCommand.upload("guide.md", b"different", replace=True),
                idempotency_key="upload-guide-v1",
            )
    finally:
        jobs.shutdown()

    def fail_build() -> dict:
        raise RuntimeError("private failure detail")

    failed_jobs = LocalIndexJobs(
        db_path=isolated_runtime / "failed-jobs.db",
        raw_dir=isolated_runtime / "failed-raw",
        build=fail_build,
    )
    try:
        failed = failed_jobs.submit(IndexCommand.rebuild())
        failed = _wait_for_job(failed_jobs, failed.job_id)
        assert failed.state == "failed"
        assert failed.error_code == "index_build_failed"
        assert "private" not in str(failed.to_dict())
    finally:
        failed_jobs.shutdown()


def test_upload_update_and_delete_are_serial_and_idempotent(
    isolated_runtime: Path,
) -> None:
    from src.core.index_jobs import IndexCommand
    from src.infra.index_jobs import LocalIndexJobs

    raw_dir = isolated_runtime / "raw"

    def build() -> dict:
        return {"version_id": _snapshot_version(raw_dir)}

    jobs = LocalIndexJobs(
        db_path=isolated_runtime / "jobs.db",
        raw_dir=raw_dir,
        build=build,
    )
    try:
        created = jobs.submit(
            IndexCommand.upload("guide.md", b"first"),
            idempotency_key="create-guide",
        )
        assert _wait_for_job(jobs, created.job_id).state == "succeeded"
        assert (raw_dir / "guide.md").read_bytes() == b"first"

        conflict = jobs.submit(
            IndexCommand.upload("guide.md", b"blocked"),
            idempotency_key="conflicting-create",
        )
        conflict = _wait_for_job(jobs, conflict.job_id)
        assert conflict.state == "failed"
        assert conflict.error_code == "document_conflict"
        assert (raw_dir / "guide.md").read_bytes() == b"first"

        updated = jobs.submit(
            IndexCommand.upload("guide.md", b"second", replace=True),
            idempotency_key="update-guide",
        )
        assert _wait_for_job(jobs, updated.job_id).state == "succeeded"
        assert (raw_dir / "guide.md").read_bytes() == b"second"

        deleted = jobs.submit(
            IndexCommand.delete("guide.md"),
            idempotency_key="delete-guide",
        )
        repeated = jobs.submit(
            IndexCommand.delete("guide.md"),
            idempotency_key="delete-guide",
        )
        assert repeated.job_id == deleted.job_id
        assert _wait_for_job(jobs, deleted.job_id).state == "succeeded"
        assert not (raw_dir / "guide.md").exists()
    finally:
        jobs.shutdown()


