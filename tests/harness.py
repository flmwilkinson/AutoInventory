"""Fixture harness (SPEC §8): run the scanner over fixture repos, compare
against golden ``expected/record.json`` + ``expected/graph.json`` (exact match,
timestamps excluded), and bless new goldens.

Bless from the CLI (review the diff before committing the result):

    python -m tests.harness bless <fixture> [<fixture> ...]
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path
from typing import cast

from aiscan.cli import run_scan
from aiscan.inventory.identity import identity_record
from tests.conftest import fixture_repo

FIXTURES = Path(__file__).parent / "fixtures"

# SPEC-3 §10: report.html goldens for the flagship detected case, the
# framework+handoff case, and the negative-attestation page.
REPORT_FIXTURES = frozenset(
    {
        "bespoke_gateway_loop",
        "fw_openai_agents_basic",
        "no_ai_clean",
        "ts_bespoke_gateway_loop",
    }
)

_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T[0-9:.]+(?:\+00:00|Z)")


def normalize_report(text: str) -> str:
    """Same timestamps-excluded treatment as record.json, for report.html."""
    return _TIMESTAMP_RE.sub("<normalized>", text)


def run_fixture(name: str, out_root: Path) -> Path:
    """Scan one fixture repo (with its org pack when present).

    The repo is scanned from an isolated temp copy (see ``fixture_repo``) so the
    golden scan is independent of the project's own VCS state."""
    repo = fixture_repo(name)
    org = FIXTURES / name / "org.yaml"
    return run_scan(
        str(repo),
        out=out_root,
        org_pack=org if org.is_file() else None,
    )


def normalize_record(data: dict[str, object]) -> dict[str, object]:
    """Strip the fields the golden compare excludes (SPEC §2: timestamps).

    Delegates to the production ``identity_record`` (SPEC-10 §5a) so the golden
    compare and the store's ``(repo, commit)`` change-detection share one
    definition of "the same scan"."""
    return identity_record(data)


def normalize_health(data: dict[str, object]) -> dict[str, object]:
    data["stage_ms"] = {}
    return data


def compare_fixture(name: str, out_root: Path) -> list[str]:
    """Return human-readable diffs between a fresh scan and the goldens."""
    expected_dir = FIXTURES / name / "expected"
    scan_dir = run_fixture(name, out_root)
    diffs: list[str] = []
    for artifact, normalize in (
        ("record.json", normalize_record),
        ("graph.json", None),
    ):
        golden_path = expected_dir / artifact
        actual_path = scan_dir / artifact
        if not golden_path.is_file():
            diffs.append(f"{name}/{artifact}: no golden")
            continue
        golden = json.loads(golden_path.read_text(encoding="utf-8"))
        actual = json.loads(actual_path.read_text(encoding="utf-8"))
        if normalize is not None:
            golden = normalize(golden)
            actual = normalize(actual)
        diffs.extend(_diff(f"{name}/{artifact}", golden, actual))
    for html_artifact in ("report.html", "inventory.html"):
        html_golden = expected_dir / html_artifact
        if html_golden.is_file():
            golden_html = normalize_report(html_golden.read_text(encoding="utf-8"))
            actual_html = normalize_report(
                (scan_dir / html_artifact).read_text(encoding="utf-8")
            )
            if golden_html != actual_html:
                diffs.append(f"{name}/{html_artifact}: differs from golden")
    return diffs


def _diff(prefix: str, golden: object, actual: object) -> list[str]:
    if golden == actual:
        return []
    out: list[str] = []
    if isinstance(golden, dict) and isinstance(actual, dict):
        g = cast(dict[str, object], golden)
        a = cast(dict[str, object], actual)
        for key in sorted(set(g) | set(a)):
            if key not in g:
                out.append(f"{prefix}.{key}: unexpected (={_short(a[key])})")
            elif key not in a:
                out.append(f"{prefix}.{key}: missing (golden={_short(g[key])})")
            else:
                out.extend(_diff(f"{prefix}.{key}", g[key], a[key]))
        return out
    if isinstance(golden, list) and isinstance(actual, list):
        if len(golden) != len(actual):
            out.append(f"{prefix}: length {len(golden)} != {len(actual)}")
        for i, (gi, ai) in enumerate(zip(golden, actual, strict=False)):
            out.extend(_diff(f"{prefix}[{i}]", gi, ai))
        return out
    return [f"{prefix}: golden={_short(golden)} actual={_short(actual)}"]


def _short(value: object) -> str:
    text = json.dumps(value, sort_keys=True, default=str)
    return text if len(text) <= 120 else text[:117] + "..."


def bless(name: str) -> Path:
    """Write current scanner output as the fixture's goldens."""
    expected_dir = FIXTURES / name / "expected"
    expected_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="aiscan-bless-") as tmp:
        scan_dir = run_fixture(name, Path(tmp))
        for artifact in ("record.json", "graph.json"):
            src = scan_dir / artifact
            data = json.loads(src.read_text(encoding="utf-8"))
            if artifact == "record.json":
                data = normalize_record(data)
            (expected_dir / artifact).write_text(
                json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        if name in REPORT_FIXTURES:
            for html_artifact in ("report.html", "inventory.html"):
                (expected_dir / html_artifact).write_text(
                    normalize_report(
                        (scan_dir / html_artifact).read_text(encoding="utf-8")
                    ),
                    encoding="utf-8",
                )
    return expected_dir


def _record_entities(record: dict[str, object]) -> dict[str, set[str]]:
    """Identity sets per entity type, for precision/recall accounting."""

    def ids(key: str, id_field: str) -> set[str]:
        items = record.get(key)
        if not isinstance(items, list):
            return set()
        return {str(i.get(id_field)) for i in items if isinstance(i, dict)}

    usages = record.get("model_usages")
    usage_ids: set[str] = set()
    if isinstance(usages, list):
        for u in usages:
            if isinstance(u, dict):
                usage_ids.add(json.dumps(u.get("evidence"), sort_keys=True))
    findings = record.get("findings")
    finding_ids: set[str] = set()
    if isinstance(findings, list):
        for f in findings:
            if isinstance(f, dict):
                finding_ids.add(
                    f"{f.get('kind')}|{json.dumps(f.get('evidence'), sort_keys=True)}"
                )
    return {
        "agents": ids("agents", "agent_id"),
        "tools": ids("tools", "tool_id"),
        "mcp_servers": ids("mcp_servers", "server_id"),
        "model_usages": usage_ids,
        "findings": finding_ids,
    }


def compute_metrics(names: list[str]) -> dict[str, tuple[int, int, int]]:
    """Per-entity-type (tp, fp, fn) of fresh scans vs the golden labels."""
    totals: dict[str, tuple[int, int, int]] = {}
    for name in names:
        golden_path = FIXTURES / name / "expected" / "record.json"
        if not golden_path.is_file():
            continue
        golden = _record_entities(json.loads(golden_path.read_text(encoding="utf-8")))
        with tempfile.TemporaryDirectory(prefix="aiscan-metrics-") as tmp:
            scan_dir = run_fixture(name, Path(tmp))
            actual = _record_entities(
                json.loads((scan_dir / "record.json").read_text(encoding="utf-8"))
            )
        for kind in golden:
            tp, fp, fn = totals.get(kind, (0, 0, 0))
            tp += len(golden[kind] & actual[kind])
            fp += len(actual[kind] - golden[kind])
            fn += len(golden[kind] - actual[kind])
            totals[kind] = (tp, fp, fn)
    return totals


def print_metrics(names: list[str]) -> None:
    totals = compute_metrics(names)
    print(f"{'entity type':<15} {'tp':>4} {'fp':>4} {'fn':>4} {'precision':>10} {'recall':>8}")
    for kind in sorted(totals):
        tp, fp, fn = totals[kind]
        precision = tp / (tp + fp) if tp + fp else 1.0
        recall = tp / (tp + fn) if tp + fn else 1.0
        print(f"{kind:<15} {tp:>4} {fp:>4} {fn:>4} {precision:>10.3f} {recall:>8.3f}")


def all_golden_fixtures() -> list[str]:
    return sorted(
        p.name for p in FIXTURES.iterdir() if (p / "expected" / "record.json").is_file()
    )


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "bless":
        for name in argv[1:]:
            out = bless(name)
            print(f"blessed {name} -> {out}")
        return 0
    if len(argv) >= 2 and argv[0] == "compare":
        failures = 0
        for name in argv[1:]:
            with tempfile.TemporaryDirectory(prefix="aiscan-cmp-") as tmp:
                diffs = compare_fixture(name, Path(tmp))
            for d in diffs:
                print(d)
            failures += bool(diffs)
        return 1 if failures else 0
    if argv and argv[0] == "metrics":
        print_metrics(argv[1:] or all_golden_fixtures())
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
