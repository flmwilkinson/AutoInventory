"""Wrapper fixed point (SPEC §6.7.1).

Makes ``from bank_ai import LLMClient`` a first-class sink with no pre-written
signature: definitions enclosing known sinks are scored; classified wrappers
turn their call sites into wrapper sinks; wrappers of wrappers follow by
monotone iteration (finite defs → terminates).

Classifications are persisted to an org-wide JSON registry keyed by
``(fq, content_hash)`` so later scans and other repos reuse them; org-pack
``known_wrapper_packages`` seed external wrappers whose source is absent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from aiscan.context import OrgPack
from aiscan.facts.models import ApiStyle, FindingRecord, ModelRefF, Task, stable_id
from aiscan.frontends.common import derived_names, root_name, subtree, value_fqs
from aiscan.ir.nodes import (
    AttrE,
    CallE,
    Expr,
    ForS,
    FuncIR,
    NameE,
    ReturnS,
    Span,
    SubscriptE,
    WhileS,
)
from aiscan.ir.values import (
    ClassInstance,
    JsonRepr,
    Str,
    Symbolic,
    ValueSet,
    to_repr,
)
from aiscan.ir.walk import iter_calls, iter_stmts
from aiscan.resolve.engine import Resolver
from aiscan.sinks.attribution import (
    RUNG_CONSTANT,
    RUNG_WRAPPER,
    ModelAttribution,
    attribute_model,
)
from aiscan.sinks.engine import CallSite, Sink, SinkEngine, iter_call_sites

_PARAM_SIGNALS = frozenset(
    {"prompt", "messages", "model", "system", "input", "text", "query", "temperature"}
)
_METHOD_SIGNALS = frozenset(
    {"chat", "complete", "completion", "generate", "invoke", "ask", "query", "call"}
)
_MESSAGES_PARAMS = ("messages", "prompt", "input", "text", "query")

WRAPPER_THRESHOLD = 3


class WrapperInfo(BaseModel):
    """One classified wrapper — an auditable, evidence-carrying registry entry."""

    model_config = ConfigDict(frozen=True)

    fq: str
    kind: Literal["class", "function"]
    attribution: Literal["fixed", "passthrough", "default"]
    model_param: str | None = None  # passthrough: call-site kwarg
    ctor_model_param: str | None = None  # class wrappers: ctor kwarg overriding the model
    resolved_model: JsonRepr = None  # fixed/default: statically resolved value
    endpoint: JsonRepr = None
    api_style: ApiStyle = "unknown"
    task: Task = "chat"
    messages_param: str | None = None
    messages_pos: int | None = None
    sink_methods: tuple[str, ...] = ()  # class wrappers: methods that reach the sink
    content_hash: str | None = None
    source: Literal["derived", "org_pack", "registry"] = "derived"
    evidence: tuple[str, ...] = ()
    score: int | None = None


# SPEC-7 Z6: bumped whenever wrapper classification/attribution logic changes —
# a cached classification must not outlive the detector that produced it.
_REGISTRY_SCHEMA = 2


_AGENT_RESULT_FIELDS = frozenset(
    {"tool_calls", "function_call", "finish_reason", "stop_reason", "choices"}
)


def _agent_shaped(fn: FuncIR, sinks: list[Sink]) -> bool:
    """A loop-contained sink counts as agent-shaped only when the function
    also branches on response fields — a bare retry loop around the call
    (``for attempt in range(10)``) is wrapper plumbing, not an agent (SPEC-7).
    Sink-as-iterable (``async for chunk in llm.astream(...)``) is streaming
    consumption, also wrapper-shaped. Recursion always counts."""
    sink_spans = [s.span for s in sinks]
    loop_carried = any(
        isinstance(stmt, WhileS | ForS)
        and any(any(b.span.contains(sp) for b in stmt.body) for sp in sink_spans)
        for stmt in iter_stmts(fn.body)
    )
    if loop_carried and _branches_on_response(fn):
        return True
    return any(
        isinstance(c.callee, NameE) and c.callee.name == fn.name
        for c in iter_calls(fn.body)
    )


def _branches_on_response(fn: FuncIR) -> bool:
    from aiscan.ir.nodes import IfS, StrE
    from aiscan.ir.walk import expr_children

    for stmt in iter_stmts(fn.body):
        if not isinstance(stmt, IfS):
            continue
        stack: list[Expr] = [stmt.test]
        while stack:
            e = stack.pop()
            if isinstance(e, AttrE) and e.attr in _AGENT_RESULT_FIELDS:
                return True
            if (
                isinstance(e, SubscriptE)
                and isinstance(e.index, StrE)
                and e.index.value in _AGENT_RESULT_FIELDS
            ):
                return True
            stack.extend(expr_children(e))
    return False


def load_registry(path: Path | None) -> dict[str, WrapperInfo]:
    if path is None or not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("schema") != _REGISTRY_SCHEMA:
        return {}  # stale or pre-versioned registry: re-derive everything
    out: dict[str, WrapperInfo] = {}
    entries = raw.get("entries", []) if isinstance(raw, dict) else []
    for e in entries:
        try:
            info = WrapperInfo.model_validate(e)
        except ValueError:
            continue
        out[info.fq] = info.model_copy(update={"source": "registry"})
    return out


def save_registry(path: Path, infos: dict[str, WrapperInfo]) -> None:
    entries = [
        infos[fq].model_dump(mode="json")
        for fq in sorted(infos)
        if infos[fq].source in ("derived", "registry")
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"schema": _REGISTRY_SCHEMA, "entries": entries},
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


@dataclass(slots=True)
class WrapperResult:
    wrapper_sinks: list[Sink] = field(default_factory=list)
    classified: dict[str, WrapperInfo] = field(default_factory=dict)
    used_wrappers: set[str] = field(default_factory=set)  # wrappers with ≥1 call site
    wrapper_def_spans: set[Span] = field(default_factory=set)  # sink spans inside used wrappers
    findings: list[FindingRecord] = field(default_factory=list)


class WrapperAnalyzer:
    """Runs the monotone fixed point over the analysis set."""

    def __init__(
        self,
        resolver: Resolver,
        sink_engine: SinkEngine,
        org_pack: OrgPack,
        registry: dict[str, WrapperInfo],
        analyze_only: frozenset[str] | None = None,
    ) -> None:
        self.resolver = resolver
        self.engine = sink_engine
        self.org_pack = org_pack
        self.registry = registry
        # SPEC-8: on an incremental scan, only look for wrapper *call sites* in the
        # affected modules (unaffected wrapper sinks are carried from cache). The
        # registry still supplies every wrapper's classification, so an affected
        # sink calling a wrapper defined elsewhere still attributes correctly.
        self.analyze_only = analyze_only
        self._all_sites: list[CallSite] | None = None

    # -- public --------------------------------------------------------------

    def fixed_point(self, initial_sinks: list[Sink]) -> WrapperResult:
        result = WrapperResult()
        for wrapper in self.org_pack.known_wrapper_packages:
            result.classified[wrapper.fqname] = WrapperInfo(
                fq=wrapper.fqname,
                kind="class",
                attribution=wrapper.attribution,
                model_param="model" if wrapper.attribution == "passthrough" else None,
                source="org_pack",
                evidence=("org-pack",),
            )
        for fq, info in self.registry.items():
            if fq not in result.classified and self._hash_current(fq, info):
                result.classified[fq] = info

        sinks_by_def: dict[str, list[Sink]] = {}
        for sink in initial_sinks:
            d = self._enclosing_def_fq(sink.site)
            if d is not None:
                sinks_by_def.setdefault(d, []).append(sink)

        expanded: set[str] = set()
        worklist: list[str] = sorted(set(sinks_by_def) | set(result.classified))
        while worklist:
            d = worklist.pop(0)
            if d in expanded:
                continue
            if d not in result.classified:
                derived_info = self._score_and_classify(d, sinks_by_def.get(d, []))
                if derived_info is None:
                    expanded.add(d)
                    continue
                result.classified[d] = derived_info
            expanded.add(d)
            new_sinks = self._wrapper_sinks_for(result.classified[d], result)
            for ns in new_sinks:
                result.wrapper_sinks.append(ns)
                nd = self._enclosing_def_fq(ns.site)
                if nd is not None:
                    sinks_by_def.setdefault(nd, []).append(ns)
                    if nd not in expanded:
                        worklist.append(nd)
            worklist.sort()

        for fq in result.used_wrappers:
            winfo = result.classified.get(fq)
            for s in sinks_by_def.get(fq, []):
                if (
                    winfo is not None
                    and winfo.kind == "class"
                    and winfo.sink_methods
                    and s.site.funcs
                    and s.site.funcs[-1].name not in winfo.sink_methods
                ):
                    # SPEC-7: a sibling method's wrapper-call sink is a
                    # consumer of the wrapper, not its implementation.
                    continue
                result.wrapper_def_spans.add(s.span)

        # SPEC-7 Z7: a wrapper-shaped *class* with sinks but zero in-repo call
        # sites is a dormant AI library — inventory-relevant (the code-level
        # analogue of a dormant BOM dependency). Function-shaped candidates
        # are excluded: agent loops score as wrappers by the P5 arithmetic and
        # top-level orchestrators legitimately have no in-repo callers.
        for fq in sorted(result.classified):
            info = result.classified[fq]
            if (
                info.source == "derived"
                and info.kind == "class"
                and fq not in result.used_wrappers
                and sinks_by_def.get(fq)
            ):
                result.findings.append(
                    FindingRecord(
                        kind="dormant_ai_wrapper",
                        evidence=info.evidence
                        or (str(sinks_by_def[fq][0].span),),
                        detail=(
                            f"AI wrapper '{fq}' is defined but never called "
                            "in this repository"
                        ),
                        subject_ref="system",
                    )
                )
        return result

    # -- plumbing ------------------------------------------------------------

    def _hash_current(self, fq: str, info: WrapperInfo) -> bool:
        """Registry entries are content-addressed: stale hashes re-derive."""
        entry = self.resolver.lookup_class(fq) or self.resolver.lookup_def(fq)
        if entry is None:
            return True  # external: nothing to compare against
        mod = self.resolver.graph.by_name.get(entry[0])
        return mod is not None and mod.content_hash == info.content_hash

    def _all_call_sites(self) -> list[CallSite]:
        if self._all_sites is None:
            sites: list[CallSite] = []
            for module_name in sorted(self.resolver.graph.by_name):
                if self.analyze_only is not None and module_name not in self.analyze_only:
                    continue
                mod = self.resolver.graph.by_name[module_name]
                sites.extend(iter_call_sites(mod, module_name))
            self._all_sites = sites
        return self._all_sites

    def _enclosing_def_fq(self, site: CallSite) -> str | None:
        if site.owner is not None:
            return f"{site.module}.{site.owner.name}"
        if site.funcs:
            return f"{site.module}.{site.funcs[0].name}"
        return None

    # -- scoring -------------------------------------------------------------

    def _score_and_classify(self, fq: str, sinks: list[Sink]) -> WrapperInfo | None:
        if not sinks:
            return None
        module = fq.rsplit(".", 1)[0]
        cls_entry = self.resolver.lookup_class(fq)
        is_class = cls_entry is not None
        score = 0
        evidence: list[str] = []

        sink = min(sinks, key=lambda s: (s.span.file, s.span.line_start, s.span.col_start))
        fn = sink.site.funcs[-1] if sink.site.funcs else None
        if fn is None:
            return None

        # SPEC-7 Z1 guard: a def whose sink sits inside a loop, or that
        # recurses, is agent-shaped — the agent pipeline owns it. Classifying
        # it as a wrapper would suppress the agent at its call sites.
        if _agent_shaped(fn, sinks):
            return None

        score += 2  # body contains a classified sink call (by construction)
        evidence.append(str(sink.span))

        param_names = {p.name for p in fn.params}
        has_kwargs = any(p.kind == "kwarg" for p in fn.params)
        if param_names & _PARAM_SIGNALS or has_kwargs:
            score += 1
        if self._returns_sink_result(fn, sink):
            score += 1
        if is_class and cls_entry is not None:
            method_names = {m.name for m in cls_entry[1].methods}
            if method_names & _METHOD_SIGNALS:
                score += 1
        if sink.site.in_tests or sink.site.under_main:
            score -= 2
        if score < WRAPPER_THRESHOLD:
            return None

        mod = self.resolver.graph.by_name.get(module)
        info = self._derive_attribution(fq, module, fn, sink, is_class)
        return info.model_copy(
            update={
                "content_hash": mod.content_hash if mod is not None else None,
                "score": score,
                "evidence": tuple(evidence),
            }
        )

    def _returns_sink_result(self, fn: FuncIR, sink: Sink) -> bool:
        derived = derived_names(fn, [sink.site.call])
        for stmt in iter_stmts(fn.body):
            if isinstance(stmt, ReturnS) and stmt.value is not None:
                root = root_name(stmt.value)
                if root is not None and root in derived:
                    return True
                if any(e is sink.site.call for e in subtree(stmt.value)):
                    return True
        return False

    # -- attribution ---------------------------------------------------------

    def _derive_attribution(
        self, fq: str, module: str, fn: FuncIR, sink: Sink, is_class: bool
    ) -> WrapperInfo:
        model_expr = self.engine.model_expr_of(sink.site.call)
        attribution: Literal["fixed", "passthrough", "default"] = "default"
        model_param: str | None = None
        resolved_model: JsonRepr = None

        param_names = {p.name for p in fn.params}
        if isinstance(model_expr, NameE) and model_expr.name in param_names:
            attribution = "passthrough"
            model_param = model_expr.name
        else:
            self_instance = ClassInstance(class_fq=fq) if is_class else None
            scope = self.resolver.function_scope(
                module, (fn,), None, self_instance=self_instance
            )
            if model_expr is not None:
                vals = self.resolver.resolve(model_expr, module, scope)
                single = _single_value(vals)
                if isinstance(single, Str):
                    is_self_attr = isinstance(model_expr, AttrE) and isinstance(
                        model_expr.base, NameE
                    ) and model_expr.base.name == "self"
                    attribution = "default" if is_self_attr else "fixed"
                    resolved_model = single.s
                elif vals and any(isinstance(v, Str | Symbolic) for v in vals):
                    # SPEC-6: a resolvable union (the ``env || literal`` idiom,
                    # resolvable since SPEC-5 X0) IS the wrapper's default —
                    # far more informative than a bare wrapper_default symbolic.
                    attribution = "default"
                    resolved_model = to_repr(vals)
            elif sink.kind == "wrapper":
                # Second-order wrapper: inherit the inner wrapper's resolution.
                attribution = "default"
                resolved_model = sink.model.model

        endpoint = self._derive_endpoint(fq, module, fn, sink, is_class)
        messages_param, messages_pos = _messages_param(fn)
        ctor_model_param = None
        if is_class:
            cls_entry = self.resolver.lookup_class(fq)
            if cls_entry is not None:
                init = next((m for m in cls_entry[1].methods if m.name == "__init__"), None)
                if init is not None:
                    for p in init.params:
                        if p.name in ("model", "model_name", "deployment"):
                            ctor_model_param = p.name
                            break
        sink_methods = (fn.name,) if is_class else ()
        return WrapperInfo(
            fq=fq,
            kind="class" if is_class else "function",
            attribution=attribution,
            model_param=model_param,
            ctor_model_param=ctor_model_param,
            resolved_model=resolved_model,
            endpoint=endpoint,
            api_style=sink.api_style,
            task=sink.task,
            messages_param=messages_param,
            messages_pos=messages_pos,
            sink_methods=sink_methods,
        )

    def _derive_endpoint(
        self, fq: str, module: str, fn: FuncIR, sink: Sink, is_class: bool
    ) -> JsonRepr:
        ep = sink.model.endpoint
        if ep is not None and not (isinstance(ep, dict) and "unresolved" in ep):
            return ep
        url_e = self.engine.url_expr(sink.site.call)
        if url_e is None:
            return None
        self_instance = ClassInstance(class_fq=fq) if is_class else None
        scope = self.resolver.function_scope(module, (fn,), None, self_instance=self_instance)
        vals = self.resolver.resolve(url_e, module, scope)
        single = _single_value(vals)
        if isinstance(single, Str):
            return single.s
        return None

    # -- wrapper sink construction -------------------------------------------

    def _wrapper_sinks_for(self, info: WrapperInfo, result: WrapperResult) -> list[Sink]:
        out: list[Sink] = []
        for site in self._all_call_sites():
            enclosing = self._enclosing_def_fq(site)
            if enclosing == info.fq:
                if info.kind == "function":
                    continue  # the wrapper's own body
                # SPEC-7 sweep fix: for a class wrapper, only the sink methods
                # themselves are "own body" — a sibling method calling the
                # class's sink method (Swarm.run → self.get_chat_completion)
                # is a legitimate wrapper call site.
                fn_name = site.funcs[-1].name if site.funcs else None
                if fn_name is None or fn_name in info.sink_methods:
                    continue
            scope = self.engine.scope_for_site(site)
            base, path = self.resolver.chain_root(site.call.callee, site.module, scope)
            matched_instance: ClassInstance | None = None
            matched = False
            for v in sorted(base, key=repr):
                fqs = value_fqs(v, path)
                if info.kind == "function":
                    if info.fq in fqs:
                        matched = True
                        break
                else:
                    method_ok = (
                        any(f == f"{info.fq}.{m}" for f in fqs for m in info.sink_methods)
                        if info.sink_methods
                        else any(f.startswith(info.fq + ".") for f in fqs)
                    )
                    if method_ok:
                        matched = True
                        if isinstance(v, ClassInstance):
                            matched_instance = v
                        break
            if not matched:
                continue
            result.used_wrappers.add(info.fq)
            out.append(self._build_wrapper_sink(info, site, matched_instance))
        return out

    def _build_wrapper_sink(
        self, info: WrapperInfo, site: CallSite, instance: ClassInstance | None
    ) -> Sink:
        scope = self.engine.scope_for_site(site)
        attribution: ModelAttribution
        if info.attribution == "passthrough" and info.model_param is not None:
            expr = _kwarg_expr(site.call, info.model_param)
            vals = (
                self.resolver.resolve(expr, site.module, scope) if expr is not None else None
            )
            attribution = attribute_model(expr, vals)
        else:
            ctor_vals: ValueSet | None = None
            if instance is not None and info.ctor_model_param is not None:
                ctor_vals = instance.ctor_args.get(info.ctor_model_param)
            if ctor_vals:
                single = _single_value(ctor_vals)
                if isinstance(single, Str):
                    attribution = ModelAttribution(
                        model=single.s, method=RUNG_CONSTANT, resolved=True
                    )
                else:
                    attribution = ModelAttribution(
                        model=to_repr(ctor_vals), method=RUNG_CONSTANT, resolved=True
                    )
            elif info.resolved_model is not None:
                attribution = ModelAttribution(
                    model=info.resolved_model, method=RUNG_WRAPPER, resolved=True
                )
            else:
                sym = Symbolic(kind="wrapper_default", key=info.fq)
                attribution = ModelAttribution(
                    model=to_repr(frozenset({sym})), method=RUNG_WRAPPER, resolved=True
                )

        prompt = None
        if info.messages_param is not None:
            expr = _kwarg_expr(site.call, info.messages_param)
            if (
                expr is None
                and info.messages_pos is not None
                and info.messages_pos < len(site.call.args)
            ):
                expr = site.call.args[info.messages_pos]
            if expr is not None:
                vals = self.resolver.resolve(expr, site.module, scope)
                prompt = self.engine.prompt_from_messages(vals, site)
                if prompt is None:
                    prompt = self.engine.build_prompt_fact(
                        vals, site, method="wrapper:prompt_arg"
                    )

        model_fact = ModelRefF(
            id=stable_id("model", info.api_style, attribution.model, info.endpoint),
            evidence=(str(site.call.span),),
            confidence="high",
            method=attribution.method,
            source_files=(site.path,),
            provider=None,
            model=attribution.model,
            endpoint=info.endpoint,
            deployment=None,
            api_style=info.api_style,
            task=info.task,
        )
        return Sink(
            site=site,
            kind="wrapper",
            api_style=info.api_style,
            task=info.task,
            confidence="high",
            score=None,
            signals=(f"wrapper:{info.fq}",),
            model=model_fact,
            prompt=prompt,
            endpoint=info.endpoint,
            payload=None,
            credential_headers=None,
        )


# -- helpers -----------------------------------------------------------------


def _messages_param(fn: FuncIR) -> tuple[str | None, int | None]:
    positional = [p for p in fn.params if p.kind == "pos" and p.name not in ("self", "cls")]
    for i, p in enumerate(positional):
        if p.name in _MESSAGES_PARAMS:
            return p.name, i
    return None, None


def _kwarg_expr(call: CallE, name: str) -> Expr | None:
    for k in call.kwargs:
        if k.name == name:
            return k.value
    return None


def _single_value(vals: ValueSet | None) -> object | None:
    if vals is not None and len(vals) == 1:
        return next(iter(vals))
    return None


