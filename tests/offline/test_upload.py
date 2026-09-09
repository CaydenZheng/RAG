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

    calls: list[dict] = []

    def index(shared: dict) -> None:
        calls.append(shared)
        shared["index_info"] = {
            "chunks_count": 1,
            "fingerprint": "offline-upload",
        }

    monkeypatch.setattr(api, "get_offline_flow", lambda: SimpleNamespace(run=index))
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

    assert response.status_code == 200
    assert response.json() == {
        "status": "indexed",
        "chunks": 1,
        "fingerprint": "offline-upload",
    }
    assert (isolated_runtime / "data/raw/normal.txt").read_bytes() == (
        b"x" * 20
    )
    assert len(calls) == 1


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


def test_existing_document_is_not_overwritten_or_indexed(
    upload_client: tuple[TestClient, list[dict]], isolated_runtime: Path
) -> None:
    client, calls = upload_client
    raw_dir = isolated_runtime / "data/raw"
    raw_dir.mkdir(parents=True)
    existing = raw_dir / "document.md"
    existing.write_text("original", encoding="utf-8")

    response = post_document(client, "document.md", b"replacement")

    assert response.status_code == 409
    assert existing.read_text(encoding="utf-8") == "original"
    assert calls == []


def test_windows_absolute_path_is_rejected_before_writing(
    isolated_runtime: Path,
) -> None:
    from src.infra.uploads import save_upload

    upload = UploadFile(file=BytesIO(b"valid text"), filename=r"C:\document.txt")
    with pytest.raises(HTTPException) as error:
        asyncio.run(save_upload(upload, isolated_runtime / "data/raw", 20))

    assert error.value.status_code == 400
    assert not (isolated_runtime / "data/raw").exists()
