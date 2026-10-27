"""Exact web origins and explicit loopback-only HTTP development transport."""

from ipaddress import ip_address
from urllib.parse import urlsplit


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def validate_origins(origins: tuple[str, ...], allow_http: bool) -> None:
    if len(origins) > 16 or len(set(origins)) != len(origins):
        raise ValueError("AUTH_ALLOWED_ORIGINS must contain at most 16 unique origins")
    for origin in origins:
        try:
            parsed = urlsplit(origin)
            valid: bool = (
                parsed.scheme in {"https", "http"}
                and bool(parsed.hostname)
                and parsed.path == ""
                and not parsed.query
                and not parsed.fragment
                and parsed.username is None
                and parsed.password is None
                and parsed.port != 0
                and origin == f"{parsed.scheme}://{parsed.netloc}"
                and "*" not in origin
                and not any(char.isspace() for char in origin)
                and (
                    parsed.scheme == "https"
                    or (allow_http and is_loopback(parsed.hostname or ""))
                )
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("AUTH_ALLOWED_ORIGINS contains an invalid origin")
