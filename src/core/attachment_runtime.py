"""One-process attachment task ownership, cancellation and deferred finalization."""

import asyncio
from collections.abc import Callable
from typing import Any

from loguru import logger

from src.core.run_options import WorkspaceError
from src.infra.attachment_store import AttachmentStore
from src.infra.document_parser import parse_document, release_parser, reserve_parser
from src.infra.sqlite_transactions import CommitPermit
from src.security.request_authorization import AuthorizationRevoked, PrivateSessionError

_TASKS: dict[tuple[str, str], asyncio.Task[None]] = {}
_ACCEPTING: set[asyncio.Task[Any]] = set()
_FINALIZING: set[asyncio.Task[None]] = set()
# Failed maintenance is retried by explicit status reconciliation or shutdown;
# startup recovery handles persisted parsing/cleanup records after a restart.
_PENDING: dict[tuple[str, str], tuple[AttachmentStore, str, str]] = {}


async def _join(worker: asyncio.Task[Any]) -> Any:
    # Cancelling an await does not stop a thread. Retain ownership until it exits.
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            continue
    return worker.result()


async def _write(operation: Callable[..., Any], *args: Any) -> Any:
    permit = CommitPermit()
    worker = asyncio.create_task(asyncio.to_thread(operation, *args, can_commit=permit))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        permit.revoke()
        try:
            await _join(worker)
        except Exception as error:
            if not isinstance(error, AuthorizationRevoked):
                logger.warning("Cancelled attachment write failed: code={}", error.code if isinstance(error, WorkspaceError) else "attachment_unavailable")
        raise
    finally:
        permit.revoke()


async def _mark_interrupted(store: AttachmentStore, public_id: str, attachment_id: str, code: str) -> None:
    key = (str(store.root), attachment_id)
    _PENDING[key] = (store, public_id, code)
    try:
        await _join(asyncio.create_task(asyncio.to_thread(store.interrupt, attachment_id, attachment_id, code)))
    except WorkspaceError:
        raise
    except Exception as error:
        logger.warning("Attachment finalization failed: error_type={}", type(error).__name__)
        raise WorkspaceError("attachment_unavailable", 503) from error
    _PENDING.pop(key, None)


async def _defer_interrupted(store: AttachmentStore, public_id: str, attachment_id: str, code: str) -> None:
    try:
        await _mark_interrupted(store, public_id, attachment_id, code)
    except WorkspaceError as error:
        logger.warning("Attachment finalization deferred: code={}", error.code)


async def _parse(store: AttachmentStore, public_id: str, attachment_id: str, reservation: str) -> None:
    parser: asyncio.Task[dict[str, Any]] | None = None
    try:
        generation, data, suffix = await asyncio.to_thread(store.parse_input, public_id, attachment_id)
        parser = asyncio.create_task(parse_document(data, suffix, reservation=reservation))
        while not parser.done():
            await asyncio.wait({parser}, timeout=.1)
            current = await asyncio.to_thread(store.read, public_id, attachment_id)
            if current["state"] != "parsing":
                return
        document = await parser
        await _write(store.finish, public_id, attachment_id, generation, document)
    except (AuthorizationRevoked, PrivateSessionError):
        return
    except asyncio.CancelledError:
        raise
    except Exception as error:
        code = error.code if isinstance(error, WorkspaceError) else "document_processing_failed"
        # No input-invalid fallback for a storage failure. Finalizer can persist
        # the cause after authorization ends, without ever publishing ready.
        raise WorkspaceError(code, 503) from error
    finally:
        if parser is not None:
            parser.cancel()
            await asyncio.gather(parser, return_exceptions=True)


async def _finalize(store: AttachmentStore, public_id: str, attachment_id: str, reservation: str, code: str) -> None:
    try:
        await _defer_interrupted(store, public_id, attachment_id, code)
    finally:
        release_parser(reservation)
        _TASKS.pop((str(store.root), attachment_id), None)


def _parse_done(task: asyncio.Task[None], store: AttachmentStore, public_id: str, attachment_id: str, reservation: str) -> None:
    error = None if task.cancelled() else task.exception()
    code = error.code if isinstance(error, WorkspaceError) else "parse_interrupted"
    finalizer = asyncio.create_task(_finalize(store, public_id, attachment_id, reservation, code))
    _FINALIZING.add(finalizer)
    finalizer.add_done_callback(_finalized)


def _consume_failure(task: asyncio.Task[Any]) -> None:
    _ACCEPTING.discard(task)
    if not task.cancelled() and (error := task.exception()) is not None:
        logger.warning("Attachment operation failed: code={}", error.code if isinstance(error, WorkspaceError) else "attachment_unavailable")


def _finalized(task: asyncio.Task[None]) -> None:
    _FINALIZING.discard(task)
    _consume_failure(task)


async def _accept(store: AttachmentStore, public_id: str, upload_id: str, name: str, suffix: str, data: bytes) -> dict[str, Any]:
    permit = CommitPermit()
    reservations: list[str] = []

    def admit() -> None:
        with permit.guard() as valid:
            if not valid:
                raise AuthorizationRevoked("auth_required")
            reservations.append(reserve_parser())

    worker = asyncio.create_task(asyncio.to_thread(store.accept, public_id, upload_id, name, suffix, data, can_commit=permit, admit=admit))
    try:
        try:
            item, created = await asyncio.shield(worker)
        except asyncio.CancelledError:
            permit.revoke()
            try:
                outcome = await _join(worker)
            except Exception:
                outcome = None  # Failed worker rolled back; no parser may start.
            if isinstance(outcome, tuple) and outcome[1]:
                await _defer_interrupted(store, public_id, outcome[0]["id"], "parse_interrupted")
            raise
        if created:
            reservation = reservations.pop()
            task = asyncio.create_task(_parse(store, public_id, item["id"], reservation))
            _TASKS[(str(store.root), item["id"])] = task
            task.add_done_callback(lambda completed: _parse_done(completed, store, public_id, item["id"], reservation))
        return item
    finally:
        permit.revoke()
        for reservation in reservations:
            release_parser(reservation)


async def accept(store: AttachmentStore, public_id: str, upload_id: str, name: str, suffix: str, data: bytes) -> dict[str, Any]:
    task = asyncio.create_task(_accept(store, public_id, upload_id, name, suffix, data))
    _ACCEPTING.add(task)
    task.add_done_callback(_consume_failure)
    # Lost HTTP response means unknown outcome; reconcile by upload ID.
    return await asyncio.shield(task)


async def reconcile(store: AttachmentStore, public_id: str) -> None:
    for (root, attachment_id), (_, pending_public_id, code) in tuple(_PENDING.items()):
        if root == str(store.root) and pending_public_id == public_id:
            await _mark_interrupted(store, public_id, attachment_id, code)


async def delete(store: AttachmentStore, public_id: str, attachment_id: str) -> None:
    await _write(store.delete, public_id, attachment_id)
    task = _TASKS.get((str(store.root), attachment_id))
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def close() -> None:
    for task in tuple(_ACCEPTING):
        task.cancel()
    await asyncio.gather(*tuple(_ACCEPTING), return_exceptions=True)
    tasks = tuple(_TASKS.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.gather(*tuple(_FINALIZING), return_exceptions=True)
    for (_, attachment_id), (store, public_id, code) in tuple(_PENDING.items()):
        await _defer_interrupted(store, public_id, attachment_id, code)
