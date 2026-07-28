"""Resolver memoisation (SPEC §6.4).

Keys are ``(module content hash, span, node kind)`` — content-addressed, so a
changed file never reuses stale results. Only *context-free* queries are
memoised: anything resolved under call-site-bound arguments would poison the
table across call sites and is skipped by the engine.
"""

from __future__ import annotations

from aiscan.ir.nodes import Span
from aiscan.ir.values import ValueSet

MemoKey = tuple[str, str, int, int, int, int, str]


def memo_key(content_hash: str, span: Span, kind: str) -> MemoKey:
    return (
        content_hash,
        span.file,
        span.line_start,
        span.col_start,
        span.line_end,
        span.col_end,
        kind,
    )


class Memo:
    """In-memory memo table for one scan."""

    def __init__(self) -> None:
        self._table: dict[MemoKey, ValueSet] = {}

    def get(self, key: MemoKey) -> ValueSet | None:
        return self._table.get(key)

    def put(self, key: MemoKey, value: ValueSet) -> None:
        self._table[key] = value

    def __len__(self) -> int:
        return len(self._table)
