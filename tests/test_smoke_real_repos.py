"""SPEC §2 item 6: smoke scans of real public repos.

Network-dependent (git clone), so gated behind AISCAN_SMOKE=1:

    AISCAN_SMOKE=1 pytest tests/test_smoke_real_repos.py -q
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

SMOKE_REPOS = [
    "https://github.com/openai/openai-agents-python",
    "https://github.com/langchain-ai/react-agent",
    "https://github.com/crewAIInc/crewAI-examples",
    # SPEC-4: a TypeScript agent framework repo.
    "https://github.com/openai/openai-agents-js",
]

pytestmark = pytest.mark.skipif(
    not os.environ.get("AISCAN_SMOKE"),
    reason="network smoke test; set AISCAN_SMOKE=1 to run",
)

# A local TS repo clone, when present, exercises the full polyglot pipeline
# offline (SPEC-4 W7 DocGen acceptance).
_DOCGEN = Path(__file__).resolve().parents[1] / "aiscan-out" / "_clones" / "DocGen"


@pytest.mark.skipif(not _DOCGEN.is_dir(), reason="DocGen clone not present")
def test_docgen_typescript_detection(tmp_path: Path) -> None:
    from aiscan.cli import run_scan

    out = run_scan(str(_DOCGEN), out=tmp_path)
    record = json.loads((out / "record.json").read_text(encoding="utf-8"))
    assert record["ai_verdict"] == "ai_detected"
    # TS agents are recovered (the DocGen headline).
    ts_agents = [a for a in record["agents"] if a["language"] == "typescript"]
    assert ts_agents, "no TypeScript agents detected in DocGen"
    # The npm openai dependency is imported in analysed code.
    npm = {r["package"]: r for r in record["ai_dependencies"] if r["ecosystem"] == "npm"}
    assert npm.get("openai", {}).get("used") is True
    # No false partial-coverage banner (TS is analysed).
    report = (out / "report.html").read_text(encoding="utf-8")
    assert "Partial coverage" not in report


@pytest.mark.parametrize("url", SMOKE_REPOS)
def test_smoke_repo(url: str, tmp_path: Path) -> None:
    from aiscan.cli import run_scan

    started = time.monotonic()
    out = run_scan(url, out=tmp_path)
    elapsed = time.monotonic() - started
    record = json.loads((out / "record.json").read_text(encoding="utf-8"))

    assert elapsed < 180, f"scan exceeded p95 budget: {elapsed:.0f}s"
    assert record["scanned_commit"] not in ("", "unknown")
    assert not record["no_ai_detected"]
    health = record["scan_health"]
    assert health["files"] > 0
    # Non-empty, sensible output: something was detected in each repo.
    detected = len(record["agents"]) + len(record["model_usages"]) + len(record["tools"])
    assert detected > 0, "smoke repo produced an empty inventory"
