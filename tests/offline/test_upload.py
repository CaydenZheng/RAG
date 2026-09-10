import asyncio
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile
from fastapi.testclient import TestClient
from httpx import Response


@pytest.fixture
def upload_client(
    monkeypatch: pytest.MonkeyPatch, isolated_runtime: Path
) -> Iterator[tuple[TestClient, list[dict]]]:
    import tiktoken

    monkeypatch.setattr(
        tiktoken,
        "get_encoding",
        lambda name: SimpleNamespace(
            encode=lambda *args, **kwargs: pytest.fail(
                "Upload test unexpectedly invoked tokenization"
            )
        ),
    )

    import app as api
    from src.core.index_jobs import IndexJob, IndexJobState, IndexOperation

    calls: list[dict] = []

    class FakeJobs:
        def status(self, job_id):
            if job_id != "1" * 32:
                return None
            return IndexJob(
                job_id=job_id,
                operation=IndexOperation.REBUILD,
                state=IndexJobState.SUCCEEDED,
                submitted_at="2026-09-10T00:00:00+00:00",
                finished_at="2026-09-10T00:00:01+00:00",
                index_version="2" * 24,
            )

        def submit(self, command, *, idempotency_key=None):
            calls.append(
                {
                    "command": command,
                    "idempotency_key": idempotency_key,
                }
            )
            return IndexJob(
                job_id="1" * 32,
                operation=command.operation,
                state=IndexJobState.QUEUED,
                submitted_at="2026-09-10T00:00:00+00:00",
            )

    monkeypatch.setattr(api, "get_index_jobs", lambda: FakeJobs())
    monkeypatch.setattr(api.settings, "max_upload_bytes", 20)
    client = TestClient(api.app)
    try:
        yield client, calls
    finally:
        client.close()


def post_document(
    client: TestClient,
    filename: str,
    content: bytes = b"x" * 20,
) -> Response:
    return client.post(
        "/upload",
        files={"file": (filename, content, "application/octet-stream")},
    )


def test_valid_upload_is_saved_and_indexed(
    upload_client: tuple[TestClient, list[dict]], isolated_runtime: Path
) -> None:
    client, calls = upload_client

    response = post_document(client, "normal.txt")

    assert response.status_code == 202
    assert response.json() == {
        "job_id": "1" * 32,
        "operation": "upload",
        "state": "queued",
        "submitted_at": "2026-09-10T00:00:00+00:00",
        "started_at": None,
        "finished_at": None,
        "index_version": None,
        "error_code": None,
    }
    assert not (isolated_runtime / "data/raw/normal.txt").exists()
    assert len(calls) == 1
    assert calls[0]["command"].filename == "normal.txt"
    assert calls[0]["command"].content == b"x" * 20


@pytest.mark.parametrize(
    ("filename", "status_code"),
    [
        ("../escaped.txt", 400),
        ("/document.txt", 400),
        ("folder/document.txt", 400),
        (r"folder\document.txt", 400),
        ("document.txt:stream", 400),
        ("CON.txt", 400),
        ("document.TXT", 415),
        ("document.pdf", 415),
        ("a" * 300 + ".txt", 400),
    ],
)
def test_invalid_filename_or_type_has_no_side_effects(
    filename: str,
    status_code: int,
    upload_client: tuple[TestClient, list[dict]],
    isolated_runtime: Path,
) -> None:
    client, calls = upload_client

    response = post_document(client, filename)

    assert response.status_code == status_code
    raw_dir = isolated_runtime / "data/raw"
    assert not raw_dir.exists() or list(raw_dir.iterdir()) == []
    assert not (isolated_runtime / "data/escaped.txt").exists()
    assert calls == []


@pytest.mark.parametrize(
    ("content", "status_code"),
    [
        (b"x" * 21, 413),
        (b"\xff", 415),
        (b"   \n", 400),
        (b"text\x00suffix", 400),
    ],
)
def test_invalid_content_has_no_side_effects(
    content: bytes,
    status_code: int,
    upload_client: tuple[TestClient, list[dict]],
    isolated_runtime: Path,
) -> None:
    client, calls = upload_client

    response = post_document(client, "document.txt", content)

    assert response.status_code == status_code
    assert not (isolated_runtime / "data/raw/document.txt").exists()
    assert calls == []


def test_existing_document_is_queued_without_synchronous_overwrite(
    upload_client: tuple[TestClient, list[dict]], isolated_runtime: Path
) -> None:
    client, calls = upload_client
    raw_dir = isolated_runtime / "data/raw"
    raw_dir.mkdir(parents=True)
    existing = raw_dir / "document.md"
    existing.write_text("original", encoding="utf-8")

    response = post_document(client, "document.md", b"replacement")

    assert response.status_code == 202
    assert existing.read_text(encoding="utf-8") == "original"
    assert len(calls) == 1
    assert calls[0]["command"].replace is False


def test_index_job_control_endpoints(
    upload_client: tuple[TestClient, list[dict]],
) -> None:
    client, calls = upload_client

    rebuild = client.post(
        "/index/rebuild", headers={"Idempotency-Key": "rebuild-v1"}
    )
    rollback = client.post(
        "/index/rollback?version_id=" + "2" * 24,
        headers={"Idempotency-Key": "rollback-v1"},
    )
    delete = client.delete(
        "/documents/guide.md",
        headers={"Idempotency-Key": "delete-v1"},
    )
    status = client.get("/index/jobs/" + "1" * 32)

    assert rebuild.status_code == 202
    assert rollback.status_code == 202
    assert delete.status_code == 202
    assert [call["command"].operation for call in calls] == [
        "rebuild",
        "rollback",
        "delete",
    ]
    assert status.status_code == 200
    assert status.json()["state"] == "succeeded"
    assert client.get("/index/jobs/not-a-job").status_code == 422


def test_windows_absolute_path_is_rejected_before_writing(
    isolated_runtime: Path,
) -> None:
    from src.infra.uploads import save_upload

    upload = UploadFile(file=BytesIO(b"valid text"), filename=r"C:\document.txt")
    with pytest.raises(HTTPException) as error:
        asyncio.run(save_upload(upload, isolated_runtime / "data/raw", 20))

    assert error.value.status_code == 400
    assert not (isolated_runtime / "data/raw").exists()
