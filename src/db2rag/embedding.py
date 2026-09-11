"""Ruri-v3-310m FP32。量子化への自動降格はしない。"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

import numpy as np

from db2rag.constants import (
    EMBED_DIM,
    GOLDEN_DOC_TEXT,
    PREFIX_DOC,
    PREFIX_QUERY,
    RURI_MODEL_ID,
    RURI_REVISION,
)


class EmbeddingError(RuntimeError):
    pass


class Embedder:
    def __init__(self, *, backend: str, models_dir: Path, hf_offline: bool = False) -> None:
        self.backend = backend
        self.models_dir = models_dir
        self.hf_offline = hf_offline
        self._model: Any = None
        self._lock = threading.Lock()
        self.device = "cpu"

    def describe_device(self) -> dict[str, Any]:
        info: dict[str, Any] = {"backend": self.backend, "device": self.device, "cuda": False}
        try:
            import torch

            info["cuda"] = bool(torch.cuda.is_available())
            if torch.cuda.is_available():
                info["device"] = "cuda:0"
                info["gpu_name"] = torch.cuda.get_device_name(0)
        except Exception as exc:
            info["cuda_error"] = str(exc)
        return info

    def load(self) -> None:
        with self._lock:
            if self.backend == "hash" or self._model is not None:
                return
            device_info = self.describe_device()
            self.device = "cuda:0" if device_info.get("cuda") else "cpu"
            from sentence_transformers import SentenceTransformer

            kwargs: dict[str, Any] = {
                "device": self.device,
                "revision": RURI_REVISION,
            }
            local_config = self.models_dir / "config.json"
            if local_config.exists():
                self._model = SentenceTransformer(str(self.models_dir), device=self.device)
            else:
                self._model = SentenceTransformer(RURI_MODEL_ID, **kwargs)
            hidden = int(self._model.get_embedding_dimension())
            if hidden != EMBED_DIM:
                raise EmbeddingError(f"hidden_size={hidden}, expected {EMBED_DIM}")

    def encode_documents(self, texts: list[str]) -> np.ndarray:
        return self._encode([f"{PREFIX_DOC}{text}" for text in texts])

    def encode_queries(self, texts: list[str]) -> np.ndarray:
        return self._encode([f"{PREFIX_QUERY}{text}" for text in texts])

    def _encode(self, sentences: list[str]) -> np.ndarray:
        if not sentences:
            return np.zeros((0, EMBED_DIM), dtype=np.float32)
        if self.backend == "hash":
            return np.vstack([_hash_vector(s) for s in sentences])
        if self._model is None:
            self.load()
        batch_size = 64 if self.device != "cpu" else 8
        last_error: Exception | None = None
        while batch_size >= 8:
            try:
                vectors = self._model.encode(
                    sentences,
                    batch_size=batch_size,
                    show_progress_bar=False,
                    convert_to_numpy=True,
                    normalize_embeddings=True,
                    precision="float32",
                )
                return np.asarray(vectors, dtype=np.float32)
            except RuntimeError as exc:
                if "out of memory" not in str(exc).lower() and "oom" not in str(exc).lower():
                    raise
                last_error = exc
                batch_size //= 2
        raise EmbeddingError(f"OOM even at batch_size=8: {last_error}")

    def golden_check(self) -> dict[str, Any]:
        doc_a = self.encode_documents([GOLDEN_DOC_TEXT])[0]
        doc_b = self.encode_documents([GOLDEN_DOC_TEXT])[0]
        query = self.encode_queries([GOLDEN_DOC_TEXT])[0]
        norm = float(np.linalg.norm(doc_a))
        cosine_repeat = float(np.dot(doc_a, doc_b))
        cosine_prefix = float(np.dot(doc_a, query))
        dim = int(doc_a.shape[0])
        ok = (
            dim == EMBED_DIM
            and abs(norm - 1.0) < 1e-4
            and cosine_repeat >= 1.0 - 1e-4
            and cosine_prefix < 1.0 - 1e-6
        )
        payload = {
            "ok": ok,
            "dim": dim,
            "l2_norm": norm,
            "repeat_cosine": cosine_repeat,
            "prefix_cosine": cosine_prefix,
            "device": self.device,
            "backend": self.backend,
        }
        golden_path = self.models_dir / "golden_vectors.json"
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload


def _hash_vector(text: str) -> np.ndarray:
    """単体試験用。本番検索では使わない。"""
    rng_seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big") % (2**32)
    rng = np.random.default_rng(rng_seed)
    vec = rng.standard_normal(EMBED_DIM).astype(np.float32)
    vec /= np.linalg.norm(vec) + 1e-12
    return vec
