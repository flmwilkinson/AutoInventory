"""F1 — generic YAML rule interpreter (SPEC §6.6).

Matching is on **resolved identity**, never surface text: a rule for
``agents.Agent`` fires only when the callee resolves to that fully-qualified
name. A user's own class named ``Agent`` never matches.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import ClassVar, Literal

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from aiscan.facts.models import (
    AgentDefF,
    AnyFact,
    ApiStyle,
    AttachPolicyF,
    BindMCPF,
    BindModelF,
    BindPromptF,
    BindToolF,
    MCPDefF,
    ModelRefF,
    PolicyDefF,
    PromptDefF,
    SignatureF,
    StateDefF,
    ToolDefF,
    TransferF,
    stable_id,
)
from aiscan.frontends.common import value_fqs
from aiscan.ir.nodes import (
    AssignS,
    CallE,
    ClassIR,
    DictE,
    Expr,
    FuncIR,
    ModuleIR,
    NameE,
    Span,
    StrE,
)
from aiscan.ir.values import (
    BoundArgs,
    ClassInstance,
    DictVal,
    FuncRef,
    JsonRepr,
    ListVal,
    PackageRef,
    Str,
    ValueSet,
    to_repr,
)
from aiscan.ir.walk import expr_children, iter_stmts
from aiscan.resolve.engine import Resolver, Scope
from aiscan.sinks.attribution import RUNG_UNRESOLVED, ModelAttribution, attribute_model
from aiscan.sinks.engine import (
    CallSite,
    Sink,
    SinkEngine,
    ctor_kwarg,
    iter_call_sites,
    path_language,
    path_location,
)
from aiscan.sinks.side_effects import classify_body

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# SPEC-9 A2 — agnostic agent-constructor fallback. A construction is treated as
# an agent when a model *resolves* AND an agent-shaped companion (tools /
# instructions) is present. The kwarg name-sets are generous supersets, but the
# gate is the resolved model, never the class name (surface text). A companion
# kwarg NAME present is a cheap syntactic prefilter before any resolution runs.
_AGENT_COMPANION_KW = frozenset(
    {
        "tools", "instructions", "system_prompt", "system_message", "system",
        "plugins", "handoffs", "agents", "prompt",
    }
)
_MODEL_KW: tuple[str, ...] = ("model_client", "model", "llm", "service", "chat_model")
# A value "looks like a model" if it is a model-adapter instance (checked
# structurally) or a string naming a known model family / provider-prefixed
# model. This is the content gate that keeps `model=SchemaV2` (a class ref) and
# `name='Researcher'` (a plain string) from counting as models.
_MODEL_ADAPTER_KW = ("model", "model_name", "modelName", "ai_model_id")
_MODEL_NAME_RE = re.compile(
    r"(?i)(?:^[a-z][a-z0-9_.-]*:[a-z0-9]"  # provider-prefixed: openai:gpt-4o
    r"|gpt[-.]?[0-9o]|o[1-4][-_]|claude|gemini|llama|mistral|mixtral|deepseek"
    r"|qwen|command[-_]?r|grok|phi-|gemma|text-embedding|whisper|dall-e)"
)
_INSTR_KW: tuple[str, ...] = (
    "system_message", "system_prompt", "instructions", "system", "prompt",
)
_TOOLS_KW: tuple[str, ...] = ("tools", "plugins", "functions", "toolset")


def slug(name: str) -> str:
    return _SLUG_RE.sub("-", name.lower()).strip("-") or "unnamed"


# Chat-model constructor classes whose instances carry model/endpoint kwargs
# (SPEC §6.6 langgraph pack: capture base_url/azure_endpoint/api_base).
_CHAT_MODEL_CLASSES: dict[str, dict[str, object]] = {
    "langchain_openai.ChatOpenAI": {
        "api_style": "openai",
        "provider": "openai",
        "model_kwargs": ("model", "model_name"),
        "endpoint_kwargs": ("base_url", "api_base"),
        "deployment_kwargs": (),
    },
    # SPEC-4 W4: LangChain.js chat models (dot-joined identity, options object).
    "@langchain/openai.ChatOpenAI": {
        "api_style": "openai",
        "provider": "openai",
        "model_kwargs": ("model", "modelName"),
        "endpoint_kwargs": ("baseURL", "basePath"),
        "deployment_kwargs": (),
    },
    "@langchain/anthropic.ChatAnthropic": {
        "api_style": "anthropic",
        "provider": "anthropic",
        "model_kwargs": ("model", "modelName"),
        "endpoint_kwargs": ("baseURL",),
        "deployment_kwargs": (),
    },
    "langchain_openai.AzureChatOpenAI": {
        "api_style": "openai",
        "provider": "azure-openai",
        "model_kwargs": ("model", "model_name"),
        "endpoint_kwargs": ("azure_endpoint", "base_url"),
        "deployment_kwargs": ("azure_deployment", "deployment_name"),
    },
    "langchain_anthropic.ChatAnthropic": {
        "api_style": "anthropic",
        "provider": "anthropic",
        "model_kwargs": ("model", "model_name"),
        "endpoint_kwargs": ("base_url",),
        "deployment_kwargs": (),
    },
    "langchain.chat_models.init_chat_model": {
        "api_style": "unknown",
        "provider": None,
        "model_kwargs": ("model",),
        "model_position": 0,
        "endpoint_kwargs": ("base_url",),
        "deployment_kwargs": (),
    },
}


# -- rule schema -------------------------------------------------------------


class RuleMatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["call", "decorator", "class_base", "config_file"]
    callee_fqname: tuple[str, ...] = ()
    base_fqname: tuple[str, ...] = ()
    glob: tuple[str, ...] = ()

    @field_validator("callee_fqname", "base_fqname", "glob", mode="before")
    @classmethod
    def _coerce(cls, v: object) -> object:
        if isinstance(v, str):
            return (v,)
        return v


class ExtractSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    # A single position/kwarg, or a list of them tried in order (first that is
    # present wins) — lets one rule accept an arg passed positionally *or* by
    # keyword (e.g. AutoGen `AssistantAgent("name", client)` vs
    # `AssistantAgent(name=..., model_client=...)`).
    arg: int | str | tuple[int | str, ...]
    resolve: Literal[
        "string", "model_ref", "prompt", "ref", "ref_list", "policy", "value", "raw"
    ]
    required: bool = False
    endpoint_args: tuple[str, ...] = ()  # model_ref only: kwargs holding an endpoint


class ModelDefaults(BaseModel):
    model_config = ConfigDict(frozen=True)

    api_style: ApiStyle = "unknown"
    provider: str | None = None
    # SPEC-7 Z10: fq of the framework's default-model resolution point
    # (e.g. agents.models.default_models.get_default_model). Resolved as a
    # zero-arg call when the agent ctor passes no model — only when that
    # function's source is in the analysis set; consumer repos stay honest-null.
    default_ref: str | None = None


class Rule(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    match: RuleMatch
    extract: dict[str, ExtractSpec] = {}
    model_defaults: ModelDefaults = ModelDefaults()
    emit: tuple[dict[str, str], ...] = ()


class RulePack(BaseModel):
    model_config = ConfigDict(frozen=True)

    framework: str
    version: str
    rules: tuple[Rule, ...]


def load_packs(pack_dir: Path | None = None) -> tuple[RulePack, ...]:
    d = pack_dir or Path(__file__).parent / "packs"
    packs = []
    for p in sorted(d.glob("*.yaml")):
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
        packs.append(RulePack.model_validate(raw))
    return tuple(packs)


# -- extraction results ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgentRef:
    """A reference to another agent (by resolved constructor name)."""

    name: str
    framework: str


RefItem = ToolDefF | MCPDefF | AgentRef


@dataclass(slots=True)
class Extracted:
    """One extracted field: JSON projection, resolved values, built facts."""

    repr: JsonRepr = None
    values: ValueSet | None = None
    model: ModelRefF | None = None
    prompt: PromptDefF | None = None
    refs: tuple[RefItem, ...] = ()
    side_facts: tuple[AnyFact, ...] = ()  # e.g. approval policies from MCP refs


@dataclass(frozen=True, slots=True)
class Candidate:
    """An AgentCandidate awaiting sink-based promotion (SPEC §6.6)."""

    name: str
    fn_fq: str
    framework: str
    rule_id: str
    evidence: str
    path: str


@dataclass(frozen=True, slots=True)
class RouteEdge:
    from_name: str
    to_name: str
    guard_fq: str | None
    framework: str
    rule_id: str
    evidence: str
    path: str


@dataclass(frozen=True, slots=True)
class CrewSpec:
    agent_names: tuple[str, ...]  # in task order
    process: str
    framework: str
    rule_id: str
    evidence: str
    path: str


@dataclass(slots=True)
class F1Result:
    facts: list[AnyFact] = field(default_factory=list)
    rulepack_versions: dict[str, str] = field(default_factory=dict)
    unpromoted_candidates: int = 0
    consumed_sink_spans: set[Span] = field(default_factory=set)
    # SPEC-8: entrypoint marks as (framework, agent name, source module) so an
    # incremental scan can cache them per module and merge — an entrypoint is
    # controlled by a `Runner.run` in one module but flags an agent in another.
    entrypoint_marks: list[tuple[str, str, str]] = field(default_factory=list)


class FrameworkEngine:
    """Applies rule packs across the module set and emits facts."""

    def __init__(
        self,
        resolver: Resolver,
        sink_engine: SinkEngine,
        packs: tuple[RulePack, ...],
        llm_sink_spans: frozenset[Span] = frozenset(),
        sinks: tuple[Sink, ...] = (),
    ) -> None:
        self.resolver = resolver
        self.sinks = sink_engine
        self.packs = packs
        self.llm_sink_spans = llm_sink_spans
        self.sink_list = sinks
        self._fqset_cache: dict[tuple[str, ...], frozenset[str]] = {}
        self._agent_ctor_rules: dict[str, tuple[RulePack, Rule]] = {}
        for pack in packs:
            for rule in pack.rules:
                if rule.match.kind == "call" and any(
                    e.get("fact") == "AgentDef" for e in rule.emit
                ):
                    for fq in self._fqset(rule.match.callee_fqname):
                        self._agent_ctor_rules[fq] = (pack, rule)
        # base agent id -> the (file, line) that owns the bare (undisambiguated)
        # id. Compared by location, not full Span, so an incremental scan can
        # seed it from cached agents and reproduce the full scan's ids (SPEC-8).
        self._used_agent_ids: dict[str, tuple[str, int]] = {}
        self._candidates: list[Candidate] = []
        self._routes: list[RouteEdge] = []
        self._crews: list[CrewSpec] = []
        self._mcp_flavors_canonical: dict[str, str] | None = None
        # Synthesized rule/pack for the A2 fallback so it can drive the existing
        # `_extract`/`_extract_model` machinery (which need a rule for its id +
        # model_defaults) without a YAML pack.
        self._generic_rule = Rule(
            id="agent_shape:ctor", match=RuleMatch(kind="call"), model_defaults=ModelDefaults()
        )
        self._generic_pack = RulePack(
            framework="generic", version="0.1", rules=(self._generic_rule,)
        )

    def _fqset(self, fqs: tuple[str, ...]) -> frozenset[str]:
        """A rule's identities plus their canonical (re-export-resolved) forms,
        so a construct matches whether seen as a third-party import or resolved
        into vendored/monorepo source."""
        cached = self._fqset_cache.get(fqs)
        if cached is not None:
            return cached
        out = set(fqs)
        for fq in fqs:
            out.add(self.resolver.canonicalize_fq(fq))
        result = frozenset(out)
        self._fqset_cache[fqs] = result
        return result

    # -- driving ------------------------------------------------------------

    def run(
        self,
        analyze_only: frozenset[str] | None = None,
        carried_facts: list[AnyFact] | None = None,
        carried_entrypoint_marks: list[tuple[str, str, str]] | None = None,
    ) -> F1Result:
        """Run the framework frontend.

        For an incremental scan (SPEC-8), ``analyze_only`` limits per-module rule
        application to the affected modules; ``carried_facts`` injects the cached
        facts of unaffected modules *before* the cross-module post-passes (so
        routes/entrypoints that reference a cached agent still resolve); and
        ``carried_entrypoint_marks`` supplies the unaffected modules' entrypoint
        marks (an entrypoint is set by a `Runner.run` in one module but flags an
        agent in another, so the marks are cached per module rather than
        re-derived). Resolution is always whole-program. With all ``None`` this
        is the full-scan path, byte-identical to before."""
        result = F1Result()
        for pack in self.packs:
            result.rulepack_versions[pack.framework] = pack.version
        if carried_facts:
            self._seed_agent_ids(carried_facts)
        for module_name in sorted(self.resolver.graph.by_name):
            if analyze_only is not None and module_name not in analyze_only:
                continue
            mod = self.resolver.graph.by_name[module_name]
            self._run_call_rules(mod, module_name, result)
            self._run_decorator_rules(mod, module_name, result)
            self._run_class_rules(mod, module_name, result)
        self._run_config_rules(result)
        if carried_entrypoint_marks:
            result.entrypoint_marks.extend(carried_entrypoint_marks)
        # Merge in the unaffected modules' cached facts so the global post-passes
        # (promotion, routes, crews, entrypoints) see the full agent set.
        if carried_facts:
            result.facts.extend(carried_facts)
        self._promote_candidates(result)
        self._emit_routes(result)
        self._emit_crews(result)
        self._apply_entrypoints(result, reset=analyze_only is not None)
        return result

    def _run_call_rules(self, mod: ModuleIR, module_name: str, result: F1Result) -> None:
        call_rules = [
            (pack, rule)
            for pack in self.packs
            for rule in pack.rules
            if rule.match.kind == "call"
        ]
        if not call_rules:
            return
        for site in iter_call_sites(mod, module_name):
            scope = self.sinks.scope_for_site(site)
            fqs = self._callee_fqs(site, scope)
            if not fqs:
                continue
            matched = False
            for pack, rule in call_rules:
                if fqs & self._fqset(rule.match.callee_fqname):
                    self._apply_rule(pack, rule, site, scope, result)
                    matched = True
            # SPEC-9 A2: no pack claimed this site — try the agnostic
            # resolved-model fallback so a renamed/moved/unknown framework agent
            # is still detected. Reuses the callee resolution already in hand.
            if not matched:
                self._match_agent_ctor(site, scope, fqs, result)

    def _run_decorator_rules(self, mod: ModuleIR, module_name: str, result: F1Result) -> None:
        deco_rules = [
            (pack, rule)
            for pack in self.packs
            for rule in pack.rules
            if rule.match.kind == "decorator"
        ]
        if not deco_rules:
            return
        funcs = list(mod.defs) + [m for cls in mod.classes for m in cls.methods]
        for fn in funcs:
            for dec in fn.decorators:
                dec_fqs = self._expr_fqs(dec, module_name, None)
                for pack, rule in deco_rules:
                    if dec_fqs & self._fqset(rule.match.callee_fqname):
                        self._emit_tool_from_def(pack, rule, fn, module_name, result)

    def _run_class_rules(self, mod: ModuleIR, module_name: str, result: F1Result) -> None:
        base_rules = [
            (pack, rule)
            for pack in self.packs
            for rule in pack.rules
            if rule.match.kind == "class_base"
        ]
        if not base_rules:
            return
        for cls in mod.classes:
            base_fqs: set[str] = set()
            for b in cls.bases:
                base_fqs |= self._expr_fqs(b, module_name, None)
            for _pack, rule in base_rules:
                if base_fqs & self._fqset(rule.match.base_fqname):
                    self._emit_tool_from_class(rule, cls, module_name, result)

    def _emit_tool_from_class(
        self, rule: Rule, cls: ClassIR, module: str, result: F1Result
    ) -> None:
        name = cls.name
        for a in cls.body_assigns:
            for t in a.targets:
                if isinstance(t, NameE) and t.name == "name" and isinstance(a.value, StrE):
                    name = a.value.value
        run_method = next((m for m in cls.methods if m.name in ("_run", "run")), None)
        params: tuple[str, ...] = ()
        effects: tuple[str, ...] = ()
        report = None
        if run_method is not None:
            report = classify_body(
                run_method, module, self.resolver, self.sinks.org_pack, self.llm_sink_spans
            )
            params = tuple(
                p.name
                for p in run_method.params
                if p.kind in ("pos", "kwonly") and p.name != "self"
            )
            effects = report.effects
        result.facts.append(
            ToolDefF(
                id=f"tool:{module}.{cls.name}",
                evidence=(str(cls.span),),
                confidence="high",
                method=rule.id,
                source_files=(self._path_of(module),),
                name=name,
                kind="function",
                signature=SignatureF(params=params),
                side_effects=effects,  # type: ignore[arg-type]
                external_target=report.external_target if report else None,
                credential_ref=report.credential_ref if report else None,
                auth_verb=report.http_verb if report else None,
            )
        )

    # -- config-file rules ---------------------------------------------------

    def _run_config_rules(self, result: F1Result) -> None:
        cfg_rules = [
            (pack, rule)
            for pack in self.packs
            for rule in pack.rules
            if rule.match.kind == "config_file"
        ]
        repo_root = self.sinks.repo_root
        if not cfg_rules or repo_root is None:
            return
        for rel in _discover_config_files(repo_root):
            for _pack, rule in cfg_rules:
                if not _glob_match(rel, rule.match.glob):
                    continue
                for spec in rule.emit:
                    match spec.get("fact"):
                        case "MCPFromConfig":
                            self._emit_mcp_config(repo_root, rel, rule, result)
                        case "PromptFromFile":
                            self._emit_prompt_file(repo_root, rel, rule, result)
                        case "ToolFromSkill":
                            self._emit_skill_tool(repo_root, rel, rule, result)
                        case _:
                            continue

    def _emit_mcp_config(
        self, repo_root: Path, rel: str, rule: Rule, result: F1Result
    ) -> None:
        try:
            data = json.loads((repo_root / rel).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        if not isinstance(servers, dict):
            return
        for name in sorted(servers):
            cfg = servers[name]
            if not isinstance(cfg, dict):
                continue
            url = cfg.get("url")
            command = cfg.get("command")
            if isinstance(url, str):
                transport: Literal["stdio", "sse", "http", "ws"] = (
                    "sse" if url.endswith("/sse") else "http"
                )
                server: JsonRepr = url
            else:
                transport = "stdio"
                args = cfg.get("args")
                arg_s = " ".join(str(a) for a in args) if isinstance(args, list) else ""
                server = f"{command} {arg_s}".strip() if isinstance(command, str) else None
            declared = cfg.get("tools")
            declared_tools: tuple[str, ...] | Literal["dynamic"] = (
                tuple(str(t) for t in declared) if isinstance(declared, list) else "dynamic"
            )
            result.facts.append(
                MCPDefF(
                    id=f"mcp:{slug(name)}",
                    evidence=(f"{rel}:1-1",),
                    confidence="high",
                    method=rule.id,
                    source_files=(rel,),
                    server=server,
                    transport=transport,
                    declared_tools=declared_tools,
                )
            )

    def _emit_prompt_file(
        self, repo_root: Path, rel: str, rule: Rule, result: F1Result
    ) -> None:
        try:
            text = (repo_root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        result.facts.append(
            PromptDefF(
                id=f"prompt:{digest}",
                evidence=(f"{rel}:1-1",),
                confidence="medium",
                method=rule.id,
                source_files=(rel,),
                content=text,
                dynamic=False,
                origin="file",
                file_ref=rel,
                content_hash=digest,
            )
        )

    def _emit_skill_tool(
        self, repo_root: Path, rel: str, rule: Rule, result: F1Result
    ) -> None:
        try:
            text = (repo_root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        front = _yaml_frontmatter(text)
        if front is None:
            return
        name = front.get("name")
        if not isinstance(name, str):
            return
        result.facts.append(
            ToolDefF(
                id=f"tool:skill:{slug(name)}",
                evidence=(f"{rel}:1-1",),
                confidence="high",
                method=rule.id,
                source_files=(rel,),
                name=name,
                kind="schema_declared",
            )
        )

    # -- identity ------------------------------------------------------------

    def _callee_fqs(self, site: CallSite, scope: Scope | None) -> set[str]:
        base, path = self.resolver.chain_root(site.call.callee, site.module, scope)
        return _fqs_of(base, path)

    def _expr_fqs(self, expr: Expr, module: str, scope: Scope | None) -> set[str]:
        node = expr
        if isinstance(node, CallE):
            node = node.callee
        base, path = self.resolver.chain_root(node, module, scope)
        return _fqs_of(base, path)

    # -- rule application ----------------------------------------------------

    def _apply_rule(
        self,
        pack: RulePack,
        rule: Rule,
        site: CallSite,
        scope: Scope | None,
        result: F1Result,
    ) -> None:
        extracted: dict[str, Extracted] = {}
        for name, spec in rule.extract.items():
            value = self._extract(spec, site, scope, pack, rule)
            if value is None and spec.required:
                return
            extracted[name] = value if value is not None else Extracted()
        self._emit(pack, rule, site, extracted, result)

    def _match_agent_ctor(
        self, site: CallSite, scope: Scope | None, fqs: set[str], result: F1Result
    ) -> None:
        """SPEC-9 A2: the agnostic fallback for a call site no pack claimed. Emit
        a medium-confidence agent iff a model *resolves* AND an agent-shaped
        companion (tools/instructions) resolves. The class name is never a gate —
        that keeps the adversarial local ``Agent(name, schedule)`` out (no model
        resolves) while catching a framework that renamed/moved its agent class
        (its ``model_client=…`` still resolves)."""
        call = site.call
        kw_names = {k.name for k in call.kwargs if k.name is not None}
        if not (kw_names & _AGENT_COMPANION_KW):
            return  # cheap syntactic prefilter — not agent-shaped, no resolution
        # An agent is a class *construction*, never a method call on an instance:
        # `client.messages.create(model=…, system=…)` resolves to a ClassInstance
        # base (that is a raw LLM sink carrying model+system, handled elsewhere),
        # whereas `Assistant(…)` / `pkg.Assistant(…)` resolve to a class/package.
        base, _path = self.resolver.chain_root(call.callee, site.module, scope)
        if any(isinstance(v, ClassInstance) for v in base):
            return

        model = self._agent_model(site, scope, kw_names)
        if model is None or model.model is None:
            return

        gp, gr = self._generic_pack, self._generic_rule
        instr = self._extract(
            ExtractSpec(arg=_INSTR_KW, resolve="prompt"), site, scope, gp, gr
        )
        tools = self._extract(
            ExtractSpec(arg=_TOOLS_KW, resolve="ref_list"), site, scope, gp, gr
        )
        has_companion = (tools is not None and tools.refs) or (
            instr is not None and instr.prompt is not None
        )
        if not has_companion:
            return  # a bare model call is a sink/usage, not an agent

        name = self._assign_target_name(site) or sorted(fqs)[0].rsplit(".", 1)[-1]
        agent_id = self._agent_id("generic", name, call.span)
        evidence = (str(call.span),)
        source = (site.path,)

        def bk(fact_id: str) -> dict[str, object]:
            return {
                "id": fact_id,
                "evidence": evidence,
                "confidence": "medium",
                "method": "agent_shape:ctor",
                "source_files": source,
            }

        result.facts.append(
            AgentDefF(
                **bk(agent_id),  # type: ignore[arg-type]
                name=name,
                kind="bespoke",
                framework="generic",
                location=path_location(site.path),
                language=path_language(site.path),
            )
        )
        result.facts.append(model.model)
        result.facts.append(
            BindModelF(
                **bk(stable_id("bind", agent_id, model.model.id)),  # type: ignore[arg-type]
                agent_id=agent_id,
                model_id=model.model.id,
            )
        )
        if instr is not None and instr.prompt is not None:
            result.facts.append(instr.prompt)
            result.facts.append(
                BindPromptF(
                    **bk(stable_id("bind", agent_id, instr.prompt.id)),  # type: ignore[arg-type]
                    agent_id=agent_id,
                    prompt_id=instr.prompt.id,
                )
            )
        if tools is not None:
            result.facts.extend(tools.side_facts)
            for item in tools.refs:
                if isinstance(item, ToolDefF):
                    result.facts.append(item)
                    result.facts.append(
                        BindToolF(
                            **bk(stable_id("bind", agent_id, item.id)),  # type: ignore[arg-type]
                            agent_id=agent_id,
                            tool_id=item.id,
                        )
                    )

    def _agent_model(
        self, site: CallSite, scope: Scope | None, kw_names: set[str]
    ) -> Extracted | None:
        """The agent's model, if any kwarg holds one — a model-named kwarg whose
        value looks like a model, else any other kwarg whose *value* is a model
        adapter / model string (name-independent, so a renamed model kwarg is
        still caught). ``_is_model_value`` is the gate that rejects a bare class
        ref (``model=SchemaV2``) or a plain string (``name='Researcher'``)."""
        gp, gr = self._generic_pack, self._generic_rule
        candidates: list[str] = [k for k in _MODEL_KW if k in kw_names]
        candidates += [
            k.name
            for k in site.call.kwargs
            if k.name is not None
            and k.name not in _MODEL_KW
            and k.name not in _AGENT_COMPANION_KW
        ]
        for name in candidates:
            expr = self._arg_expr(site.call, name)
            if expr is None:
                continue
            vals = self.resolver.resolve(expr, site.module, scope)
            if not _is_model_value(vals):
                continue
            ex = self._extract(
                ExtractSpec(arg=(name,), resolve="model_ref"), site, scope, gp, gr
            )
            if ex is not None and ex.model is not None and ex.model.method != RUNG_UNRESOLVED:
                return ex
        return None

    def _arg_expr(self, call: CallE, arg: int | str | tuple[int | str, ...]) -> Expr | None:
        if isinstance(arg, tuple):
            for candidate in arg:
                expr = self._arg_expr(call, candidate)
                if expr is not None:
                    return expr
            return None
        if isinstance(arg, int):
            return call.args[arg] if arg < len(call.args) else None
        for k in call.kwargs:
            if k.name == arg:
                return k.value
        # JS convention: constructor options as a single object literal —
        # `new Agent({ name, instructions, tools })` (SPEC-4 W4). Python kwargs
        # are matched first, so this only adds the object-member fallback.
        if call.args and isinstance(call.args[0], DictE):
            for entry in call.args[0].entries:
                if isinstance(entry.key, StrE) and entry.key.value == arg:
                    return entry.value
        return None

    def _extract(
        self,
        spec: ExtractSpec,
        site: CallSite,
        scope: Scope | None,
        pack: RulePack,
        rule: Rule,
    ) -> Extracted | None:
        if spec.arg == "__assign_target__":
            return Extracted(repr=self._assign_target_name(site) or "agent")
        expr = self._arg_expr(site.call, spec.arg)
        if expr is None:
            if spec.resolve == "model_ref" and rule.model_defaults.default_ref:
                # SPEC-7 Z10: no model argument → the framework default applies;
                # resolve the pack-declared resolution point from source.
                default_vals = self.resolver.resolve_default_call(
                    rule.model_defaults.default_ref
                )
                if default_vals:
                    return self._extract_model(
                        spec, site, scope, None, default_vals, rule
                    )
            return None
        vals = self.resolver.resolve(expr, site.module, scope)
        match spec.resolve:
            case "model_ref":
                return self._extract_model(spec, site, scope, expr, vals, rule)
            case "prompt":
                prompt = self.sinks.build_prompt_fact(vals, site, method=rule.id)
                return Extracted(prompt=prompt, values=vals)
            case "ref" | "ref_list":
                return self._extract_refs(site, vals, rule)
            case _:  # "string" | "value" | "policy"
                return Extracted(repr=to_repr(vals), values=vals)

    def _extract_model(
        self,
        spec: ExtractSpec,
        site: CallSite,
        scope: Scope | None,
        expr: Expr | None,
        vals: ValueSet,
        rule: Rule,
    ) -> Extracted:
        defaults = rule.model_defaults
        api_style: ApiStyle = defaults.api_style
        provider = defaults.provider
        deployment: JsonRepr = None
        endpoint: JsonRepr = None
        attribution: ModelAttribution

        instance = _single(vals, ClassInstance)
        chat_cfg = _CHAT_MODEL_CLASSES.get(instance.class_fq) if instance else None
        if instance is not None and chat_cfg is not None:
            model_vals: ValueSet | None = None
            for k in _as_str_tuple(chat_cfg.get("model_kwargs")):
                model_vals = ctor_kwarg(instance, k)
                if model_vals:
                    break
            if model_vals is None and "model_position" in chat_cfg:
                model_vals = instance.ctor_args.get("", position=0)
            attribution = attribute_model(None, model_vals)
            for k in _as_str_tuple(chat_cfg.get("endpoint_kwargs")):
                ev = ctor_kwarg(instance, k)
                if ev:
                    endpoint = to_repr(ev)
                    break
            for k in _as_str_tuple(chat_cfg.get("deployment_kwargs")):
                dv = ctor_kwarg(instance, k)
                if dv:
                    deployment = to_repr(dv)
                    break
            style = chat_cfg.get("api_style")
            if isinstance(style, str):
                api_style = style  # type: ignore[assignment]
            prov = chat_cfg.get("provider")
            provider = prov if isinstance(prov, str) else None
        elif instance is not None:
            # SPEC-7 Z10: ANY adapter-class model instance — the constructor's
            # model kwarg IS the model identity a BOM needs (proven total loss:
            # gpt-oss:20b behind OpenAIChatCompletionsModel(model=...,
            # openai_client=AsyncOpenAI(base_url="http://localhost:11434/v1"))).
            # Generic detection mirroring the registry kwarg vocabulary; a
            # class with no model-named ctor kwarg (test fakes) keeps today's
            # class-row behaviour.
            model_vals = None
            # ``ai_model_id`` is Semantic Kernel's model kwarg
            # (`OpenAIChatCompletion(ai_model_id=...)`).
            for k in ("model", "model_name", "modelName", "ai_model_id"):
                model_vals = ctor_kwarg(instance, k)
                if model_vals:
                    break
            attribution = (
                attribute_model(None, model_vals)
                if model_vals
                else attribute_model(expr, vals)
            )
            for k in ("base_url", "baseURL", "api_base"):
                ev = ctor_kwarg(instance, k)
                if ev:
                    endpoint = to_repr(ev)
                    break
            if endpoint is None:
                # One level into a nested client instance (openai_client=...).
                for _name, arg_vals in instance.ctor_args.named:
                    inner = next(
                        (
                            v
                            for v in sorted(arg_vals, key=repr)
                            if isinstance(v, ClassInstance)
                        ),
                        None,
                    )
                    if inner is None:
                        continue
                    for k in ("base_url", "baseURL", "api_base"):
                        ev = ctor_kwarg(inner, k)
                        if ev:
                            endpoint = to_repr(ev)
                            break
                    if endpoint is not None:
                        break
        else:
            attribution = attribute_model(expr, vals)
            for k in spec.endpoint_args:
                e = self._arg_expr(site.call, k)
                if e is not None:
                    endpoint = to_repr(self.resolver.resolve(e, site.module, scope))
                    break

        model = ModelRefF(
            id=stable_id("model", api_style, attribution.model, endpoint),
            evidence=(str(site.call.span),),
            confidence="high",
            method=attribution.method,
            source_files=(site.path,),
            provider=provider,
            model=attribution.model,
            endpoint=endpoint,
            deployment=deployment,
            api_style=api_style,
            task="chat",
        )
        return Extracted(model=model, values=vals)

    def _extract_refs(self, site: CallSite, vals: ValueSet, rule: Rule) -> Extracted:
        items: list[tuple[RefItem, tuple[AnyFact, ...]]] = []
        elements: list[ValueSet] = []
        lv = _single(vals, ListVal)
        if lv is not None:
            elements = list(lv.elems)
        else:
            elements = [vals]
        for elem in elements:
            for v in sorted(elem, key=repr):
                item = self._ref_item(v, site, rule)
                if item is not None:
                    items.append(item)
                    break
        return Extracted(
            refs=tuple(i for i, _ in items),
            side_facts=tuple(f for _, facts in items for f in facts),
        )

    def _ref_item(
        self, v: object, site: CallSite, rule: Rule
    ) -> tuple[RefItem, tuple[AnyFact, ...]] | None:
        if isinstance(v, FuncRef):
            tool = self.tool_from_fq(v.fq, str(site.call.span), site.path, rule.id)
            if tool is not None:
                return tool, ()
        if isinstance(v, ClassInstance):
            agent_rule = self._agent_ctor_rules.get(v.class_fq)
            if agent_rule is not None:
                pack, arule = agent_rule
                name = self._agent_name_from_instance(v, arule)
                if name is not None:
                    return AgentRef(name=name, framework=pack.framework), ()
            mcp = self.mcp_from_instance(v, str(site.call.span), site.path, rule.id)
            if mcp is not None:
                return mcp[0], mcp[1]
        return None

    def _assign_target_name(self, site: CallSite) -> str | None:
        """Name of the variable this call is assigned to (for unnamed agents
        like ``agent = create_react_agent(...)``)."""
        mod = self.resolver.graph.by_name.get(site.module)
        if mod is None:
            return None
        for stmt in iter_stmts(mod.body, into_defs=True):
            if not isinstance(stmt, AssignS):
                continue
            stack: list[Expr] = [stmt.value]
            while stack:
                e = stack.pop()
                if e is site.call:
                    for t in stmt.targets:
                        if isinstance(t, NameE):
                            return t.name
                    return None
                stack.extend(expr_children(e))
        return None

    def _agent_name_from_instance(self, v: ClassInstance, rule: Rule) -> str | None:
        spec = rule.extract.get("name")
        if spec is None or not isinstance(spec.arg, str):
            return None
        name_vals = ctor_kwarg(v, spec.arg)  # named kwarg or JS options-object member
        if name_vals is None:
            return None
        s = _single(name_vals, Str)
        return s.s if s else None

    # -- entity builders -----------------------------------------------------

    def tool_from_fq(
        self, fq: str, evidence: str, path: str, method: str
    ) -> ToolDefF | None:
        from aiscan.frontends.common import build_tool_from_fq

        return build_tool_from_fq(
            self.resolver, self.sinks.org_pack, self.llm_sink_spans, fq, method, path
        )

    _MCP_CLASSES: ClassVar[dict[str, str]] = {
        "agents.HostedMCPTool": "hosted",
        "agents.mcp.MCPServerStdio": "stdio",
        "agents.MCPServerStdio": "stdio",
        "agents.mcp.MCPServerSse": "sse",
        "agents.MCPServerSse": "sse",
        "agents.mcp.MCPServerStreamableHttp": "http",
        "agents.MCPServerStreamableHttp": "http",
    }

    def _mcp_flavor(self, fq: str) -> str | None:
        """``_MCP_CLASSES`` lookup that also accepts the canonical
        (re-export-resolved) form of each public path, so the framework's own
        monorepo source (``agents.mcp.server.MCPServerStdio``) matches like a
        consumer repo's import — same rule as ``_fqset`` (SPEC-1 §7.2)."""
        if self._mcp_flavors_canonical is None:
            flavors = dict(self._MCP_CLASSES)
            for public, flavor in self._MCP_CLASSES.items():
                flavors.setdefault(self.resolver.canonicalize_fq(public), flavor)
            self._mcp_flavors_canonical = flavors
        return self._mcp_flavors_canonical.get(fq)

    def mcp_from_instance(
        self, v: ClassInstance, evidence: str, path: str, method: str
    ) -> tuple[MCPDefF, tuple[AnyFact, ...]] | None:
        flavor = self._mcp_flavor(v.class_fq)
        if flavor is None:
            return None
        label: str | None = None
        server: JsonRepr = None
        transport: Literal["stdio", "sse", "http", "ws"] = "http"
        approval: JsonRepr = None
        if flavor == "hosted":
            cfg_vals = v.ctor_args.get("tool_config", position=0)
            cfg = _single(cfg_vals, DictVal) if cfg_vals else None
            if cfg is not None:
                label = _str_of(cfg.get("server_label"))
                url = _str_of(cfg.get("server_url"))
                server = url
                transport = "sse" if url and url.endswith("/sse") else "http"
                approval_vals = cfg.get("require_approval")
                if approval_vals is not None:
                    approval = to_repr(approval_vals)
        else:
            transport = "stdio" if flavor == "stdio" else "sse" if flavor == "sse" else "http"
            label = _str_of(v.ctor_args.get("name"))
            params_vals = v.ctor_args.get("params", position=0)
            params = _single(params_vals, DictVal) if params_vals else None
            if params is not None:
                if flavor == "stdio":
                    cmd = _str_of(params.get("command"))
                    server = cmd
                else:
                    server = _str_of(params.get("url"))
        mcp_id = f"mcp:{slug(label)}" if label else stable_id("mcp", server)
        mcp = MCPDefF(
            id=mcp_id,
            evidence=(evidence,),
            confidence="high",
            method=method,
            source_files=(path,),
            server=server,
            transport=transport,
            declared_tools="dynamic",
            approval_policy=approval,
        )
        side: tuple[AnyFact, ...] = ()
        if approval is not None:
            policy = PolicyDefF(
                id=stable_id("policy", "approval", mcp_id),
                evidence=(evidence,),
                confidence="high",
                method=method,
                source_files=(path,),
                kind="approval",
                params=approval,
            )
            attach = AttachPolicyF(
                id=stable_id("attach", policy.id, mcp_id),
                evidence=(evidence,),
                confidence="high",
                method=method,
                source_files=(path,),
                policy_id=policy.id,
                target_id=mcp_id,
            )
            side = (policy, attach)
        return mcp, side

    # -- emission ------------------------------------------------------------

    def _seed_agent_ids(self, carried_facts: list[AnyFact]) -> None:
        """Pre-register the bare (undisambiguated) agent ids owned by cached,
        unaffected agents, so a re-analysed agent of the same name gets the same
        disambiguated ``@file:line`` id it would in a full scan (SPEC-8). Only
        bare-id owners are seeded: if the bare owner was itself affected (its
        cached fact dropped), the base stays free and the fresh owner correctly
        reclaims the bare id — matching the full scan's deterministic order."""
        for fact in carried_facts:
            if not isinstance(fact, AgentDefF) or "@" in fact.id or not fact.evidence:
                continue
            file, _, line = fact.evidence[0].rpartition(":")
            if file and line.split("-")[0].isdigit():
                self._used_agent_ids[fact.id] = (file, int(line.split("-")[0]))

    def _agent_id(self, framework: str, name: str, span: Span) -> str:
        base = f"agent:framework:{slug(name)}"
        key = (span.file, span.line_start)
        prior = self._used_agent_ids.get(base)
        if prior is not None and prior != key:
            return f"{base}@{span.file}:{span.line_start}"
        self._used_agent_ids[base] = key
        return base

    def _emit(
        self,
        pack: RulePack,
        rule: Rule,
        site: CallSite,
        extracted: dict[str, Extracted],
        result: F1Result,
    ) -> None:
        evidence = (str(site.call.span),)
        source = (site.path,)
        agent_id: str | None = None

        def base_kwargs(fact_id: str) -> dict[str, object]:
            return {
                "id": fact_id,
                "evidence": evidence,
                "confidence": "high",
                "method": rule.id,
                "source_files": source,
            }

        def get(fieldref: str) -> Extracted | None:
            return extracted.get(fieldref.removeprefix("$"))

        for spec in rule.emit:
            fact_kind = spec.get("fact")
            match fact_kind:
                case "AgentDef":
                    name_ex = get(spec.get("name", "$name"))
                    name = (
                        name_ex.repr
                        if name_ex is not None and isinstance(name_ex.repr, str)
                        else None
                    )
                    if name is None:
                        return
                    agent_id = self._agent_id(pack.framework, name, site.call.span)
                    result.facts.append(
                        AgentDefF(
                            **base_kwargs(agent_id),  # type: ignore[arg-type]
                            name=name,
                            kind="framework",
                            framework=spec.get("framework", pack.framework),
                            location=path_location(site.path),
                            language=path_language(site.path),
                        )
                    )
                case "BindModel":
                    ex = get(spec.get("model", "$model"))
                    if ex is not None and ex.model is not None and agent_id is not None:
                        result.facts.append(ex.model)
                        result.facts.append(
                            BindModelF(
                                **base_kwargs(stable_id("bind", agent_id, ex.model.id)),  # type: ignore[arg-type]
                                agent_id=agent_id,
                                model_id=ex.model.id,
                            )
                        )
                case "BindPrompt":
                    ex = get(spec.get("prompt", "$instructions"))
                    if ex is not None and ex.prompt is not None and agent_id is not None:
                        result.facts.append(ex.prompt)
                        result.facts.append(
                            BindPromptF(
                                **base_kwargs(stable_id("bind", agent_id, ex.prompt.id)),  # type: ignore[arg-type]
                                agent_id=agent_id,
                                prompt_id=ex.prompt.id,
                            )
                        )
                case "BindComponents":
                    ex = get(spec.get("items", "$tools"))
                    if ex is None or agent_id is None:
                        continue
                    result.facts.extend(ex.side_facts)
                    for item in ex.refs:
                        if isinstance(item, ToolDefF):
                            result.facts.append(item)
                            result.facts.append(
                                BindToolF(
                                    **base_kwargs(stable_id("bind", agent_id, item.id)),  # type: ignore[arg-type]
                                    agent_id=agent_id,
                                    tool_id=item.id,
                                )
                            )
                        elif isinstance(item, MCPDefF):
                            result.facts.append(item)
                            result.facts.append(
                                BindMCPF(
                                    **base_kwargs(stable_id("bind", agent_id, item.id)),  # type: ignore[arg-type]
                                    agent_id=agent_id,
                                    mcp_id=item.id,
                                )
                            )
                case "Transfer":
                    ex = get(spec.get("targets", "$handoffs"))
                    if ex is None or agent_id is None:
                        continue
                    kind = spec.get("kind", "handoff")
                    for item in ex.refs:
                        if isinstance(item, AgentRef):
                            to_id = f"agent:framework:{slug(item.name)}"
                            result.facts.append(
                                TransferF(
                                    **base_kwargs(stable_id("transfer", agent_id, to_id, kind)),  # type: ignore[arg-type]
                                    from_id=agent_id,
                                    to_id=to_id,
                                    kind=kind,  # type: ignore[arg-type]
                                )
                            )
                case "MarkEntrypoint":
                    ex = get(spec.get("agent", "$agent"))
                    if ex is not None:
                        for item in ex.refs:
                            if isinstance(item, AgentRef):
                                result.entrypoint_marks.append(
                                    (item.framework, item.name, site.module)
                                )
                case "AgentCandidate":
                    name_ex = get(spec.get("name", "$name"))
                    fn_ex = get(spec.get("fn", "$fn"))
                    if fn_ex is None or fn_ex.values is None:
                        continue
                    fn_ref = _single(fn_ex.values, FuncRef)
                    if fn_ref is None:
                        continue
                    if name_ex is not None and isinstance(name_ex.repr, str):
                        cand_name = name_ex.repr
                    else:
                        cand_name = fn_ref.fq.rsplit(".", 1)[-1]
                    candidate = Candidate(
                        name=cand_name,
                        fn_fq=fn_ref.fq,
                        framework=pack.framework,
                        rule_id=rule.id,
                        evidence=str(site.call.span),
                        path=site.path,
                    )
                    if not any(
                        c.fn_fq == candidate.fn_fq and c.evidence == candidate.evidence
                        for c in self._candidates
                    ):
                        self._candidates.append(candidate)
                case "RouteEdge":
                    from_ex = get(spec.get("from", "$from"))
                    to_ex = get(spec.get("to", "$to"))
                    if (
                        from_ex is not None
                        and to_ex is not None
                        and isinstance(from_ex.repr, str)
                        and isinstance(to_ex.repr, str)
                    ):
                        self._routes.append(
                            RouteEdge(
                                from_name=from_ex.repr,
                                to_name=to_ex.repr,
                                guard_fq=None,
                                framework=pack.framework,
                                rule_id=rule.id,
                                evidence=str(site.call.span),
                                path=site.path,
                            )
                        )
                case "CondRouteEdges":
                    src_ex = get(spec.get("source", "$source"))
                    guard_ex = get(spec.get("guard", "$guard"))
                    map_ex = get(spec.get("mapping", "$mapping"))
                    if src_ex is None or not isinstance(src_ex.repr, str):
                        continue
                    guard_fq: str | None = None
                    if guard_ex is not None and guard_ex.values is not None:
                        g = _single(guard_ex.values, FuncRef)
                        guard_fq = g.fq if g else None
                    targets: list[str] = []
                    if map_ex is not None and map_ex.values is not None:
                        dv = _single(map_ex.values, DictVal)
                        if dv is not None:
                            for _, tv in dv.entries:
                                t_str = _str_of(tv)
                                if t_str is not None:
                                    targets.append(t_str)
                    for target in targets:
                        self._routes.append(
                            RouteEdge(
                                from_name=src_ex.repr,
                                to_name=target,
                                guard_fq=guard_fq,
                                framework=pack.framework,
                                rule_id=rule.id,
                                evidence=str(site.call.span),
                                path=site.path,
                            )
                        )
                case "TaskBind":
                    agent_ex = get(spec.get("agent", "$agent"))
                    tools_ex = get(spec.get("tools", "$tools"))
                    if agent_ex is None or tools_ex is None:
                        continue
                    target_agents = [i for i in agent_ex.refs if isinstance(i, AgentRef)]
                    if not target_agents:
                        continue
                    task_agent_id = f"agent:framework:{slug(target_agents[0].name)}"
                    for item in tools_ex.refs:
                        if isinstance(item, ToolDefF):
                            result.facts.append(item)
                            result.facts.append(
                                BindToolF(
                                    **base_kwargs(stable_id("bind", task_agent_id, item.id)),  # type: ignore[arg-type]
                                    agent_id=task_agent_id,
                                    tool_id=item.id,
                                )
                            )
                case "CrewTransfers":
                    tasks_ex = get(spec.get("tasks", "$tasks"))
                    process_ex = get(spec.get("process", "$process"))
                    ordered = self._crew_task_agents(tasks_ex)
                    process = "sequential"
                    if process_ex is not None and process_ex.values is not None:
                        p = _single(process_ex.values, PackageRef)
                        if p is not None and p.name.rsplit(".", 1)[-1] == "hierarchical":
                            process = "hierarchical"
                    if ordered:
                        self._crews.append(
                            CrewSpec(
                                agent_names=tuple(ordered),
                                process=process,
                                framework=pack.framework,
                                rule_id=rule.id,
                                evidence=str(site.call.span),
                                path=site.path,
                            )
                        )
                case "BindPromptConstructed":
                    goal_ex = get(spec.get("goal", "$goal"))
                    back_ex = get(spec.get("backstory", "$backstory"))
                    if agent_id is None:
                        continue
                    parts = [
                        ex.repr
                        for ex in (goal_ex, back_ex)
                        if ex is not None and isinstance(ex.repr, str)
                    ]
                    if not parts:
                        continue
                    content = "\n\n".join(parts)
                    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
                    prompt = PromptDefF(
                        id=f"prompt:{digest}",
                        evidence=evidence,
                        confidence="high",
                        method=rule.id,
                        source_files=source,
                        content=content,
                        dynamic=False,
                        origin="constructed",
                        content_hash=digest,
                    )
                    result.facts.append(prompt)
                    result.facts.append(
                        BindPromptF(
                            **base_kwargs(stable_id("bind", agent_id, prompt.id)),  # type: ignore[arg-type]
                            agent_id=agent_id,
                            prompt_id=prompt.id,
                        )
                    )
                case "ToolFromRefs":
                    ex = get(spec.get("items", "$items"))
                    if ex is not None:
                        for item in ex.refs:
                            if isinstance(item, ToolDefF):
                                result.facts.append(item)
                case "StateFromCall":
                    state_kind = spec.get("kind", "session")
                    if state_kind in ("messages", "session", "vectorstore", "shared_object"):
                        result.facts.append(
                            StateDefF(
                                **base_kwargs(  # type: ignore[arg-type]
                                    stable_id("state", state_kind, str(site.call.span))
                                ),
                                kind=state_kind,  # type: ignore[arg-type]
                            )
                        )
                case "MCPFromCall":
                    fqs = self._callee_fqs(site, None)
                    for fq in sorted(fqs):
                        if self._mcp_flavor(fq) is not None:
                            instance = ClassInstance(
                                class_fq=fq,
                                ctor_args=_bound_args_of(site, self.resolver),
                            )
                            built = self.mcp_from_instance(
                                instance, str(site.call.span), site.path, rule.id
                            )
                            if built is not None:
                                result.facts.append(built[0])
                                result.facts.extend(built[1])
                            break
                case _:
                    continue

    def _emit_tool_from_def(
        self, pack: RulePack, rule: Rule, fn: FuncIR, module: str, result: F1Result
    ) -> None:
        fq = f"{module}.{fn.name}"
        tool = self.tool_from_fq(fq, str(fn.span), self._path_of(module), rule.id)
        if tool is not None:
            result.facts.append(tool)

    def _path_of(self, module: str) -> str:
        mod = self.resolver.graph.by_name.get(module)
        return mod.path if mod is not None else module

    def _crew_task_agents(self, tasks_ex: Extracted | None) -> list[str]:
        """Agent names in task order from a resolved Crew(tasks=[...]) list."""
        if tasks_ex is None or tasks_ex.values is None:
            return []
        lv = _single(tasks_ex.values, ListVal)
        if lv is None:
            return []
        names: list[str] = []
        for elem in lv.elems:
            task = _single(elem, ClassInstance)
            if task is None or not task.class_fq.endswith(".Task"):
                continue
            agent_vals = task.ctor_args.get("agent")
            agent = _single(agent_vals, ClassInstance) if agent_vals else None
            if agent is None:
                continue
            ctor_rule = self._agent_ctor_rules.get(agent.class_fq)
            if ctor_rule is None:
                continue
            name = self._agent_name_from_instance(agent, ctor_rule[1])
            if name is not None:
                names.append(name)
        return names

    # -- post passes ---------------------------------------------------------

    def _emitted_agent_ids(self, result: F1Result) -> set[str]:
        return {f.id for f in result.facts if isinstance(f, AgentDefF)}

    def _promote_candidates(self, result: F1Result) -> None:
        """AgentCandidate → AgentDef iff its function contains an LLM sink
        (SPEC §6.6 candidate promotion); otherwise the candidate is dropped."""
        for cand in sorted(self._candidates, key=lambda c: (c.path, c.evidence, c.name)):
            entry = self.resolver.lookup_def(cand.fn_fq)
            if entry is None:
                result.unpromoted_candidates += 1
                continue
            _, fn = entry
            anchored = [
                s
                for s in self.sink_list
                if s.task != "embedding" and any(f.span == fn.span for f in s.site.funcs)
            ]
            if not anchored:
                result.unpromoted_candidates += 1
                continue
            anchored.sort(key=lambda s: (s.span.file, s.span.line_start))
            sink = anchored[0]
            for s in anchored:
                result.consumed_sink_spans.add(s.span)
            agent_id = self._agent_id(cand.framework, cand.name, fn.span)
            base = {
                "evidence": (cand.evidence, str(sink.span)),
                "confidence": "high",
                "method": f"{cand.rule_id}+promotion:sink",
                "source_files": (cand.path,),
            }
            result.facts.append(
                AgentDefF(
                    id=agent_id,
                    **base,  # type: ignore[arg-type]
                    name=cand.name,
                    kind="framework",
                    framework=cand.framework,
                    location=path_location(cand.path),
                    language=path_language(cand.path),
                )
            )
            result.facts.append(sink.model)
            result.facts.append(
                BindModelF(
                    id=stable_id("bind", agent_id, sink.model.id),
                    **base,  # type: ignore[arg-type]
                    agent_id=agent_id,
                    model_id=sink.model.id,
                )
            )
            if sink.prompt is not None:
                result.facts.append(sink.prompt)
                result.facts.append(
                    BindPromptF(
                        id=stable_id("bind", agent_id, sink.prompt.id),
                        **base,  # type: ignore[arg-type]
                        agent_id=agent_id,
                        prompt_id=sink.prompt.id,
                    )
                )

    def _emit_routes(self, result: F1Result) -> None:
        agent_ids = self._emitted_agent_ids(result)
        for route in sorted(self._routes, key=lambda r: (r.path, r.evidence, r.from_name)):
            from_id = f"agent:framework:{slug(route.from_name)}"
            to_id = f"agent:framework:{slug(route.to_name)}"
            if from_id not in agent_ids or to_id not in agent_ids:
                continue
            result.facts.append(
                TransferF(
                    id=stable_id("transfer", from_id, to_id, "route", route.guard_fq),
                    evidence=(route.evidence,),
                    confidence="high",
                    method=route.rule_id,
                    source_files=(route.path,),
                    from_id=from_id,
                    to_id=to_id,
                    kind="route",
                    guard=route.guard_fq,
                )
            )

    def _emit_crews(self, result: F1Result) -> None:
        agent_ids = self._emitted_agent_ids(result)
        for crew in sorted(self._crews, key=lambda c: (c.path, c.evidence)):
            if crew.process == "hierarchical":
                continue  # needs a manager agent; no corpus construct yet
            chain = [
                f"agent:framework:{slug(n)}"
                for n in crew.agent_names
                if f"agent:framework:{slug(n)}" in agent_ids
            ]
            for from_id, to_id in itertools.pairwise(chain):
                if from_id == to_id:
                    continue
                result.facts.append(
                    TransferF(
                        id=stable_id("transfer", from_id, to_id, "route", crew.rule_id),
                        evidence=(crew.evidence,),
                        confidence="high",
                        method=crew.rule_id,
                        source_files=(crew.path,),
                        from_id=from_id,
                        to_id=to_id,
                        kind="route",
                    )
                )

    def _apply_entrypoints(self, result: F1Result, reset: bool = False) -> None:
        """Mark entrypoint agents from ``result.entrypoint_marks`` (fresh + any
        carried). ``reset`` (incremental scans) also clears the flag on agents no
        longer marked, so a carried agent's stale ``entrypoint`` from a prior scan
        is corrected; in a full scan every agent already starts unmarked, so reset
        is a harmless no-op."""
        marked_names = {slug(name) for _, name, _ in result.entrypoint_marks}
        if not marked_names and not reset:
            return
        for i, fact in enumerate(result.facts):
            if not isinstance(fact, AgentDefF):
                continue
            want = slug(fact.name) in marked_names
            if fact.entrypoint != want:
                result.facts[i] = fact.model_copy(update={"entrypoint": want})


# -- module helpers ----------------------------------------------------------


def _is_model_value(vals: ValueSet) -> bool:
    """True when a resolved value looks like an LLM model (SPEC-9 A2 gate): a
    model-adapter instance (a class carrying a model kwarg) or a string naming a
    known model family / provider-prefixed model. A bare class ref or an
    unrelated string is not a model."""
    inst = _single(vals, ClassInstance)
    if inst is not None:
        return any(ctor_kwarg(inst, k) for k in _MODEL_ADAPTER_KW)
    return any(isinstance(v, Str) and _MODEL_NAME_RE.search(v.s) for v in vals)


def _fqs_of(base: ValueSet, path: tuple[str, ...]) -> set[str]:
    return {fq for v in base for fq in value_fqs(v, path)}


def _single[T](vals: ValueSet | None, kind: type[T]) -> T | None:
    if vals is not None and len(vals) == 1:
        v = next(iter(vals))
        if isinstance(v, kind):
            return v
    return None


def _str_of(vals: ValueSet | None) -> str | None:
    s = _single(vals, Str)
    return s.s if s else None


def _as_str_tuple(v: object) -> tuple[str, ...]:
    if isinstance(v, tuple):
        return tuple(x for x in v if isinstance(x, str))
    return ()


_SKIP_DIRS = {".git", "__pycache__", "node_modules", "venv"}


def _discover_config_files(repo_root: Path) -> list[str]:
    """All non-Python files (repo-relative posix), sorted; skips VCS dirs."""
    found: list[str] = []
    stack = [repo_root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name in _SKIP_DIRS or entry.name.startswith("."):
                    continue
                stack.append(entry)
            elif entry.is_file() and not entry.name.endswith(".py"):
                found.append(entry.relative_to(repo_root).as_posix())
    return sorted(found)


def _glob_match(rel: str, globs: tuple[str, ...]) -> bool:
    basename = rel.rsplit("/", 1)[-1]
    for g in globs:
        if g.endswith("/**"):
            if rel.startswith(g[: -len("/**")] + "/"):
                return True
        elif fnmatch(rel, g) or fnmatch(basename, g):
            return True
    return False


def _yaml_frontmatter(text: str) -> dict[str, object] | None:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return None
    loaded = yaml.safe_load("\n".join(lines[1:end]))
    return loaded if isinstance(loaded, dict) else None


def _bound_args_of(site: CallSite, resolver: Resolver) -> BoundArgs:
    scope = None
    if site.funcs:
        scope = resolver.function_scope(site.module, site.funcs, site.call.span)
    return BoundArgs(
        positional=tuple(
            resolver.resolve(a, site.module, scope) for a in site.call.args
        ),
        named=tuple(
            (k.name, resolver.resolve(k.value, site.module, scope))
            for k in site.call.kwargs
            if k.name is not None
        ),
    )
