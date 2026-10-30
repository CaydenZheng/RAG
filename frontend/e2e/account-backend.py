"""Real account/SQLite API for browser validation; no model or index startup."""

import asyncio
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    with TemporaryDirectory(prefix="ragrag-browser-account-") as directory:
        root = Path(directory)
        os.environ.update(
            AUTH_ENABLED="true",
            AUTH_ALLOW_LOOPBACK_HTTP="true",
            AUTH_ALLOWED_ORIGINS='["http://127.0.0.1:8002"]',
            AUTH_DB_PATH=str(root / "auth.db"),
            CHROMA_PERSIST_DIR=str(root / "chroma"),
            CACHE_DB_PATH=str(root / "cache.db"),
            OPENAI_API_KEY="browser-fixture-key",
            OPENAI_BASE_URL="http://127.0.0.1:9/v1",
            ADMIN_API_KEY="browser-admin-fixture-key",
        )
        from app import app
        from src.api.user_auth import open_user_auth

        asyncio.run(open_user_auth(app))
        service = app.state.user_auth
        service.create_user("browser-check", "initial-browser-password", role="member")
        login = service.login("browser-check", "initial-browser-password", "local")
        service.change_password(login.token, "initial-browser-password", "verified-browser-password")
        uvicorn.run(app, host="127.0.0.1", port=8002, lifespan="off")


if __name__ == "__main__":
    main()
