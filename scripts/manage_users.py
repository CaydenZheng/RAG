"""Trusted-local account administration; passwords never accepted as arguments."""

import argparse
import getpass
import sqlite3
import warnings
from pathlib import Path

from argon2.exceptions import Argon2Error

from src.security.user_auth import AuthError, AuthService


def _password() -> str:
    # Refuse getpass's echoed-stdin fallback, including redirected CI input.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        first: str = getpass.getpass("临时密码（12～128 字符，不回显）：")
        second: str = getpass.getpass("再次输入：")
    if first != second:
        raise AuthError("auth_password_mismatch")
    return first


def main() -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, required=True, help="明确指定认证专用 SQLite 路径"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("username")
    create.add_argument("--role", choices=("admin", "member"), default="member")
    for name in ("disable", "enable", "reset-password"):
        command = subparsers.add_parser(name)
        command.add_argument("username")
    args = parser.parse_args()
    try:
        if args.command != "create" and not args.db.is_file():
            raise AuthError("auth_account_not_found")
        service: AuthService = AuthService(args.db.resolve())
        if args.command == "create":
            service.create_user(args.username, _password(), role=args.role)
        elif args.command == "reset-password":
            service.reset_password(args.username, _password())
        else:
            service.set_enabled(args.username, args.command == "enable")
    except AuthError as exc:
        print("操作未完成：" + str(exc))
        return 1
    except (sqlite3.Error, Argon2Error, OSError, getpass.GetPassWarning, EOFError):
        print("操作未完成：请检查认证文件访问权限和安全终端。")
        return 1
    print("操作完成；本操作不迁移匿名数据，不代表业务数据隔离已完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
