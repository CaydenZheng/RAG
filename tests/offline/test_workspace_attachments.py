"""Real owned SQLite/files with isolated accounts; no external model or index."""

import asyncio
import io
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from tests.offline.test_account_ownership import accounts as accounts
from tests.offline.test_account_ownership import authorize


def conversation(accounts: Any, mode: str = "rag") -> str:
    public_id: str = str(uuid.uuid4())
    assert accounts.browsers["alice"].post("/conversations", json={"id": public_id, "mode": mode}).status_code == 201
    return public_id


def ready(accounts: Any, public_id: str, text: str = "The synthetic code is ORANGE-17.") -> dict[str, Any]:
    from src.infra.attachment_store import AttachmentStore

    store = AttachmentStore(accounts.store)
    with authorize(accounts.service, accounts.logins["alice"]):
        item, _ = store.accept(public_id, str(uuid.uuid4()), "notes.txt", "txt", text.encode())
        generation, _, _ = store.parse_input(public_id, item["id"])
        store.finish(public_id, item["id"], generation, {"text": text, "pages": []})
    return item


def test_attachment_owner_snapshot_download_and_tombstone(accounts: Any) -> None:
    from src.core.run_options import WorkspaceError
    from src.infra.attachment_store import AttachmentStore

    public_id = conversation(accounts)
    item = ready(accounts, public_id)
    path: str = f"/conversations/{public_id}/attachments"
    alice, bob = accounts.browsers["alice"], accounts.browsers["bob"]
    for suffix in ("", f"/{item['id']}", f"/{item['id']}/content"):
        assert bob.get(path + suffix).status_code == 404
    content = alice.get(path + f"/{item['id']}/content")
    assert content.status_code == 200 and b"ORANGE-17" in content.content
    assert content.headers["content-disposition"].startswith("attachment;")
    assert content.headers["cache-control"] == "no-store"
    with authorize(accounts.service, accounts.logins["alice"]):
        store = AttachmentStore(accounts.store)
        assert store.snapshot(public_id, [item["id"]])[0].text.endswith("ORANGE-17.")
    assert alice.delete(path + f"/{item['id']}").status_code == 204
    assert alice.get(path + f"/{item['id']}/content").status_code == 404
    with authorize(accounts.service, accounts.logins["alice"]):
        with pytest.raises(WorkspaceError, match="upload_deleted"):
            store.accept(public_id, item["client_upload_id"], "notes.txt", "txt", b"The synthetic code is ORANGE-17.")
        with pytest.raises(WorkspaceError, match="attachment_not_ready"):
            store.snapshot(public_id, [item["id"]])
    assert not store._path(item["id"], "raw").exists()


@pytest.mark.parametrize("mode,reset_path,chat_path", [("rag", "/session/reset", "/query"), ("agent", "/agent/reset", "/agent/chat")])
@pytest.mark.parametrize("locked", [False, True])
def test_legacy_reset_cleans_ready_files_or_retains_pending_cleanup(accounts: Any, monkeypatch: pytest.MonkeyPatch, mode: str, reset_path: str, chat_path: str, locked: bool) -> None:
    from contextlib import closing

    from src.infra.attachment_store import AttachmentStore

    public_id = conversation(accounts, mode)
    item = ready(accounts, public_id)
    browser = accounts.browsers["alice"]
    payload = {"session_id":public_id, "query" if mode == "rag" else "message":"Synthetic completed turn"}
    assert browser.post(chat_path, json=payload).status_code == 200
    store = AttachmentStore(accounts.store)
    paths = [store._path(item["id"], suffix) for suffix in ("raw", "text")]
    assert all(path.exists() for path in paths)
    attempts: list[Path] = []
    original = Path.unlink
    def unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        if path in paths:
            attempts.append(path)
            if locked:
                raise PermissionError("synthetic file lock")
        original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", unlink)
    response = browser.post(reset_path, params={"session_id":public_id})
    assert response.status_code == 200
    assert attempts, "legacy reset did not attempt registered file cleanup"
    assert all(path.exists() == locked for path in paths)
    with closing(accounts.store._connect()) as connection:
        row = connection.execute("SELECT state,cleanup,text_bytes FROM conversation_attachments WHERE id=?", (item["id"],)).fetchone()
    assert row[0] == "deleted" and row[1] == int(locked)
    assert (row[2] > 0) == locked
    assert browser.get(f"/conversations/{public_id}/attachments").json()["items"] == []


def test_upload_retry_concurrency_capacity_conflict_and_failed_state(accounts: Any) -> None:
    from src.core.run_options import WorkspaceError
    from src.infra.attachment_store import AttachmentStore

    public_id = conversation(accounts)
    store = AttachmentStore(accounts.store)
    upload_id: str = str(uuid.uuid4())
    def accept() -> tuple[dict[str, Any], bool]:
        with authorize(accounts.service, accounts.logins["alice"]):
            return store.accept(public_id, upload_id, "notes.txt", "txt", b"notes")
    # Real SQLite serialization; duplicate detection precedes quota enforcement.
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(accept), pool.submit(accept)]
        first, second = [future.result() for future in futures]
    assert first[0]["id"] == second[0]["id"] and sorted([first[1], second[1]]) == [False, True]
    with authorize(accounts.service, accounts.logins["alice"]):
        for _ in range(4):
            store.accept(public_id, str(uuid.uuid4()), "more.txt", "txt", b"more")
        assert store.accept(public_id, upload_id, "different-name.txt", "txt", b"notes")[0]["name"] == "notes.txt"
        with pytest.raises(WorkspaceError, match="attachment_limit"):
            store.accept(public_id, str(uuid.uuid4()), "last.txt", "txt", b"last")
        with pytest.raises(WorkspaceError, match="upload_id_conflict"):
            store.accept(public_id, upload_id, "notes.txt", "txt", b"changed")
        generation, _, _ = store.parse_input(public_id, first[0]["id"])
        store.finish(public_id, first[0]["id"], generation, None, "document_invalid")
        retry = store.accept(public_id, upload_id, "notes.txt", "txt", b"notes")
        assert retry[0]["state"] == "failed" and retry[1] is False


@pytest.mark.parametrize("action", ["delete", "clear", "restart"])
def test_parser_result_cannot_revive_invalidated_file(accounts: Any, action: str) -> None:
    from src.infra.attachment_store import AttachmentStore
    from src.security.request_authorization import PrivateSessionError

    public_id = conversation(accounts)
    store = AttachmentStore(accounts.store)
    with authorize(accounts.service, accounts.logins["alice"]):
        item, _ = store.accept(public_id, str(uuid.uuid4()), "notes.txt", "txt", b"notes")
        generation, _, _ = store.parse_input(public_id, item["id"])
        if action == "delete":
            store.delete(public_id, item["id"])
        elif action == "clear":
            summary = accounts.store.conversation(public_id)
            accounts.store.mutate_conversation(public_id, summary["revision"], clear=True)
            store.cleanup()
        else:
            store.recover()
        store.finish(public_id, item["id"], generation, {"text": "LATE", "pages": []})
        if action == "restart":
            assert store.read(public_id, item["id"])["error_code"] == "parse_interrupted"
            retried, created = store.accept(public_id, item["client_upload_id"], "notes.txt", "txt", b"notes")
            assert not created and retried["state"] == "failed"
        else:
            with pytest.raises(PrivateSessionError):
                store.read(public_id, item["id"])
        assert not store._path(item["id"], "raw").exists()


@pytest.mark.parametrize("suffix", ["txt", "md", "pdf", "docx"])
def test_four_real_local_formats(suffix: str) -> None:
    from src.infra.document_parser import extract_document

    text: str = "synthetic reference 42"
    data: bytes = text.encode()
    if suffix == "pdf":
        import fitz

        with fitz.open() as document:
            document.new_page().insert_text((30, 30), text)
            data = document.tobytes()
    elif suffix == "docx":
        from docx import Document

        document = Document()
        document.add_paragraph(text)
        output = io.BytesIO()
        document.save(output)
        data = output.getvalue()
    result = extract_document(data, suffix)
    assert text in result["text"]
    if suffix == "pdf":
        assert result["pages"][0]["page"] == 1


def test_scan_and_wrong_pdf_are_rejected() -> None:
    import fitz

    from src.core.run_options import WorkspaceError
    from src.infra.document_parser import extract_document

    with fitz.open() as document:
        document.new_page()
        data: bytes = document.tobytes()
    for data in (data, b"not a PDF"):
        with pytest.raises(WorkspaceError, match="document_invalid"):
            extract_document(data, "pdf")


def test_rag_full_attachment_bypasses_index_and_persists_reference(accounts: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.infra.exchange_context import completed_model
    from src.llm import llm_client

    public_id = conversation(accounts)
    item = ready(accounts, public_id)
    calls: list[list[dict]] = []
    async def provider(messages: list[dict], **kwargs: Any):
        calls.append(messages)
        completed_model.set(kwargs["model"])
        yield "ORANGE-17 [1]"
    monkeypatch.setattr(llm_client, "chat_stream_async", provider)
    monkeypatch.setattr(accounts.api, "get_retrieval_flow", lambda: pytest.fail("Attachments must not require an index"))
    turn_id = str(uuid.uuid4())
    result = accounts.browsers["alice"].get("/query/stream", params={"query":"What is the code?", "conversation_id":public_id, "client_turn_id":turn_id, "attachment_ids":[item["id"]]})
    assert result.status_code == 200 and '"event": "done"' in result.text
    assert "ORANGE-17" in json.dumps(calls)
    history = accounts.browsers["alice"].get(f"/conversations/{public_id}/turns").json()["items"]
    assert len(history) == 1 and history[0]["attachments"][0]["id"] == item["id"]
    assert history[0]["sources"][0]["source_kind"] == "attachment"
    assert accounts.browsers["alice"].delete(f"/conversations/{public_id}/attachments/{item['id']}").status_code == 204
    history = accounts.browsers["alice"].get(f"/conversations/{public_id}/turns").json()["items"]
    assert history[0]["attachments"][0]["deleted"] is True


def test_context_overflow_fails_before_model_and_history(accounts: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from config.settings import settings
    from src.llm import llm_client

    public_id = conversation(accounts)
    item = ready(accounts, public_id, "x" * 5000)
    monkeypatch.setattr(settings, "max_context_tokens", 1000)
    monkeypatch.setattr(llm_client, "chat_stream_async", lambda *a, **k: pytest.fail("oversized input must not reach model"))
    result = accounts.browsers["alice"].get("/query/stream", params={"query":"Summarize", "conversation_id":public_id, "attachment_ids":[item["id"]]})
    assert result.status_code == 422 and result.json()["detail"]["code"] == "context_too_large"
    assert accounts.browsers["alice"].get(f"/conversations/{public_id}/turns").json()["items"] == []


def test_attachment_output_ceiling_uses_remaining_context_without_changing_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    from config.settings import settings
    from src.core.attachments import attachment_output_limit, require_attachment_context
    from src.core.run_options import AttachmentSnapshot, RunOptions, WorkspaceError
    from src.utils import token_counter

    monkeypatch.setattr(settings, "max_context_tokens", 4096)
    monkeypatch.setattr(token_counter, "count_tokens", len)
    options = RunOptions("flash", "synthetic", attachments=(AttachmentSnapshot(str(uuid.uuid4()), "notes.txt", "a"*64, "FULL_FILE"),))
    messages = [{"role":"user", "content":"FULL_FILE"}]
    output = attachment_output_limit(messages, 4096, options)
    assert 256 <= output < 4096
    require_attachment_context(messages, output, options)
    assert messages[0]["content"] == "FULL_FILE" and settings.max_context_tokens == 4096
    with pytest.raises(WorkspaceError, match="context_too_large"):
        attachment_output_limit([{"role":"user", "content":"x"*4096}], 4096, options)


def test_attachment_special_token_spellings_are_counted_as_untrusted_text(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.core.attachments import attachment_output_limit, require_attachment_context
    from src.core.run_options import AttachmentSnapshot, RunOptions
    from src.utils import token_counter

    def encoding_rejected(text: str) -> int:
        raise ValueError("synthetic special token")
    monkeypatch.setattr(token_counter, "count_tokens", encoding_rejected)
    options = RunOptions("flash", "synthetic", attachments=(AttachmentSnapshot(str(uuid.uuid4()), "notes.txt", "a"*64, "<|endoftext|>"),))
    messages = [{"role":"user", "content":"<|endoftext|>"}]
    limit = attachment_output_limit(messages, 512, options)
    require_attachment_context(messages, limit, options)
    assert limit == 512


def test_agent_refuses_tool_before_side_effect_when_full_result_cannot_leave_answer_space(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from config.settings import settings
    from src.core.run_options import AttachmentSnapshot, RunOptions, scoped_run_options
    from src.llm import llm_client
    from src.utils import token_counter
    from tests.offline.test_agent_runtime import _runtime

    monkeypatch.setattr(settings, "max_context_tokens", 1000)
    monkeypatch.setattr(token_counter, "count_tokens", lambda text: 100)
    harness, _, tools = _runtime(tmp_path, max_token_budget=100_000, max_tool_result_length=100)
    tools.get_model_tools = lambda: []
    async def planner(*args: Any, **kwargs: Any) -> str:
        return '{"action":"tool_call","tool_name":"calculator","tool_params":{"expression":"2+2"}}'
    monkeypatch.setattr(llm_client, "chat_async", planner)
    monkeypatch.setattr(llm_client, "chat_stream_async", lambda *a, **k: pytest.fail("No final generation expected after preflight refusal"))
    options = RunOptions("flash", "synthetic", attachments=(AttachmentSnapshot(str(uuid.uuid4()), "notes.txt", "a"*64, "full data"),))
    async def run() -> None:
        with scoped_run_options(options):
            result = await harness.execute("preflight-files", "Compute from file")
        assert result.error_code == "context_too_large"
    asyncio.run(run())
    assert tools.calls == []


def test_short_attachment_can_use_calculator_with_default_context_and_bounded_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from config.settings import settings
    from src.core.run_options import AttachmentSnapshot, RunOptions, scoped_run_options
    from src.llm import llm_client
    from tests.offline.test_agent_runtime import _runtime

    monkeypatch.setattr(settings, "max_context_tokens", 4096)
    harness, _, tools = _runtime(tmp_path)
    tools.data = {"result":4}
    tools.get_model_tools = lambda: []
    plans: list[int] = []
    observed: list[list[dict]] = []
    async def planner(messages: list[dict], **kwargs: Any) -> str:
        plans.append(1)
        if len(plans) == 1:
            return '{"action":"tool_call","tool_name":"calculator","tool_params":{"expression":"2+2"}}'
        return '{"action":"final_answer"}'
    async def final(messages: list[dict], **kwargs: Any):
        observed.append(messages)
        yield "4 [1]"
    monkeypatch.setattr(llm_client, "chat_async", planner)
    monkeypatch.setattr(llm_client, "chat_stream_async", final)
    options = RunOptions("flash", "synthetic", attachments=(AttachmentSnapshot(str(uuid.uuid4()), "notes.txt", "a"*64, "Compute 2+2."),))
    async def run() -> None:
        with scoped_run_options(options):
            result = await harness.execute("attachment-calculation", "Compute from my file")
        assert not result.error_code and result.answer == "4 [1]"
    asyncio.run(run())
    assert len(tools.calls) == 1 and len(observed) == 1
    assert "Compute 2+2." in json.dumps(observed[0])
    assert '"result": 4' in " ".join(item.get("content", "") for item in observed[0])


def test_deleted_snapshot_cannot_commit_history(accounts: Any) -> None:
    from src.infra.exchange_context import ExchangeContext, exchange_context
    from src.security.request_authorization import ConversationConflict

    public_id = conversation(accounts)
    item = ready(accounts, public_id)
    with authorize(accounts.service, accounts.logins["alice"]):
        sid = accounts.store.owned_session(accounts.service.authenticate(accounts.logins["alice"].token).id, public_id, "rag", create=False)
        snapshot = accounts.store.read_snapshot(sid)
        from src.infra.attachment_store import AttachmentStore

        AttachmentStore(accounts.store).delete(public_id, item["id"])
        token = exchange_context.set(ExchangeContext(str(uuid.uuid4()), "question", attachments=({"id":item["id"],"name":item["name"],"checksum":item["checksum"]},)))
        try:
            with pytest.raises(ConversationConflict):
                from src.infra.session_store import SessionTurn

                accounts.store.append_turns_if_epoch(sid, snapshot.revision[0], [SessionTurn("user", "question"), SessionTurn("assistant", "late")])
        finally:
            exchange_context.reset(token)
        assert accounts.store.get_history(sid) == []


def test_knowledge_requires_manifest_and_owner(accounts: Any) -> None:
    alice = accounts.browsers["alice"]
    result = alice.get("/knowledge/documents")
    assert result.status_code == 503 and result.json()["detail"]["code"] == "knowledge_unavailable"
    assert result.headers["cache-control"] == "no-store"


def test_knowledge_projects_manifest_and_never_reads_source_path(accounts: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from src.api import knowledge
    from src.core.index_versions import IndexVersion
    from src.infra import chroma_clients
    from src.infra.index_catalog import IndexCatalog

    catalog = IndexCatalog(tmp_path / "manifests")
    manifest = IndexVersion.create([{"chunk_id":"doc-a_chunk0","doc_id":"doc-a","text":"safe indexed excerpt","embedding":[.1,.2],
                                     "metadata":{"source":"C:/private/handbook.md?token=PRIVATE","doc_id":"doc-a","chunk_index":0}}],
                                   parser="fixture",chunker="fixture",chunk_size=100,chunk_overlap=0,embedding_model="fixture")
    catalog.publish(manifest)
    monkeypatch.setattr(knowledge, "index_catalog", catalog)
    observed: list[dict[str, Any]] = []
    def chunks(**kwargs: Any) -> dict[str, Any]:
        observed.append(kwargs)
        return {"documents":["safe indexed excerpt"],"metadatas":[{"chunk_index":0}]}
    monkeypatch.setattr(chroma_clients, "get_chroma_client", lambda: SimpleNamespace(get_collection=lambda _: SimpleNamespace(get=chunks)))
    browser = accounts.browsers["alice"]
    page = browser.get("/knowledge/documents")
    assert page.status_code == 200 and "PRIVATE" not in page.text and "C:/private" not in page.text
    item = page.json()["items"][0]
    assert item["name"] == "handbook.md" and item["updated_at"] is None
    assert browser.post("/knowledge/documents/search", json={"title_query":"missing"}).json()["items"] == []
    assert browser.get("/knowledge/documents?cursor=bad").status_code == 422
    assert browser.get("/knowledge/documents/doc-a?version="+"0"*24).status_code == 409
    result = browser.get("/knowledge/documents/doc-a?version="+manifest.version_id)
    assert result.status_code == 200 and result.json()["text"] == "safe indexed excerpt" and result.json()["excerpt_only"] is True
    assert observed == [{"where":{"source_version":manifest.source_checksum("doc-a")},"include":["documents","metadatas"],"limit":21}]


def test_knowledge_excerpt_matches_actual_index_metadata(accounts: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from src.api import knowledge
    from src.core.index_versions import IndexVersion
    from src.core.indexing import IndexBuilderNode
    from src.core.ingestion import ChunkerNode
    from src.infra import chroma_clients
    from src.infra.index_catalog import IndexCatalog

    chunks: list[dict[str, Any]] = ChunkerNode().exec({
        "doc_id": "synthetic-doc", "text": "TAIL-EXCERPT-29", "metadata": {"source": "notes.txt", "extension": ".txt"},
    })
    for chunk in chunks:
        chunk["embedding"] = [.1, .2]
    manifest = IndexVersion.create(chunks, parser="fixture", chunker="fixture", chunk_size=512,
                                   chunk_overlap=50, embedding_model="fixture")
    stored = IndexBuilderNode._attach_provenance(chunks, manifest)
    assert "doc_id" not in stored[0]["metadata"]
    catalog = IndexCatalog(tmp_path / "excerpt-manifests")
    catalog.publish(manifest)
    monkeypatch.setattr(knowledge, "index_catalog", catalog)
    def read(**kwargs: Any) -> dict[str, Any]:
        matches = [chunk for chunk in stored if all(chunk["metadata"].get(key) == value for key, value in kwargs["where"].items())]
        return {"documents": [chunk["text"] for chunk in matches], "metadatas": [chunk["metadata"] for chunk in matches]}
    monkeypatch.setattr(chroma_clients, "get_chroma_client", lambda: SimpleNamespace(get_collection=lambda _: SimpleNamespace(get=read)))
    result = accounts.browsers["alice"].get(f"/knowledge/documents/synthetic-doc?version={manifest.version_id}")
    assert result.status_code == 200
    assert result.json()["text"] == "TAIL-EXCERPT-29"


@pytest.mark.parametrize("mode", ["json", "native"])
def test_agent_full_files_reach_planning_and_final_without_becoming_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    from types import SimpleNamespace

    from src.core.run_options import AttachmentSnapshot, RunOptions, scoped_run_options
    from src.llm import llm_client
    from src.llm.client import NativeChatResponse
    from tests.offline.test_agent_runtime import _runtime

    harness, memory, tools = _runtime(tmp_path, planner_mode=mode)
    tools.get_model_tools = lambda: []
    tools.get_model_tool_catalog = lambda: SimpleNamespace(tools=[])
    calls: list[str] = []
    async def planner(messages: list[dict], **kwargs: Any) -> str:
        calls.append(json.dumps(messages))
        return '{"action":"final_answer"}'
    async def native(messages: list[dict], tools: list[dict], **kwargs: Any) -> Any:
        await planner(messages)
        return NativeChatResponse(content="READY_TO_ANSWER")
    async def final(messages: list[dict], **kwargs: Any):
        calls.append(json.dumps(messages))
        yield "answer [1]"
    monkeypatch.setattr(llm_client, "chat_async", planner)
    monkeypatch.setattr(llm_client, "chat_with_tools_async", native)
    monkeypatch.setattr(llm_client, "chat_stream_async", final)
    options = RunOptions("flash", "fixture-flash", attachments=(AttachmentSnapshot(str(uuid.uuid4()), "notes.txt", "a"*64, "FULL_FILE_SENTINEL"),))
    async def run() -> None:
        with scoped_run_options(options):
            result = await harness.execute("attachment-agent", "Use my file")
        assert not result.error_code and result.answer == "answer [1]"
    asyncio.run(run())
    assert len(calls) == 2 and all("FULL_FILE_SENTINEL" in item for item in calls)
    assert "FULL_FILE_SENTINEL" not in str(memory.load_history("attachment-agent"))


def test_real_http_accept_poll_retry_capacity_and_parser_delete(accounts: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from src.api import attachments
    from src.security.user_auth import csrf_token

    public_id = conversation(accounts)
    path = f"/conversations/{public_id}/attachments"
    started: list[str] = []
    stopped: list[str] = []
    async def run() -> None:
        release = asyncio.Event()
        async def parser(data: bytes, suffix: str, **kwargs: Any) -> dict[str, Any]:
            started.append(data.decode())
            try:
                await release.wait()
                return {"text":data.decode(), "pages":[]}
            finally:
                stopped.append(data.decode())
        monkeypatch.setattr(attachments, "parse_document", parser)
        token = accounts.logins["alice"].token
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=accounts.api.app), base_url="https://workspace.test", cookies={"ragflow_login":token}, headers={"Origin":"https://workspace.test","X-CSRF-Token":csrf_token(token)}) as client:
            fields = {"client_upload_id":str(uuid.uuid4())}
            files = {"file":("notes.txt",b"first")}
            response = await client.post(path, data=fields, files=files)
            assert response.status_code == 202
            first = response.json()
            again = await client.post(path, data=fields, files=files)
            assert again.status_code == 202 and again.json()["id"] == first["id"]
            changed = await client.post(path, data=fields, files={"file":("notes.txt",b"changed")})
            assert changed.status_code == 409
            busy = await client.post(path, data={"client_upload_id":str(uuid.uuid4())}, files=files)
            assert busy.status_code == 503 and busy.json()["detail"]["code"] == "document_busy"
            release.set()
            for _ in range(40):
                detail = await client.get(path+"/"+first["id"])
                if detail.json()["state"] == "ready":
                    break
                await asyncio.sleep(.025)
            assert detail.json()["state"] == "ready" and started == ["first"]
            release.clear()
            response = await client.post(path, data={"client_upload_id":str(uuid.uuid4())}, files={"file":("second.txt",b"second")})
            assert response.status_code == 202
            second = response.json()
            for _ in range(40):
                if "second" in started:
                    break
                await asyncio.sleep(.025)
            assert (await client.delete(path+"/"+second["id"])).status_code == 204
            assert "second" in stopped
            assert (await client.get(path+"/"+second["id"])).status_code == 404
        await attachments.close_attachments()
    asyncio.run(run())


def test_failed_physical_cleanup_remains_in_owner_quota(accounts: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import closing

    from src.core.run_options import WorkspaceError
    from src.infra.attachment_store import OWNER_BYTES, AttachmentStore

    public_id = conversation(accounts)
    item = ready(accounts, public_id)
    store = AttachmentStore(accounts.store)
    original_unlink = Path.unlink
    def locked(path: Path, *args: Any, **kwargs: Any) -> None:
        if path.parent == store.root:
            raise PermissionError("synthetic locked file")
        original_unlink(path, *args, **kwargs)
    with authorize(accounts.service, accounts.logins["alice"]):
        with closing(accounts.store._connect()) as connection, connection:
            connection.execute("UPDATE conversation_attachments SET bytes=? WHERE id=?", (OWNER_BYTES, item["id"]))
        monkeypatch.setattr(Path, "unlink", locked)
        store.delete(public_id, item["id"])
        with pytest.raises(WorkspaceError, match="attachment_quota"):
            store.accept(public_id, str(uuid.uuid4()), "next.txt", "txt", b"next")
        monkeypatch.setattr(Path, "unlink", original_unlink)
        store.cleanup()
        assert store.accept(public_id, str(uuid.uuid4()), "next.txt", "txt", b"next")[1]


def test_failed_ready_commit_cleans_text_before_releasing_quota(accounts: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import sqlite3

    from src.infra.attachment_store import AttachmentStore

    public_id = conversation(accounts)
    store = AttachmentStore(accounts.store)
    with authorize(accounts.service, accounts.logins["alice"]):
        item, _ = store.accept(public_id, str(uuid.uuid4()), "notes.txt", "txt", b"notes")
        generation, _, _ = store.parse_input(public_id, item["id"])
        original_commit = store._commit
        def fail(*args: Any, **kwargs: Any) -> None:
            raise sqlite3.OperationalError("synthetic reader lock")
        monkeypatch.setattr(store, "_commit", fail)
        with pytest.raises(sqlite3.OperationalError):
            store.finish(public_id, item["id"], generation, {"text":"parsed secret", "pages":[]})
        assert store._path(item["id"], "text").exists()
        monkeypatch.setattr(store, "_commit", original_commit)
        store.finish(public_id, item["id"], generation, None, "document_invalid")
        assert store.read(public_id, item["id"])["state"] == "failed"
        assert not store._path(item["id"], "text").exists()
        assert not store._path(item["id"], "raw").exists()


@pytest.mark.parametrize("cancel_before_start", [False, True])
def test_parse_preparation_failure_or_early_cancel_releases_record_slot_and_registry(accounts: Any, monkeypatch: pytest.MonkeyPatch, cancel_before_start: bool) -> None:
    from src.api import attachments
    from src.infra.attachment_store import AttachmentStore
    from src.infra.document_parser import reserve_parser
    from src.security.request_authorization import AuthorizationRevoked

    public_id = conversation(accounts)
    store = AttachmentStore(accounts.store)
    async def run() -> None:
        with authorize(accounts.service, accounts.logins["alice"]):
            item, _ = store.accept(public_id, str(uuid.uuid4()), "notes.txt", "txt", b"notes")
            reservation = reserve_parser()
            def revoked(*args: Any) -> Any:
                raise AuthorizationRevoked("auth_required")
            monkeypatch.setattr(store, "parse_input", revoked)
            monkeypatch.setattr(store, "finish", revoked)
            task = asyncio.create_task(attachments._parse(store, public_id, item["id"], reservation))
            attachments._TASKS[(str(store.root), item["id"])] = task
            task.add_done_callback(lambda done: attachments._parse_done(done, store, item["id"], reservation))
            if cancel_before_start:
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await attachments.close_attachments()
            assert not attachments._TASKS and not attachments._FINALIZING
            assert store.read(public_id, item["id"])["state"] == "failed"
            # The global/owner slot can be acquired again.
            from src.infra.document_parser import release_parser

            release_parser(reserve_parser())
    asyncio.run(run())
