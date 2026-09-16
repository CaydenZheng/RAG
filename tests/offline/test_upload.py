from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from httpx import Response

ADMIN_HEADERS: dict[str, str] = {"X-Admin-Key": "offline-admin-key"}


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
        headers=ADMIN_HEADERS,
    )


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Admin-Key": "wrong-admin-key"},
    ],
)
@pytest.mark.parametrize(
    ("method", "path", "request_kwargs"),
    [
        (
            "POST",
            "/upload",
            {
                "files": {
                    "file": (
                        "document.txt",
                        b"valid content",
                        "application/octet-stream",
                    )
                }
            },
        ),
        ("DELETE", "/documents/document.txt", {}),
        ("POST", "/index/rebuild", {}),
        ("POST", "/index/rollback", {}),
    ],
)
def test_index_write_requires_admin_credential_before_submission(
    headers: dict[str, str],
    method: str,
    path: str,
    request_kwargs: dict,
    upload_client: tuple[TestClient, list[dict]],
) -> None:
    client, calls = upload_client

    response = client.request(
        method,
        path,
        headers=headers,
        **request_kwargs,
    )

    assert response.status_code == 401
    assert response.json() == {
        "detail": {
            "code": "admin_auth_required",
            "message": "需要有效的管理员凭据",
        }
    }
    assert calls == []


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
        "/index/rebuild", headers={**ADMIN_HEADERS, "Idempotency-Key": "rebuild-v1"}
    )
    rollback = client.post(
        "/index/rollback?version_id=" + "2" * 24,
        headers={**ADMIN_HEADERS, "Idempotency-Key": "rollback-v1"},
    )
    delete = client.delete(
        "/documents/guide.md",
        headers={**ADMIN_HEADERS, "Idempotency-Key": "delete-v1"},
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


def test_unauthenticated_large_upload_is_rejected_before_multipart_spooling(
    monkeypatch: pytest.MonkeyPatch,
    upload_client: tuple[TestClient, list[dict]],
) -> None:
    from starlette import formparsers

    client, calls = upload_client
    spool_calls: list[None] = []
    original_spooled_file = formparsers.SpooledTemporaryFile

    def track_spooled_file(*args: object, **kwargs: object) -> object:
        spool_calls.append(None)
        return original_spooled_file(*args, **kwargs)

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", track_spooled_file)

    response = client.post(
        "/upload",
        files={
            "file": (
                "large.txt",
                b"x" * (1024 * 1024 + 1),
                "application/octet-stream",
            )
        },
    )

    assert response.status_code == 401
    assert spool_calls == []
    assert calls == []


@pytest.mark.parametrize(
    ("base_url", "client_host", "status_code"),
    [
        ("http://127.0.0.1", "127.0.0.1", 202),
        ("http://127.0.0.1", "203.0.113.10", 401),
        ("http://example.com", "127.0.0.1", 401),
    ],
)
def test_unauthenticated_admin_bypass_is_limited_to_loopback(
    monkeypatch: pytest.MonkeyPatch,
    upload_client: tuple[TestClient, list[dict]],
    base_url: str,
    client_host: str,
    status_code: int,
) -> None:
    import app as api

    _, calls = upload_client
    monkeypatch.setattr(api.settings, "allow_unauthenticated_admin", True)
    client = TestClient(
        api.app,
        base_url=base_url,
        client=(client_host, 50000),
    )
    try:
        response = client.post("/index/rebuild")
    finally:
        client.close()

    assert response.status_code == status_code
    assert len(calls) == (1 if status_code == 202 else 0)


@pytest.mark.parametrize(
    ("method", "path", "request_kwargs"),
    [
        (
            "POST",
            "/api/upload",
            {
                "files": {
                    "file": (
                        "document.txt",
                        b"valid content",
                        "application/octet-stream",
                    )
                }
            },
        ),
        ("DELETE", "/api/documents/document.txt", {}),
        ("POST", "/api/index/rebuild", {}),
        ("POST", "/api/index/rollback", {}),
    ],
)
def test_index_write_auth_applies_behind_root_path(
    method: str,
    path: str,
    request_kwargs: dict[str, object],
    upload_client: tuple[TestClient, list[dict]],
) -> None:
    import app as api

    _, calls = upload_client
    client = TestClient(api.app, root_path="/api")
    try:
        response = client.request(method, path, **request_kwargs)
    finally:
        client.close()

    assert response.status_code == 401
    assert calls == []
