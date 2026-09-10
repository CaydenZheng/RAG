"""Regression coverage for session identifiers and caller isolation."""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient


@dataclass
class SessionClients:
    client_a: TestClient
    client_b: TestClient
    rag_store: object
    agent_memory: object


@pytest.fixture
def session_clients(
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: Path,
    temporary_sessions,
) -> Iterator[SessionClients]:
    import tiktoken

    monkeypatch.setattr(
        tiktoken,
        "get_encoding",
        lambda name: SimpleNamespace(encode=lambda text: list(text)),
    )

    import app as api
    from src.agent import memory as memory_module
    from src.agent.memory import MemoryManager

    Path("logs").mkdir(exist_ok=True)
    agent_memory = MemoryManager(str(isolated_runtime / "agent-memory"))
    monkeypatch.setattr(memory_module, "memory_manager", agent_memory)

    class QueryFlow:
        async def run_async(self, shared: dict) -> None:
            if shared["session_id"]:
                temporary_sessions.add_turn(
                    shared["session_id"], "user", shared["query"]
                )
            shared.update(answer="answer", sources=[])

    class AgentFlow:
        def run(self, shared: dict) -> None:
            agent_memory.add_turn(
                shared["session_id"], "user", shared["user_message"]
            )
            shared.update(answer="answer", tool_calls=[], iterations=1)

    class AgentResetFlow:
        def run(self, shared: dict) -> None:
            shared["session_found"] = agent_memory.clear_session(
                shared["session_id"]
            )
            shared["answer"] = "Session reset."

    monkeypatch.setattr(api, "get_online_flow", QueryFlow)
    monkeypatch.setattr(api, "get_agent_flow", AgentFlow)
    monkeypatch.setattr(api, "get_agent_reset_flow", AgentResetFlow)

    client_a = TestClient(api.app)
    client_b = TestClient(api.app)
    try:
        yield SessionClients(client_a, client_b, temporary_sessions, agent_memory)
    finally:
        client_a.close()
        client_b.close()


def test_invalid_identity_cookie_is_replaced(session_clients: SessionClients) -> None:
    response = session_clients.client_a.get(
        "/health", headers={"cookie": "ragflow_client=attacker-chosen-invalid"}
    )

    assert response.status_code == 200
    replacement = response.headers["set-cookie"].split("=", 1)[1].split(";", 1)[0]
    assert replacement != "attacker-chosen-invalid"
    assert len(replacement) == 32


def test_https_identity_cookie_is_secure(session_clients: SessionClients) -> None:
    response = session_clients.client_a.get("https://testserver/health")

    assert response.status_code == 200
    assert "Secure" in response.headers["set-cookie"]


def test_rag_history_is_scoped_to_the_client_identity(
    session_clients: SessionClients,
) -> None:
    created = session_clients.client_a.post(
        "/query",
        json={"query": "client-a secret", "session_id": "shared-session"},
    )
    assert created.status_code == 200
    assert "ragflow_client=" in created.headers["set-cookie"]
    assert "HttpOnly" in created.headers["set-cookie"]
    assert "SameSite=strict" in created.headers["set-cookie"]

    own_history = session_clients.client_a.get("/session/shared-session")
    assert own_history.status_code == 200
    assert own_history.headers["cache-control"] == "no-store"
    assert own_history.json()["history"][0]["content"] == "client-a secret"

    foreign_history = session_clients.client_b.get("/session/shared-session")
    assert foreign_history.status_code == 404
    assert foreign_history.headers["cache-control"] == "no-store"
    assert (
        session_clients.client_a.cookies.get("ragflow_client")
        != session_clients.client_b.cookies.get("ragflow_client")
    )

    foreign_reset = session_clients.client_b.post(
        "/session/reset", params={"session_id": "shared-session"}
    )
    assert foreign_reset.status_code == 404

    second_created = session_clients.client_b.post(
        "/query",
        json={"query": "client-b secret", "session_id": "shared-session"},
    )
    assert second_created.status_code == 200
    second_history = session_clients.client_b.get("/session/shared-session")
    assert second_history.json()["history"][0]["content"] == "client-b secret"

    preserved = session_clients.client_a.get("/session/shared-session")
    assert preserved.status_code == 200
    assert preserved.json()["history"][0]["content"] == "client-a secret"
    storage_ids = session_clients.rag_store.list_sessions()
    assert "shared-session" not in storage_ids
    assert session_clients.client_a.cookies.get("ragflow_client") not in storage_ids[0]


def test_agent_history_is_scoped_to_the_client_identity(
    session_clients: SessionClients,
) -> None:
    session_clients.agent_memory.update_long_term("legacy global secret")
    created = session_clients.client_a.post(
        "/agent/chat",
        json={"message": "client-a secret", "session_id": "shared-agent"},
    )
    assert created.status_code == 200
    assert created.json()["error"] == "", created.json()
    assert created.json()["session_id"] == "shared-agent"

    own_memory = session_clients.client_a.get("/agent/memory/shared-agent")
    assert own_memory.status_code == 200, {
        "body": own_memory.text,
        "cookie": session_clients.client_a.cookies.get("ragflow_client"),
        "stored": [
            path.name
            for path in (session_clients.agent_memory.memory_dir / "sessions").iterdir()
        ],
    }
    assert own_memory.headers["cache-control"] == "no-store"
    assert "legacy global secret" not in own_memory.json()["long_term_memory"]
    assert own_memory.json()["history"][0]["content"] == "client-a secret"

    foreign_memory = session_clients.client_b.get("/agent/memory/shared-agent")
    assert foreign_memory.status_code == 404
    foreign_reset = session_clients.client_b.post(
        "/agent/reset", params={"session_id": "shared-agent"}
    )
    assert foreign_reset.status_code == 404

    preserved = session_clients.client_a.get("/agent/memory/shared-agent")
    assert preserved.status_code == 200
    assert preserved.json()["history"][0]["content"] == "client-a secret"

    own_reset = session_clients.client_a.post(
        "/agent/reset", params={"session_id": "shared-agent"}
    )
    assert own_reset.status_code == 200
    assert session_clients.client_a.get("/agent/memory/shared-agent").status_code == 404


@pytest.mark.parametrize(
    "session_id",
    [".", "..", "../escape", r"..\escape", "contains space", "会话", "x" * 65],
)
def test_http_endpoints_reject_invalid_session_ids(
    session_clients: SessionClients, session_id: str
) -> None:
    query = session_clients.client_a.post(
        "/query",
        json={"query": "probe", "session_id": session_id},
    )
    reset = session_clients.client_a.post(
        "/agent/reset", params={"session_id": session_id}
    )

    assert query.status_code == 422
    assert reset.status_code == 422


def test_every_http_session_entry_rejects_invalid_ids(
    session_clients: SessionClients,
) -> None:
    client = session_clients.client_a
    invalid_id = "bad$id"
    path_id = quote(invalid_id, safe="")

    responses = [
        client.post("/query", json={"query": "probe", "session_id": invalid_id}),
        client.get(
            "/query/stream", params={"query": "probe", "session_id": invalid_id}
        ),
        client.post("/session/reset", params={"session_id": invalid_id}),
        client.get(f"/session/{path_id}"),
        client.post(
            "/agent/chat",
            json={"message": "probe", "session_id": invalid_id},
        ),
        client.get(
            "/agent/chat/stream",
            params={"message": "probe", "session_id": invalid_id},
        ),
        client.post("/agent/reset", params={"session_id": invalid_id}),
        client.get(f"/agent/memory/{path_id}"),
    ]

    assert [response.status_code for response in responses] == [422] * len(responses)
    assert client.post("/session/reset", params={"session_id": ""}).status_code == 422
    assert client.post("/agent/reset", params={"session_id": ""}).status_code == 422
    assert client.post("/query", json={"query": "one shot"}).status_code == 200


def test_generated_agent_session_id_is_public_and_path_safe(
    session_clients: SessionClients,
) -> None:
    response = session_clients.client_a.post(
        "/agent/chat", json={"message": "probe"}
    )

    assert response.status_code == 200
    public_id = response.json()["session_id"]
    assert len(public_id) == 32
    assert public_id.isascii()
    assert public_id.isalnum()
    storage_ids = {
        path.name for path in (session_clients.agent_memory.memory_dir / "sessions").iterdir()
    }
    assert public_id not in storage_ids
    assert all(
        session_clients.client_a.cookies.get("ragflow_client") not in storage_id
        for storage_id in storage_ids
    )


def test_agent_stream_returns_public_id_without_leaking_storage_key(
    session_clients: SessionClients,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.agent.harness import agent_harness

    storage_ids: list[str] = []

    async def stream(session_id: str, message: str):
        storage_ids.append(session_id)
        yield f'data: {json.dumps({"done": True, "session_id": session_id})}\n\n'

    monkeypatch.setattr(agent_harness, "run_async_stream", stream)
    response = session_clients.client_a.get(
        "/agent/chat/stream",
        params={"message": "probe", "session_id": "public-agent"},
    )

    assert response.status_code == 200
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert events[-1]["session_id"] == "public-agent"
    assert storage_ids and storage_ids[0] != "public-agent"
    assert storage_ids[0] not in response.text


def test_rag_stream_returns_public_id_without_leaking_storage_key(
    session_clients: SessionClients,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app as api

    storage_ids: list[str] = []

    class RetrievalFlow:
        async def run_async(self, shared: dict) -> None:
            storage_ids.append(shared["session_id"])
            shared.update(context="context", sources=[])

    async def stream_answer(*args, **kwargs):
        yield "answer"

    monkeypatch.setattr(api, "get_retrieval_flow", RetrievalFlow)
    from src.core import generation

    monkeypatch.setattr(generation.llm_client, "chat_stream_async", stream_answer)
    response = session_clients.client_a.get(
        "/query/stream",
        params={"query": "probe", "session_id": "public-rag"},
    )

    assert response.status_code == 200
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert events[-1]["session_id"] == "public-rag"
    assert storage_ids and storage_ids[0] != "public-rag"
    assert storage_ids[0] not in response.text


def test_storage_boundaries_reject_invalid_session_ids(
    isolated_runtime: Path,
) -> None:
    from src.agent.memory import MemoryManager
    from src.infra.session_store import SessionStore

    memory_root = isolated_runtime / "agent-memory"
    manager = MemoryManager(str(memory_root))
    store = SessionStore(str(isolated_runtime / "sessions.db"))

    with pytest.raises(ValueError, match="session ID"):
        manager.load_history("../escaped")
    with pytest.raises(ValueError, match="session ID"):
        store.add_turn("../escaped", "user", "secret")

    assert not (memory_root / "escaped").exists()
