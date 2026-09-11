"""MySQL パスワード等の局所暗号化。鍵は data/secrets.key。"""

from __future__ import annotations

from pathlib import Path

from cryptography.fernet import Fernet


def load_or_create_key(path: Path) -> Fernet:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return Fernet(path.read_bytes())
    key = Fernet.generate_key()
    path.write_bytes(key)
    try:
        path.chmod(0o600)
    except OSError:
        # Windows では POSIX 権限を付けられないことがある
        pass
    return Fernet(key)


def encrypt_secret(fernet: Fernet, plaintext: str) -> bytes:
    return fernet.encrypt(plaintext.encode("utf-8"))


def decrypt_secret(fernet: Fernet, ciphertext: bytes) -> str:
    return fernet.decrypt(ciphertext).decode("utf-8")
