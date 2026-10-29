"""Owner-scoped directory and immutable completed-exchange views."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from config.settings import settings
from src.api.client_identity import RequestSession, scope_request_session
from src.api.public_errors import public_error
from src.core.run_options import ModelChoice, current_run_options
from src.infra import session_store as stores
from src.infra.exchange_context import (
    ExchangeContext,
    canonical_uuid,
    completed_model,
    exchange_context,
)
from src.infra.skill_store import SkillStore
from src.security.request_authorization import current_authorization
from src.security.session_ids import validate_public_session_id

router: APIRouter = APIRouter(prefix="/conversations", tags=["conversations"])


class CreateConversation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    mode: Literal["rag", "agent"]
    model: ModelChoice | None = None


class UpdateConversation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0, strict=True)
    title: str | None = Field(default=None, min_length=1, max_length=80)
    model: ModelChoice | None = None


class ClearConversation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0, strict=True)


class SearchConversations(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title_query: str = Field(min_length=1, max_length=80)
    mode: Literal["rag", "agent"] | None = None
    limit: int = Field(default=20, ge=1, le=50, strict=True)
    cursor: str | None = Field(default=None, max_length=512)


def identifier(value: str) -> str:
    try:
        return canonical_uuid(value)
    except ValueError:
        raise HTTPException(422, detail=public_error("auth_invalid_request")) from None


def resource_identifier(value: str) -> str:
    """Legacy account clients may have safe non-UUID public session IDs."""
    try:
        return validate_public_session_id(value)
    except ValueError:
        raise HTTPException(422, detail=public_error("auth_invalid_request")) from None


def begin_exchange(
    request: Request,
    session_id: str,
    conversation_id: str | None,
    client_turn_id: str | None,
    mode: Literal["rag", "agent"],
    question: str,
    *,
    allow_empty: bool = False,
    model: ModelChoice | None = None,
) -> RequestSession:
    """Validate duplicate keys before retrieval, planning or tool side effects."""
    if model is not None and not settings.auth_enabled:
        raise HTTPException(401, detail=public_error("auth_required"))
    if client_turn_id is not None:
        client_turn_id = identifier(client_turn_id)
    if conversation_id is not None:
        conversation_id = identifier(conversation_id)
        if session_id and session_id != conversation_id:
            raise HTTPException(422, detail=public_error("auth_invalid_request"))
        session_id = conversation_id
    session = scope_request_session(
        request, session_id, mode, allow_empty=allow_empty, create=True
    )
    if settings.auth_enabled and session.storage_id:
        # Legacy clients still work, but receive a fresh server-side correlation
        # ID. The new workspace always supplies its browser-generated UUID.
        import uuid

        turn_id: str = (
            client_turn_id if client_turn_id is not None else str(uuid.uuid4())
        )
        stores.session_store.ensure_new_turn(session.storage_id, turn_id)
        options = SkillStore(stores.session_store).snapshot(session.public_id, model, agent=mode == "agent")
        current_run_options.set(options)
        exchange_context.set(ExchangeContext(turn_id, question, requested_model=options.choice,
            skills=tuple({"id": skill.id, "name": skill.name, "version": skill.version} for skill in options.skills)))
        completed_model.set(None)
    return session


@router.post("", status_code=201)
def create(body: CreateConversation) -> dict[str, Any]:
    public_id: str = identifier(body.id)
    authorization = current_authorization()
    if authorization is None:
        raise HTTPException(401, detail=public_error("auth_required"))
    stores.session_store.owned_session(
        authorization.user_id, public_id, body.mode, create=True, model=body.model
    )
    return stores.session_store.conversation(public_id)


@router.get("")
def listing(
    mode: Literal["rag", "agent"] | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> dict[str, Any]:
    return stores.session_store.conversations(mode=mode, limit=limit, cursor=cursor)


@router.post("/search")
def search(body: SearchConversations) -> dict[str, Any]:
    query: str = body.title_query.strip()
    if not query:
        raise HTTPException(422, detail=public_error("auth_invalid_request"))
    return stores.session_store.conversations(
        mode=body.mode, limit=body.limit, cursor=body.cursor, title_query=query
    )


@router.get("/{conversation_id}")
def detail(conversation_id: str) -> dict[str, Any]:
    return stores.session_store.conversation(resource_identifier(conversation_id))


@router.get("/{conversation_id}/turns")
def turns(
    conversation_id: str,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    before: Annotated[str | None, Query(max_length=512)] = None,
    client_turn_id: str | None = None,
) -> dict[str, Any]:
    if client_turn_id is not None and (
        before is not None or "limit" in request.query_params
    ):
        raise HTTPException(422, detail=public_error("auth_invalid_request"))
    return stores.session_store.transcript(
        resource_identifier(conversation_id),
        limit=limit,
        before=before,
        client_turn_id=identifier(client_turn_id)
        if client_turn_id is not None
        else None,
    )


@router.patch("/{conversation_id}")
def rename(conversation_id: str, body: UpdateConversation) -> dict[str, Any]:
    title: str | None = body.title.strip() if body.title is not None else None
    if (body.title is not None and not title) or (title is None and body.model is None):
        raise HTTPException(422, detail=public_error("auth_invalid_request"))
    return stores.session_store.mutate_conversation(
        resource_identifier(conversation_id), body.expected_revision, title=title, model=body.model
    )


@router.post("/{conversation_id}/clear")
def clear(conversation_id: str, body: ClearConversation) -> dict[str, Any]:
    return stores.session_store.mutate_conversation(
        resource_identifier(conversation_id), body.expected_revision, clear=True
    )


@router.delete("/{conversation_id}", status_code=204)
def delete(conversation_id: str, if_match: Annotated[str, Header()]) -> None:
    try:
        revision: int = int(if_match.strip('"'))
        if revision < 0:
            raise ValueError
    except ValueError:
        raise HTTPException(422, detail=public_error("auth_invalid_request")) from None
    stores.session_store.mutate_conversation(
        resource_identifier(conversation_id), revision, delete=True
    )
