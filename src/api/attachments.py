"""Authenticated uploads and one-process, cancellable local parsing tasks."""

import asyncio
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request, Response

from config.settings import settings
from src.api.conversations import identifier, resource_identifier
from src.api.workspace import _upload
from src.core.run_options import WorkspaceError
from src.infra import session_store as stores
from src.infra.attachment_store import AttachmentStore
from src.infra.document_parser import (
    MAX_DOCUMENT_BYTES,
    parse_document,
    release_parser,
    reserve_parser,
)
from src.security.request_authorization import AuthorizationRevoked, PrivateSessionError

router: APIRouter = APIRouter(prefix="/conversations/{conversation_id}/attachments", tags=["attachments"])
_TASKS: dict[tuple[str, str], asyncio.Task[None]] = {}
_ACCEPTING: set[asyncio.Task[Any]] = set()
_FINALIZING: set[asyncio.Task[None]] = set()


async def _parse(store: AttachmentStore, public_id: str, attachment_id: str, reservation: str) -> None:
    # IDs are never reused/reparsed: the initial generation equals the opaque ID.
    generation: str = attachment_id
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
        await asyncio.to_thread(store.finish, public_id, attachment_id, generation, document)
    except asyncio.CancelledError:
        if generation:
            try:
                await asyncio.to_thread(store.finish, public_id, attachment_id, generation, None, "parse_interrupted")
            except (AuthorizationRevoked, PrivateSessionError):
                pass
        raise
    except Exception as error:
        if generation:
            try:
                await asyncio.to_thread(store.finish, public_id, attachment_id, generation, None,
                                        error.code if isinstance(error, WorkspaceError) else "document_invalid")
            except (AuthorizationRevoked, PrivateSessionError):
                pass
    finally:
        if parser is not None:
            parser.cancel()
            await asyncio.gather(parser, return_exceptions=True)


async def _finalize(store: AttachmentStore, attachment_id: str, reservation: str) -> None:
    try:
        await asyncio.to_thread(store.interrupt, attachment_id, attachment_id)
    finally:
        release_parser(reservation)
        _TASKS.pop((str(store.root), attachment_id), None)


def _parse_done(task: asyncio.Task[None], store: AttachmentStore, attachment_id: str, reservation: str) -> None:
    _consume_failure(task)
    finalizer = asyncio.create_task(_finalize(store, attachment_id, reservation))
    _FINALIZING.add(finalizer)
    finalizer.add_done_callback(_finalized)


def _finalized(task: asyncio.Task[None]) -> None:
    _FINALIZING.discard(task)
    _consume_failure(task)


async def _accept(store: AttachmentStore, public_id: str, upload_id: str, name: str, suffix: str, data: bytes) -> dict[str, Any]:
    from src.infra.session_store import CommitPermit

    permit: CommitPermit = CommitPermit()
    reservations: list[str] = []
    def admit() -> None:
        with permit.guard() as valid:
            if not valid:
                raise AuthorizationRevoked("auth_required")
            reservations.append(reserve_parser())
    try:
        try:
            item, created = await asyncio.to_thread(store.accept, public_id, upload_id, name, suffix, data, can_commit=permit, admit=admit)
        finally:
            permit.revoke()
        if created:
            reservation = reservations.pop()
            task = asyncio.create_task(_parse(store, public_id, item["id"], reservation))
            _TASKS[(str(store.root), item["id"])] = task
            task.add_done_callback(lambda completed: _parse_done(completed, store, item["id"], reservation))
        return item
    finally:
        for reservation in reservations:
            release_parser(reservation)


def _consume_failure(task: asyncio.Task[Any]) -> None:
    _ACCEPTING.discard(task)
    if not task.cancelled():
        task.exception()


@router.post("")
async def upload(conversation_id: str, request: Request, response: Response) -> dict[str, Any]:
    public_id: str = resource_identifier(conversation_id)
    store = AttachmentStore(stores.session_store)
    # Check ownership before reading the upload body.
    await asyncio.to_thread(store.listing, public_id)
    data, suffix, fields = await _upload(request, MAX_DOCUMENT_BYTES, {"client_upload_id"}, include_filename=True)
    upload_id: str = identifier(fields.get("client_upload_id", ""))
    task = asyncio.create_task(_accept(store, public_id, upload_id, fields["_filename"], suffix, data))
    _ACCEPTING.add(task)
    task.add_done_callback(_consume_failure)
    item = await asyncio.shield(task)
    response.status_code = 202 if item["state"] == "parsing" else 200
    return item


@router.get("")
def listing(conversation_id: str) -> dict[str, Any]:
    return {"items": AttachmentStore(stores.session_store).listing(resource_identifier(conversation_id))}


@router.get("/{attachment_id}")
def detail(conversation_id: str, attachment_id: str) -> dict[str, Any]:
    return AttachmentStore(stores.session_store).read(resource_identifier(conversation_id), identifier(attachment_id))


@router.get("/{attachment_id}/content")
def content(conversation_id: str, attachment_id: str) -> Response:
    item, data = AttachmentStore(stores.session_store).read(resource_identifier(conversation_id), identifier(attachment_id), content=True)
    return Response(data, media_type="application/octet-stream", headers={
        "Content-Disposition": "attachment; filename*=UTF-8''" + quote(item["name"], safe=""),
        "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
    })


@router.delete("/{attachment_id}", status_code=204)
async def delete(conversation_id: str, attachment_id: str) -> None:
    store = AttachmentStore(stores.session_store)
    attachment_id = identifier(attachment_id)
    await asyncio.to_thread(store.delete, resource_identifier(conversation_id), attachment_id)
    task = _TASKS.get((str(store.root), attachment_id))
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def open_attachments() -> None:
    if settings.auth_enabled:
        await asyncio.to_thread(AttachmentStore(stores.session_store).recover)


async def close_attachments() -> None:
    for task in tuple(_ACCEPTING):
        task.cancel()
    await asyncio.gather(*tuple(_ACCEPTING), return_exceptions=True)
    tasks = tuple(_TASKS.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.gather(*tuple(_FINALIZING), return_exceptions=True)
