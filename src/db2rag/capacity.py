"""容量 quick 見積。係数 1.6 は設計書の初期値。"""

from __future__ import annotations

from db2rag.constants import CAPACITY_QUICK_FACTOR, TOKEN_CHAR_RATIO


def estimate_from_bytes(extracted_bytes: int, kind: str = "file") -> dict[str, float | int]:
    chars = extracted_bytes
    if kind == "mysql":
        child_target = 380
    elif kind == "table":
        child_target = 250
    else:
        child_target = 400
    est_chunks = max(1, int(chars / max(child_target, 1)))
    vector_bytes = est_chunks * 768 * 4
    payload_bytes = est_chunks * 800
    storage = int((vector_bytes + payload_bytes) * CAPACITY_QUICK_FACTOR)
    return {
        "extracted_bytes": extracted_bytes,
        "est_chunks": est_chunks,
        "est_storage_bytes": storage,
        "token_estimate": int(chars / TOKEN_CHAR_RATIO),
        "factor": CAPACITY_QUICK_FACTOR,
    }
