"""Account-owned skill definitions/preferences in the existing session database."""

import hashlib
import sqlite3
import time
import uuid
from contextlib import closing
from typing import TYPE_CHECKING, Any

from src.core.run_options import (
    ModelChoice,
    RunOptions,
    SkillSnapshot,
    WorkspaceError,
    conversation_model,
    resolve_model,
)
from src.core.skills import (
    CREATOR_BODY,
    CREATOR_ID,
    MAX_CUSTOM_SKILLS,
    MAX_ENABLED_SKILL_TOKENS,
    SkillDocument,
)
from src.security.request_authorization import AuthorizationRevoked, PrivateSessionError

if TYPE_CHECKING:
    from src.infra.session_store import CommitPermit, SessionStore


def initialize_workspace_schema(connection: sqlite3.Connection) -> None:
    version = connection.execute("SELECT version FROM schema_migrations WHERE name='workspace_models_skills'").fetchone()
    if version is not None and version[0] != 1:
        raise RuntimeError("Unsupported workspace schema")
    statements: str = """
        CREATE TABLE IF NOT EXISTS workspace_preferences (
            owner_user_id TEXT PRIMARY KEY, default_model TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS skills (
            owner_user_id TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,
            description TEXT NOT NULL, body TEXT NOT NULL, content TEXT NOT NULL,
            version TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
            revision INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(owner_user_id,id), UNIQUE(owner_user_id,name)
        );
        CREATE TABLE IF NOT EXISTS skill_preferences (
            owner_user_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
            revision INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS creator_operations (
            owner_user_id TEXT NOT NULL, operation_id TEXT NOT NULL, created_at REAL NOT NULL,
            PRIMARY KEY(owner_user_id,operation_id)
        );
    """
    # executescript would commit the caller's migration transaction early.
    for statement in statements.split(";"):
        if statement.strip():
            connection.execute(statement)
    connection.execute("INSERT OR IGNORE INTO schema_migrations VALUES('workspace_models_skills',1,?)", (time.time(),))


class SkillStore:
    def __init__(self, store: "SessionStore") -> None:
        self.store: SessionStore = store

    def _commit(self, connection: sqlite3.Connection, can_commit: "CommitPermit | None" = None) -> None:
        if not self.store._commit_if_permitted(connection, can_commit):
            raise AuthorizationRevoked("auth_required")

    @staticmethod
    def _builtin(enabled: bool = False, revision: int = 0) -> dict[str, Any]:
        return {"id": CREATOR_ID, "name": CREATOR_ID,
                "description": "将导入资料整理为符合上传格式的技能文件；不自动安装。",
                "builtin": True, "enabled": enabled, "revision": revision,
                "version": hashlib.sha256(CREATOR_BODY.encode()).hexdigest(), "content": CREATOR_BODY}

    @staticmethod
    def _custom(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "name": row["name"], "description": row["description"],
                "builtin": False, "enabled": bool(row["enabled"]), "revision": row["revision"],
                "version": row["version"], "content": row["content"]}

    def _read(self, connection: sqlite3.Connection, owner: str, skill_id: str) -> dict[str, Any]:
        connection.row_factory = sqlite3.Row
        if skill_id == CREATOR_ID:
            row = connection.execute("SELECT enabled,revision FROM skill_preferences WHERE owner_user_id=?", (owner,)).fetchone()
            return self._builtin(bool(row[0]) if row else False, row[1] if row else 0)
        row = connection.execute("SELECT * FROM skills WHERE owner_user_id=? AND id=?", (owner, skill_id)).fetchone()
        if row is None:
            raise PrivateSessionError("private_session_not_found")
        return self._custom(row)

    def listing(self) -> list[dict[str, Any]]:
        owner: str = self.store._owner_id()
        with closing(self.store._connect()) as connection:
            connection.row_factory = sqlite3.Row
            result: list[dict[str, Any]] = [self._read(connection, owner, CREATOR_ID)]
            result.extend(self._custom(row) for row in connection.execute("SELECT * FROM skills WHERE owner_user_id=? ORDER BY name,id", (owner,)))
        return [{key: value for key, value in item.items() if key != "content"} for item in result]

    def detail(self, skill_id: str) -> dict[str, Any]:
        owner: str = self.store._owner_id()
        with closing(self.store._connect()) as connection:
            return self._read(connection, owner, skill_id)

    def upload(self, document: SkillDocument, *, can_commit: "CommitPermit | None" = None) -> dict[str, Any]:
        owner: str = self.store._owner_id()
        with closing(self.store._connect()) as connection, connection:
            if can_commit is not None:
                # Async uploads fail fast on contention; never keep a cancelled
                # worker waiting 30 seconds to acquire its first write lock.
                connection.execute("PRAGMA busy_timeout = 0")
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT id,version FROM skills WHERE owner_user_id=? AND name=?", (owner, document.name)).fetchone()
            if row is not None:
                if row[1] == document.checksum:
                    return self._read(connection, owner, row[0])
                raise WorkspaceError("skill_exists", 409)
            if connection.execute("SELECT COUNT(*) FROM skills WHERE owner_user_id=?", (owner,)).fetchone()[0] >= MAX_CUSTOM_SKILLS:
                raise WorkspaceError("skill_limit", 413)
            skill_id: str = str(uuid.uuid4())
            connection.execute("INSERT INTO skills(owner_user_id,id,name,description,body,content,version) VALUES(?,?,?,?,?,?,?)",
                               (owner, skill_id, document.name, document.description, document.body, document.content, document.checksum))
            self._commit(connection, can_commit)
            return self._read(connection, owner, skill_id)

    def update(self, skill_id: str, enabled: bool, revision: int) -> dict[str, Any]:
        owner: str = self.store._owner_id()
        with closing(self.store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            item = self._read(connection, owner, skill_id)
            if item["revision"] != revision:
                raise WorkspaceError("skill_conflict", 409)
            if skill_id == CREATOR_ID:
                connection.execute("INSERT INTO skill_preferences VALUES(?,?,?) ON CONFLICT(owner_user_id) DO UPDATE SET enabled=excluded.enabled,revision=excluded.revision", (owner, int(enabled), revision + 1))
            else:
                if enabled:
                    from src.utils.token_counter import count_tokens

                    bodies: list[str] = [row[0] for row in connection.execute("SELECT body FROM skills WHERE owner_user_id=? AND (enabled=1 OR id=?)", (owner, skill_id))]
                    if sum(count_tokens(body) for body in bodies) > MAX_ENABLED_SKILL_TOKENS:
                        raise WorkspaceError("skills_context_too_large")
                connection.execute("UPDATE skills SET enabled=?,revision=revision+1 WHERE owner_user_id=? AND id=?", (int(enabled), owner, skill_id))
            self._commit(connection)
            return self._read(connection, owner, skill_id)

    def delete(self, skill_id: str) -> None:
        owner: str = self.store._owner_id()
        with closing(self.store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._read(connection, owner, skill_id)
            if skill_id == CREATOR_ID:
                raise WorkspaceError("skill_builtin", 409)
            connection.execute("DELETE FROM skills WHERE owner_user_id=? AND id=?", (owner, skill_id))
            self._commit(connection)

    def preferences(self) -> dict[str, Any]:
        owner: str = self.store._owner_id()
        with closing(self.store._connect()) as connection:
            row = connection.execute("SELECT default_model,revision FROM workspace_preferences WHERE owner_user_id=?", (owner,)).fetchone()
        return {"default_model": row[0] if row else "flash", "revision": row[1] if row else 0}

    def set_preferences(self, choice: ModelChoice, revision: int) -> dict[str, Any]:
        resolve_model(choice)
        owner: str = self.store._owner_id()
        with closing(self.store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT revision FROM workspace_preferences WHERE owner_user_id=?", (owner,)).fetchone()
            if (row[0] if row else 0) != revision:
                raise WorkspaceError("skill_conflict", 409)
            connection.execute("INSERT INTO workspace_preferences VALUES(?,?,?) ON CONFLICT(owner_user_id) DO UPDATE SET default_model=excluded.default_model,revision=excluded.revision", (owner, choice, revision + 1))
            self._commit(connection)
        return {"default_model": choice, "revision": revision + 1}

    def snapshot(self, public_id: str | None, explicit: ModelChoice | None, *, agent: bool) -> RunOptions:
        owner: str = self.store._owner_id()
        with closing(self.store._connect()) as connection, connection:
            connection.execute("BEGIN")
            selected: str | None = None
            used: str | None = None
            if public_id:
                row = connection.execute("SELECT selected_model,last_model FROM conversations WHERE owner_user_id=? AND public_id=? AND deleted=0", (owner, public_id)).fetchone()
                if row is None:
                    raise PrivateSessionError("private_session_not_found")
                selected = row[0]
                used = row[1]
            preference = connection.execute("SELECT default_model FROM workspace_preferences WHERE owner_user_id=?", (owner,)).fetchone()
            choice: ModelChoice = explicit or conversation_model(selected, used, preference[0] if preference else "flash")
            skills: tuple[SkillSnapshot, ...] = tuple(
                SkillSnapshot(*row) for row in connection.execute("SELECT id,name,version,body FROM skills WHERE owner_user_id=? AND enabled=1 ORDER BY name,id", (owner,))
            ) if agent else ()
            return RunOptions(choice, resolve_model(choice), skills)

    def claim_creator(self, operation_id: str, *, can_commit: "CommitPermit | None" = None) -> None:
        owner: str = self.store._owner_id()
        with closing(self.store._connect()) as connection, connection:
            if can_commit is not None:
                connection.execute("PRAGMA busy_timeout = 0")
            connection.execute("BEGIN IMMEDIATE")
            if not self._read(connection, owner, CREATOR_ID)["enabled"]:
                raise WorkspaceError("creator_disabled", 409)
            # Correlation tombstones only; drafts/results are never persisted.
            connection.execute("DELETE FROM creator_operations WHERE owner_user_id=? AND created_at<?", (owner, time.time() - 86400))
            if connection.execute("SELECT COUNT(*) FROM creator_operations WHERE owner_user_id=?", (owner,)).fetchone()[0] >= 128:
                raise WorkspaceError("creator_limit", 429)
            try:
                connection.execute("INSERT INTO creator_operations VALUES(?,?,?)", (owner, operation_id, time.time()))
            except sqlite3.IntegrityError:
                raise WorkspaceError("creator_operation_unknown", 409) from None
            self._commit(connection, can_commit)
