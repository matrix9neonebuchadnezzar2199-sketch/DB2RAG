"""任意の Cross-Encoder。無効のときはモデルを読まない。"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

# テストが logit を渡す。本番の CrossEncoder も活性化前の logit をここに通す。
LogitScorer = Callable[[str, list[str]], Sequence[float]]


class Reranker:
    """クエリと child 本文の logit を sigmoid で 0..1 に較正する。

    Args:
        enabled: False のとき ``score`` は空を返し、モデルをロードしない。
        model_id: Hugging Face の Cross-Encoder ID。
        models_dir: ローカル配置またはダウンロード先。
        top_n: 検索側が再スコアする件数。
        scorer: テスト用。渡された logit 列を sigmoid する。モデルは読まない。
    """

    def __init__(
        self,
        enabled: bool,
        model_id: str,
        models_dir: Path,
        *,
        top_n: int = 30,
        scorer: LogitScorer | None = None,
    ) -> None:
        self.enabled = enabled
        self.model_id = model_id
        self.models_dir = models_dir
        self.top_n = top_n
        self._scorer = scorer
        self._model: Any = None

    def score(self, query: str, passages: list[str]) -> list[float]:
        """passage ごとの較正スコアを返す。

        Args:
            query: 検索クエリ。
            passages: child 本文。空なら空リスト。

        Returns:
            sigmoid 後のスコア。``enabled`` が False のときは空で、モデルは読まない。

        Raises:
            RuntimeError: スコア件数が passage 件数と違うとき。
        """
        if not self.enabled or not passages:
            return []
        if self._scorer is not None:
            raw_scores = self._scorer(query, passages)
        else:
            raw_scores = self._predict_logits(query, passages)
        values = [_sigmoid(float(value)) for value in raw_scores]
        if len(values) != len(passages):
            raise RuntimeError(
                f"reranker returned {len(values)} scores for {len(passages)} passages"
            )
        return values

    def _predict_logits(self, query: str, passages: list[str]) -> list[float]:
        model = self._load()
        pairs = [(query, passage) for passage in passages]
        # モデル既定の Sigmoid を掛けず、logit をこちらで一度だけ較正する。
        raw = model.predict(
            pairs,
            show_progress_bar=False,
            convert_to_numpy=True,
            activation_fn=_identity,
        )
        return _as_float_list(raw)

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        import torch
        from sentence_transformers import CrossEncoder

        local_config = self.models_dir / "config.json"
        target = str(self.models_dir) if local_config.is_file() else self.model_id
        # FP32 のまま使う。量子化や半精度へ自動降格しない。
        self._model = CrossEncoder(
            target,
            device=_device(),
            trust_remote_code=True,
            local_files_only=local_config.is_file(),
            cache_folder=str(self.models_dir),
            model_kwargs={"torch_dtype": torch.float32},
        )
        return self._model


def _device() -> str:
    import torch

    if torch.cuda.is_available():
        return "cuda:0"
    return "cpu"


def _identity(logits: Any) -> Any:
    return logits


def _sigmoid(value: float) -> float:
    """logit を ``1/(1+e^{-x})`` に写す。大きい絶対値でも exp が溢れない。"""
    if value >= 0.0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def _as_float_list(raw: Any) -> list[float]:
    if hasattr(raw, "tolist"):
        raw = raw.tolist()
    if isinstance(raw, (int, float)):
        return [float(raw)]
    return [float(item) for item in raw]
