"""実行時設定。環境変数は DB2RAG_ プレフィックス。"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def repo_root() -> Path:
    """src/db2rag からリポジトリルートを決める。"""
    return Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DB2RAG_", extra="ignore")

    data_dir: Path = Field(default_factory=lambda: repo_root() / "data")
    host: str = "127.0.0.1"
    api_port: int = 8781
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_path: str | None = None
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3307
    mysql_user: str = "db2rag_ro"
    mysql_password: str = "db2rag_ro"
    mysql_database: str = "hr"
    embedding_backend: str = "ruri"  # ruri | hash
    hf_offline: bool = False
    worker_id: str = "worker-local-1"
    allow_bind_all: bool = False

    def sqlite_path(self) -> Path:
        return self.data_dir / "db2rag.db"

    def models_dir(self) -> Path:
        return self.data_dir / "models" / "ruri-v3-310m"

    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    def secrets_key_path(self) -> Path:
        return self.data_dir / "secrets.key"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.models_dir(),
            self.uploads_dir(),
            self.exports_dir(),
            self.data_dir / "qdrant_storage",
        ):
            path.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir = Path(settings.data_dir)
    if not settings.data_dir.is_absolute():
        settings.data_dir = repo_root() / settings.data_dir
    settings.ensure_dirs()
    return settings
