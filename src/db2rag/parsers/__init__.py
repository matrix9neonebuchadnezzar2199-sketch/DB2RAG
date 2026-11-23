"""拡張子から parser を割り当てる。symlink / 除外は呼び出し側。"""

from __future__ import annotations

from pathlib import Path

from db2rag.parsers.canonical import CanonicalDocument
from db2rag.parsers.csvxlsx import parse_csv, parse_xlsx
from db2rag.parsers.office import parse_docx, parse_html, parse_pdf, parse_pptx
from db2rag.parsers.txtmd import parse_md, parse_txt

EXCLUDED_DIRS = {".git", "node_modules", "__pycache__", "$RECYCLE.BIN"}
EXCLUDED_SUFFIXES = {".exe", ".dll", ".bin", ".zip", ".7z", ".tmp"}
MACRO_SUFFIXES = {".docm", ".xlsm", ".pptm"}
MAX_FILE_BYTES = 500 * 1024 * 1024
MAX_FILES_PER_JOB = 10_000
# utf-8 を先に試し、日本語の従来エンコーディングへ落とす。
_TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "cp932", "shift_jis", "euc_jp")


def read_text_with_fallback(path: Path) -> str:
    """Read a text file, trying Japanese encodings in order.

    Args:
        path: File to decode.

    Returns:
        Decoded text. A leading UTF-8 BOM is removed when ``utf-8-sig`` matches.

    Raises:
        ValueError: If every encoding fails. The message is
            ``ERR_FILE_ENCODING:{filename}``.
        OSError: If the file cannot be read.
    """
    data = path.read_bytes()
    errors: list[UnicodeDecodeError] = []
    for encoding in _TEXT_ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError as exc:
            errors.append(exc)
    message = f"ERR_FILE_ENCODING:{path.name}"
    if not errors:
        raise ValueError(message)
    raise ValueError(message) from errors[-1]


def parse_file(path: Path) -> CanonicalDocument:
    """Parse a supported local file into a canonical document.

    Args:
        path: File path. The suffix selects the parser.

    Returns:
        Parsed document.

    Raises:
        ValueError: If the suffix is a macro Office file
            (``ERR_FILE_MACRO:{suffix}``) or is unsupported
            (``ERR_FILE_UNSUPPORTED:{suffix}``).
    """
    suffix = path.suffix.lower()
    # マクロ付き Office は中身を開かずに拒否する。
    if suffix in MACRO_SUFFIXES:
        raise ValueError(f"ERR_FILE_MACRO:{suffix}")
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
    """Return the MIME type for a known suffix.

    Args:
        path: File path. Only the suffix is used.

    Returns:
        A MIME type. Unknown suffixes map to ``application/octet-stream``.
    """
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
