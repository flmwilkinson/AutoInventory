"""SPEC-4 W6: end-to-end polyglot scan — one record over Python + TS, language
tags, npm BOM `used`, report chips, declared-agent artefacts, and the
dependency-injection chain-shape sink."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aiscan.cli import run_scan
from aiscan.frontends.declared import find_declared_agents


def scan_repo(repo: Path, **kwargs: object) -> tuple[dict[str, Any], str]:
    out = run_scan(str(repo), out=repo.parent / "out", **kwargs)  # type: ignore[arg-type]
    record = json.loads((out / "record.json").read_text(encoding="utf-8"))
    report = (out / "report.html").read_text(encoding="utf-8")
    return record, report


def write(repo: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


class TestPolyglotScan:
    def test_one_record_two_languages(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        write(
            repo,
            {
                "requirements.txt": "openai==1.35.0\n",
                "package.json": '{"dependencies": {"openai": "^4.28.0"}}',
                "py_app/agent.py": (
                    "import openai\n"
                    "client = openai.OpenAI()\n"
                    "def pyRun(q):\n"
                    "    msgs = [{'role': 'user', 'content': q}]\n"
                    "    while True:\n"
                    "        r = client.chat.completions.create(model='gpt-4o', messages=msgs)\n"
                    "        m = r.choices[0].message\n"
                    "        if m.finish_reason != 'tool_calls':\n"
                    "            return m.content\n"
                    "        msgs.append(m)\n"
                ),
                "ts_app/agent.ts": (
                    "import OpenAI from 'openai';\n"
                    "const client = new OpenAI();\n"
                    "export async function tsRun(q: string) {\n"
                    "  const msgs = [{ role: 'user', content: q }];\n"
                    "  while (true) {\n"
                    "    const r = await client.chat.completions.create(\n"
                    "      { model: 'gpt-4o', messages: msgs });\n"
                    "    const m = r.choices[0].message;\n"
                    "    if (m.finish_reason !== 'tool_calls') { return m.content; }\n"
                    "    msgs.push(m);\n"
                    "  }\n"
                    "}\n"
                ),
            },
        )
        record, report = scan_repo(repo)
        assert record["ai_verdict"] == "ai_detected"
        langs = {a["language"] for a in record["agents"]}
        assert "typescript" in langs and "python" in langs
        by_lang = record["derived"]["agents_by_language"]["value"]
        assert by_lang.get("python", 0) >= 1 and by_lang.get("typescript", 0) >= 1
        # npm openai dependency is imported in analysed TS → used.
        npm = next(
            r for r in record["ai_dependencies"] if r["ecosystem"] == "npm"
        )
        assert npm["package"] == "openai" and npm["used"] is True
        # Report shows language chips.
        assert "typescript" in report and "python" in report

    def test_dependency_injected_client_detected(self, tmp_path: Path) -> None:
        # The chain-shape sink: an opaque `ctx.openai.chat.completions.create`.
        repo = tmp_path / "repo"
        write(
            repo,
            {
                "package.json": '{"dependencies": {"openai": "^4.28.0"}}',
                "svc/agent.ts": (
                    "export async function orchestrate(ctx, q) {\n"
                    "  const msgs = [{ role: 'user', content: q }];\n"
                    "  while (true) {\n"
                    "    const r = await ctx.openai.chat.completions.create(\n"
                    "      { model: 'gpt-4o', messages: msgs });\n"
                    "    const m = r.choices[0].message;\n"
                    "    if (m.finish_reason !== 'tool_calls') { return m.content; }\n"
                    "    msgs.push(m);\n"
                    "  }\n"
                    "}\n"
                ),
            },
        )
        record, _ = scan_repo(repo)
        assert record["ai_verdict"] == "ai_detected"
        assert record["model_usages"] or record["agents"]


class TestDeclaredAgents:
    def test_roster_surfaced_and_bound(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        write(
            repo,
            {
                "package.json": '{"dependencies": {"openai": "^4.28.0"}}',
                "packages/prompts/agents/block-writer/system.md": "You write blocks.\n",
                "packages/prompts/agents/gap-detector/system.md": "You find gaps.\n",
                "src/writer.ts": (
                    "import OpenAI from 'openai';\n"
                    "const client = new OpenAI();\n"
                    "export async function blockWriter(q: string) {\n"
                    "  const msgs = [{ role: 'user', content: q }];\n"
                    "  while (true) {\n"
                    "    const r = await client.chat.completions.create(\n"
                    "      { model: 'gpt-4o', messages: msgs });\n"
                    "    const m = r.choices[0].message;\n"
                    "    if (m.finish_reason !== 'tool_calls') { return m.content; }\n"
                    "    msgs.push(m);\n"
                    "  }\n"
                    "}\n"
                ),
            },
        )
        record, _ = scan_repo(repo)
        health = record["scan_health"]["declared_agents"]
        assert health == ["block-writer", "gap-detector"]
        findings = {f["kind"]: f for f in record["findings"]}
        # gap-detector has no code agent → surfaced as a declared-artefact finding.
        assert "declared_agent_artefact" in findings
        detail = " ".join(
            f["detail"]
            for f in record["findings"]
            if f["kind"] == "declared_agent_artefact"
        )
        assert "gap-detector" in detail
        # block-writer IS matched to blockWriter (slug) → not a gap finding.
        assert "block-writer" not in detail

    def test_finder_matches_docgen_layout(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        write(
            repo,
            {
                "packages/prompts/agents/chart-builder/system.md": "x\n",
                "packages/prompts/agents/chart-builder/task.md": "y\n",
                "other/agents/not-a-roster/readme.md": "z\n",  # parent not `prompts`
            },
        )
        roster = find_declared_agents(repo)
        assert list(roster) == ["chart-builder"]
        assert len(roster["chart-builder"]) == 2  # system.md + task.md
