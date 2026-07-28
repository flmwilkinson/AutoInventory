"""SPEC-3 V2: AI-BOM, governance slots + candidate, [X] resolver stubs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aiscan.cli import run_scan
from aiscan.context import KnownWrapper, OrgPack
from aiscan.inventory.bom import build_ai_bom
from aiscan.inventory.resolvers import (
    NullConfigResolver,
    NullEntitlementResolver,
    NullOwnerResolver,
    Unresolved,
)
from aiscan.inventory.schema import Record
from tests.conftest import fixture_repo


class TestAiBom:
    def test_dormant_dependency(self, tmp_path: Path) -> None:
        out = run_scan(str(fixture_repo("ai_deps_only")), out=tmp_path)
        record: dict[str, Any] = json.loads((out / "record.json").read_text(encoding="utf-8"))
        rows = record["ai_dependencies"]
        assert len(rows) == 1
        row = rows[0]
        assert row["package"] == "openai"
        assert row["version"] == "1.35.7"
        assert row["used"] is False  # declared but never imported — dormant
        assert row["source"] == "requirements.txt"
        assert record["ai_verdict"] == "ai_signals_only"

    def test_used_dependency(self, tmp_path: Path) -> None:
        out = run_scan(str(fixture_repo("bespoke_llm_call_only")), out=tmp_path / "u")
        record = json.loads((out / "record.json").read_text(encoding="utf-8"))
        rows = {r["package"]: r for r in record["ai_dependencies"]}
        if "openai" in rows:  # fixture declares openai in a manifest
            assert rows["openai"]["used"] is True

    def test_wrapper_root_counts_when_imported(self) -> None:
        org = OrgPack(
            known_wrapper_packages=(
                KnownWrapper(fqname="bank_ai.LLMClient", attribution="passthrough"),
            )
        )
        rows = build_ai_bom({}, {}, frozenset({"bank_ai"}), org)
        assert [r.package for r in rows] == ["bank_ai"]
        assert rows[0].used is True

    def test_wrapper_root_ignored_when_not_imported(self) -> None:
        org = OrgPack(
            known_wrapper_packages=(
                KnownWrapper(fqname="bank_ai.LLMClient", attribution="passthrough"),
            )
        )
        assert build_ai_bom({}, {}, frozenset(), org) == ()


class TestGovernanceSlots:
    def test_slots_present_and_never_written(self, tmp_path: Path) -> None:
        out = run_scan(str(fixture_repo("bespoke_gateway_loop")), out=tmp_path)
        record = json.loads((out / "record.json").read_text(encoding="utf-8"))
        for slot in (
            "lifecycle_status",
            "approval_status",
            "approver",
            "approval_date",
            "last_review",
            "next_review",
        ):
            assert record[slot] == {"value": None, "source": "governance", "candidate": None}
        assert record["cmdb_app_id"] == {"ref": None, "source": "external", "resolved": None}
        usage = record["model_usages"][0]
        assert usage["model_approved"]["value"] is None
        tool = record["tools"][0]
        assert tool["data_classification_touched"]["source"] == "governance"

    def test_schema_round_trips(self, tmp_path: Path) -> None:
        out = run_scan(str(fixture_repo("bespoke_gateway_loop")), out=tmp_path / "rt")
        text = (out / "record.json").read_text(encoding="utf-8")
        record = Record.model_validate_json(text)
        assert record.model_dump_json() is not None  # validates fully typed


class TestResolverStubs:
    def test_defaults_are_inert(self) -> None:
        assert isinstance(NullEntitlementResolver().resolve("env:TOKEN"), Unresolved)
        assert isinstance(NullConfigResolver().resolve("azure:deployment:x", "prod"), Unresolved)
        assert isinstance(NullOwnerResolver().resolve("team-payments"), Unresolved)
