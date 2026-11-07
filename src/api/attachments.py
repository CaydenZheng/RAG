"""Authenticated uploads and one-process, cancellable local parsing tasks."""

import asyncio
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request, Response

from config.settings import settings
from src.api.conversations import identifier, resource_identifier
from src.api.workspace import _upload
from src.core import attachment_runtime
from src.infra import session_store as stores
from src.infra.attachment_store import AttachmentStore
from src.infra.document_parser import MAX_DOCUMENT_BYTES

router: APIRouter = APIRouter(prefix="/conversations/{conversation_id}/attachments", tags=["attachments"])


@router.post("")
async def upload(conversation_id: str, request: Request, response: Response) -> dict[str, Any]:
    public_id: str = resource_identifier(conversation_id)
    store = AttachmentStore(stores.session_store)
    # Check ownership before reading the upload body.
    await asyncio.to_thread(store.listing, public_id)
    data, suffix, fields = await _upload(request, MAX_DOCUMENT_BYTES, {"client_upload_id"}, include_filename=True)
    upload_id: str = identifier(fields.get("client_upload_id", ""))
    item = await attachment_runtime.accept(store, public_id, upload_id, fields["_filename"], suffix, data)
    response.status_code = 202 if item["state"] == "parsing" else 200
    return item


@router.get("")
async def listing(conversation_id: str) -> dict[str, Any]:
    store = AttachmentStore(stores.session_store)
    public_id = resource_identifier(conversation_id)
    await attachment_runtime.reconcile(store, public_id)
    return {"items": await asyncio.to_thread(store.listing, public_id)}


@router.get("/{attachment_id}")
async def detail(conversation_id: str, attachment_id: str) -> dict[str, Any]:
    store = AttachmentStore(stores.session_store)
    public_id = resource_identifier(conversation_id)
    await attachment_runtime.reconcile(store, public_id)
    return await asyncio.to_thread(store.read, public_id, identifier(attachment_id))


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
    await attachment_runtime.delete(store, resource_identifier(conversation_id), attachment_id)


async def open_attachments() -> None:
    if settings.auth_enabled:
        await asyncio.to_thread(AttachmentStore(stores.session_store).recover)


async def close_attachments() -> None:
    await attachment_runtime.close()
