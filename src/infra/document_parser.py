"""Complete, bounded text extraction in a cancellable, owned worker process."""

import asyncio
import io
import multiprocessing
import threading
import time
import zipfile
from pathlib import PurePosixPath
from typing import Any

from loguru import logger

from src.core.run_options import WorkspaceError
from src.security.request_authorization import (
    AuthorizationRevoked,
    current_authorization,
    require_active_authorization,
)

MAX_DOCUMENT_BYTES: int = 10 * 1024 * 1024
MAX_DOCUMENT_CHARS: int = 200_000
MAX_TEXT_BYTES: int = 2 * 1024 * 1024
PARSER_TIMEOUT_SECONDS: float = 30.0
_PARSER_SLOTS: threading.BoundedSemaphore = threading.BoundedSemaphore(2)
_OWNERS_LOCK: threading.Lock = threading.Lock()
_PARSING_OWNERS: set[str] = set()


def _checked_text(parts: list[str]) -> str:
    text: str = "\n\n".join(parts)
    if len(text) > MAX_DOCUMENT_CHARS or len(text.encode("utf8")) > MAX_TEXT_BYTES:
        raise WorkspaceError("document_too_large", 413)
    if "\x00" in text:
        raise WorkspaceError("document_invalid")
    return text


def extract_document(data: bytes, suffix: str) -> dict[str, Any]:
    """Translate only known format errors at the third-party parser seam."""
    try:
        return _extract_document(data, suffix)
    except WorkspaceError:
        raise
    except Exception as error:
        if suffix == "pdf":
            import fitz

            if isinstance(error, fitz.FileDataError):
                raise WorkspaceError("document_invalid") from None
        elif suffix == "docx":
            from lxml.etree import XMLSyntaxError

            if isinstance(error, (zipfile.BadZipFile, KeyError, XMLSyntaxError)):
                raise WorkspaceError("document_invalid") from None
        raise


def _extract_document(data: bytes, suffix: str) -> dict[str, Any]:
    """Worker-only parser. Never accepts paths, opens URLs or extracts ZIP files."""
    if len(data) > MAX_DOCUMENT_BYTES:
        raise WorkspaceError("document_too_large", 413)
    pages: list[dict[str, int]] = []
    if suffix in {"txt", "md"}:
        try:
            text = _checked_text([data.decode("utf-8-sig")])
        except UnicodeError:
            raise WorkspaceError("document_invalid") from None
    elif suffix == "pdf":
        import fitz

        fitz.TOOLS.mupdf_display_errors(False)
        fitz.TOOLS.mupdf_display_warnings(False)
        if not data.startswith(b"%PDF-"):
            raise WorkspaceError("document_invalid")
        with fitz.open(stream=data, filetype="pdf") as document:
            if document.needs_pass or document.is_encrypted:
                raise WorkspaceError("document_invalid")
            if document.page_count > 200:
                raise WorkspaceError("document_too_large", 413)
            parts: list[str] = []
            offset: int = 0
            for index, page in enumerate(document):
                part: str = page.get_text()
                parts.append(part)
                _checked_text(parts)
                pages.append({"page": index + 1, "start": offset, "end": offset + len(part)})
                offset += len(part) + 2
            text = _checked_text(parts)
    elif suffix == "docx":
        from docx import Document

        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            items = archive.infolist()
            if len(items) > 2000 or sum(item.file_size for item in items) > 32 * 1024 * 1024:
                raise WorkspaceError("document_too_large", 413)
            for item in items:
                path: PurePosixPath = PurePosixPath(item.filename)
                if (path.is_absolute() or ".." in path.parts or "\\" in item.filename or ":" in item.filename
                    or item.flag_bits & 1 or "vbaproject" in item.filename.lower() or item.filename.startswith("word/embeddings/")):
                    raise WorkspaceError("document_invalid")
                if item.file_size > max(item.compress_size, 1) * 100:
                    raise WorkspaceError("document_too_large", 413)
        try:
            document = Document(io.BytesIO(data))
        except ValueError:
            # python-docx rejects packages whose main part has a wrong type.
            raise WorkspaceError("document_invalid") from None
        parts = []
        for paragraph in document.paragraphs:
            parts.append(paragraph.text)
            _checked_text(parts)
        for table in document.tables:
            for row in table.rows:
                parts.append("\t".join(cell.text for cell in row.cells))
                _checked_text(parts)
        text = _checked_text(parts)
    else:
        raise WorkspaceError("document_unsupported", 415)
    if not text.strip():
        raise WorkspaceError("document_invalid")
    return {"text": text, "pages": pages}


def _worker(data: bytes, suffix: str, channel: Any) -> None:
    try:
        try:
            result = {"document": extract_document(data, suffix)}
        except WorkspaceError as error:
            result = {"code": error.code, "status": error.status}
        except Exception as error:
            logger.warning("Document worker failed: error_type={}", type(error).__name__)
            result = {"code": "document_processing_failed", "status": 503}
        channel.send(result)
    finally:
        channel.close()


def reserve_parser() -> str:
    authorization = current_authorization()
    owner: str = authorization.user_id if authorization is not None else "local-diagnostic"
    with _OWNERS_LOCK:
        if owner in _PARSING_OWNERS or not _PARSER_SLOTS.acquire(blocking=False):
            raise WorkspaceError("document_busy", 503)
        _PARSING_OWNERS.add(owner)
    return owner


def release_parser(owner: str) -> None:
    with _OWNERS_LOCK:
        _PARSING_OWNERS.discard(owner)
        _PARSER_SLOTS.release()


async def parse_document(data: bytes, suffix: str, *, reservation: str | None = None) -> dict[str, Any]:
    owner: str = reservation if reservation is not None else reserve_parser()
    reader = writer = process = None
    started: bool = False
    try:
        context = multiprocessing.get_context("spawn")
        reader, writer = context.Pipe(duplex=False)
        process = context.Process(target=_worker, args=(data, suffix, writer), daemon=True)
        process.start()
        started = True
        writer.close()
        deadline: float = time.monotonic() + PARSER_TIMEOUT_SECONDS
        while not reader.poll():
            require_active_authorization()
            if not process.is_alive():
                logger.warning("Document worker exited without result: exit_code={}", process.exitcode)
                raise WorkspaceError("document_processing_failed", 503)
            if time.monotonic() >= deadline:
                raise WorkspaceError("document_timeout", 503)
            await asyncio.sleep(.025)
        result = reader.recv()
        if "code" in result:
            raise WorkspaceError(result["code"], result["status"])
        return result["document"]
    except (WorkspaceError, AuthorizationRevoked):
        raise
    except Exception as error:
        logger.warning("Document process/IPC failed: error_type={}", type(error).__name__)
        raise WorkspaceError("document_processing_failed", 503) from error
    finally:
        # This actually stops the dedicated worker, including native parsers.
        try:
            if started:
                if process.is_alive():
                    process.terminate()
                try:
                    await asyncio.to_thread(process.join, 1)
                finally:
                    # A second cancellation cannot leave a native worker running.
                    if process.is_alive():
                        process.kill()
                        process.join(timeout=1)
                    process.close()
            elif process is not None:
                process.close()
        finally:
            if reader is not None:
                reader.close()
            if writer is not None:
                writer.close()
            if reservation is None:
                release_parser(owner)
