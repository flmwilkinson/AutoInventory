"""Agent-shape reconstruction (SPEC §6.7.2).

Anchor = smallest enclosing callable region with >=1 non-embedding sink.
Feature detectors F1-F5 over the region's IR, deterministic tiering (no ML):

    F1 and F2 and (F3 or F4)        -> AgentDef(bespoke), high
    F2 and F3 -- or -- F1 and F4    -> AgentDef(bespoke), medium
    >=2 features, pattern unclear   -> finding ambiguous_agent_shape
    sink, <2 features               -> LLMCallSite (call_sites.py)
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from aiscan.context import OrgPack
from aiscan.facts.models import (
    AgentDefF,
    AnyFact,
    AttachPolicyF,
    BindModelF,
    BindPromptF,
    BindStateF,
    BindToolF,
    Confidence,
    FindingRecord,
    PolicyDefF,
    SideEffect,
    StateDefF,
    ToolDefF,
    TransferF,
    stable_id,
)
from aiscan.frontends.common import (
    build_tool_from_fq,
    derived_names,
    root_name,
    subtree,
)
from aiscan.frontends.framework.engine import slug
from aiscan.ir.nodes import (
    AssignS,
    AttrE,
    AugAssignS,
    BoolE,
    BreakS,
    CallE,
    CompareE,
    ContinueS,
    DictE,
    Expr,
    ForS,
    FuncIR,
    IfS,
    ListE,
    NameE,
    ReturnS,
    Span,
    Stmt,
    StrE,
    SubscriptE,
    WhileS,
)
from aiscan.ir.values import (
    ClassInstance,
    DictVal,
    FuncRef,
    JsonRepr,
    ListVal,
    Str,
    Value,
    ValueSet,
)
from aiscan.ir.walk import (
    dotted_name,
    enclosing_region,
    iter_calls,
    iter_exprs,
    iter_stmts,
)
from aiscan.resolve.engine import Resolver
from aiscan.sinks.attribution import attribute_model
from aiscan.sinks.engine import Sink, SinkEngine, path_language, path_location
from aiscan.sinks.side_effects import classify_body

_RESULT_FIELDS = frozenset(
    {"tool_calls", "function_call", "finish_reason", "stop_reason", "name", "content"}
)


@dataclass(slots=True)
class ShapeResult:
    facts: list[AnyFact] = field(default_factory=list)
    consumed_spans: set[Span] = field(default_factory=set)
    # SPEC-5 §5: sink spans attributed to an agent via its call closure —
    # still emitted as usages (nothing is dropped), but ``in_agent=True``.
    member_spans: set[Span] = field(default_factory=set)
    findings: list[FindingRecord] = field(default_factory=list)


@dataclass(slots=True)
class _Anchor:
    module: str
    path: str
    fn: FuncIR
    sinks: list[Sink]


def analyze_shapes(
    sinks: list[Sink],
    resolver: Resolver,
    sink_engine: SinkEngine,
    org_pack: OrgPack,
    llm_sink_spans: frozenset[Span],
    excluded_spans: frozenset[Span],
) -> ShapeResult:
    """Run F1-F5 over every anchor region; emit bespoke agents and bindings."""
    result = ShapeResult()
    anchors: dict[tuple[str, Span], _Anchor] = {}
    for sink in sinks:
        if sink.task == "embedding" or sink.span in excluded_spans:
            continue
        if not sink.site.funcs:
            continue  # module-level sink: plain call site
        fn = sink.site.funcs[-1]
        key = (sink.site.module, fn.span)
        anchor = anchors.setdefault(
            key, _Anchor(module=sink.site.module, path=sink.site.path, fn=fn, sinks=[])
        )
        anchor.sinks.append(sink)

    agent_fqs: dict[str, str] = {}  # anchor fn fq -> agent id (for invoke transfers)
    analyses: list[tuple[_Anchor, _Features, str]] = []
    for key in sorted(anchors, key=lambda k: (anchors[k].path, k[1].line_start)):
        anchor = anchors[key]
        features = _detect_features(anchor, resolver)
        tier = _tier(features)
        if tier == "ambiguous":
            result.findings.append(
                FindingRecord(
                    kind="ambiguous_agent_shape",
                    evidence=(str(anchor.fn.span),),
                    detail=f"features={','.join(sorted(features.present))}",
                )
            )
            continue
        if tier is None:
            continue
        analyses.append((anchor, features, tier))
        fn_fq = f"{anchor.module}.{anchor.fn.name}"
        agent_fqs[fn_fq] = f"agent:bespoke:{slug(anchor.fn.name)}"

    # SPEC-5 §5: bounded call closure — sinks in helpers the agent calls
    # (≤2 hops, internal FuncRefs only, other anchors and wrapper defs
    # skipped) attribute to the agent instead of orphaning.
    sinks_by_fn: dict[tuple[str, Span], list[Sink]] = {}
    for sink in sinks:
        if sink.site.funcs and sink.span not in excluded_spans:
            fn_key = (sink.site.module, sink.site.funcs[-1].span)
            sinks_by_fn.setdefault(fn_key, []).append(sink)
    promoted_spans = {(a.module, a.fn.span) for a, _f, _t in analyses}

    for anchor, features, tier in analyses:
        closure: list[Sink] = []
        for fn_key in _call_closure(anchor, resolver, promoted_spans, excluded_spans):
            closure.extend(sinks_by_fn.get(fn_key, []))
        _emit_agent(
            anchor,
            features,
            tier,
            result,
            resolver,
            sink_engine,
            org_pack,
            llm_sink_spans,
            agent_fqs,
            closure,
        )
    return result


# -- feature detection --------------------------------------------------------


@dataclass(slots=True)
class _Features:
    present: set[str] = field(default_factory=set)
    dispatch_targets: list[str] = field(default_factory=list)  # FuncRef fqs
    schema_names: list[str] = field(default_factory=list)
    state_name: str | None = None
    state_span: Span | None = None
    approval_span: Span | None = None


def _detect_features(anchor: _Anchor, resolver: Resolver) -> _Features:
    fn = anchor.fn
    module = anchor.module
    f = _Features()
    sink_calls = [s.site.call for s in anchor.sinks]
    sink_spans = [s.span for s in anchor.sinks]

    derived = derived_names(fn, sink_calls)

    # F1: sink inside a loop *body* (a sink as the loop's iterable is
    # streaming consumption, not an agent loop — SPEC-7), or recursion.
    loops = [
        stmt
        for stmt in iter_stmts(fn.body)
        if isinstance(stmt, WhileS | ForS)
        and any(any(b.span.contains(sp) for b in stmt.body) for sp in sink_spans)
    ]
    recurses = any(
        isinstance(c.callee, NameE) and c.callee.name == fn.name
        for c in _calls_in(fn.body)
    )
    if loops or recurses:
        f.present.add("F1")

    # F2: dispatch — mapping lookup into a dict of callables (keyed from the
    # response), or response-field branch guarding a resolvable call.
    # SPEC-7 sweep fix: method anchors bind self/this to their owning class so
    # `self.handle_tool_calls(...)` resolves (the swarm Swarm.run shape).
    owner = next((s.site.owner for s in anchor.sinks if s.site.owner is not None), None)
    self_instance = (
        ClassInstance(class_fq=f"{module}.{owner.name}") if owner is not None else None
    )
    scope = resolver.function_scope(module, (fn,), None, self_instance=self_instance)
    dispatch_var_names: set[str] = set()
    for stmt in iter_stmts(fn.body):
        if isinstance(stmt, AssignS) and isinstance(stmt.value, SubscriptE):
            targets = _dict_dispatch_targets(stmt.value, module, scope, resolver)
            if targets:
                f.dispatch_targets.extend(t for t in targets if t not in f.dispatch_targets)
                for t_expr in stmt.targets:
                    if isinstance(t_expr, NameE):
                        dispatch_var_names.add(t_expr.name)
    for call in _calls_in(fn.body):
        if isinstance(call.callee, SubscriptE):
            targets = _dict_dispatch_targets(call.callee, module, scope, resolver)
            if targets:
                f.dispatch_targets.extend(t for t in targets if t not in f.dispatch_targets)
    if f.dispatch_targets:
        f.present.add("F2")
    elif _has_result_branch(fn, derived):
        _guarded_funcref_calls(
            fn, derived, module, scope, resolver, f, tuple(sink_spans)
        )

    # dispatch-result names (for F3): dict-dispatch variables and direct calls
    # to detected dispatch targets (SPEC-7: `partial = self.handle_tool_calls(...)`).
    dispatch_tails = {fq.rsplit(".", 1)[-1] for fq in f.dispatch_targets}
    result_names: set[str] = set()
    for stmt in iter_stmts(fn.body):
        if isinstance(stmt, AssignS) and isinstance(stmt.value, CallE):
            callee = stmt.value.callee
            called_name = (
                callee.name
                if isinstance(callee, NameE)
                else callee.attr
                if isinstance(callee, AttrE)
                else None
            )
            if called_name is not None and (
                called_name in dispatch_var_names or called_name in dispatch_tails
            ):
                for t_expr in stmt.targets:
                    if isinstance(t_expr, NameE):
                        result_names.add(t_expr.name)

    # F4: role-tagged accumulator list, or tools/functions schemas in payload.
    accumulator = _find_message_accumulator(fn, sink_calls)
    if accumulator is not None:
        f.state_name, f.state_span = accumulator
    payload_has_tools = any(
        s.payload is not None and (set(s.payload.keys()) & {"tools", "functions"})
        for s in anchor.sinks
    )
    if accumulator is not None or payload_has_tools:
        f.present.add("F4")

    # F3: dispatched call's result feeds back into the message state/payload
    # (append/push/extend — SPEC-7 accepts `history.extend(partial.messages)`).
    if result_names and f.state_name is not None:
        for call in _calls_in(fn.body):
            if (
                isinstance(call.callee, AttrE)
                and call.callee.attr in ("append", "push", "extend")
                and isinstance(call.callee.base, NameE)
                and call.callee.base.name == f.state_name
            ):
                for arg in call.args:
                    if any(
                        isinstance(e, NameE) and e.name in result_names
                        for e in subtree(arg)
                    ):
                        f.present.add("F3")

    # F5: termination — loop guard on a counter or response fields; or a
    # while-True with a return guarded by the response.
    for loop in loops:
        if isinstance(loop, WhileS):
            if isinstance(loop.test, BoolE) and loop.test.value:
                if _has_guarded_return(loop.body, derived):
                    f.present.add("F5")
            else:
                counters = {
                    s.target.name
                    for s in iter_stmts(loop.body)
                    if isinstance(s, AugAssignS) and isinstance(s.target, NameE)
                }
                test_names = {
                    e.name for e in subtree(loop.test) if isinstance(e, NameE)
                }
                if test_names & (counters | derived):
                    f.present.add("F5")
        elif _has_guarded_return(loop.body, derived):
            f.present.add("F5")

    # Approval gate: input()/confirm dominating a dispatch branch.
    for stmt in iter_stmts(fn.body):
        if isinstance(stmt, IfS) and _mentions(stmt.test, derived):
            for call in _calls_in(stmt.body):
                if isinstance(call.callee, NameE) and call.callee.name == "input":
                    f.approval_span = stmt.span

    # Schema-declared tool names from payload tools/functions lists.
    for s in anchor.sinks:
        if s.payload is None:
            continue
        for key in ("tools", "functions"):
            vals = s.payload.get(key)
            if vals is None:
                continue
            for name in _schema_tool_names(vals):
                if name not in f.schema_names:
                    f.schema_names.append(name)
    return f


def _tier(f: _Features) -> str | None:
    p = f.present
    if "F1" in p and "F2" in p and ("F3" in p or "F4" in p):
        return "high"
    if ("F2" in p and "F3" in p) or ("F1" in p and "F4" in p):
        return "medium"
    if len(p) >= 2:
        return "ambiguous"
    return None


def _emit_agent(
    anchor: _Anchor,
    features: _Features,
    tier: str,
    result: ShapeResult,
    resolver: Resolver,
    sink_engine: SinkEngine,
    org_pack: OrgPack,
    llm_sink_spans: frozenset[Span],
    agent_fqs: dict[str, str],
    closure_sinks: list[Sink],
) -> None:
    fn = anchor.fn
    confidence: Confidence = "high" if tier == "high" else "medium"
    agent_id = f"agent:bespoke:{slug(fn.name)}"
    method = f"bespoke:agent_shape[{','.join(sorted(features.present))}]"
    evidence = (str(fn.span),)
    source = (anchor.path,)

    def base(fact_id: str) -> dict[str, object]:
        return {
            "id": fact_id,
            "evidence": evidence,
            "confidence": confidence,
            "method": method,
            "source_files": source,
        }

    result.facts.append(
        AgentDefF(
            **base(agent_id),  # type: ignore[arg-type]
            name=fn.name,
            kind="bespoke",
            location=path_location(anchor.path),
            language=path_language(anchor.path),
        )
    )
    for sink in anchor.sinks:
        result.consumed_spans.add(sink.span)
    # Closure sinks stay visible as usages (in_agent=True) — never dropped —
    # but their models/prompts bind to this agent (SPEC-5 §5). Embedding
    # sinks are attributed without binding a chat-model slot.
    for sink in closure_sinks:
        result.member_spans.add(sink.span)
    bindable = [s for s in closure_sinks if s.task != "embedding"]

    # SPEC-7 Z1: prompt/tools/model that arrive as *parameters* of the anchor
    # resolve by binding arguments from the anchor's own call sites (the
    # ``orchestrator(prompt, tools)`` idiom). Bounded: ≤5 call sites, union
    # per parameter, existing resolver budgets.
    declares_tools = _payload_declares_tools(anchor)
    needs_binding = (not features.schema_names and declares_tools) or any(
        s.prompt is None or _unresolved_model(s.model.model) for s in anchor.sinks
    )
    caller_env = _caller_bound_env(anchor, resolver) if needs_binding else {}
    upgraded_sinks = list(anchor.sinks)
    if caller_env:
        upgraded_sinks = [
            _rebind_sink(s, anchor, caller_env, resolver, sink_engine)
            for s in anchor.sinks
        ]
        if not features.schema_names and declares_tools:
            for sink in anchor.sinks:
                expr = sink_engine.named_arg(sink.site.call, "tools") or sink_engine.named_arg(
                    sink.site.call, "functions"
                )
                if expr is None:
                    continue
                bound_scope = resolver.function_scope(
                    anchor.module, sink.site.funcs, sink.site.call.span, bound=caller_env
                )
                for name in _schema_tool_names(
                    resolver.resolve(expr, anchor.module, bound_scope)
                ):
                    if name not in features.schema_names:
                        features.schema_names.append(name)

    seen_models: set[str] = set()
    for sink in sorted(
        [*upgraded_sinks, *bindable], key=lambda s: (s.span.file, s.span.line_start)
    ):
        if sink.model.id not in seen_models:
            seen_models.add(sink.model.id)
            result.facts.append(sink.model)
            result.facts.append(
                BindModelF(
                    **base(stable_id("bind", agent_id, sink.model.id)),  # type: ignore[arg-type]
                    agent_id=agent_id,
                    model_id=sink.model.id,
                )
            )
        if sink.prompt is not None:
            result.facts.append(sink.prompt)
            result.facts.append(
                BindPromptF(
                    **base(stable_id("bind", agent_id, sink.prompt.id)),  # type: ignore[arg-type]
                    agent_id=agent_id,
                    prompt_id=sink.prompt.id,
                )
            )

    bound_tool_names: set[str] = set()
    for fq in features.dispatch_targets:
        entry = resolver.lookup_def(fq)
        if entry is not None and any(
            _string_case_branches(entry[1].body, n) for n in features.schema_names
        ):
            # The target is itself the name-keyed dispatcher for this agent's
            # schema tools — the schema ToolDefs carry the real tool surface;
            # the dispatcher is plumbing, not a tool (SPEC-5 §4).
            continue
        tool = build_tool_from_fq(
            resolver, org_pack, llm_sink_spans, fq, method, anchor.path
        )
        if tool is None:
            continue
        bound_tool_names.add(tool.name)
        result.facts.append(tool)
        result.facts.append(
            BindToolF(
                **base(stable_id("bind", agent_id, tool.id)),  # type: ignore[arg-type]
                agent_id=agent_id,
                tool_id=tool.id,
            )
        )
    for name in features.schema_names:
        if name in bound_tool_names:
            continue  # implementation matched by name → single merged ToolDef
        tool_id = f"tool:schema:{slug(name)}"
        # SPEC-5 §4: link the schema to its implementation through a
        # string-keyed dispatcher (executeTool-style switch); every internal
        # callee in a matching branch runs when the tool is invoked, so their
        # side effects union into the tool's blast radius.
        impls = _name_dispatch_impls(name, anchor.module, resolver)
        effects: list[SideEffect] = []
        external_target: JsonRepr = None
        credential_ref: JsonRepr = None
        auth_verb: str | None = None
        impl_evidence: list[str] = []
        for fq in impls:
            entry = resolver.lookup_def(fq)
            if entry is None:
                continue
            impl_module, impl_fn = entry
            report = classify_body(
                impl_fn, impl_module, resolver, org_pack, llm_sink_spans
            )
            for eff in report.effects:
                if eff not in effects:
                    effects.append(eff)
            external_target = external_target or report.external_target
            credential_ref = credential_ref or report.credential_ref
            auth_verb = auth_verb or report.http_verb
            impl_evidence.append(str(impl_fn.span))
        result.facts.append(
            ToolDefF(
                id=tool_id,
                evidence=(*evidence, *impl_evidence),
                confidence=confidence,
                method=method,
                source_files=source,
                name=name,
                kind="function" if impl_evidence else "schema_declared",
                side_effects=tuple(effects),
                external_target=external_target,
                credential_ref=credential_ref,
                auth_verb=auth_verb,
            )
        )
        result.facts.append(
            BindToolF(
                **base(stable_id("bind", agent_id, tool_id)),  # type: ignore[arg-type]
                agent_id=agent_id,
                tool_id=tool_id,
            )
        )

    # SPEC-7 Z4: a payload that declares tools whose definitions never resolved
    # is a known-unknown, not an absence — finding + honest card phrasing.
    if declares_tools and not bound_tool_names and not features.schema_names:
        result.findings.append(
            FindingRecord(
                kind="unresolved_tools",
                evidence=evidence,
                detail=(
                    "the agent passes tools to the model, but their definitions "
                    "could not be resolved from code"
                ),
                subject_ref=f"agent:{agent_id}",
            )
        )

    if features.state_name is not None and features.state_span is not None:
        state_id = stable_id("state", "messages", f"{anchor.module}.{fn.name}")
        result.facts.append(
            StateDefF(
                id=state_id,
                evidence=(str(features.state_span),),
                confidence=confidence,
                method=method,
                source_files=source,
                kind="messages",
            )
        )
        result.facts.append(
            BindStateF(
                **base(stable_id("bind", agent_id, state_id)),  # type: ignore[arg-type]
                agent_id=agent_id,
                state_id=state_id,
            )
        )

    if features.approval_span is not None:
        policy_id = stable_id("policy", "approval", agent_id)
        result.facts.append(
            PolicyDefF(
                id=policy_id,
                evidence=(str(features.approval_span),),
                confidence="medium",
                method=method,
                source_files=source,
                kind="approval",
                params="input_gate",
            )
        )
        result.facts.append(
            AttachPolicyF(
                **base(stable_id("attach", policy_id, agent_id)),  # type: ignore[arg-type]
                policy_id=policy_id,
                target_id=agent_id,
            )
        )

    # Multi-agent: a call resolving to another anchor → Transfer(kind=invoke).
    for call in _calls_in(fn.body):
        if isinstance(call.callee, NameE):
            for target_fq, target_id in agent_fqs.items():
                if target_id == agent_id:
                    continue
                if target_fq == f"{anchor.module}.{call.callee.name}":
                    result.facts.append(
                        TransferF(
                            **base(stable_id("transfer", agent_id, target_id, "invoke")),  # type: ignore[arg-type]
                            from_id=agent_id,
                            to_id=target_id,
                            kind="invoke",
                        )
                    )


# -- IR helpers ---------------------------------------------------------------


def _calls_in(body: tuple[Stmt, ...]) -> list[CallE]:
    return [e for e in iter_exprs(body, into_defs=False) if isinstance(e, CallE)]


def _unresolved_model(model: JsonRepr) -> bool:
    return isinstance(model, dict) and "unresolved" in model


def _payload_declares_tools(anchor: _Anchor) -> bool:
    return any(
        s.payload is not None and (set(s.payload.keys()) & {"tools", "functions"})
        for s in anchor.sinks
    ) or any(
        isinstance(entry.key, StrE) and entry.key.value in ("tools", "functions")
        for s in anchor.sinks
        if s.site.call.args and isinstance(s.site.call.args[0], DictE)
        for entry in s.site.call.args[0].entries
    )


def _caller_bound_env(anchor: _Anchor, resolver: Resolver) -> dict[str, ValueSet]:
    """SPEC-7 Z1: union of parameter bindings from up to 5 resolvable call
    sites of the anchor function."""
    target_fq = f"{anchor.module}.{anchor.fn.name}"
    merged: dict[str, set[Value]] = {}
    found = 0
    for mod_name in sorted(resolver.tables):
        if found >= 5:
            break
        mod = resolver.graph.by_name.get(mod_name)
        if mod is None:
            continue
        for call in iter_calls(mod.body, into_defs=True):
            if found >= 5:
                break
            name = dotted_name(call.callee)
            if not name or not name.endswith(anchor.fn.name):
                continue  # cheap prefilter before resolving
            funcs, _owner = enclosing_region(mod, call.span)
            scope = (
                resolver.function_scope(mod_name, funcs, call.span) if funcs else None
            )
            callee_vals = resolver.resolve(call.callee, mod_name, scope)
            if not any(isinstance(v, FuncRef) and v.fq == target_fq for v in callee_vals):
                continue
            args = resolver.bind_call_args(call, mod_name, scope)
            for param, vals in resolver.params_env(anchor.fn, args, anchor.module).items():
                merged.setdefault(param, set()).update(vals)
            found += 1
    return {k: frozenset(v) for k, v in merged.items()}


def _rebind_sink(
    sink: Sink,
    anchor: _Anchor,
    caller_env: dict[str, ValueSet],
    resolver: Resolver,
    sink_engine: SinkEngine,
) -> Sink:
    """Re-extract model/prompt for one anchor sink under caller-bound params."""
    scope = resolver.function_scope(
        anchor.module, sink.site.funcs, sink.site.call.span, bound=caller_env
    )
    model = sink.model
    if _unresolved_model(model.model):
        expr = sink_engine.model_expr_of(sink.site.call)
        if expr is not None:
            attribution = attribute_model(
                expr, resolver.resolve(expr, anchor.module, scope)
            )
            if attribution.resolved:
                model = model.model_copy(
                    update={
                        "model": attribution.model,
                        "method": attribution.method,
                        "id": stable_id(
                            "model", model.api_style, attribution.model, model.endpoint
                        ),
                    }
                )
    prompt = sink.prompt
    if prompt is None:
        m_expr = sink_engine.named_arg(sink.site.call, "messages")
        if m_expr is not None:
            prompt = sink_engine.prompt_from_messages(
                resolver.resolve(m_expr, anchor.module, scope), sink.site
            )
        if prompt is None:
            s_expr = sink_engine.named_arg(sink.site.call, "system")
            if s_expr is not None:
                prompt = sink_engine.build_prompt_fact(
                    resolver.resolve(s_expr, anchor.module, scope),
                    sink.site,
                    method="caller:system_arg",
                )
    if model is sink.model and prompt is sink.prompt:
        return sink
    return replace(sink, model=model, prompt=prompt)


def _call_closure(
    anchor: _Anchor,
    resolver: Resolver,
    promoted_spans: set[tuple[str, Span]],
    excluded_spans: frozenset[Span],
) -> list[tuple[str, Span]]:
    """SPEC-5 §5: (module, span) of functions reachable from the anchor via
    internal calls, ≤2 hops. Other promoted agents keep their own sinks;
    classified wrapper defs keep their suppression semantics."""
    out: list[tuple[str, Span]] = []
    seen: set[str] = set()
    frontier: list[tuple[str, FuncIR, int]] = [(anchor.module, anchor.fn, 0)]
    while frontier:
        module, fn, depth = frontier.pop(0)
        if depth >= 2:
            continue
        scope = resolver.function_scope(module, (fn,), None)
        for call in _calls_in(fn.body):
            for v in sorted(resolver.resolve(call.callee, module, scope), key=repr):
                if not isinstance(v, FuncRef) or v.fq in seen:
                    continue
                seen.add(v.fq)
                entry = resolver.lookup_def(v.fq)
                if entry is None:
                    continue
                t_module, t_fn = entry
                key = (t_module, t_fn.span)
                if (
                    key == (anchor.module, anchor.fn.span)
                    or key in promoted_spans
                    or t_fn.span in excluded_spans
                ):
                    continue
                out.append(key)
                frontier.append((t_module, t_fn, depth + 1))
    return out


def _name_dispatch_impls(name: str, module: str, resolver: Resolver) -> list[str]:
    """SPEC-5 §4: fqs of internal functions that a string-keyed dispatcher
    (a lowered ``switch``/if-chain comparing against the tool-name literal)
    routes ``name`` to. Searched in ``module`` and its directly-imported
    internal modules — the schema and its dispatcher travel together."""
    modules = [module]
    table = resolver.tables.get(module)
    if table is not None:
        for imp in table.imports.values():
            target = imp.module
            if resolver.graph.has_module(target) and target not in modules:
                modules.append(target)
    out: list[str] = []
    for mod_name in modules:
        t = resolver.tables.get(mod_name)
        if t is None:
            continue
        for fname in sorted(t.functions):
            fn = t.functions[fname]
            branches = _string_case_branches(fn.body, name)
            if not branches:
                continue
            scope = resolver.function_scope(mod_name, (fn,), None)
            for branch in branches:
                for call in _calls_in(branch):
                    for v in resolver.resolve(call.callee, mod_name, scope):
                        if (
                            isinstance(v, FuncRef)
                            and resolver.lookup_def(v.fq) is not None
                            and v.fq not in out
                        ):
                            out.append(v.fq)
    return out


def _string_case_branches(body: tuple[Stmt, ...], name: str) -> list[tuple[Stmt, ...]]:
    """Bodies of ``if <x> == "<name>"`` arms (the lowered ``switch`` form)."""
    out: list[tuple[Stmt, ...]] = []
    for stmt in iter_stmts(body):
        if (
            isinstance(stmt, IfS)
            and isinstance(stmt.test, CompareE)
            and stmt.test.ops == ("==",)
            and any(
                isinstance(side, StrE) and side.value == name
                for side in (stmt.test.left, *stmt.test.comparators)
            )
        ):
            out.append(stmt.body)
    return out


def _dict_dispatch_targets(
    sub: SubscriptE, module: str, scope: object, resolver: Resolver
) -> list[str]:
    """FuncRef fqs when ``sub`` subscripts a dict of callables."""
    from aiscan.resolve.engine import Scope

    assert scope is None or isinstance(scope, Scope)
    base_vals = resolver.resolve(sub.base, module, scope)
    for v in base_vals:
        if isinstance(v, DictVal):
            fqs: list[str] = []
            for _, entry_vals in v.entries:
                for ev in sorted(entry_vals, key=repr):
                    if isinstance(ev, FuncRef):
                        fqs.append(ev.fq)
            if fqs:
                return fqs
    return []


def _guarded_funcref_calls(
    fn: FuncIR,
    derived: set[str],
    module: str,
    scope: object,
    resolver: Resolver,
    f: _Features,
    sink_spans: tuple[Span, ...] = (),
) -> bool:
    """Response-field branches guarding resolvable calls — two accepted forms:
    the direct form (call inside the branch body) and the break-guarded
    inversion (`if not msg.tool_calls: break` then dispatch — the swarm shape;
    mirrors the P5 F5 tail-return precedent). Callees may be plain names or
    self/this methods (SPEC-7)."""
    from aiscan.resolve.engine import Scope

    assert scope is None or isinstance(scope, Scope)

    def internal_fqs(call: CallE) -> list[str]:
        callee = call.callee
        is_name = isinstance(callee, NameE)
        is_method = (
            isinstance(callee, AttrE)
            and isinstance(callee.base, NameE)
            and callee.base.name in ("self", "this")
        )
        if not (is_name or is_method):
            return []
        out: list[str] = []
        for v in resolver.resolve(callee, module, scope):
            if isinstance(v, FuncRef) and resolver.lookup_def(v.fq) is not None:
                entry = resolver.lookup_def(v.fq)
                if entry is not None and not any(
                    entry[1].span.contains(sp) for sp in sink_spans
                ):
                    out.append(v.fq)
        return out

    found = False
    inversion = False
    for stmt in iter_stmts(fn.body):
        if not isinstance(stmt, IfS) or not _mentions(stmt.test, derived):
            continue
        if any(isinstance(s, BreakS | ContinueS | ReturnS) for s in stmt.body):
            # A terminating guard: its calls are loop housekeeping (logging,
            # cleanup), not dispatch — note the inversion and move on.
            inversion = True
            continue
        for call in _calls_in(stmt.body):
            for fq in internal_fqs(call):
                if fq not in f.dispatch_targets:
                    f.dispatch_targets.append(fq)
                found = True
    if inversion:
        # Break-guarded inversion (the swarm shape): the loop terminates on
        # the response condition, so the dispatch is an internal call whose
        # result the loop consumes and whose arguments carry the response.
        for stmt in iter_stmts(fn.body):
            if not isinstance(stmt, AssignS) or not isinstance(stmt.value, CallE):
                continue
            call = stmt.value
            if not any(_mentions(a, derived) for a in call.args):
                continue
            for fq in internal_fqs(call):
                if fq not in f.dispatch_targets:
                    f.dispatch_targets.append(fq)
                found = True
    if found:
        f.present.add("F2")
    return found


def _has_result_branch(fn: FuncIR, derived: set[str]) -> bool:
    for stmt in iter_stmts(fn.body):
        if isinstance(stmt, IfS) and _mentions(stmt.test, derived):
            return True
    return False


def _mentions(expr: Expr, names: set[str]) -> bool:
    for e in subtree(expr):
        if isinstance(e, NameE) and e.name in names:
            return True
        if (
            isinstance(e, SubscriptE)
            and isinstance(e.index, StrE)
            and e.index.value in _RESULT_FIELDS
        ):
            root = root_name(e)
            if root is not None and root in names:
                return True
    return False


def _has_guarded_return(body: tuple[Stmt, ...], derived: set[str]) -> bool:
    """A return dominated by a response-field branch — either inside the
    branch, or at loop tail behind an ``if …: continue`` guard."""
    has_result_branch = False
    for stmt in iter_stmts(body):
        if isinstance(stmt, IfS) and _mentions(stmt.test, derived):
            has_result_branch = True
            if any(isinstance(s, ReturnS) for s in iter_stmts(stmt.body)):
                return True
    return has_result_branch and any(isinstance(s, ReturnS) for s in iter_stmts(body))


def _find_message_accumulator(
    fn: FuncIR, sink_calls: list[CallE] | None = None
) -> tuple[str, Span] | None:
    """A name assigned a list of role-tagged dicts and later .append()ed —
    or (SPEC-7) a loop-carried list fed INTO the sink call and mutated in the
    function (swarm's ``history``: initialised from a parameter, so the role
    tags are not syntactically visible)."""
    candidates: dict[str, Span] = {}
    for stmt in iter_stmts(fn.body):
        if not isinstance(stmt, AssignS) or not isinstance(stmt.value, ListE):
            continue
        has_role = any(
            isinstance(el, DictE)
            and any(
                isinstance(en.key, StrE) and en.key.value == "role" for en in el.entries
            )
            for el in stmt.value.elems
        )
        if has_role:
            for t in stmt.targets:
                if isinstance(t, NameE):
                    candidates[t.name] = stmt.span
    for call in _calls_in(fn.body):
        if (
            isinstance(call.callee, AttrE)
            and call.callee.attr in ("append", "push")  # Python append / JS push
            and isinstance(call.callee.base, NameE)
            and call.callee.base.name in candidates
        ):
            name = call.callee.base.name
            return name, candidates[name]
    if sink_calls:
        fed: set[str] = set()
        for sc in sink_calls:
            for e in _subtree_exprs(sc):
                if isinstance(e, NameE):
                    fed.add(e.name)
        for call in _calls_in(fn.body):
            if (
                isinstance(call.callee, AttrE)
                and call.callee.attr in ("append", "push", "extend")
                and isinstance(call.callee.base, NameE)
                and call.callee.base.name in fed
            ):
                return call.callee.base.name, call.span
    return None


def _subtree_exprs(call: CallE) -> list[Expr]:
    out: list[Expr] = []
    for arg in call.args:
        out.extend(subtree(arg))
    for k in call.kwargs:
        out.extend(subtree(k.value))
    return out


def _schema_tool_names(vals: ValueSet) -> list[str]:
    names: list[str] = []
    for v in vals:
        if not isinstance(v, ListVal):
            continue
        for elem in v.elems:
            for ev in sorted(elem, key=repr):
                if not isinstance(ev, DictVal):
                    continue
                fn_vals = ev.get("function")
                inner = None
                if fn_vals is not None:
                    for fv in fn_vals:
                        if isinstance(fv, DictVal):
                            inner = fv
                target = inner if inner is not None else ev
                name_vals = target.get("name")
                if name_vals is not None:
                    for nv in name_vals:
                        if isinstance(nv, Str) and nv.s not in names:
                            names.append(nv.s)
    return names
