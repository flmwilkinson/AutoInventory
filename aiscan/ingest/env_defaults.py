"""Config-at-rest env defaults (SPEC-5 §6).

A ``Symbolic("env", KEY)`` value in a detected fact is truthful but unbound.
The repo's own declared configuration — dotenv examples, docker-compose
``environment:`` maps, k8s manifest ``env:`` lists — usually pins the value.
This module collects those declarations as *candidates with provenance*.
They are presentation-level context: never facts, never overriding detection
(a candidate is not a detection). Secret-shaped values are redacted with the
SPEC-1 §6.5 patterns before anything leaves this module.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from aiscan.parse.py_ast import _SECRET_PATTERNS

_AddFn = Callable[[str, str, Path, str], None]

_DOTENV_LINE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_MAX_FILES = 200
_MAX_BYTES = 256 * 1024

# Directories whose yaml plausibly deploys this repo.
_MANIFEST_DIRS = frozenset({"infra", "k8s", "kubernetes", "deploy", "manifests"})
_SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "dist", "build"})


class EnvDefault(BaseModel):
    """One declared default for an env key: value + where it was declared."""

    model_config = ConfigDict(frozen=True)

    value: str
    source: str  # repo-relative path
    form: str  # "example" (template file) | "pinned" (live config/manifest)


def collect_env_defaults(repo_root: Path) -> dict[str, tuple[EnvDefault, ...]]:
    """All declared env defaults in the repo, keyed by env-var name."""
    found: dict[str, list[EnvDefault]] = {}

    def add(key: str, value: str, path: Path, form: str) -> None:
        value = value.strip()
        if not value:
            return
        if any(p.search(value) for _n, p in _SECRET_PATTERNS):
            value = "[redacted]"
        rel = path.relative_to(repo_root).as_posix()
        entry = EnvDefault(value=value, source=rel, form=form)
        bucket = found.setdefault(key, [])
        if entry not in bucket:
            bucket.append(entry)

    for path in _candidate_files(repo_root):
        try:
            if path.stat().st_size > _MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        name = path.name.lower()
        form = "example" if "example" in name or "sample" in name else "pinned"
        if name.endswith((".yml", ".yaml")):
            _from_yaml(text, path, form, add)
        else:
            _from_dotenv(text, path, form, add)

    return {
        k: tuple(sorted(found[k], key=lambda e: (e.source, e.value)))
        for k in sorted(found)
    }


def _candidate_files(repo_root: Path) -> list[Path]:
    out: list[Path] = []
    for path in sorted(repo_root.rglob("*")):
        if len(out) >= _MAX_FILES:
            break
        if not path.is_file():
            continue
        parts = set(path.relative_to(repo_root).parts[:-1])
        if parts & _SKIP_DIRS:
            continue
        name = path.name.lower()
        is_dotenv = (
            name == ".env"
            or name.startswith(".env.")
            or name.endswith(("env.example", ".env.sample"))
        )
        is_yaml = name.endswith((".yml", ".yaml"))
        if (
            is_dotenv
            or (is_yaml and name.startswith("docker-compose"))
            or (is_yaml and parts & _MANIFEST_DIRS)
        ):
            out.append(path)
    return out


def _from_dotenv(text: str, path: Path, form: str, add: _AddFn) -> None:
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _DOTENV_LINE.match(line)
        if m is None:
            continue
        key, raw = m.group(1), m.group(2).strip()
        if raw[:1] in ("'", '"') and raw[-1:] == raw[:1] and len(raw) >= 2:
            value = raw[1:-1]
        else:
            value = raw.split(" #", 1)[0].strip()
        add(key, value, path, form)


def _from_yaml(text: str, path: Path, form: str, add: _AddFn) -> None:
    try:
        docs = list(yaml.safe_load_all(text))
    except yaml.YAMLError:
        return
    for doc in docs:
        _walk_yaml(doc, path, form, add)


def _walk_yaml(node: object, path: Path, form: str, add: _AddFn) -> None:
    if isinstance(node, dict):
        env = node.get("environment")
        if isinstance(env, dict):  # compose map form
            for k, v in env.items():
                if isinstance(k, str) and isinstance(v, str | int | float | bool):
                    add(k, str(v), path, form)
        elif isinstance(env, list):  # compose list form: "K=V"
            for item in env:
                if isinstance(item, str) and "=" in item:
                    k, _, v = item.partition("=")
                    add(k.strip(), v, path, form)
        k8s_env = node.get("env")
        if isinstance(k8s_env, list):  # k8s [{name, value}]
            for item in k8s_env:
                if (
                    isinstance(item, dict)
                    and isinstance(item.get("name"), str)
                    and isinstance(item.get("value"), str | int | float | bool)
                ):
                    add(item["name"], str(item["value"]), path, form)
        for v in node.values():
            _walk_yaml(v, path, form, add)
    elif isinstance(node, list):
        for v in node:
            _walk_yaml(v, path, form, add)
