"""Account credentials are separate from anonymous conversation identifiers."""

import asyncio
import hashlib
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from src.security.user_auth import AuthError, AuthService, User

PASSWORD: str = "temporary-password-123"
NEW_PASSWORD: str = "changed-password-456"
ORIGIN: str = "https://workspace.test"


@pytest.fixture
def service(isolated_runtime: Path) -> AuthService:
    return AuthService(isolated_runtime / "auth.db")


@pytest.fixture
def client(service: AuthService, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from config.settings import settings
    from src.api.user_auth import (
        UserAuthBoundaryMiddleware,
        require_admin,
        require_user,
        router,
    )

    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_allowed_origins", (ORIGIN,))
    app: FastAPI = FastAPI()
    app.state.user_auth = service
    app.include_router(router)
    app.add_middleware(UserAuthBoundaryMiddleware)

    @app.get("/private")
    def private(user: User = Depends(require_user)) -> dict[str, str]:
        return {"id": user.id}

    @app.get("/admin")
    def admin(user: User = Depends(require_admin)) -> dict[str, str]:
        return {"id": user.id}

    return TestClient(app, base_url=ORIGIN)


def sign_in(
    client: TestClient, username: str = "alice", password: str = PASSWORD
) -> Any:
    return client.post(
        "/auth/login",
        json={"username": username, "password": password},
        headers={"Origin": ORIGIN},
    )


def test_account_login_and_logout(isolated_runtime: Path) -> None:
    from src.security.user_auth import AuthError, AuthService

    service = AuthService(isolated_runtime / "auth.db")
    user = service.create_user("Alice", "temporary-password-123", role="member")
    login = service.login("alice", "temporary-password-123", "127.0.0.1")
    assert login.user.id == user.id
    assert login.user.must_change_password
    assert service.authenticate(login.token).id == user.id
    service.logout(login.token)
    with pytest.raises(AuthError, match="auth_required"):
        service.authenticate(login.token)


def test_passwords_and_tokens_are_not_stored_or_represented(
    service: AuthService, isolated_runtime: Path
) -> None:
    service.create_user("alice", PASSWORD, role="member")
    login = service.login("alice", PASSWORD, "local")
    with sqlite3.connect(isolated_runtime / "auth.db") as connection:
        password_hash = connection.execute(
            "SELECT password_hash FROM auth_users"
        ).fetchone()[0]
        token_hash = connection.execute(
            "SELECT token_hash FROM auth_sessions"
        ).fetchone()[0]
    assert password_hash.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert token_hash == hashlib.sha256(login.token.encode()).hexdigest()
    assert PASSWORD not in password_hash
    assert login.token not in repr(login)


@pytest.mark.parametrize(
    "operation", ["logout", "reset", "disable", "change", "expire"]
)
def test_revocation_and_expiry(
    service: AuthService, isolated_runtime: Path, operation: str
) -> None:
    service.create_user("alice", PASSWORD, role="member")
    first = service.login("alice", PASSWORD, "local")
    second = service.login("alice", PASSWORD, "local")
    if operation == "logout":
        service.logout(first.token)
        assert service.authenticate(second.token).id == first.user.id
    elif operation == "reset":
        service.reset_password("alice", NEW_PASSWORD)
        assert service.login("alice", NEW_PASSWORD, "local").user.must_change_password
    elif operation == "disable":
        service.set_enabled("alice", False)
        with pytest.raises(AuthError, match="auth_invalid_credentials"):
            service.login("alice", PASSWORD, "local")
        service.set_enabled("alice", True)
    elif operation == "change":
        service.change_password(first.token, PASSWORD, NEW_PASSWORD)
        assert not service.login(
            "alice", NEW_PASSWORD, "local"
        ).user.must_change_password
    else:
        with sqlite3.connect(isolated_runtime / "auth.db") as connection:
            connection.execute("UPDATE auth_sessions SET expires_at=0")
    with pytest.raises(AuthError, match="auth_required"):
        service.authenticate(first.token)
    if operation != "logout":
        with pytest.raises(AuthError, match="auth_required"):
            service.authenticate(second.token)


@pytest.mark.parametrize(
    "username,password",
    [("missing", PASSWORD), ("alice", "wrong-password"), ("disabled", PASSWORD)],
)
def test_invalid_login_is_indistinguishable(
    client: TestClient, service: AuthService, username: str, password: str
) -> None:
    service.create_user("alice", PASSWORD, role="member")
    service.create_user("disabled", PASSWORD, role="member")
    service.set_enabled("disabled", False)
    response = sign_in(client, username, password)
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "auth_invalid_credentials"
    assert response.headers["cache-control"] == "no-store"
    assert password not in response.text
    assert username not in response.text


def test_cookie_csrf_initial_change_and_role_boundary(
    client: TestClient, service: AuthService
) -> None:
    service.create_user("alice", PASSWORD, role="member")
    response = sign_in(client)
    assert response.status_code == 200
    cookie: str = response.headers["set-cookie"]
    for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/", "Max-Age=28800"):
        assert flag in cookie
    assert "token" not in response.json()["user"]
    assert client.get("/private").status_code == 403
    csrf: str = client.get("/auth/me").json()["csrf_token"]
    changed = client.post(
        "/auth/password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
    )
    assert changed.status_code == 204
    assert client.get("/auth/me").status_code == 401
    assert sign_in(client, password=NEW_PASSWORD).status_code == 200
    assert client.get("/private").status_code == 200
    assert client.get("/admin").status_code == 403
    csrf = client.get("/auth/me").json()["csrf_token"]
    assert (
        client.post(
            "/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}
        ).status_code
        == 204
    )
    assert client.get("/auth/me").status_code == 401


def test_admin_helper_does_not_bypass_initial_password_change(
    client: TestClient, service: AuthService
) -> None:
    service.create_user("alice", PASSWORD, role="admin")
    sign_in(client)
    assert client.get("/admin").status_code == 403
    token: str = client.cookies["ragflow_login"]
    service.change_password(token, PASSWORD, NEW_PASSWORD)
    sign_in(client, password=NEW_PASSWORD)
    assert client.get("/admin").status_code == 200


@pytest.mark.parametrize("path", ["/auth/login", "/auth/logout", "/auth/password"])
@pytest.mark.parametrize(
    "origin", [None, "null", "https://attacker.test", ORIGIN + ".attacker.test"]
)
def test_mutations_require_exact_origin(
    client: TestClient, path: str, origin: str | None
) -> None:
    headers: dict[str, str] = {"Origin": origin} if origin else {}
    response = client.post(path, json={}, headers=headers)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "auth_csrf_rejected"


@pytest.mark.parametrize("header", [None, "wrong", "非ASCII", "a" * 64])
def test_mutations_require_session_bound_csrf(
    client: TestClient, service: AuthService, header: str | None
) -> None:
    service.create_user("alice", PASSWORD, role="member")
    sign_in(client)
    headers: dict[str, str] = {"Origin": ORIGIN}
    # HTTPX enforces ASCII on headers; exercise non-ASCII using raw UTF-8 bytes.
    if header is not None:
        headers["X-CSRF-Token"] = header if header.isascii() else "\xe9".encode("utf-8")
    response = client.post("/auth/logout", headers=headers)
    assert response.status_code == 403
    assert client.get("/auth/me").status_code == 200


@pytest.mark.parametrize("cookie", [None, "forged", "a" * 43, "a" * 128])
def test_anonymous_and_forged_credentials_never_authenticate(
    client: TestClient, cookie: str | None
) -> None:
    client.cookies.set("ragflow_client", "b" * 32)
    if cookie:
        client.cookies.set("ragflow_login", cookie)
    assert client.get("/auth/me").status_code == 401
    assert client.get("/private").status_code == 401


@pytest.mark.parametrize(
    "content",
    [
        b'{"username":"alice","password":"secret","owner":"admin"}',
        b'{"username":"alice","password":"secret","password":"other"}',
        b'{"username":false,"password":"secret"}',
        b'"secret"',
        b"{broken",
        b'{"username":"alice","password":"\\ud800"}',
    ],
)
def test_invalid_body_never_echoes_input(client: TestClient, content: bytes) -> None:
    response = client.post(
        "/auth/login",
        content=content,
        headers={"Origin": ORIGIN, "Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert "secret" not in response.text and "alice" not in response.text


def test_oversized_chunked_body_and_query_rejected(client: TestClient) -> None:
    response = client.post(
        "/auth/login",
        content=iter([b"x" * 2048] * 3),
        headers={"Origin": ORIGIN, "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    response = client.post(
        "/auth/login?password=private", json={}, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 422 and "private" not in response.text


def test_auth_disabled_or_not_initialized_fails_closed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from config.settings import settings

    monkeypatch.setattr(settings, "auth_enabled", False)
    assert sign_in(client).status_code == 503
    monkeypatch.setattr(settings, "auth_enabled", True)
    client.app.state.user_auth = None
    assert sign_in(client).status_code == 503


def test_http_is_explicit_and_loopback_only(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from config.settings import settings

    response = client.get("http://workspace.test/auth/me")
    assert response.status_code == 403
    monkeypatch.setattr(settings, "auth_allow_loopback_http", True)
    assert client.get("http://workspace.test/auth/me").status_code == 403
    local = TestClient(
        client.app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 1234)
    )
    assert local.get("/auth/me").status_code == 401


def test_login_rotates_cookie_and_invalidates_old_session(
    client: TestClient, service: AuthService
) -> None:
    service.create_user("alice", PASSWORD, role="member")
    sign_in(client)
    old: str = client.cookies["ragflow_login"]
    sign_in(client)
    assert client.cookies["ragflow_login"] != old
    with pytest.raises(AuthError, match="auth_required"):
        service.authenticate(old)


def test_login_limits_both_account_and_source(service: AuthService) -> None:
    # Exercise the real admission boundary without repeating expensive hash work.
    for index in range(10):
        service._admit_login("missing", str(index))
    with pytest.raises(AuthError, match="auth_rate_limited"):
        service.login("missing", PASSWORD, "new-ip")
    for index in range(30):
        service._admit_login(str(index), "same-ip")
    with pytest.raises(AuthError, match="auth_rate_limited"):
        service.login("new-account", PASSWORD, "same-ip")


def test_limiter_capacity_and_expiry(
    service: AuthService, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.security import user_auth

    monkeypatch.setattr(user_auth, "MAX_LOGIN_BUCKETS", 2)
    service._admit_login("one", "local")
    with pytest.raises(AuthError, match="auth_busy"):
        service._admit_login("two", "local")
    for key in service._attempts:
        service._attempts[key] = (0, 1)
    service._admit_login("two", "local")
    assert len(service._attempts) == 2


def test_password_worker_capacity_is_bounded(service: AuthService) -> None:
    with service._password_work(), service._password_work():
        with pytest.raises(AuthError, match="auth_busy"):
            service.login("missing", PASSWORD, "local")


@pytest.mark.parametrize("action", ["reset", "disable"])
def test_login_rechecks_credential_version_after_verify(
    service: AuthService, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    service.create_user("alice", PASSWORD, role="member")
    original = service._verify

    def racing_verify(hashed: str, password: str) -> bool:
        valid: bool = original(hashed, password)
        if action == "reset":
            service.reset_password("alice", NEW_PASSWORD)
        else:
            service.set_enabled("alice", False)
        return valid

    monkeypatch.setattr(service, "_verify", racing_verify)
    with pytest.raises(AuthError, match="auth_invalid_credentials"):
        service.login("alice", PASSWORD, "local")


def test_change_password_rechecks_logout_after_verify(
    service: AuthService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.create_user("alice", PASSWORD, role="member")
    token: str = service.login("alice", PASSWORD, "local").token
    original = service._verify

    def racing_verify(hashed: str, password: str) -> bool:
        valid: bool = original(hashed, password)
        service.logout(token)
        return valid

    monkeypatch.setattr(service, "_verify", racing_verify)
    with pytest.raises(AuthError, match="auth_required"):
        service.change_password(token, PASSWORD, NEW_PASSWORD)


def test_account_names_password_policy_and_duplicates(service: AuthService) -> None:
    user: User = service.create_user("Alice", PASSWORD, role="member")
    assert user.username == "alice"
    with pytest.raises(AuthError, match="auth_account_exists"):
        service.create_user("ALICE", PASSWORD, role="member")
    for invalid in ("../alice", " alice", "ab", "中文名", "a" * 65):
        with pytest.raises(AuthError, match="auth_invalid_request"):
            service.create_user(invalid, PASSWORD, role="member")
    for invalid in ("short", "a" * 129, "x" * 12 + "\x00"):
        with pytest.raises(AuthError, match="auth_password_invalid"):
            service.create_user("other", invalid, role="member")


def test_schema_rejects_unrelated_database_and_newer_version(
    isolated_runtime: Path,
) -> None:
    path: Path = isolated_runtime / "unrelated.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sessions(content TEXT)")
    with pytest.raises(AuthError, match="auth_unavailable"):
        AuthService(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall() == [("sessions",)]


def test_unknown_schema_version_rejected(
    service: AuthService, isolated_runtime: Path
) -> None:
    path: Path = isolated_runtime / "auth.db"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=99")
    with pytest.raises(AuthError, match="auth_unavailable"):
        AuthService(path)


def test_session_survives_service_restart(
    service: AuthService, isolated_runtime: Path
) -> None:
    service.create_user("alice", PASSWORD, role="member")
    token: str = service.login("alice", PASSWORD, "local").token
    restored = AuthService(isolated_runtime / "auth.db")
    assert restored.authenticate(token).username == "alice"


def test_storage_errors_are_sanitized(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, service: AuthService
) -> None:
    def broken(*args: Any) -> None:
        raise sqlite3.OperationalError("private-database-path-secret")

    monkeypatch.setattr(service, "authenticate", broken)
    response = client.get("/auth/me")
    assert response.status_code == 503
    assert "private-database-path-secret" not in response.text


def test_disabled_startup_does_not_create_database(isolated_runtime: Path) -> None:
    from src.api.user_auth import open_user_auth

    application: FastAPI = FastAPI()
    asyncio.run(open_user_auth(application))
    assert not (isolated_runtime / "data/auth.db").exists()
    assert application.state.user_auth is None


def test_account_creation_concurrency_is_atomic(service: AuthService) -> None:
    def create() -> str:
        try:
            return service.create_user("alice", PASSWORD, role="member").id
        except AuthError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results: list[str] = list(pool.map(lambda _: create(), range(2)))
    assert results.count("auth_account_exists") == 1
    assert len(set(results)) == 2


def test_auth_query_redacted_from_access_log(
    isolated_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tiktoken

    monkeypatch.setattr(
        tiktoken,
        "get_encoding",
        lambda name: SimpleNamespace(encode=lambda text: list(text)),
    )
    from app import _OAUTH_CALLBACK_ACCESS_LOG_FILTER

    record = logging.LogRecord(
        "uvicorn.access",
        20,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("local", "POST", "/auth/login?password=private", "1.1", 422),
        None,
    )
    assert _OAUTH_CALLBACK_ACCESS_LOG_FILTER.filter(record)
    assert "private" not in record.getMessage()


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "null",
        "https://workspace.test/",
        "https://workspace.test/path",
        "https://name:password@workspace.test",
        "https://workspace.test?token=private",
        "https://*.test",
        "http://192.168.0.1:8000",
        "http://localhost:8000",
        "https://workspace.test:0",
        "https://workspace.test:wrong",
    ],
)
def test_invalid_origin_configuration_rejected(origin: str) -> None:
    from pydantic import ValidationError

    from config.settings import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None, AUTH_ENABLED=True, AUTH_ALLOWED_ORIGINS=(origin,))


def test_origin_configuration_defaults_and_explicit_local_http() -> None:
    from pydantic import ValidationError

    from config.settings import Settings

    assert not Settings(_env_file=None).auth_enabled
    with pytest.raises(ValidationError):
        Settings(_env_file=None, AUTH_ENABLED=True)
    configured = Settings(
        _env_file=None,
        AUTH_ENABLED=True,
        AUTH_ALLOW_LOOPBACK_HTTP=True,
        AUTH_ALLOWED_ORIGINS=("http://127.0.0.1:8000", "http://localhost:5173"),
    )
    assert configured.auth_allow_loopback_http


def test_explicit_auth_startup_and_failure_are_safe(
    isolated_runtime: Path, monkeypatch: pytest.MonkeyPatch, temporary_sessions: Any
) -> None:
    from config.settings import settings
    from src.api.user_auth import open_user_auth

    application: FastAPI = FastAPI()
    monkeypatch.setattr(settings, "auth_enabled", True)
    path: Path = isolated_runtime / "data/auth.db"
    monkeypatch.setattr(settings, "auth_db_path", str(path))
    asyncio.run(open_user_auth(application))
    assert isinstance(application.state.user_auth, AuthService)
    assert Path(temporary_sessions._db_path).is_relative_to(isolated_runtime)
    assert path.is_file()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=99")
    with pytest.raises(RuntimeError, match="Account storage initialization failed"):
        asyncio.run(open_user_auth(application))
    assert application.state.user_auth is None


def test_session_capacity_evicts_oldest(
    service: AuthService, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.security import user_auth

    monkeypatch.setattr(user_auth, "MAX_USER_SESSIONS", 2)
    service.create_user("alice", PASSWORD, role="member")
    first = service.login("alice", PASSWORD, "local")
    second = service.login("alice", PASSWORD, "local")
    third = service.login("alice", PASSWORD, "local")
    with pytest.raises(AuthError, match="auth_required"):
        service.authenticate(first.token)
    assert service.authenticate(second.token).id == service.authenticate(third.token).id


def test_cli_passwords_are_prompted_and_never_printed(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import sys

    from scripts import manage_users

    path: Path = isolated_runtime / "cli-auth.db"
    monkeypatch.setattr(manage_users.getpass, "getpass", lambda _: PASSWORD)
    monkeypatch.setattr(
        sys, "argv", ["manage_users", "--db", str(path), "create", "alice"]
    )
    assert manage_users.main() == 0
    monkeypatch.setattr(
        sys, "argv", ["manage_users", "--db", str(path), "disable", "alice"]
    )
    assert manage_users.main() == 0
    assert PASSWORD not in capsys.readouterr().out
    with pytest.raises(AuthError, match="auth_invalid_credentials"):
        AuthService(path).login("alice", PASSWORD, "local")


def test_cli_refuses_echoed_stdin(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import sys
    import warnings

    from scripts import manage_users

    def insecure_prompt(prompt: str) -> str:
        warnings.warn("cannot suppress echo", manage_users.getpass.GetPassWarning)
        pytest.fail("Must not fall back to echoed password input")

    monkeypatch.setattr(manage_users.getpass, "getpass", insecure_prompt)
    monkeypatch.setattr(
        sys,
        "argv",
        ["manage_users", "--db", str(isolated_runtime / "cli.db"), "create", "alice"],
    )
    assert manage_users.main() == 1
    assert "cannot suppress echo" not in capsys.readouterr().out


def test_cross_site_and_duplicate_origin_rejected(client: TestClient) -> None:
    response = client.post(
        "/auth/login",
        json={},
        headers={"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 403
    response = client.post(
        "/auth/login", json={}, headers=[("Origin", ORIGIN), ("Origin", ORIGIN)]
    )
    assert response.status_code == 403


def test_auth_on_mounted_application_cannot_bypass_boundary(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from config.settings import settings

    parent: FastAPI = FastAPI()
    parent.mount("/nested", client.app)
    mounted: TestClient = TestClient(parent, base_url=ORIGIN)
    monkeypatch.setattr(settings, "auth_enabled", False)
    assert mounted.get("/nested/auth/me").status_code == 503
    monkeypatch.setattr(settings, "auth_enabled", True)
    assert mounted.get("http://workspace.test/nested/auth/me").status_code == 403
