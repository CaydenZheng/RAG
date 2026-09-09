"""Validate document uploads before persisting or invoking the indexer."""

import ntpath
from pathlib import Path

from fastapi import HTTPException, UploadFile

MAX_FILENAME_BYTES: int = 255
SUPPORTED_SUFFIXES: frozenset[str] = frozenset({".md", ".txt"})


async def save_upload(file: UploadFile, raw_dir: Path, max_bytes: int) -> Path:
    """Store one UTF-8 document under a portable, exclusive filename."""
    filename: str = file.filename or ""
    if (
        not filename
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or ntpath.isreserved(filename)
        or len(filename.encode("utf-8")) > MAX_FILENAME_BYTES
    ):
        raise HTTPException(status_code=400, detail="Invalid document filename")
    if Path(filename).suffix not in SUPPORTED_SUFFIXES:
        raise HTTPException(status_code=415, detail="Only .md and .txt documents are supported")

    # The multipart parser has already spooled the upload; bound application memory.
    content: bytes = await file.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail="Document exceeds the upload size limit")
    try:
        text: str = content.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=415, detail="Document must be UTF-8 text") from None
    if not text.strip() or "\x00" in text:
        raise HTTPException(status_code=400, detail="Document must contain nonempty text without NUL bytes")

    file_path: Path = raw_dir / filename
    created: bool = False
    try:
        raw_dir.mkdir(parents=True, exist_ok=True)
        # Exclusive creation rejects existing files, symlinks and concurrent collisions.
        with file_path.open("xb") as target:
            created = True
            target.write(content)
    except FileExistsError:
        raise HTTPException(status_code=409, detail="Document already exists") from None
    except OSError:
        if created:
            file_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="Unable to save document") from None
    return file_path
