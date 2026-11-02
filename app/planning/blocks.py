"""回复块构造器。"""
from __future__ import annotations

from typing import Any


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def kv(pairs: list[list], source: str = "tool") -> dict:
    return {"type": "kv", "pairs": pairs, "source": source}


def table(columns: list[str], rows: list[list], source: str = "tool") -> dict:
    return {"type": "table", "columns": columns, "rows": rows, "source": source}


def note(t: str, level: str = "info") -> dict:
    return {"type": "note", "text": t, "level": level}


def quote(t: str) -> dict:
    return {"type": "quote", "text": t}


def source(tool: str, params: dict, src: str = "tool") -> dict:
    return {"type": "source", "tool": tool, "params": params, "source": src}


def confirm_hint(level: str) -> dict:
    return {"type": "confirm_hint", "level": level}


def suggestions(items: list[str]) -> dict:
    return {"type": "suggestions", "items": items}


def blocks_text(blocks: list[dict]) -> str:
    """拼接 text/note 块为纯文本 reply（对齐契约 v0.1 的 reply 字段，向后兼容）。"""
    parts = [b.get("text", "") for b in blocks if b.get("type") in ("text", "note")]
    return "\n".join(p for p in parts if p)
