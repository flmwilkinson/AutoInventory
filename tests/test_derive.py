"""SPEC-3 V1: derived-indicator engine — capability flags, autonomy, roles,
provider classes, severity-graded findings. All deterministic, no LLM."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aiscan.cli import run_scan
from aiscan.context import OrgPack
from aiscan.derive.engine import SEVERITY, derive_record
from aiscan.inventory.schema import Record
from aiscan.sinks.engine import load_host_registry
from tests.conftest import FIXTURES, fixture_repo


def scan_record(name: str, tmp_path: Path) -> dict[str, Any]:
    org = FIXTURES / name / "org.yaml"
    out = run_scan(
        str(fixture_repo(name)),
        out=tmp_path / name,
        org_pack=org if org.is_file() else None,
    )
    loaded: dict[str, Any] = json.loads((out / "record.json").read_text(encoding="utf-8"))
    return loaded


class TestDerivedIndicators:
    """The fully-configured org: gateway approved, payment rails declared."""

    def test_flagship_fixture(self, tmp_path: Path) -> None:
        record = scan_record("derived_indicators", tmp_path)
        agent = record["agents"][0]

        assert agent["capability_flags"]["value"]["moves_money"] is True
        assert agent["capability_flags"]["value"]["sends_external"] is True
        assert agent["autonomy_level"]["value"] == "autonomous"
        assert agent["role_class"]["value"] == "solo"
        assert set(agent["reachable_tools"]["value"]) == {"lookup_account", "send_payment"}

        derived = record["derived"]
        assert derived["agent_count"]["value"] == 1
        assert derived["capability_flags"]["value"]["moves_money"] is True
        assert derived["autonomy_profile"]["value"] == "autonomous"
        # gw.internal.example IS approved in this org pack.
        assert derived["has_unapproved_endpoint"]["value"] is False
        models = derived["models_used"]["value"]
        assert any(m["provider_class"] == "internal_gateway" for m in models)
        assert "payments-core.internal" in derived["external_systems"]["value"]

        kinds = {f["kind"]: f for f in record["findings"]}
        assert kinds["high_privilege_agent"]["severity"] == "high"
        assert kinds["high_privilege_agent"]["subject_ref"] == "agent:run-agent"
        assert "unapproved_gateway" not in kinds

        send = next(t for t in record["tools"] if t["tool_id"] == "send_payment")
        assert send["is_sensitive"]["value"] is True

    def test_without_org_pack_endpoint_is_unapproved(self, tmp_path: Path) -> None:
        out = run_scan(str(fixture_repo("derived_indicators")), out=tmp_path / "no_org")
        record = json.loads((out / "record.json").read_text(encoding="utf-8"))
        assert record["derived"]["has_unapproved_endpoint"]["value"] is True
        kinds = {f["kind"] for f in record["findings"]}
        assert "unapproved_gateway" in kinds
        # No payment_hosts declared → moves_money is never inferred.
        assert record["derived"]["capability_flags"]["value"]["moves_money"] is False


class TestSeverityGrading:
    def test_existing_kinds_are_graded(self, tmp_path: Path) -> None:
        record = scan_record("secret_literal", tmp_path)
        secret = next(f for f in record["findings"] if f["kind"] == "secret_literal_redacted")
        assert secret["severity"] == "high"

    def test_test_location_agent_is_not_high_privilege(self) -> None:
        # A test-suite agent with dangerous tools must not raise a high finding.
        record = Record.model_validate(
            {
                "bundle_id": "repo:x",
                "name": "x",
                "agents": [
                    {
                        "agent_id": "a",
                        "location": "test",
                        "detection": {"method": "m", "confidence": "high", "evidence": ["t.py:1"]},
                        "tools": ["boom"],
                    }
                ],
                "tools": [
                    {
                        "tool_id": "boom",
                        "kind": "function",
                        "side_effects": ["code_exec"],
                        "evidence": ["t.py:2"],
                    }
                ],
            }
        )
        derived = derive_record(record, OrgPack(), load_host_registry(OrgPack()))
        flags = derived.agents[0].capability_flags
        assert flags is not None and isinstance(flags.value, dict)
        assert flags.value["executes_code"] is True
        assert not any(f.kind == "high_privilege_agent" for f in derived.findings)

    def test_severity_map_is_total_over_known_kinds(self) -> None:
        assert set(SEVERITY.values()) <= {"high", "medium", "low", "info"}


class TestProviderClass:
    def test_vendor_and_orphan(self, tmp_path: Path) -> None:
        record = scan_record("bespoke_llm_call_only", tmp_path)
        usage = record["model_usages"][0]
        # Plain OpenAI SDK call, no base_url → the vendor's default host.
        assert usage["provider_class"]["value"] == "vendor_external"
        kinds = {f["kind"] for f in record["findings"]}
        assert "orphan_model_usage" in kinds


def test_orphan_mcp_server_flagged_when_unattached() -> None:
    """SPEC_INVENTORY: an MCP server bound to no agent is shadow tooling and is
    surfaced; an attached one is not."""
    from aiscan.inventory.schema import AgentRecord, DetectionInfo, McpRecord

    org = OrgPack()
    reg = load_host_registry(org)
    ghost = McpRecord(server_id="ghost", transport="http", evidence=("m.py:1",))

    orphan = derive_record(
        Record(bundle_id="repo:x", name="x", mcp_servers=(ghost,)), org, reg
    )
    assert "orphan_mcp_server" in {f.kind for f in orphan.findings}
    assert orphan.mcp_servers[0].attached_agent_count == 0

    wired = derive_record(
        Record(
            bundle_id="repo:x",
            name="x",
            agents=(
                AgentRecord(
                    agent_id="a",
                    detection=DetectionInfo(method="m", confidence="high", evidence=("a.py:1",)),
                    mcp_servers=("ghost",),
                ),
            ),
            mcp_servers=(ghost,),
        ),
        org,
        reg,
    )
    assert "orphan_mcp_server" not in {f.kind for f in wired.findings}
    assert wired.mcp_servers[0].attached_agent_count == 1
