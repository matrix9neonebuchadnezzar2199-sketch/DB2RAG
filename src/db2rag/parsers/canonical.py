"""CanonicalDocument — parser 共通の中間表現。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Block:
    type: str
    text: str
    level: int = 0
    page: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CanonicalDocument:
    title: str
    language: str
    mime: str
    blocks: list[Block]
    extra: dict[str, Any] = field(default_factory=dict)

    def plain_text(self) -> str:
        parts: list[str] = []
        for block in self.blocks:
            if block.text.strip():
                parts.append(block.text.strip())
        return "\n\n".join(parts)
