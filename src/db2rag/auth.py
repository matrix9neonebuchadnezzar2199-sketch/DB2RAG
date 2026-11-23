"""API キー照合と、非 loopback 起動の拒否。"""

from __future__ import annotations

import hmac

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def configured_api_key(value: str | None) -> str | None:
    """空白だけの鍵は未設定として扱う。"""
    if value is None:
        return None
    text = value.strip()
    return text or None


def is_loopback(host: str) -> bool:
    return host.strip().lower() in LOOPBACK_HOSTS


def require_api_key_for_bind(host: str, api_key: str | None) -> None:
    """loopback 以外を鍵なしで開けない。

    Raises:
        RuntimeError: 非 loopback で ``DB2RAG_API_KEY`` が空のとき。
    """
    if is_loopback(host):
        return
    if configured_api_key(api_key) is None:
        raise RuntimeError("DB2RAG_API_KEY is required when binding non-loopback host")


def resolve_bind_host(requested: str, *, allow_bind_all: bool) -> str:
    """``allow_bind_all`` が無いときは loopback に固定する。"""
    if allow_bind_all:
        return requested
    return "127.0.0.1"


def bearer_matches(authorization: str | None, expected: str) -> bool:
    """Bearer トークンを定数時間で比べる。長さが違っても False。"""
    if not authorization or not authorization.startswith("Bearer "):
        return False
    token = authorization[7:]
    return hmac.compare_digest(token, expected)


def path_needs_auth(path: str, *, api_key: str | None, host: str) -> bool:
    """鍵があるとき、Dify 検索は常に、LAN では health 以外の API も要求する。"""
    if configured_api_key(api_key) is None:
        return False
    if not path.startswith("/api/v1/"):
        return False
    if path == "/api/v1/health":
        return False
    if path == "/api/v1/dify/retrieval":
        return True
    return not is_loopback(host)
