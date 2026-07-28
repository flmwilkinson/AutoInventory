"""AI-signal triage gate (SPEC §6.1) + language-coverage census (SPEC-3).

Recall-biased and regex/manifest level: its only job is to skip repos with no
AI signal at all. Zero signals → ``no_ai_detected`` record stub with the
signal table in scan_health, and the scan stops. Any signal → full pipeline.

The census (:func:`count_source_files`) exists for honesty: aiscan analyses
Python only, and a record claiming "AI signals only" over a TypeScript
monorepo must SAY that 80 source files were never analysed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from aiscan.context import OrgPack
from aiscan.ingest.source import Source, as_source
from aiscan.sinks.engine import load_host_registry

_AI_PACKAGES = (
    "openai",
    "anthropic",
    "langchain",
    "langgraph",
    "crewai",
    "llama-index",
    "llama_index",
    "litellm",
    "google-genai",
    "google-generativeai",
    "mistralai",
    "cohere",
    "groq",
    "together",
    "boto3",
    "semantic-kernel",
    "semantic_kernel",
    "autogen",
    "autogen-agentchat",
    "autogen-ext",
    "pydantic-ai",
    "pydantic_ai",
    "mcp",
)

_SDK_IMPORT_RE = re.compile(
    r"^\s*(?:from|import)\s+"
    r"(openai|anthropic|langchain\w*|langgraph|crewai|llama_index|litellm|"
    r"google\.genai|google\.generativeai|mistralai|cohere|groq|together|boto3|"
    r"semantic_kernel|autogen\w*|pydantic_ai|mcp)\b",
    re.MULTILINE,
)

_CHAT_PATHS = ("/chat/completions", "/v1/messages", ":generateContent", "/converse")

_HTTP_TOKENS = ("requests.", "httpx.", "aiohttp.", "urllib", "fetch(", "axios", "got.", ".post(")

_MANIFESTS = (
    "pyproject.toml",
    "poetry.lock",
    "Pipfile",
    "setup.py",
    "setup.cfg",
)

_PROMPT_ARTEFACTS = (".mcp.json", "mcp.json", "SKILL.md")

# npm AI packages: exact dependency names + scoped-package prefixes. Matched
# against package.json dependency KEYS (not free text), so plain "ai" (the
# Vercel AI SDK) is safe to include.
_JS_AI_PACKAGES = frozenset(
    {
        "openai",
        "ai",
        "langchain",
        "llamaindex",
        "cohere-ai",
        "groq-sdk",
        "together-ai",
        "@openai/agents",
        "@openai/agents-core",
        "@anthropic-ai/sdk",
        "@azure/openai",
        "@azure-rest/ai-inference",
        "@google/generative-ai",
        "@google/genai",
        "@mistralai/mistralai",
        "@aws-sdk/client-bedrock-runtime",
        "@modelcontextprotocol/sdk",
        "crewai-ts",
    }
)
_JS_AI_PREFIXES = ("@langchain/", "@ai-sdk/", "@openai/")

# JS/TS AI import: `import ... from 'openai'` / `require('@openai/agents')`.
_JS_SDK_IMPORT_RE = re.compile(
    r"""(?:from|require\(\s*)\s*['"]"""
    r"""(openai|ai|langchain|llamaindex|@openai/[\w-]+|@anthropic-ai/sdk|"""
    r"""@langchain/[\w-]+|@ai-sdk/[\w-]+|@azure/openai|@google/gen[\w-]+|"""
    r"""@mistralai/mistralai|@aws-sdk/client-bedrock-runtime|@modelcontextprotocol/sdk)"""
    r"""['"]""",
)

# Extensions counted by the language census (analysable + the common rest).
_SOURCE_EXTS = frozenset(
    {
        ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".java", ".go",
        ".rb", ".cs", ".kt", ".kts", ".scala", ".php", ".rs", ".swift",
    }
)

_MAX_FILE_BYTES = 1_000_000
_NEAR = 300


def _read(path: Path) -> str:
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _walk_files(repo_root: Path) -> list[Path]:
    out: list[Path] = []
    stack = [repo_root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name in (".git", "__pycache__", "node_modules", "venv") or (
                    entry.name.startswith(".")
                ):
                    continue
                stack.append(entry)
            elif entry.is_file():
                out.append(entry)
    return sorted(out)


def count_source_files(source: Source | Path) -> dict[str, int]:
    """Language census: source-file counts by extension (SPEC-3 coverage
    honesty). Cheap — extensions only, no file reads."""
    counts: dict[str, int] = {}
    for rel in as_source(source).list_files():
        ext = Path(rel).suffix.lower()  # exact parity with the old path.suffix
        if ext in _SOURCE_EXTS:
            counts[ext] = counts.get(ext, 0) + 1
    return dict(sorted(counts.items()))


def _js_ai_dependencies(text: str) -> list[str]:
    """AI packages among a package.json's dependency keys."""
    try:
        parsed = json.loads(text)
    except ValueError:
        return []
    if not isinstance(parsed, dict):
        return []
    deps: set[str] = set()
    for section in ("dependencies", "devDependencies"):
        block = parsed.get(section)
        if isinstance(block, dict):
            deps.update(str(k) for k in block)
    return sorted(
        d for d in deps if d in _JS_AI_PACKAGES or d.startswith(_JS_AI_PREFIXES)
    )


def run_triage(repo_root: Path, org_pack: OrgPack) -> dict[str, list[str]]:
    """Signal name → evidence (paths / matched tokens); empty dict = no AI."""
    signals: dict[str, list[str]] = {}

    def add(name: str, evidence: str) -> None:
        bucket = signals.setdefault(name, [])
        if evidence not in bucket and len(bucket) < 10:
            bucket.append(evidence)

    hosts = load_host_registry(org_pack)
    host_needles = tuple(hosts.hosts) + tuple(
        w.replace("*.", ".").replace(".*", ".") for w in hosts.wildcards
    ) + tuple(org_pack.gateway_hosts)
    wrapper_packages = tuple(
        w.fqname.split(".")[0] for w in org_pack.known_wrapper_packages
    )

    files = _walk_files(repo_root)
    for path in files:
        rel = path.relative_to(repo_root).as_posix()
        name = path.name

        if (name.startswith("requirements") and name.endswith(".txt")) or name in _MANIFESTS:
            text = _read(path)
            for pkg in _AI_PACKAGES:
                if re.search(rf"(?<![A-Za-z0-9_-]){re.escape(pkg)}(?![A-Za-z0-9])", text):
                    add("manifest_ai_package", f"{rel}:{pkg}")

        if name == "package.json":
            for dep in _js_ai_dependencies(_read(path)):
                add("manifest_ai_package_js", f"{rel}:{dep}")

        if name in _PROMPT_ARTEFACTS or name.endswith(".prompt") or (
            name.startswith("system") and name.endswith(".md")
        ):
            add("prompt_artefact", rel)
        if "/prompts/" in f"/{rel}":
            add("prompt_artefact", rel)

        if not name.endswith(
            (
                ".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".cfg",
                ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
            )
        ):
            continue
        text = _read(path)
        if not text:
            continue

        if name.endswith(".py"):
            m = _SDK_IMPORT_RE.search(text)
            if m:
                add("sdk_import", f"{rel}:{m.group(1)}")
            for wpkg in wrapper_packages:
                if re.search(rf"^\s*(?:from|import)\s+{re.escape(wpkg)}\b", text, re.MULTILINE):
                    add("org_wrapper_import", f"{rel}:{wpkg}")
        elif name.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")):
            jm = _JS_SDK_IMPORT_RE.search(text)
            if jm:
                add("sdk_import", f"{rel}:{jm.group(1)}")

        for host in host_needles:
            if host and host in text:
                add("llm_host", f"{rel}:{host}")
                break
        for frag in _CHAT_PATHS:
            if frag in text:
                add("chat_path_fragment", f"{rel}:{frag}")
                break

        if any(tok in text for tok in _HTTP_TOKENS):
            for m2 in re.finditer(r"[\"']model[\"']", text):
                window = text[m2.start() : m2.start() + _NEAR]
                back = text[max(0, m2.start() - _NEAR) : m2.start()]
                near = window + back
                if re.search(r"[\"'](messages|prompt|input)[\"']", near):
                    add("model_near_messages", rel)
                    break
    return signals
