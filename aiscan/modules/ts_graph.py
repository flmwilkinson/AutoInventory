"""TS/JS module resolution (SPEC-4 §3).

TS modules are named by repo-relative path without extension
(``apps/web/src/lib/evidence-agent``) — unique, dot-free (so ``fq =
"<module>.<symbol>"`` splits cleanly), and opaque to the resolver. Import
specifiers resolve to those names when the target file was parsed, else stay
as the raw npm specifier — which the resolver then treats as an external
``PackageRef``, exactly like an unresolvable Python import.

External identity convention (deviation from SPEC-4 §3's ``:`` proposal,
recorded in DECISIONS): dot-joined ``<specifier>.<export>`` — e.g.
``openai.default``, ``@openai/agents.Agent`` — because that is what the
existing resolver already produces for ``SymbolImport`` bindings; zero forks.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

_EXT_ORDER = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")

_PNPM_PKG = re.compile(r"^\s*[/']?(@?[^\s@'/]+(?:/[^\s@']+)?)@([0-9][^\s:'()]*)")


def ts_module_name(path: str) -> str:
    """Repo-relative posix path → TS module name ('' if not a TS/JS file)."""
    for ext in _EXT_ORDER:
        if path.endswith(ext):
            return path[: -len(ext)]
    return ""


@dataclass(slots=True)
class TsConfig:
    """Repo-level TS resolution inputs: path aliases, workspace packages,
    npm lockfile versions. Built once per scan by :func:`read_ts_config`."""

    # (tsconfig dir, alias prefix, target prefix) — '*' handled as prefix map.
    aliases: tuple[tuple[str, str, str], ...] = ()
    # workspace package name → repo-relative package dir
    workspace_packages: dict[str, str] = field(default_factory=dict)
    # npm package name → pinned version (best-effort, lockfiles only)
    npm_versions: dict[str, str] = field(default_factory=dict)


def _load_json(path: Path) -> dict[str, object] | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        # tsconfig files commonly contain // comments and trailing commas.
        text = re.sub(r"//[^\n]*", "", text)
        text = re.sub(r",\s*([}\]])", r"\1", text)
        loaded = json.loads(text)
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _walk(repo_root: Path, name: str) -> list[Path]:
    out = []
    for path in sorted(repo_root.rglob(name)):
        if "node_modules" in path.parts or ".git" in path.parts:
            continue
        out.append(path)
    return out


def read_ts_config(repo_root: Path) -> TsConfig:
    """Collect tsconfig aliases, workspace package map, npm lock versions."""
    aliases: list[tuple[str, str, str]] = []
    for tsconfig in _walk(repo_root, "tsconfig.json"):
        data = _load_json(tsconfig)
        if data is None:
            continue
        opts = data.get("compilerOptions")
        if not isinstance(opts, dict):
            continue
        cfg_dir = tsconfig.parent.relative_to(repo_root).as_posix()
        cfg_dir = "" if cfg_dir == "." else cfg_dir
        base = opts.get("baseUrl") if isinstance(opts.get("baseUrl"), str) else "."
        paths = opts.get("paths")
        if not isinstance(paths, dict):
            continue
        for alias, targets in paths.items():
            if not isinstance(targets, list) or not targets:
                continue
            target = targets[0]
            if not isinstance(target, str):
                continue
            prefix_parts = [p for p in (cfg_dir, str(base).lstrip("./")) if p and p != "."]
            target_rel = "/".join([*prefix_parts, target.lstrip("./")])
            aliases.append(
                (cfg_dir, alias.rstrip("*").rstrip("/"), target_rel.rstrip("*").rstrip("/"))
            )

    workspace: dict[str, str] = {}
    for pkg_json in _walk(repo_root, "package.json"):
        data = _load_json(pkg_json)
        if data is None:
            continue
        name = data.get("name")
        if isinstance(name, str) and pkg_json.parent != repo_root:
            workspace[name] = pkg_json.parent.relative_to(repo_root).as_posix()

    npm_versions: dict[str, str] = {}
    lock = repo_root / "package-lock.json"
    if lock.is_file():
        data = _load_json(lock)
        packages = data.get("packages") if data else None
        if isinstance(packages, dict):
            for key, meta in packages.items():
                if not isinstance(meta, dict) or not isinstance(key, str):
                    continue
                version = meta.get("version")
                if key.startswith("node_modules/") and isinstance(version, str):
                    npm_versions[key[len("node_modules/") :]] = version
    pnpm = repo_root / "pnpm-lock.yaml"
    if pnpm.is_file():
        try:
            for line in pnpm.read_text(encoding="utf-8", errors="replace").splitlines():
                m = _PNPM_PKG.match(line)
                if m and line.rstrip().endswith(":"):
                    npm_versions.setdefault(m.group(1), m.group(2))
        except OSError:
            pass
    return TsConfig(
        aliases=tuple(sorted(aliases)), workspace_packages=workspace, npm_versions=npm_versions
    )


def _normalise(parts: list[str]) -> str:
    out: list[str] = []
    for part in parts:
        if part in ("", "."):
            continue
        if part == "..":
            if out:
                out.pop()
        else:
            out.append(part)
    return "/".join(out)


def _resolve_as_file(candidate: str, module_names: frozenset[str]) -> str | None:
    """A path-ish candidate → module name, honouring extension omission and
    index files (SPEC-4 §3 resolution order)."""
    stripped = ts_module_name(candidate)
    if stripped and stripped in module_names:
        return stripped
    if candidate in module_names:
        return candidate
    for suffix in ("", "/index", "/src/index"):
        name = candidate + suffix
        if name in module_names:
            return name
    return None


def resolve_specifier(
    importer_path: str,
    spec: str,
    module_names: frozenset[str],
    config: TsConfig,
) -> str:
    """Import specifier → internal TS module name, or the specifier itself
    (external npm package) when nothing in the parsed set matches."""
    if spec.startswith("."):
        base = importer_path.rsplit("/", 1)[0] if "/" in importer_path else ""
        candidate = _normalise([*base.split("/"), *spec.split("/")])
        return _resolve_as_file(candidate, module_names) or spec

    def _join(prefix: str, rest: str) -> str:
        parts = prefix.split("/")
        if rest:
            parts = [*parts, *rest.split("/")]
        return _normalise(parts)

    # tsconfig path aliases (longest alias prefix wins).
    for _, alias, target in sorted(config.aliases, key=lambda a: -len(a[1])):
        if spec == alias or spec.startswith(alias + "/"):
            candidate = _join(target, spec[len(alias) :].lstrip("/"))
            resolved = _resolve_as_file(candidate, module_names)
            if resolved is not None:
                return resolved

    # workspace packages: '@org/pkg' or '@org/pkg/sub' → package dir.
    for pkg_name, pkg_dir in sorted(
        config.workspace_packages.items(), key=lambda i: -len(i[0])
    ):
        if spec == pkg_name or spec.startswith(pkg_name + "/"):
            candidate = _join(pkg_dir, spec[len(pkg_name) :].lstrip("/"))
            resolved = _resolve_as_file(candidate, module_names)
            if resolved is not None:
                return resolved

    return spec  # external npm package
