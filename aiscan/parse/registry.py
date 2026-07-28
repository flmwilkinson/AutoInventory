"""Parser registry (SPEC-4 §2): extension → backend, and the analysed-set.

Single source of truth for "what aiscan can parse" vs "what the pipeline
currently analyses". At W0 the pipeline still analyses Python only —
``PIPELINE_EXTS`` flips to include TS/JS at W6 when TypeScript flows through
detection end-to-end (flipping earlier would silence the partial-coverage
banner before detection actually covers the language: the DocGen false-comfort
problem all over again).
"""

from __future__ import annotations

from collections.abc import Callable

from aiscan.parse.base import Parser, SecretFinding

PY_EXTS = frozenset({".py"})
TS_JS_EXTS = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})

#: Extensions a parser backend exists for.
PARSEABLE_EXTS = PY_EXTS | TS_JS_EXTS

#: Extensions the full detection pipeline analyses (census/banner basis).
#: SPEC-4 W6: TS/JS now flow through detection end-to-end, so they leave the
#: "not analysed" set — the partial-coverage banner fires only for languages
#: aiscan still cannot read (Java/Go/…).
PIPELINE_EXTS = PY_EXTS | TS_JS_EXTS


def make_parser(
    ext: str, on_secret: Callable[[SecretFinding], None] | None = None
) -> Parser | None:
    """Backend for one file extension, or None when unparseable."""
    ext = ext.lower()
    if ext in PY_EXTS:
        from aiscan.parse.py_ast import AstParser

        return AstParser(on_secret=on_secret)
    if ext in TS_JS_EXTS:
        from aiscan.parse.ts_tree_sitter import TsParser

        return TsParser(on_secret=on_secret)
    return None
