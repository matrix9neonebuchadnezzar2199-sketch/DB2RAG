"""DB2RAG — local persistent RAG conversion tool."""

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

try:
    __version__ = version("db2rag")
except PackageNotFoundError:
    _project = Path(__file__).resolve().parents[2] / "pyproject.toml"
    __version__ = str(tomllib.loads(_project.read_text(encoding="utf-8"))["project"]["version"])
