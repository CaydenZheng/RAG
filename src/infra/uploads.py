"""Validate and atomically mutate documents before indexing."""

import hashlib
import ntpath
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, UploadFile

MAX_FILENAME_BYTES: int = 255
SUPPORTED_SUFFIXES: frozenset[str] = frozenset({".md", ".txt"})


@dataclass(frozen=True)
class ValidatedUpload:
    """Validated upload content safe to hand to an index job."""

    filename: str
    content: bytes
    checksum: str


def validate_document_filename(filename: str) -> str:
    """Return one portable leaf filename or raise ValueError."""
    if (
        not filename
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or ntpath.isreserved(filename)
        or len(filename.encode("utf-8")) > MAX_FILENAME_BYTES
    ):
        raise ValueError("invalid document filename")
    if Path(filename).suffix not in SUPPORTED_SUFFIXES:
        raise ValueError("unsupported document type")
    return filename


async def read_upload(file: UploadFile, max_bytes: int) -> ValidatedUpload:
    """Read and validate a bounded UTF-8 upload without persisting it."""
    filename = file.filename or ""
    try:
        validate_document_filename(filename)
    except ValueError as exc:
        status = 415 if str(exc) == "unsupported document type" else 400
        detail = (
            "Only .md and .txt documents are supported"
            if status == 415
            else "Invalid document filename"
        )
        raise HTTPException(status_code=status, detail=detail) from None

    content = await file.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail="Document exceeds the upload size limit",
        )
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=415,
            detail="Document must be UTF-8 text",
        ) from None
    if not text.strip() or "\x00" in text:
        raise HTTPException(
            status_code=400,
            detail="Document must contain nonempty text without NUL bytes",
        )
    return ValidatedUpload(
        filename=filename,
        content=content,
        checksum=hashlib.sha256(content).hexdigest(),
    )


def write_document(
    upload: ValidatedUpload,
    raw_dir: Path,
    *,
    replace: bool,
    idempotent: bool = True,
) -> bool:
    """Atomically create or replace a document; return whether bytes changed."""
    validate_document_filename(upload.filename)
    raw_dir.mkdir(parents=True, exist_ok=True)
    target = raw_dir / upload.filename

    if target.exists() or target.is_symlink():
        if target.is_symlink():
            raise FileExistsError(upload.filename)
        if idempotent and target.is_file() and target.read_bytes() == upload.content:
            return False
        if not replace:
            raise FileExistsError(upload.filename)

    if not replace:
        with target.open("xb") as handle:
            handle.write(upload.content)
            handle.flush()
            os.fsync(handle.fileno())
        return True

    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(upload.content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def delete_document(filename: str, raw_dir: Path) -> bool:
    """Delete one validated document; a missing document is an idempotent no-op."""
    validate_document_filename(filename)
    target = raw_dir / filename
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    return True


async def save_upload(file: UploadFile, raw_dir: Path, max_bytes: int) -> Path:
    """Compatibility helper that preserves exclusive-create HTTP semantics."""
    upload = await read_upload(file, max_bytes)
    try:
        write_document(upload, raw_dir, replace=False, idempotent=False)
    except FileExistsError:
        raise HTTPException(
            status_code=409, detail="Document already exists"
        ) from None
    except OSError:
        raise HTTPException(
            status_code=500, detail="Unable to save document"
        ) from None
    return raw_dir / upload.filename
