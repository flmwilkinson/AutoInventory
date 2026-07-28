"""Settings, org pack, scan-health accumulator, and ScanContext (SPEC §7).

No global mutable state anywhere in the scanner: one :class:`ScanContext` is
built per scan and threaded explicitly through every stage.
"""

from __future__ import annotations

import contextlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from aiscan.facts.models import FindingRecord
from aiscan.parse.base import ParseErrorInfo, SecretFinding


class ResolverBudgets(BaseModel):
    """Hard bounds for the demand-driven resolver (SPEC §6.4)."""

    model_config = ConfigDict(frozen=True)

    max_depth: int = 8
    max_hops: int = 4
    query_ms: int = 250
    scan_budget_s: int = 60


class Settings(BaseModel):
    """Scanner configuration, loadable from YAML with ``AISCAN_*`` env overrides."""

    model_config = ConfigDict(frozen=True)

    out_dir: Path = Path("aiscan-out")
    org_pack_path: Path | None = None
    org_registry_path: Path | None = None
    json_logs: bool = False
    resolver: ResolverBudgets = ResolverBudgets()
    # Optional --adjudicate tier (OpenAI-compatible endpoint; key comes from the
    # environment, never stored here). None → provider defaults apply.
    adjudicate_base_url: str | None = None
    adjudicate_model: str | None = None
    adjudicate_budget: int = 20
    # SPEC-10 §L: per-repo scaling guard. A repo over either ceiling emits a
    # bounded record flagged in scan_health rather than risking an OOM that would
    # take down a whole org-wide batch. Generous defaults — a normal repo (even a
    # 300k-LOC monorepo) never trips them; only a pathological tree does.
    max_files: int = 100_000
    max_loc: int = 10_000_000

    @classmethod
    def load(cls, yaml_path: Path | None = None) -> Settings:
        data: dict[str, object] = {}
        if yaml_path is not None and yaml_path.is_file():
            loaded = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data.update(loaded)
        env = os.environ
        if "AISCAN_OUT_DIR" in env:
            data["out_dir"] = env["AISCAN_OUT_DIR"]
        if "AISCAN_ORG_PACK" in env:
            data["org_pack_path"] = env["AISCAN_ORG_PACK"]
        if "AISCAN_ORG_REGISTRY" in env:
            data["org_registry_path"] = env["AISCAN_ORG_REGISTRY"]
        if "AISCAN_JSON_LOGS" in env:
            data["json_logs"] = env["AISCAN_JSON_LOGS"].lower() in ("1", "true", "yes")
        if "AISCAN_ADJUDICATE_BASE_URL" in env:
            data["adjudicate_base_url"] = env["AISCAN_ADJUDICATE_BASE_URL"]
        elif "OPENAI_BASE_URL" in env:
            data["adjudicate_base_url"] = env["OPENAI_BASE_URL"]
        if "AISCAN_ADJUDICATE_MODEL" in env:
            data["adjudicate_model"] = env["AISCAN_ADJUDICATE_MODEL"]
        if "AISCAN_ADJUDICATE_BUDGET" in env:
            with contextlib.suppress(ValueError):
                data["adjudicate_budget"] = int(env["AISCAN_ADJUDICATE_BUDGET"])
        for var, setting in (("AISCAN_MAX_FILES", "max_files"), ("AISCAN_MAX_LOC", "max_loc")):
            if var in env:
                with contextlib.suppress(ValueError):
                    data[setting] = int(env[var])
        return cls.model_validate(data)


class KnownWrapper(BaseModel):
    """Org-pack seed: a wrapper package known ahead of scanning (SPEC §7)."""

    model_config = ConfigDict(frozen=True)

    fqname: str
    attribution: Literal["fixed", "passthrough", "default"]


class OrgPack(BaseModel):
    """Per-client tailoring: gateway hosts, wrapper seeds, sensitive hosts."""

    model_config = ConfigDict(frozen=True)

    gateway_hosts: tuple[str, ...] = ()
    known_wrapper_packages: tuple[KnownWrapper, ...] = ()
    sensitive_hosts: tuple[str, ...] = ()
    # SPEC-3 §3.2: hosts that are payment/ledger rails — enables the
    # moves_money capability flag. Absent → the flag is never inferred.
    payment_hosts: tuple[str, ...] = ()

    @classmethod
    def load(cls, path: Path | None) -> OrgPack:
        if path is None or not path.is_file():
            return cls()
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            return cls()
        return cls.model_validate(loaded)


class ResolverStats(BaseModel):
    """Resolver counters for ``scan_health`` (SPEC §6.9)."""

    queries: int = 0
    memo_hits: int = 0
    top: dict[str, int] = Field(default_factory=dict)
    timeouts: int = 0

    def count_top(self, reason: str) -> None:
        self.top[reason] = self.top.get(reason, 0) + 1
        if reason == "timeout":
            self.timeouts += 1


class ScanHealth(BaseModel):
    """Mutable accumulator emitted as ``scan_health.json`` and in the record."""

    files: int = 0
    loc: int = 0
    parse_errors: list[ParseErrorInfo] = Field(default_factory=list)
    triage: dict[str, list[str]] = Field(default_factory=dict)
    # SPEC-3 language census: source files by extension. Anything the pipeline
    # cannot analyse is surfaced as a coverage finding + report banner.
    language_files: dict[str, int] = Field(default_factory=dict)
    # SPEC-4 §8: agent names declared by a prompt roster (prompts/agents/<name>/).
    declared_agents: list[str] = Field(default_factory=list)
    resolver: ResolverStats = Field(default_factory=ResolverStats)
    sinks: dict[str, int] = Field(default_factory=dict)
    wrapper_classifications: int = 0
    unpromoted_candidates: int = 0
    findings: int = 0
    stage_ms: dict[str, int] = Field(default_factory=dict)
    # SPEC-10 §L: set (with the ceilings + actual counts) when a repo exceeds the
    # size budget and only triage-level analysis was run — a visible flag so a
    # bounded record is never mistaken for a complete one.
    size_budget: dict[str, int] | None = None
    # SPEC-2 §4.4 note; None until --enrich. On a run it carries a `status`
    # ("ok" | "unavailable" | "skipped") + counts or a reason, so a null
    # summary is explained rather than silent.
    enrichment: dict[str, int | str] | None = None
    # SPEC-3 §2.2: None until --adjudicate; then status + counts, or a
    # "skipped" note on a no_ai verdict (the LLM guard must be visible).
    adjudication: dict[str, int | str] | None = None


@dataclass(slots=True)
class ScanContext:
    """Everything a pipeline stage needs, threaded explicitly (SPEC §3)."""

    settings: Settings
    org_pack: OrgPack
    repo_root: Path
    repo_url: str | None
    commit: str
    bundle_name: str
    logger: logging.Logger
    health: ScanHealth = field(default_factory=ScanHealth)
    secret_findings: list[SecretFinding] = field(default_factory=list)
    analysis_findings: list[FindingRecord] = field(default_factory=list)
