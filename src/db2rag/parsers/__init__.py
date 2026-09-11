"""拡張子から parser を割り当てる。symlink / 除外は呼び出し側。"""

from __future__ import annotations

from pathlib import Path

from db2rag.parsers.canonical import CanonicalDocument
from db2rag.parsers.csvxlsx import parse_csv, parse_xlsx
from db2rag.parsers.office import parse_docx, parse_html, parse_pdf, parse_pptx
from db2rag.parsers.txtmd import parse_md, parse_txt

EXCLUDED_DIRS = {".git", "node_modules", "__pycache__", "$RECYCLE.BIN"}
EXCLUDED_SUFFIXES = {".exe", ".dll", ".bin", ".zip", ".7z", ".tmp"}
MAX_FILE_BYTES = 500 * 1024 * 1024
MAX_FILES_PER_JOB = 10_000


def parse_file(path: Path) -> CanonicalDocument:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return parse_pdf(path)
    if suffix == ".docx":
        return parse_docx(path)
    if suffix == ".pptx":
        return parse_pptx(path)
    if suffix in {".html", ".htm"}:
        return parse_html(path)
    if suffix == ".xlsx":
        return parse_xlsx(path)
    if suffix == ".csv":
        return parse_csv(path)
    if suffix == ".md":
        return parse_md(path)
    if suffix in {".txt", ".text"}:
        return parse_txt(path)
    raise ValueError(f"ERR_FILE_UNSUPPORTED:{suffix}")


def mime_for(path: Path) -> str:
    mapping = {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".csv": "text/csv",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".html": "text/html",
        ".htm": "text/html",
    }
    return mapping.get(path.suffix.lower(), "application/octet-stream")
