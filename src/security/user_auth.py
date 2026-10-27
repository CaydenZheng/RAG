"""Local account and revocable login-session boundary; not conversation ACLs."""

import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError

Role = Literal["admin", "member"]
SESSION_SECONDS: int = 8 * 60 * 60
MAX_USER_SESSIONS: int = 10
LOGIN_WINDOW_SECONDS: int = 5 * 60
MAX_LOGIN_BUCKETS: int = 4096
_USERNAME = re.compile(r"[a-z0-9][a-z0-9_.-]{2,63}\Z", re.ASCII)
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z", re.ASCII)


class AuthError(Exception):
    """A stable code only; never include credentials or database errors."""


@dataclass(frozen=True)
class User:
    id: str
    username: str
    role: Role
    must_change_password: bool


@dataclass(frozen=True)
class Login:
    user: User
    token: str = field(repr=False)


def normalize_username(value: str) -> str:
    normalized: str = value.lower()
    if not value.isascii() or not _USERNAME.fullmatch(normalized):
        raise AuthError("auth_invalid_request")
    return normalized


def validate_password(value: str) -> None:
    if not 12 <= len(value) <= 128 or "\x00" in value:
        raise AuthError("auth_password_invalid")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def csrf_token(token: str) -> str:
    """Session-bound token; only the browser receives this non-auth credential."""
    return hmac.new(
        token.encode("ascii"), b"ragrag/csrf/v1", hashlib.sha256
    ).hexdigest()


class AuthService:
    """Own password hashing, account state, SQLite sessions and bounded admission.

    This module never loads app settings, anonymous history, model or MCP state.
    Password work happens outside SQLite write transactions. Credential versions
    are checked again inside the transaction to close reset/disable/login races.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path: Path = db_path
        self._passwords: PasswordHasher = PasswordHasher(
            time_cost=3, memory_cost=65536, parallelism=4, type=Type.ID
        )
        self._dummy_hash: str = self._passwords.hash(secrets.token_urlsafe(32))
        self._password_slots = threading.BoundedSemaphore(2)
        self._rate_lock = threading.Lock()
        self._attempts: dict[str, tuple[float, int]] = {}
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection = sqlite3.connect(self._db_path, timeout=1)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            version: int = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise AuthError("auth_unavailable")
            tables: set[str] = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if (version == 0 and tables) or (
                version == 1 and tables != {"auth_users", "auth_sessions"}
            ):
                raise AuthError("auth_unavailable")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS auth_users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    role TEXT NOT NULL CHECK(role IN ('admin', 'member')),
                    password_hash TEXT NOT NULL,
                    credential_version INTEGER NOT NULL DEFAULT 1,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    must_change_password INTEGER NOT NULL DEFAULT 1
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES auth_users(id),
                    credential_version INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                )
            """)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS auth_sessions_user ON auth_sessions(user_id)"
            )
            connection.execute("PRAGMA user_version=1")

    @contextmanager
    def _password_work(self) -> Iterator[None]:
        # ponytail: two hash workers per process; distributed admission is out of scope.
        if not self._password_slots.acquire(blocking=False):
            raise AuthError("auth_busy")
        try:
            yield
        finally:
            self._password_slots.release()

    def _admit_login(self, username: str, source: str) -> None:
        now: float = time.monotonic()
        keys: tuple[tuple[str, int], ...] = (
            ("user:" + _digest(username), 10),
            ("ip:" + _digest(source), 30),
        )
        with self._rate_lock:
            self._attempts = {
                key: entry for key, entry in self._attempts.items() if entry[0] > now
            }
            new_count: int = sum(key not in self._attempts for key, _ in keys)
            if len(self._attempts) + new_count > MAX_LOGIN_BUCKETS:
                raise AuthError("auth_busy")
            if any(self._attempts.get(key, (0, 0))[1] >= limit for key, limit in keys):
                raise AuthError("auth_rate_limited")
            for key, _ in keys:
                deadline, count = self._attempts.get(
                    key, (now + LOGIN_WINDOW_SECONDS, 0)
                )
                self._attempts[key] = (deadline, count + 1)

    @staticmethod
    def _user(row: sqlite3.Row) -> User:
        return User(
            row["id"], row["username"], row["role"], bool(row["must_change_password"])
        )

    def _verify(self, hashed: str, password: str) -> bool:
        try:
            return self._passwords.verify(hashed, password)
        except (VerificationError, InvalidHashError):
            return False

    def create_user(self, username: str, password: str, *, role: Role) -> User:
        username = normalize_username(username)
        validate_password(password)
        if role not in {"admin", "member"}:
            raise AuthError("auth_invalid_request")
        with self._password_work():
            hashed: str = self._passwords.hash(password)
        user: User = User(str(uuid.uuid4()), username, role, True)
        try:
            with self._connection() as connection:
                connection.execute(
                    "INSERT INTO auth_users(id,username,role,password_hash) VALUES(?,?,?,?)",
                    (user.id, username, role, hashed),
                )
        except sqlite3.IntegrityError:
            raise AuthError("auth_account_exists") from None
        return user

    def login(self, username: str, password: str, source: str) -> Login:
        # The HTTP boundary bounds these before hashing; also protect direct callers.
        if len(username) > 64 or len(password) > 128:
            raise AuthError("auth_invalid_request")
        username = username.lower()
        with self._password_work():
            self._admit_login(username, source)
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT * FROM auth_users WHERE username=?", (username,)
                ).fetchone()
            valid: bool = self._verify(
                row["password_hash"] if row is not None else self._dummy_hash, password
            )
            if not valid or row is None or not row["enabled"]:
                raise AuthError("auth_invalid_credentials")
            token: str = secrets.token_urlsafe(32)
            now: float = time.time()
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT * FROM auth_users WHERE id=?", (row["id"],)
                ).fetchone()
                if (
                    not current["enabled"]
                    or current["credential_version"] != row["credential_version"]
                ):
                    raise AuthError("auth_invalid_credentials")
                connection.execute(
                    "DELETE FROM auth_sessions WHERE expires_at<=?", (now,)
                )
                connection.execute(
                    """
                    DELETE FROM auth_sessions WHERE token_hash IN (
                        SELECT token_hash FROM auth_sessions WHERE user_id=?
                        ORDER BY created_at DESC, token_hash DESC LIMIT -1 OFFSET ?
                    )
                """,
                    (row["id"], MAX_USER_SESSIONS - 1),
                )
                connection.execute(
                    "INSERT INTO auth_sessions VALUES(?,?,?,?,?)",
                    (
                        _digest(token),
                        row["id"],
                        row["credential_version"],
                        now,
                        now + SESSION_SECONDS,
                    ),
                )
        return Login(self._user(current), token)

    def _resolve(self, connection: sqlite3.Connection, token: str) -> sqlite3.Row:
        if not _TOKEN.fullmatch(token):
            raise AuthError("auth_required")
        row = connection.execute(
            """
            SELECT u.* FROM auth_sessions s JOIN auth_users u ON u.id=s.user_id
            WHERE s.token_hash=? AND s.expires_at>? AND u.enabled=1
                AND s.credential_version=u.credential_version
        """,
            (_digest(token), time.time()),
        ).fetchone()
        if row is None:
            raise AuthError("auth_required")
        return row

    def authenticate(self, token: str) -> User:
        with self._connection() as connection:
            return self._user(self._resolve(connection, token))

    def session_active(self, token: str) -> bool:
        try:
            return not self.authenticate(token).must_change_password
        except (AuthError, sqlite3.Error):
            return False

    @contextmanager
    def guard_session(self, token: str) -> Iterator[bool]:
        """Serialize a final commit with credential revocation, including CLI writes."""
        with self._connection() as connection:
            connection.execute("PRAGMA busy_timeout=0")
            try:
                connection.execute("BEGIN")
                user: User = self._user(self._resolve(connection, token))
                valid: bool = not user.must_change_password
            except (AuthError, sqlite3.Error):
                valid = False
            yield valid

    def logout(self, token: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM auth_sessions WHERE token_hash=?", (_digest(token),)
            )

    def change_password(
        self, token: str, current_password: str, new_password: str
    ) -> None:
        validate_password(new_password)
        if len(current_password) > 128:
            raise AuthError("auth_invalid_request")
        if current_password == new_password:
            raise AuthError("auth_password_invalid")
        with self._password_work():
            with self._connection() as connection:
                row = self._resolve(connection, token)
            self._admit_login(row["username"], "password:" + row["id"])
            if not self._verify(row["password_hash"], current_password):
                raise AuthError("auth_invalid_credentials")
            hashed: str = self._passwords.hash(new_password)
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = self._resolve(connection, token)
                if current["credential_version"] != row["credential_version"]:
                    raise AuthError("auth_required")
                connection.execute(
                    """
                    UPDATE auth_users SET password_hash=?, must_change_password=0,
                        credential_version=credential_version+1 WHERE id=?
                """,
                    (hashed, row["id"]),
                )
                connection.execute(
                    "DELETE FROM auth_sessions WHERE user_id=?", (row["id"],)
                )

    def set_enabled(self, username: str, enabled: bool) -> None:
        username = normalize_username(username)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE auth_users SET enabled=?, credential_version=credential_version+1
                WHERE username=?
            """,
                (int(enabled), username),
            )
            if cursor.rowcount != 1:
                raise AuthError("auth_account_not_found")
            connection.execute(
                """
                DELETE FROM auth_sessions WHERE user_id=(SELECT id FROM auth_users WHERE username=?)
            """,
                (username,),
            )

    def reset_password(self, username: str, password: str) -> None:
        username = normalize_username(username)
        validate_password(password)
        with self._password_work():
            hashed: str = self._passwords.hash(password)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE auth_users SET password_hash=?, must_change_password=1,
                    credential_version=credential_version+1 WHERE username=?
            """,
                (hashed, username),
            )
            if cursor.rowcount != 1:
                raise AuthError("auth_account_not_found")
            connection.execute(
                """
                DELETE FROM auth_sessions WHERE user_id=(SELECT id FROM auth_users WHERE username=?)
            """,
                (username,),
            )
