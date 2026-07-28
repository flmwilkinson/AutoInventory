"""Parser interface (SPEC §6.2).

The MVP backend is stdlib ``ast`` (:mod:`aiscan.parse.py_ast`); tree-sitter is
a v1 swap. The IR is the abstraction boundary — nothing downstream imports
``ast`` directly.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from aiscan.ir.nodes import ModuleIR


class ParseErrorInfo(BaseModel):
    """A per-file parse failure, recorded in ``scan_health.parse_errors``."""

    model_config = ConfigDict(frozen=True)

    path: str
    message: str
    line: int | None = None


class SecretFinding(BaseModel):
    """A secret-shaped literal redacted at IR-lowering time (SPEC §6.5)."""

    model_config = ConfigDict(frozen=True)

    path: str
    evidence: str  # "file:line-line"
    secret_kind: str


class Parser(Protocol):
    """Parses one file into IR, or reports a structured error (never raises)."""

    def parse(self, path: str, source: str) -> ModuleIR | ParseErrorInfo:
        """``path`` is repo-relative posix; ``source`` is the file content."""
        ...
