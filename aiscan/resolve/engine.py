"""Demand-driven resolver (SPEC §6.4).

Frontends and the sink engine issue ``resolve(expr)`` queries; there is no
whole-program pass. Results are frozen sets of abstract values (SPEC §5.1).
The resolver never raises through the pipeline: unknowns become ``Top(reason)``
and hard bounds (depth, hops, per-query and per-scan time) degrade to
``Top("timeout")`` with accounting in ``scan_health``.

Flow handling (no SSA): within a function, straight-line reassignment wins;
across branches, post-states union; loop bodies are evaluated once and unioned
with the pre-state. Over-approximation is acceptable and matches ADG semantics.
"""

from __future__ import annotations

import builtins as _py_builtins
import time
from dataclasses import dataclass, field

from aiscan.context import ResolverBudgets, ResolverStats
from aiscan.ir.nodes import (
    AssignS,
    AttrE,
    AugAssignS,
    BinOpE,
    BoolE,
    BoolOpE,
    CallE,
    ClassDefS,
    ClassIR,
    CompareE,
    DictE,
    Expr,
    ForS,
    FStrE,
    FuncDefS,
    FuncIR,
    IfS,
    LambdaE,
    ListE,
    NameE,
    NoneE,
    NumE,
    OpaqueE,
    ReturnS,
    Span,
    Stmt,
    StrE,
    SubscriptE,
    TryS,
    TupleE,
    UnaryOpE,
    WhileS,
    WithItem,
    WithS,
)
from aiscan.ir.values import (
    Bool,
    BoundArgs,
    ClassInstance,
    ClassRef,
    DictVal,
    FuncRef,
    Hole,
    ListVal,
    ModuleRef,
    NoneV,
    Num,
    PackageRef,
    Str,
    Symbolic,
    Template,
    Top,
    TopReason,
    Value,
    ValueSet,
    single,
)
from aiscan.ir.walk import approx_source, dotted_name, expr_children, iter_calls, iter_stmts
from aiscan.modules.graph import ModuleGraph
from aiscan.modules.symbols import ModuleImport, SymbolImport, SymbolTable
from aiscan.resolve.memo import Memo, memo_key

_BUILTIN_NAMES = frozenset(dir(_py_builtins))

_CONFIG_LOADERS = frozenset(
    {
        "json.load",
        "json.loads",
        "yaml.safe_load",
        "yaml.load",
        "yaml.full_load",
        "tomllib.load",
        "tomllib.loads",
        "toml.load",
    }
)

_ENV_GETTERS = frozenset({"os.environ.get", "os.getenv"})

# SPEC-4 W2: JS environment bases. ``process.env`` / ``import.meta.env`` are
# globals (no import), so they are recognised by dotted name and collapse to a
# single env-sentinel PackageRef; attr/subscript over it yields Symbolic(env).
_JS_ENV_BASES = frozenset({"process.env", "import.meta.env"})
_ENV_SENTINEL = "process.env"

# SPEC-4 W2: identity-preserving global calls. ``JSON.stringify(x)`` returns a
# serialisation of x — for payload analysis it unwraps to x (so a request body
# built as ``body: JSON.stringify(payload)`` still exposes the DictVal).
_JS_STRINGIFY = frozenset({"JSON.stringify"})

# Runnable-style methods on external instances that return (a configured view
# of) the same object — the chain identity must survive them so
# `init_chat_model(...).bind_tools(T).ainvoke(...)` still roots at the model.
_IDENTITY_METHODS = frozenset(
    {"bind_tools", "with_structured_output", "with_config", "with_retry", "bind"}
)

_STR_FOLD_METHODS = frozenset({"lower", "upper", "strip"})

_K_RET = 2  # max reachable ReturnS for call-return resolution (SPEC §6.4)
_K_RET_LIFT = 8  # SPEC-5 §2.3: cap when every return value is flow-free

# SPEC-5 §2.2: the only comparison ops the resolver folds (TS lowering already
# normalises ``===``/``!==`` to these).
_EQ_OPS = frozenset({"==", "is"})
_NE_OPS = frozenset({"!=", "is not"})


@dataclass(slots=True)
class Scope:
    """A resolution scope: current bindings plus the enclosing chain.

    ``memoizable`` is False when bindings came from a specific call site
    (bound arguments / a bound ``self``): results under such scopes must not
    enter the span-keyed memo table.
    """

    module: str
    env: dict[str, ValueSet] = field(default_factory=dict)
    func: FuncIR | None = None
    parent: Scope | None = None
    memoizable: bool = True

    def chain_memoizable(self) -> bool:
        s: Scope | None = self
        while s is not None:
            if not s.memoizable:
                return False
            s = s.parent
        return True


class Resolver:
    """One resolver instance per scan; shared memo and stats."""

    def __init__(
        self,
        graph: ModuleGraph,
        tables: dict[str, SymbolTable],
        budgets: ResolverBudgets,
        stats: ResolverStats,
    ) -> None:
        self.graph = graph
        self.tables = tables
        self.budgets = budgets
        self.stats = stats
        self.memo = Memo()
        self._spent_s = 0.0
        self._deadline = float("inf")
        self._defs: dict[str, tuple[str, FuncIR]] = {}
        self._classes: dict[str, tuple[str, ClassIR]] = {}
        self._method_class: dict[str, str] = {}
        for mod_name in sorted(tables):
            table = tables[mod_name]
            for fname in sorted(table.functions):
                self._defs[f"{mod_name}.{fname}"] = (mod_name, table.functions[fname])
            for cname in sorted(table.classes):
                cls = table.classes[cname]
                fq = f"{mod_name}.{cname}"
                self._classes[fq] = (mod_name, cls)
                for m in cls.methods:
                    self._defs[f"{fq}.{m.name}"] = (mod_name, m)
                    self._method_class[f"{fq}.{m.name}"] = fq

    # -- public API ---------------------------------------------------------

    def resolve(self, expr: Expr, module: str, scope: Scope | None = None) -> ValueSet:
        """Resolve one expression to a value set (a bounded query)."""
        self.stats.queries += 1
        if self._spent_s >= self.budgets.scan_budget_s:
            return self._top("timeout")
        start = time.monotonic()
        self._deadline = start + self.budgets.query_ms / 1000.0
        try:
            return self._resolve(expr, module, scope, 0)
        finally:
            self._spent_s += time.monotonic() - start
            self._deadline = float("inf")

    def lookup_def(self, fq: str) -> tuple[str, FuncIR] | None:
        """(module, FuncIR) for a fully-qualified function/method name."""
        return self._defs.get(fq)

    def bind_call_args(
        self, call: CallE, module: str, scope: Scope | None
    ) -> BoundArgs:
        """Resolve a call's arguments into ``BoundArgs`` (SPEC-7 Z1 public API)."""
        return self._bind_args(call, module, scope, 0)

    def params_env(
        self, fn: FuncIR, args: BoundArgs, module: str
    ) -> dict[str, ValueSet]:
        """Callee param → bound value-set mapping (SPEC-7 Z1 public API)."""
        return self._bind_params(fn, args, module, 0)

    def resolve_default_call(self, fq: str) -> ValueSet | None:
        """Zero-arg call-return resolution of an internal function — pack
        ``default_ref`` support (SPEC-7 Z10: framework default-model
        resolution points like ``get_default_model``). A ``default_ref`` may
        also name a module-level constant (``DEFAULT_REALTIME_MODEL``) when
        the framework's default is an assignment, not a function. None when
        the ref is not in the analysis set (consumer repos of the framework)."""
        canonical = self.canonicalize_fq(fq)
        entry = self._defs.get(canonical) or self._defs.get(fq)
        if entry is not None:
            return self._call_function_body(entry[1], entry[0], BoundArgs(), 0, None)
        module, _, name = fq.rpartition(".")
        table = self.tables.get(module)
        if table is not None and name in table.assignments:
            return self._module_member(module, table, name, 0, hops=0)
        return None

    def lookup_class(self, fq: str) -> tuple[str, ClassIR] | None:
        """(module, ClassIR) for a fully-qualified class name."""
        return self._classes.get(fq)

    def canonicalize_fq(self, fq: str) -> str:
        """Resolve a dotted fq (e.g. ``agents.Agent``) to its definition fq
        (e.g. ``agents.agent.Agent``) when the named module's source is in the
        analysis set; otherwise return it unchanged.

        Framework rules key on the public import path (``agents.Agent``). In a
        consumer repo ``agents`` is third-party, so a call resolves to that
        exact path and matches directly. In the framework's own vendored /
        monorepo source (SPEC-1 §7.2) the same public name resolves *through*
        the package's re-export to the definition module, so the rule fq must be
        canonicalised to that definition to still match.
        """
        parts = fq.split(".")
        for k in range(len(parts) - 1, 0, -1):
            module = ".".join(parts[:k])
            table = self.tables.get(module)
            if table is None:
                continue
            member = self._module_member(module, table, parts[k], 0, hops=0)
            if member is None:
                continue
            for v in sorted(member, key=repr):
                if isinstance(v, ClassRef | FuncRef):
                    rest = parts[k + 1 :]
                    return ".".join([v.fq, *rest]) if rest else v.fq
            break
        return fq

    def chain_root(
        self, expr: Expr, module: str, scope: Scope | None = None
    ) -> tuple[ValueSet, tuple[str, ...]]:
        """Peel ``AttrE`` layers: (resolved base values, attr path).

        ``client.chat.completions.create`` → (resolve(client), ("chat",
        "completions", "create")). SPEC §6.4 method-chain support.
        """
        path: list[str] = []
        node = expr
        while isinstance(node, AttrE):
            path.append(node.attr)
            node = node.base
        return self.resolve(node, module, scope), tuple(reversed(path))

    def function_scope(
        self,
        module: str,
        func_stack: tuple[FuncIR, ...],
        target: Span | None,
        self_instance: ClassInstance | None = None,
        bound: dict[str, ValueSet] | None = None,
    ) -> Scope:
        """Scope at ``target`` inside nested functions ``func_stack``.

        Walks each function body toward the target applying flow effects, so
        the returned scope's env holds the reaching bindings at that point.
        """
        parent: Scope | None = None
        for i, fn in enumerate(func_stack):
            innermost = i == len(func_stack) - 1
            scope = self._new_function_scope(
                module,
                fn,
                parent,
                self_instance=self_instance if innermost else None,
                bound=bound if innermost else None,
            )
            stop: Span | None
            if innermost:
                stop = target
            else:
                stop = func_stack[i + 1].span
            self._walk_to(list(fn.body), scope, stop, 0)
            parent = scope
        assert parent is not None, "func_stack must be non-empty"
        return parent

    def module_scope(self, module: str, target: Span | None) -> Scope:
        """Flow-sensitive scope over the module top level up to ``target``."""
        scope = Scope(module=module)
        mod = self.graph.by_name.get(module)
        if mod is not None:
            self._walk_to(list(mod.body), scope, target, 0)
        return scope

    # -- core dispatch ------------------------------------------------------

    def _top(self, reason: TopReason) -> ValueSet:
        self.stats.count_top(reason)
        return single(Top(reason=reason))

    def _resolve(self, expr: Expr, module: str, scope: Scope | None, depth: int) -> ValueSet:
        if depth > self.budgets.max_depth:
            return self._top("depth")
        if time.monotonic() > self._deadline:
            return self._top("timeout")
        memoizable = scope is None or scope.chain_memoizable()
        key = None
        if memoizable:
            mod = self.graph.by_name.get(module)
            if mod is not None:
                key = memo_key(mod.content_hash, expr.span, type(expr).__name__)
                hit = self.memo.get(key)
                if hit is not None:
                    self.stats.memo_hits += 1
                    return hit
        result = self._resolve_inner(expr, module, scope, depth)
        if key is not None:
            self.memo.put(key, result)
        return result

    def _resolve_inner(
        self, expr: Expr, module: str, scope: Scope | None, depth: int
    ) -> ValueSet:
        match expr:
            case StrE(value=s):
                return single(Str(s=s))
            case NumE(value=n):
                return single(Num(v=n))
            case BoolE(value=b):
                return single(Bool(v=b))
            case NoneE():
                return single(NoneV())
            case FStrE():
                return self._resolve_fstring(expr, module, scope, depth)
            case NameE(name=n):
                return self._resolve_name(n, module, scope, depth)
            case AttrE(base=base, attr=attr):
                # JS env globals (process.env / import.meta.env) → env sentinel.
                if dotted_name(expr) in _JS_ENV_BASES:
                    return single(PackageRef(name=_ENV_SENTINEL, version=None))
                base_vals = self._resolve(base, module, scope, depth + 1)
                return self._attr_over_values(base_vals, attr, module, depth)
            case CallE():
                return self._resolve_call(expr, module, scope, depth)
            case SubscriptE(base=base, index=index):
                return self._resolve_subscript(base, index, module, scope, depth)
            case ListE(elems=elems) | TupleE(elems=elems):
                return single(
                    ListVal(
                        elems=tuple(self._resolve(e, module, scope, depth + 1) for e in elems)
                    )
                )
            case DictE():
                return self._resolve_dict_literal(expr, module, scope, depth)
            case BinOpE(op="+", left=left, right=right):
                return self._resolve_concat(left, right, module, scope, depth)
            case BoolOpE(op=op, operands=operands):
                return self._resolve_bool_op(op, operands, module, scope, depth)
            case UnaryOpE(op="not", operand=operand):
                inner = _as_single(self._resolve(operand, module, scope, depth + 1), Bool)
                if inner is not None:
                    return single(Bool(v=not inner.v))
                return self._top("dynamic")
            case CompareE() as cmp:
                folded = self._fold_compare(cmp, module, scope, depth)
                if folded is not None:
                    return single(folded)
                return self._top("dynamic")
            case LambdaE():
                return self._top("dynamic")
            case OpaqueE():
                return self._top("opaque")
            case _:
                return self._top("dynamic")

    # -- literals and strings ----------------------------------------------

    def _resolve_fstring(
        self, expr: FStrE, module: str, scope: Scope | None, depth: int
    ) -> ValueSet:
        parts: list[str | Hole] = []
        for piece in expr.parts:
            if isinstance(piece, str):
                parts.append(piece)
                continue
            vals = self._resolve(piece, module, scope, depth + 1)
            folded = _as_single_str(vals)
            if folded is not None:
                parts.append(folded)
            else:
                parts.append(_hole_for(vals, piece))
        return single(_template_or_str(parts))

    def _resolve_concat(
        self, left: Expr, right: Expr, module: str, scope: Scope | None, depth: int
    ) -> ValueSet:
        lv = self._resolve(left, module, scope, depth + 1)
        rv = self._resolve(right, module, scope, depth + 1)
        parts: list[str | Hole] = []
        for side, side_expr in ((lv, left), (rv, right)):
            folded = _as_single_str(side)
            if folded is not None:
                parts.append(folded)
                continue
            tmpl = _as_single(side, Template)
            if tmpl is not None:
                parts.extend(tmpl.parts)
                continue
            if all(isinstance(v, Num) for v in side):
                return self._top("dynamic")
            parts.append(_hole_for(side, side_expr))
        return single(_template_or_str(parts))

    def _resolve_bool_op(
        self,
        op: str,
        operands: tuple[Expr, ...],
        module: str,
        scope: Scope | None,
        depth: int,
    ) -> ValueSet:
        """SPEC-5 §2.1: union of operands. For ``or`` (also ``||``/``??``/ternary
        lowering): falsy literals cannot escape a non-last operand, and a single
        definitely-truthy literal short-circuits the rest."""
        out: set[Value] = set()
        last = len(operands) - 1
        for i, operand in enumerate(operands):
            raw = self._resolve(operand, module, scope, depth + 1)
            vals = raw
            if op == "or" and i < last:
                vals = frozenset(
                    v for v in raw if not (isinstance(v, NoneV) or v == Bool(v=False))
                )
            out |= vals
            # Short-circuit on the *pre-drop* set: a post-drop singleton may
            # still be falsy at runtime and reach the next operand.
            if (
                op == "or"
                and i < last
                and len(raw) == 1
                and _definitely_truthy(next(iter(raw)))
            ):
                break
        return frozenset(out) if out else self._top("dynamic")

    def _fold_compare(
        self, e: CompareE, module: str, scope: Scope | None, depth: int
    ) -> Bool | None:
        """SPEC-5 §2.2: fold single-op ==/!= over resolved literals; else None."""
        if len(e.ops) != 1 or len(e.comparators) != 1:
            return None
        op = e.ops[0]
        if op not in _EQ_OPS and op not in _NE_OPS:
            return None
        left = _as_single_literal(self._resolve(e.left, module, scope, depth + 1))
        right = _as_single_literal(self._resolve(e.comparators[0], module, scope, depth + 1))
        if left is None or right is None:
            return None
        eq = left == right
        return Bool(v=eq if op in _EQ_OPS else not eq)

    def _resolve_dict_literal(
        self, expr: DictE, module: str, scope: Scope | None, depth: int
    ) -> ValueSet:
        entries: list[tuple[str, ValueSet]] = []
        open_ = False

        def put(key: str, vals: ValueSet) -> None:
            nonlocal entries
            entries = [(k, v) for k, v in entries if k != key]  # later wins
            entries.append((key, vals))

        for entry in expr.entries:
            if entry.key is None:
                # SPEC-7 Z3: a spread whose value resolves to a dict merges its
                # entries (`{...defaults, ...overrides}` — the config-merge
                # idiom); anything else honestly opens the dict.
                spread_vals = self._resolve(entry.value, module, scope, depth + 1)
                inner = _as_single(spread_vals, DictVal)
                if inner is not None:
                    for k, v in inner.entries:
                        put(k, v)
                    open_ = open_ or inner.open
                else:
                    open_ = True
                continue
            key_vals = self._resolve(entry.key, module, scope, depth + 1)
            key = _as_single_str(key_vals)
            if key is None:
                open_ = True  # computed key
                continue
            put(key, self._resolve(entry.value, module, scope, depth + 1))
        return single(DictVal(entries=tuple(entries), open=open_))

    # -- names --------------------------------------------------------------

    def _resolve_name(self, n: str, module: str, scope: Scope | None, depth: int) -> ValueSet:
        s = scope
        while s is not None:
            if n in s.env:
                return self._fold_mutations(n, s.env[n], s, depth)
            s = s.parent
        table = self.tables.get(module)
        if table is not None:
            result = self._module_member(module, table, n, depth, hops=0)
            if result is not None:
                mod = self.graph.by_name.get(module)
                if mod is not None:
                    return self._fold_module_mutations(n, result, mod.body, module, depth)
                return result
        if n in _BUILTIN_NAMES:
            return single(FuncRef(fq=f"builtins.{n}", def_site=""))
        return self._top("unbound")

    def _module_member(
        self, module: str, table: SymbolTable, n: str, depth: int, hops: int
    ) -> ValueSet | None:
        """Top-level member lookup: defs, classes, assigns, imports, star."""
        if n in table.functions:
            fn = table.functions[n]
            return single(FuncRef(fq=f"{module}.{n}", def_site=str(fn.span)))
        if n in table.classes:
            cls = table.classes[n]
            return single(ClassRef(fq=f"{module}.{n}", def_site=str(cls.span)))
        if n in table.assignments:
            out: set[Value] = set()
            for rhs in table.assignments[n]:
                out |= self._resolve(rhs, module, None, depth + 1)
            return frozenset(out)
        if n in table.imports:
            return self._import_binding(table.imports[n], depth, hops)
        if table.star_imports:
            return self._top("star_import")
        return None

    def _import_binding(self, binding: object, depth: int, hops: int) -> ValueSet:
        if hops > self.budgets.max_hops:
            return self._top("depth")
        if isinstance(binding, ModuleImport):
            if self.graph.has_module(binding.module):
                return single(ModuleRef(name=binding.module))
            return single(
                PackageRef(name=binding.module, version=self.graph.version_of(binding.module))
            )
        assert isinstance(binding, SymbolImport)
        m, sym = binding.module, binding.name
        if self.graph.has_module(m):
            target_table = self.tables.get(m)
            if target_table is not None:
                result = self._module_member(m, target_table, sym, depth, hops + 1)
                if result is not None:
                    return result
            if self.graph.has_module(f"{m}.{sym}"):
                return single(ModuleRef(name=f"{m}.{sym}"))
            return self._top("unbound")
        if (
            m in self.graph.packages or self.graph.has_module(m.split(".")[0])
        ) and self.graph.has_module(f"{m}.{sym}"):
            # Relative import that didn't land on a parsed module.
            return single(ModuleRef(name=f"{m}.{sym}"))
        return single(PackageRef(name=f"{m}.{sym}", version=self.graph.version_of(m)))

    # -- attributes ---------------------------------------------------------

    def _attr_over_values(
        self, base_vals: ValueSet, attr: str, module: str, depth: int
    ) -> ValueSet:
        out: set[Value] = set()
        for v in base_vals:
            out |= self._attr_one(v, attr, module, depth)
        return frozenset(out) if out else self._top("dynamic")

    def _attr_one(self, v: Value, attr: str, module: str, depth: int) -> ValueSet:
        match v:
            case ModuleRef(name=m):
                sub = f"{m}.{attr}"
                if self.graph.has_module(sub):
                    return single(ModuleRef(name=sub))
                table = self.tables.get(m)
                if table is not None:
                    result = self._module_member(m, table, attr, depth, hops=1)
                    if result is not None:
                        return result
                return self._top("unbound")
            case PackageRef(name=fq, version=ver):
                if fq == _ENV_SENTINEL:  # process.env.LLM_MODEL → Symbolic(env, ...)
                    return single(Symbolic(kind="env", key=attr))
                return single(PackageRef(name=f"{fq}.{attr}", version=ver))
            case ClassRef(fq=fq):
                return self._class_attr(fq, attr, depth)
            case ClassInstance() as ci:
                return self._instance_attr(ci, attr, depth)
            case DictVal() as dv:
                # SPEC-7 Z3: JS property access over an object literal is an
                # AttrE (`cfg.model`), unlike Python's subscript.
                hit = dv.get(attr)
                if hit is not None:
                    return hit
                return self._top("dynamic")
            case Top():
                return single(v)
            case _:
                return self._top("dynamic")

    def _class_attr(self, fq: str, attr: str, depth: int) -> ValueSet:
        entry = self._classes.get(fq)
        if entry is None:
            return single(PackageRef(name=f"{fq}.{attr}", version=None))
        cls_module, cls = entry
        for m in cls.methods:
            if m.name == attr:
                return single(FuncRef(fq=f"{fq}.{attr}", def_site=str(m.span)))
        for a in cls.body_assigns:
            for t in a.targets:
                if isinstance(t, NameE) and t.name == attr:
                    return self._resolve(a.value, cls_module, None, depth + 1)
        return self._top("dynamic")

    def _instance_attr(self, ci: ClassInstance, attr: str, depth: int) -> ValueSet:
        if ci.class_fq == "argparse.Namespace":
            return single(Symbolic(kind="cli", key=attr))
        entry = self._classes.get(ci.class_fq)
        if entry is None:
            return self._top("dynamic")
        cls_module, cls = entry
        # ``__init__`` (Python) or ``constructor`` (JS) — the ctor holds the
        # ``self.x = ...`` / ``this.x = ...`` attribute assignments.
        init = next(
            (m for m in cls.methods if m.name in ("__init__", "constructor")), None
        )
        if init is not None:
            bound = self._bind_params(init, ci.ctor_args, cls_module, depth)
            # SPEC-7 sweep fix: apply the ctor's flow up to the assignment —
            # `if not client: client = OpenAI()` before `self.client = client`
            # must be visible (last matching assignment wins, as everywhere).
            target_stmt: AssignS | None = None
            for stmt in init.body:
                if (
                    isinstance(stmt, AssignS)
                    and any(
                        isinstance(t, AttrE)
                        and isinstance(t.base, NameE)
                        and t.base.name in ("self", "this")
                        and t.attr == attr
                        for t in stmt.targets
                    )
                ):
                    target_stmt = stmt
            if target_stmt is not None:
                scope = self._new_function_scope(
                    cls_module, init, None, self_instance=None, bound=bound
                )
                self._walk_to(list(init.body), scope, target_stmt.span, depth)
                return self._resolve(target_stmt.value, cls_module, scope, depth + 1)
        for m in cls.methods:
            if m.name == attr:
                return single(FuncRef(fq=f"{ci.class_fq}.{attr}", def_site=str(m.span)))
        for a in cls.body_assigns:
            for t in a.targets:
                if isinstance(t, NameE) and t.name == attr:
                    return self._resolve(a.value, cls_module, None, depth + 1)
        return self._top("dynamic")

    # -- calls --------------------------------------------------------------

    def _bind_args(self, e: CallE, module: str, scope: Scope | None, depth: int) -> BoundArgs:
        return BoundArgs(
            positional=tuple(self._resolve(a, module, scope, depth + 1) for a in e.args),
            named=tuple(
                (k.name, self._resolve(k.value, module, scope, depth + 1))
                for k in e.kwargs
                if k.name is not None
            ),
        )

    def _bind_params(
        self, fn: FuncIR, args: BoundArgs, module: str, depth: int
    ) -> dict[str, ValueSet]:
        """Map a callee's parameters to bound argument values (with defaults)."""
        env: dict[str, ValueSet] = {}
        positional = [p for p in fn.params if p.kind == "pos"]
        skip_self = 1 if positional and positional[0].name in ("self", "cls") else 0
        for i, p in enumerate(positional[skip_self:]):
            bound = args.get(p.name, position=i)
            if bound is not None:
                env[p.name] = bound
            elif p.default is not None:
                env[p.name] = self._resolve(p.default, module, None, depth + 1)
            else:
                env[p.name] = single(Top(reason="unbound"))
        for p in fn.params:
            if p.kind == "kwonly":
                bound = args.get(p.name)
                if bound is not None:
                    env[p.name] = bound
                elif p.default is not None:
                    env[p.name] = self._resolve(p.default, module, None, depth + 1)
                else:
                    env[p.name] = single(Top(reason="unbound"))
            elif p.kind in ("vararg", "kwarg"):
                env[p.name] = single(Top(reason="dynamic"))
        return env

    def _resolve_call(
        self, e: CallE, module: str, scope: Scope | None, depth: int
    ) -> ValueSet:
        # JSON.stringify(payload) unwraps to payload (SPEC-4 W2 payload analysis).
        if dotted_name(e.callee) in _JS_STRINGIFY and e.args:
            return self._resolve(e.args[0], module, scope, depth + 1)
        out: set[Value] = set()
        remaining: set[Value] = set()
        if isinstance(e.callee, AttrE):
            base_vals = self._resolve(e.callee.base, module, scope, depth + 1)
            attr = e.callee.attr
            if attr in _STR_FOLD_METHODS and not e.args:
                # SPEC-7 Z10: `.lower()/.upper()/.strip()` fold over string
                # literals and pass symbolic/template members through — case
                # normalisation must not destroy a model identity
                # (`os.getenv(K, "gpt-5.4-mini").lower()`).
                folded: set[Value] = set()
                for bv in base_vals:
                    if isinstance(bv, Str):
                        folded.add(Str(s=getattr(bv.s, attr)()))
                    elif isinstance(bv, Symbolic | Template):
                        folded.add(bv)
                if folded and all(
                    isinstance(bv, Str | Symbolic | Template) for bv in base_vals
                ):
                    return frozenset(folded)
            for bv in base_vals:
                handled = self._instance_method_call(bv, attr, e, module, scope, depth)
                if handled is not None:
                    out |= handled
                else:
                    remaining |= self._attr_one(bv, attr, module, depth)
        else:
            remaining |= self._resolve(e.callee, module, scope, depth + 1)
        for cv in remaining:
            out |= self._call_one(cv, e, module, scope, depth)
        return frozenset(out) if out else self._top("dynamic")

    def _instance_method_call(
        self,
        base: Value,
        attr: str,
        e: CallE,
        module: str,
        scope: Scope | None,
        depth: int,
    ) -> ValueSet | None:
        """Calls where the *instance* matters; None → fall through to generic."""
        if not isinstance(base, ClassInstance):
            return None
        if base.class_fq == "builtins.file" and attr in ("read", "readlines"):
            return single(self._file_symbol(base))
        if base.class_fq == "pathlib.Path" and attr in ("read_text", "read_bytes"):
            return single(self._file_symbol(base))
        if base.class_fq == "argparse.ArgumentParser" and attr == "parse_args":
            return single(ClassInstance(class_fq="argparse.Namespace"))
        fq = f"{base.class_fq}.{attr}"
        entry = self._defs.get(fq)
        if entry is None:
            if attr in _IDENTITY_METHODS:
                return single(base)
            return None
        method_module, method = entry
        args = self._bind_args(e, module, scope, depth)
        return self._call_function_body(
            method, method_module, args, depth, self_instance=base
        )

    def _file_symbol(self, ci: ClassInstance) -> Value:
        path_vals = ci.ctor_args.get("file", position=0)
        path = _as_single_str(path_vals) if path_vals is not None else None
        return Symbolic(kind="config", key=f"file:{path}" if path else "file:<dynamic>")

    def _call_one(
        self, cv: Value, e: CallE, module: str, scope: Scope | None, depth: int
    ) -> ValueSet:
        match cv:
            case ClassRef(fq=fq):
                return single(
                    ClassInstance(class_fq=fq, ctor_args=self._bind_args(e, module, scope, depth))
                )
            case FuncRef(fq="builtins.open"):
                return single(
                    ClassInstance(
                        class_fq="builtins.file", ctor_args=self._bind_args(e, module, scope, depth)
                    )
                )
            case FuncRef(fq=fq):
                entry = self._defs.get(fq)
                if entry is None:
                    return self._top("dynamic")
                fn_module, fn = entry
                self_inst: ClassInstance | None = None
                cls_fq = self._method_class.get(fq)
                if cls_fq is not None:
                    self_inst = ClassInstance(class_fq=cls_fq)
                args = self._bind_args(e, module, scope, depth)
                return self._call_function_body(fn, fn_module, args, depth, self_inst)
            case PackageRef(name=fq):
                return self._external_call(fq, e, module, scope, depth)
            case Top():
                return single(cv)
            case _:
                return self._top("dynamic")

    def _external_call(
        self, fq: str, e: CallE, module: str, scope: Scope | None, depth: int
    ) -> ValueSet:
        if fq in _ENV_GETTERS:
            key = self._literal_arg(e, 0, module, scope, depth)
            if key is not None:
                out: set[Value] = {Symbolic(kind="env", key=key)}
                if len(e.args) > 1:
                    # SPEC-7 Z10: the getter's default is part of the value —
                    # os.getenv(K, "gpt-5.4-mini") ≙ env(K) || "gpt-5.4-mini".
                    out |= self._resolve(e.args[1], module, scope, depth + 1)
                return frozenset(out)
            return self._top("dynamic")
        if fq in _CONFIG_LOADERS:
            return single(Symbolic(kind="config", key=self._loader_source(e, module, scope, depth)))
        if fq == "boto3.client":
            service = self._literal_arg(e, 0, module, scope, depth)
            if service is not None:
                return single(
                    ClassInstance(
                        class_fq=f"boto3.{service}-client",
                        ctor_args=self._bind_args(e, module, scope, depth),
                    )
                )
        # External callable: model as an instance of its fq so method chains
        # (`OpenAI(...).chat.completions.create`) keep their root identity.
        return single(
            ClassInstance(class_fq=fq, ctor_args=self._bind_args(e, module, scope, depth))
        )

    def _loader_source(self, e: CallE, module: str, scope: Scope | None, depth: int) -> str:
        if e.args:
            vals = self._resolve(e.args[0], module, scope, depth + 1)
            for v in vals:
                if isinstance(v, ClassInstance) and v.class_fq in ("builtins.file", "pathlib.Path"):
                    inner = self._file_symbol(v)
                    if isinstance(inner, Symbolic):
                        return inner.key.removeprefix("file:")
                if isinstance(v, Symbolic) and v.kind == "config":
                    return v.key.removeprefix("file:")
        return "<dynamic>"

    def _literal_arg(
        self, e: CallE, position: int, module: str, scope: Scope | None, depth: int
    ) -> str | None:
        if position < len(e.args):
            return _as_single_str(self._resolve(e.args[position], module, scope, depth + 1))
        return None

    def _call_function_body(
        self,
        fn: FuncIR,
        fn_module: str,
        args: BoundArgs,
        depth: int,
        self_instance: ClassInstance | None,
    ) -> ValueSet:
        bound = self._bind_params(fn, args, fn_module, depth)
        entry = self._new_function_scope(
            fn_module, fn, None, self_instance=self_instance, bound=bound
        )
        returns: list[ReturnS] = []
        self._collect_reachable(fn.body, entry, fn_module, depth, returns)
        if len(returns) > _K_RET and (
            len(returns) > _K_RET_LIFT or not _returns_flow_free(returns, fn)
        ):
            return self._top("dynamic")
        if not returns:
            # Zero *reachable* returns in a function that has returns means
            # pruning lost a path (e.g. a fallthrough shape the IR cannot
            # express) — degrade honestly, never to a confident NoneV.
            if any(isinstance(s, ReturnS) for s in iter_stmts(fn.body)):
                return self._top("dynamic")
            return single(NoneV())
        out: set[Value] = set()
        for ret in returns:
            scope = self._new_function_scope(
                fn_module, fn, None, self_instance=self_instance, bound=bound
            )
            self._walk_to(list(fn.body), scope, ret.span, depth)
            if ret.value is None:
                out.add(NoneV())
            else:
                out |= self._resolve(ret.value, fn_module, scope, depth + 1)
        return frozenset(out) if out else single(NoneV())

    def _collect_reachable(
        self,
        stmts: tuple[Stmt, ...],
        scope: Scope,
        module: str,
        depth: int,
        out: list[ReturnS],
    ) -> bool:
        """SPEC-5 §2.3: collect the ``ReturnS`` reachable from the top of
        ``stmts``; True when the list unconditionally returns (statements after
        it are dead). ``IfS`` tests are evaluated against the param-bound entry
        scope only — locals-dependent tests fall back to both branches (an
        over-approximation, never a loss)."""
        for s in stmts:
            match s:
                case ReturnS():
                    out.append(s)
                    return True
                case IfS(test=test, body=body, orelse=orelse):
                    decided = _as_single(
                        self._resolve(test, module, scope, depth + 1), Bool
                    )
                    if decided is not None:
                        if decided.v and not body and orelse:
                            # A selected-but-empty arm is the lowered-switch
                            # fallthrough signature (the TS lowering leaves
                            # fallthrough case bodies empty): control continues
                            # in the sibling chain, not after the IfS. Explore
                            # it; claim nothing about termination (SPEC-5 §2.3).
                            self._collect_reachable(orelse, scope, module, depth, out)
                            continue
                        taken = body if decided.v else orelse
                        if self._collect_reachable(taken, scope, module, depth, out):
                            return True
                    else:
                        t_body = self._collect_reachable(body, scope, module, depth, out)
                        t_else = self._collect_reachable(orelse, scope, module, depth, out)
                        if t_body and t_else:
                            return True
                case WhileS(body=body) | ForS(body=body):
                    # A loop may not execute: collect but never terminate.
                    self._collect_reachable(body, scope, module, depth, out)
                case WithS(body=body):
                    if self._collect_reachable(body, scope, module, depth, out):
                        return True
                case TryS(body=body, handlers=handlers, final=final):
                    self._collect_reachable(body, scope, module, depth, out)
                    for h in handlers:
                        self._collect_reachable(h.body, scope, module, depth, out)
                    if self._collect_reachable(final, scope, module, depth, out):
                        return True
                case _:
                    pass
        return False

    # -- subscripts ---------------------------------------------------------

    def _resolve_subscript(
        self, base: Expr, index: Expr, module: str, scope: Scope | None, depth: int
    ) -> ValueSet:
        base_vals = self._resolve(base, module, scope, depth + 1)
        index_vals = self._resolve(index, module, scope, depth + 1)
        key = _as_single_str(index_vals)
        out: set[Value] = set()
        for bv in base_vals:
            match bv:
                case PackageRef(name="os.environ" | "process.env") if key is not None:
                    out.add(Symbolic(kind="env", key=key))
                case Symbolic(kind="config", key=cfg) if key is not None:
                    sep = "." if ":" in cfg else ":"
                    out.add(Symbolic(kind="config", key=f"{cfg}{sep}{key}"))
                case DictVal() as dv:
                    if key is not None:
                        hit = dv.get(key)
                        if hit is not None:
                            out |= hit
                        else:
                            out |= self._top("dynamic")
                    else:
                        out |= self._top("dynamic")
                case ListVal() as lv:
                    idx = _as_single_int(index_vals)
                    if idx is not None and 0 <= idx < len(lv.elems):
                        out |= lv.elems[idx]
                    else:
                        out |= self._top("dynamic")
                case Top():
                    out.add(bv)
                case _:
                    out |= self._top("dynamic")
        return frozenset(out) if out else self._top("dynamic")

    # -- flow walking -------------------------------------------------------

    def _new_function_scope(
        self,
        module: str,
        fn: FuncIR,
        parent: Scope | None,
        self_instance: ClassInstance | None,
        bound: dict[str, ValueSet] | None,
    ) -> Scope:
        env: dict[str, ValueSet] = {}
        click = _click_params(fn)
        for p in fn.params:
            if p.kind in ("vararg", "kwarg"):
                env[p.name] = single(Top(reason="dynamic"))
            elif p.default is not None:
                env[p.name] = self._resolve(p.default, module, None, 0)
            elif p.name in click:
                env[p.name] = single(Symbolic(kind="cli", key=p.name))
            elif p.name in ("self", "cls"):
                continue
            else:
                env[p.name] = single(Top(reason="unbound"))
        memoizable = True
        if self_instance is not None:
            # Bind both receiver names: Python methods read ``self``, JS ``this``.
            env["self"] = single(self_instance)
            env["this"] = single(self_instance)
            # SPEC-7 sweep fix: classmethod factories (`return cls(llm)`) —
            # ``cls`` binds to the owning class ref so the returned instance
            # keeps its identity (gpt-researcher's GenericLLMProvider.from_provider).
            env.setdefault("cls", single(ClassRef(fq=self_instance.class_fq, def_site="")))
            memoizable = False
        if bound:
            env.update(bound)
            memoizable = False
        return Scope(
            module=module,
            env=env,
            func=fn,
            parent=parent,
            memoizable=memoizable,
        )

    def _walk_to(
        self, stmts: list[Stmt], scope: Scope, target: Span | None, depth: int
    ) -> bool:
        """Apply flow effects to ``scope.env`` until ``target`` is reached.

        Returns True when the target was found (walking stops there)."""
        for stmt in stmts:
            if target is not None and stmt.span.contains(target):
                return self._descend(stmt, scope, target, depth)
            self._apply(stmt, scope, depth)
        return False

    def _descend(self, stmt: Stmt, scope: Scope, target: Span, depth: int) -> bool:
        match stmt:
            case IfS(body=body, orelse=orelse):
                for block in (body, orelse):
                    if any(s.span.contains(target) for s in block):
                        return self._walk_to(list(block), scope, target, depth)
                return True  # target in the test: env is the pre-state
            case WhileS(body=body):
                if any(s.span.contains(target) for s in body):
                    return self._walk_to(list(body), scope, target, depth)
                return True
            case ForS(target=loop_var, body=body):
                if isinstance(loop_var, NameE):
                    scope.env[loop_var.name] = single(Top(reason="dynamic"))
                if any(s.span.contains(target) for s in body):
                    return self._walk_to(list(body), scope, target, depth)
                return True
            case WithS(items=items, body=body):
                self._bind_with_items(items, scope, depth)
                if any(s.span.contains(target) for s in body):
                    return self._walk_to(list(body), scope, target, depth)
                return True
            case TryS(body=body, handlers=handlers, final=final):
                for block in (body, *(h.body for h in handlers), final):
                    if any(s.span.contains(target) for s in block):
                        return self._walk_to(list(block), scope, target, depth)
                return True
            case FuncDefS() | ClassDefS():
                self._apply(stmt, scope, depth)
                return True  # inner scope is built separately by the caller
            case _:
                return True  # target inside a simple statement: pre-state env

    def _apply(self, stmt: Stmt, scope: Scope, depth: int) -> None:
        module = scope.module
        match stmt:
            case AssignS(targets=targets, value=value):
                vals = self._resolve(value, module, scope, depth + 1)
                for t in targets:
                    self._apply_target(t, vals, scope, depth)
            case AugAssignS(target=NameE(name=n), op=aug_op, value=aug_value):
                current = scope.env.get(n)
                lv = _as_single(current, ListVal) if current else None
                if lv is not None:
                    scope.env[n] = single(
                        ListVal(elems=lv.elems, open=True)
                    )
                elif aug_op == "+" and current is not None:
                    # SPEC-7 Z2: string/template `+=` folds into a Template —
                    # the prompt-building idiom (`prompt += "..."`); previously
                    # degraded to Top and lost the whole prompt.
                    base: list[str | Hole] | None = None
                    base_str = _as_single_str(current)
                    base_tmpl = _as_single(current, Template)
                    if base_str is not None:
                        base = [base_str]
                    elif base_tmpl is not None:
                        base = list(base_tmpl.parts)
                    if base is not None:
                        rhs = self._resolve(aug_value, module, scope, depth + 1)
                        rhs_str = _as_single_str(rhs)
                        rhs_tmpl = _as_single(rhs, Template)
                        if rhs_str is not None:
                            base.append(rhs_str)
                        elif rhs_tmpl is not None:
                            base.extend(rhs_tmpl.parts)
                        else:
                            base.append(_hole_for(rhs, aug_value))
                        scope.env[n] = single(_template_or_str(base))
                    else:
                        scope.env[n] = single(Top(reason="dynamic"))
                else:
                    scope.env[n] = single(Top(reason="dynamic"))
            case FuncDefS(func=fn):
                fq = f"{module}.{fn.name}@{fn.span.line_start}"
                self._defs[fq] = (module, fn)
                scope.env[fn.name] = single(FuncRef(fq=fq, def_site=str(fn.span)))
            case ClassDefS(cls=cls):
                fq = f"{module}.{cls.name}@{cls.span.line_start}"
                self._classes[fq] = (module, cls)
                for m in cls.methods:
                    self._defs[f"{fq}.{m.name}"] = (module, m)
                    self._method_class[f"{fq}.{m.name}"] = fq
                scope.env[cls.name] = single(ClassRef(fq=fq, def_site=str(cls.span)))
            case IfS(body=body, orelse=orelse):
                pre = dict(scope.env)
                self._walk_all(list(body), scope, depth)
                post_body = scope.env
                scope.env = dict(pre)
                self._walk_all(list(orelse), scope, depth)
                scope.env = _union_env(post_body, scope.env)
            case WhileS(body=body):
                pre = dict(scope.env)
                self._walk_all(list(body), scope, depth)
                scope.env = _union_env(pre, scope.env)
            case ForS(target=loop_var, body=body):
                pre = dict(scope.env)
                if isinstance(loop_var, NameE):
                    scope.env[loop_var.name] = single(Top(reason="dynamic"))
                self._walk_all(list(body), scope, depth)
                scope.env = _union_env(pre, scope.env)
            case WithS(items=items, body=body):
                self._bind_with_items(items, scope, depth)
                self._walk_all(list(body), scope, depth)
            case TryS(body=body, handlers=handlers, final=final):
                self._walk_all(list(body), scope, depth)
                post = dict(scope.env)
                merged = post
                for h in handlers:
                    scope.env = dict(post)
                    self._walk_all(list(h.body), scope, depth)
                    merged = _union_env(merged, scope.env)
                scope.env = merged
                self._walk_all(list(final), scope, depth)
            case _:
                return

    def _walk_all(self, stmts: list[Stmt], scope: Scope, depth: int) -> None:
        for stmt in stmts:
            self._apply(stmt, scope, depth)

    def _apply_target(self, t: Expr, vals: ValueSet, scope: Scope, depth: int) -> None:
        match t:
            case NameE(name=n):
                scope.env[n] = vals
            case TupleE(elems=elems) | ListE(elems=elems):
                for el in elems:
                    if isinstance(el, NameE):
                        scope.env[el.name] = single(Top(reason="dynamic"))
            case SubscriptE(base=NameE(name=n), index=index):
                current = scope.env.get(n)
                dv = _as_single(current, DictVal) if current else None
                key = _as_single_str(self._resolve(index, scope.module, scope, depth + 1))
                if dv is not None and key is not None:
                    kept = tuple((k, v) for k, v in dv.entries if k != key)
                    scope.env[n] = single(
                        DictVal(entries=(*kept, (key, vals)), open=dv.open)
                    )
                elif current is not None:
                    scope.env[n] = self._top("dynamic")
            case _:
                return

    def _bind_with_items(self, items: tuple[object, ...], scope: Scope, depth: int) -> None:
        for item in items:
            if isinstance(item, WithItem) and isinstance(item.asname, NameE):
                scope.env[item.asname.name] = self._resolve(
                    item.context, scope.module, scope, depth + 1
                )

    # -- container mutation folding ----------------------------------------

    def _fold_mutations(
        self, name: str, vals: ValueSet, scope: Scope, depth: int
    ) -> ValueSet:
        lv = _as_single(vals, ListVal)
        if lv is None or scope.func is None:
            return vals
        return single(self._fold_list_calls(name, lv, scope.func.body, scope, depth))

    def _fold_module_mutations(
        self, name: str, vals: ValueSet, body: tuple[Stmt, ...], module: str, depth: int
    ) -> ValueSet:
        lv = _as_single(vals, ListVal)
        if lv is not None:
            return single(self._fold_list_calls(name, lv, body, None, depth, module=module))
        dv = _as_single(vals, DictVal)
        if dv is not None:
            return single(self._fold_dict_assigns(name, dv, body, module, depth))
        return vals

    def _fold_list_calls(
        self,
        name: str,
        lv: ListVal,
        body: tuple[Stmt, ...],
        scope: Scope | None,
        depth: int,
        module: str | None = None,
    ) -> ListVal:
        mod = module or (scope.module if scope else "")
        elems = list(lv.elems)
        open_ = lv.open
        for call in iter_calls(body):
            callee = call.callee
            # ``append``/``extend`` (Python) and ``push`` (JS) grow a list.
            if not (
                isinstance(callee, AttrE)
                and isinstance(callee.base, NameE)
                and callee.base.name == name
                and callee.attr in ("append", "extend", "push")
            ):
                continue
            if not call.args:
                continue
            if callee.attr == "extend":
                arg_vals = self._resolve(call.args[0], mod, scope, depth + 1)
                inner = _as_single(arg_vals, ListVal)
                if inner is not None and not inner.open:
                    elems.extend(inner.elems)
                else:
                    open_ = True
                continue
            # append / push — one or (JS push) several element args.
            for arg in call.args:
                arg_vals = self._resolve(arg, mod, scope, depth + 1)
                if any(isinstance(v, Top) for v in arg_vals):
                    open_ = True
                else:
                    elems.append(arg_vals)
        return ListVal(elems=tuple(elems), open=open_)

    def _fold_dict_assigns(
        self, name: str, dv: DictVal, body: tuple[Stmt, ...], module: str, depth: int
    ) -> DictVal:
        entries = list(dv.entries)
        open_ = dv.open
        for stmt in iter_stmts(body):
            if not isinstance(stmt, AssignS):
                continue
            for t in stmt.targets:
                if (
                    isinstance(t, SubscriptE)
                    and isinstance(t.base, NameE)
                    and t.base.name == name
                ):
                    key = _as_single_str(self._resolve(t.index, module, None, depth + 1))
                    if key is None:
                        open_ = True
                        continue
                    vals = self._resolve(stmt.value, module, None, depth + 1)
                    entries = [(k, v) for k, v in entries if k != key]
                    entries.append((key, vals))
        return DictVal(entries=tuple(entries), open=open_)


# -- helpers ----------------------------------------------------------------


def _as_single[T](vals: ValueSet | None, kind: type[T]) -> T | None:
    if vals is not None and len(vals) == 1:
        v = next(iter(vals))
        if isinstance(v, kind):
            return v
    return None


def _as_single_str(vals: ValueSet | None) -> str | None:
    v = _as_single(vals, Str)
    return v.s if isinstance(v, Str) else None


def _as_single_int(vals: ValueSet | None) -> int | None:
    v = _as_single(vals, Num)
    if isinstance(v, Num) and isinstance(v.v, int):
        return v.v
    return None


def _hole_for(vals: ValueSet, expr: Expr) -> Hole:
    """Template hole; symbolic values render as ``kind:key`` so downstream
    extractors (credential refs) can read them."""
    sym = _as_single(vals, Symbolic)
    if sym is not None:
        return Hole(expr=f"{sym.kind}:{sym.key}")
    return Hole(expr=approx_source(expr))


def _template_or_str(parts: list[str | Hole]) -> Value:
    if all(isinstance(p, str) for p in parts):
        return Str(s="".join(p for p in parts if isinstance(p, str)))
    merged: list[str | Hole] = []
    for p in parts:
        if isinstance(p, str) and merged and isinstance(merged[-1], str):
            merged[-1] = merged[-1] + p
        else:
            merged.append(p)
    return Template(parts=tuple(merged))


def _definitely_truthy(v: Value) -> bool:
    """True only for literals that are truthy beyond doubt (SPEC-5 §2.1)."""
    match v:
        case Str(s=s):
            return s != ""
        case Num(v=n):
            return n != 0
        case Bool(v=b):
            return b
        case _:
            return False


def _as_single_literal(vals: ValueSet) -> Value | None:
    if len(vals) == 1:
        v = next(iter(vals))
        if isinstance(v, Str | Num | Bool | NoneV):
            return v
    return None


def _returns_flow_free(returns: list[ReturnS], fn: FuncIR) -> bool:
    """SPEC-5 §2.3: a return value is flow-free when none of its names is
    assigned anywhere in the function body — its resolution cannot depend on
    the statement walk, so the ``_K_RET`` cap can be lifted safely."""
    assigned = _assigned_names(fn.body)
    for ret in returns:
        if ret.value is not None and not _expr_names(ret.value).isdisjoint(assigned):
            return False
    return True


def _assigned_names(body: tuple[Stmt, ...]) -> frozenset[str]:
    out: set[str] = set()
    for s in iter_stmts(body):
        match s:
            case AssignS(targets=targets):
                for t in targets:
                    out |= _target_names(t)
            case AugAssignS(target=t):
                out |= _target_names(t)
            case ForS(target=t):
                out |= _target_names(t)
            case WithS(items=items):
                for item in items:
                    if isinstance(item, WithItem) and isinstance(item.asname, NameE):
                        out.add(item.asname.name)
            case _:
                pass
    return frozenset(out)


def _target_names(t: Expr) -> set[str]:
    match t:
        case NameE(name=n):
            return {n}
        case TupleE(elems=elems) | ListE(elems=elems):
            return {el.name for el in elems if isinstance(el, NameE)}
        case SubscriptE(base=NameE(name=n)) | AttrE(base=NameE(name=n)):
            return {n}  # mutation through the name: treat as assigned
        case _:
            return set()


def _expr_names(expr: Expr) -> frozenset[str]:
    out: set[str] = set()
    stack: list[Expr] = [expr]
    while stack:
        e = stack.pop()
        if isinstance(e, NameE):
            out.add(e.name)
        stack.extend(expr_children(e))
    return frozenset(out)


def _union_env(a: dict[str, ValueSet], b: dict[str, ValueSet]) -> dict[str, ValueSet]:
    out = dict(a)
    for k, v in b.items():
        out[k] = out[k] | v if k in out else v
    return out


def _click_params(fn: FuncIR) -> frozenset[str]:
    """Params of click-decorated functions resolve to Symbolic(cli, name)."""
    for dec in fn.decorators:
        dotted = dotted_name(dec) or (
            dotted_name(dec.callee) if isinstance(dec, CallE) else ""
        )
        if dotted.startswith("click."):
            return frozenset(
                p.name for p in fn.params if p.kind in ("pos", "kwonly")
            )
    return frozenset()
