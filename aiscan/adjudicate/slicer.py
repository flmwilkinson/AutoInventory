"""Slice construction (SPEC §6.10): the adjudicator sees a bounded slice —
module imports + ≤40 lines around the candidate span — never whole files.
Secrets were already redacted at IR time, but slices are built from raw source,
so the same redaction patterns are applied here before anything leaves the
process."""

from __future__ import annotations

import re
from pathlib import Path

from aiscan.parse.py_ast import classify_secret

_CONTEXT_LINES = 40
_MAX_SLICE_CHARS = 24_000  # ≈6k tokens

_SPAN_RE = re.compile(r"^(?P<path>.+):(?P<start>\d+)-(?P<end>\d+)$")


def parse_span(evidence: str) -> tuple[str, int, int] | None:
    m = _SPAN_RE.match(evidence)
    if m is None:
        return None
    return m.group("path"), int(m.group("start")), int(m.group("end"))


def _redact_line(line: str) -> str:
    kind = classify_secret(line)
    return f"<REDACTED:{kind}>" if kind else line


def build_slice(repo_root: Path, evidence: str) -> str | None:
    """Source slice for one candidate: imports + context window, line-numbered."""
    parsed = parse_span(evidence)
    if parsed is None:
        return None
    rel, start, end = parsed
    path = repo_root / rel
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None

    imports = [
        _redact_line(line)
        for line in lines[:80]
        if line.startswith(("import ", "from "))
    ]
    lo = max(0, start - 1 - _CONTEXT_LINES)
    hi = min(len(lines), end + _CONTEXT_LINES)
    window = [
        f"{i + 1:5d} | {_redact_line(lines[i])}" for i in range(lo, hi)
    ]
    text = (
        f"# File: {rel} (candidate at lines {start}-{end})\n"
        + "# Module imports:\n"
        + "\n".join(imports)
        + "\n\n# Source window:\n"
        + "\n".join(window)
    )
    return text[:_MAX_SLICE_CHARS]
