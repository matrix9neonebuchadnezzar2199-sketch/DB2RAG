"""DB2RAG — local persistent RAG conversion tool."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("db2rag")
except PackageNotFoundError:
    __version__ = "0.2.0"
