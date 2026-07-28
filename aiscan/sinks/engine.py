"""Sink classification orchestrator (SPEC §6.5).

Checked in order: (a) SDK signature, (b) HTTP shape, (c) known host,
(d) wrapper (added by the bespoke frontend's fixed point, §6.7.1). First hit
classifies; later hits are recorded as corroboration signals.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

from aiscan.context import OrgPack
from aiscan.facts.models import (
    ApiStyle,
    Confidence,
    ModelRefF,
    PromptDefF,
    Task,
    stable_id,
)
from aiscan.ir.nodes import (
    AssignS,
    AttrE,
    CallE,
    ClassDefS,
    ClassIR,
    CompareE,
    DictE,
    Expr,
    FuncDefS,
    FuncIR,
    IfS,
    ModuleIR,
    NameE,
    Span,
    Stmt,
    StrE,
    SubscriptE,
)
from aiscan.ir.values import (
    ClassInstance,
    DictVal,
    JsonRepr,
    ListVal,
    ModuleRef,
    PackageRef,
    Str,
    Symbolic,
    Template,
    Top,
    ValueSet,
    to_repr,
)
from aiscan.ir.walk import (
    dotted_name,
    expr_children,
    iter_exprs,
    iter_stmts,
    stmt_child_stmts,
    stmt_exprs,
)
from aiscan.resolve.engine import Resolver, Scope
from aiscan.sinks.attribution import (
    RUNG_EXTERNAL,
    HostRegistry,
    ModelAttribution,
    attribute_model,
    azure_external_model,
    deployment_from_url,
    host_of,
)
from aiscan.sinks.shape import ShapeInputs, ShapeScore, score_shape

_HTTP_MODULE_SINK_TAILS = ("post", "request", "stream", "urlopen")
_HTTP_MODULE_ROOTS = ("requests", "httpx", "urllib.request")
_HTTP_INSTANCE_ROOTS = (
    "requests.Session",
    "httpx.Client",
    "httpx.AsyncClient",
    "aiohttp.ClientSession",
)

_RESPONSE_KEY_FIELDS = {"choices": "choices0", "candidates": "candidates0", "content": "content0"}
_RESPONSE_ATTR_FIELDS = {"stop_reason", "finish_reason"}

# SPEC-4 W6: SDK-method chain suffixes. A call whose attribute path ENDS with
# one of these AND whose options object has model + messages/prompt is an LLM
# call even when the root is opaque — the dependency-injection case
# (`ctx.openai.chat.completions.create`, `this.client.messages.create`), which
# dominates real TS code. Endpoint-agnostic detection applied to call shape.
_CHAIN_SHAPE_SUFFIXES = (
    "chat.completions.create",
    "chat.completions.stream",
    "responses.create",
    "messages.create",
    "messages.stream",
    "chat.complete",
    # SPEC-5 §3: embeddings behind any indirection (typed DI param, factory
    # client) were invisible — {model, input} alone scores below threshold.
    "embeddings.create",
    # Audio behind DI (`self._client.audio...` in a voice adapter): STT carries
    # {model, prompt}, TTS {model, input} — both clear threshold via the suffix
    # + model+prompt/input signal. The streaming-response variant is a distinct
    # chain tail, so it is listed explicitly.
    "audio.transcriptions.create",
    "audio.speech.create",
    "audio.speech.with_streaming_response.create",
)

# SPEC-4 W3: JS/TS HTTP callees. fetch/$fetch take (url, options{body,headers});
# axios/got .post|.put|.request take (url, data, config) or a single config.
_TS_FETCH_CALLEES = frozenset({"fetch", "globalThis.fetch", "window.fetch", "$fetch"})
_TS_CLIENT_ROOTS = frozenset({"axios", "got", "ky"})
_TS_CLIENT_METHODS = frozenset({"post", "put", "patch", "request"})
_TS_STREAM_METHODS = frozenset({"stream"})

# SPEC-7 sweep fix: the LangChain invocation family. `.invoke`/`.ainvoke` are
# far too generic to match on name alone — they count only when the first
# argument is a role-tagged messages list, which is decisive (the
# `self.llm.ainvoke(messages)` idiom that made gpt-researcher scan empty).
_INVOKE_SUFFIXES = frozenset({"invoke", "ainvoke", "stream", "astream", "batch", "abatch"})

# At a wrapper method's def site the messages arrive as a parameter, so the
# role-tag shape is invisible — a bare argument *named* like a messages list is
# accepted instead (same precedent as the wrapper scorer's _PARAM_SIGNALS).
_MESSAGES_ARG_NAMES = frozenset({"messages", "msgs", "history", "chat_history", "conversation"})

SinkKind = str  # "sdk" | "shape" | "host" | "wrapper"


class SdkEntry(BaseModel):
    """One sdk_registry.yaml entry."""

    model_config = ConfigDict(frozen=True)

    roots: tuple[str, ...]
    chains: tuple[str, ...]
    api_style: ApiStyle
    provider: str | None
    model_arg: str | None = None
    model_ctor_arg: int | None = None
    model_ctor_kwargs: tuple[str, ...] = ()
    system_arg: str | None = None
    messages_arg: str | None = None
    ctor_endpoint_kwargs: tuple[str, ...] = ()
    call_endpoint_kwargs: tuple[str, ...] = ()
    task_by_chain: dict[str, Task] = {}


def load_sdk_registry(path: Path | None = None) -> tuple[SdkEntry, ...]:
    p = path or Path(__file__).with_name("sdk_registry.yaml")
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    return tuple(SdkEntry.model_validate(e) for e in raw)


def load_host_registry(org_pack: OrgPack, path: Path | None = None) -> HostRegistry:
    p = path or Path(__file__).with_name("hosts.yaml")
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    return HostRegistry(
        hosts=dict(raw.get("hosts", {})),
        wildcards=dict(raw.get("wildcards", {})),
        gateway_hosts=tuple(org_pack.gateway_hosts),
    )


@dataclass(frozen=True, slots=True)
class CallSite:
    """A call expression with its enclosing region and location flags."""

    call: CallE
    module: str
    path: str
    funcs: tuple[FuncIR, ...]
    owner: ClassIR | None
    under_main: bool
    in_tests: bool


@dataclass(frozen=True, slots=True)
class Sink:
    """A call site classified as invoking an LLM."""

    site: CallSite
    kind: SinkKind
    api_style: ApiStyle
    task: Task
    confidence: Confidence
    score: int | None
    signals: tuple[str, ...]
    model: ModelRefF
    prompt: PromptDefF | None
    endpoint: JsonRepr
    payload: DictVal | None
    credential_headers: DictVal | None

    @property
    def span(self) -> Span:
        return self.site.call.span


@dataclass(slots=True)
class SinkScanResult:
    sinks: list[Sink] = field(default_factory=list)
    suspected: list[tuple[CallSite, int]] = field(default_factory=list)


def _is_main_guard(stmt: Stmt) -> bool:
    if not isinstance(stmt, IfS) or not isinstance(stmt.test, CompareE):
        return False
    test = stmt.test
    sides = [test.left, *test.comparators]
    has_name = any(isinstance(s, NameE) and s.name == "__name__" for s in sides)
    has_main = any(isinstance(s, StrE) and s.value == "__main__" for s in sides)
    return has_name and has_main


def _expr_subtree(exprs: tuple[Expr, ...]) -> Iterator[Expr]:
    stack = list(reversed(exprs))
    while stack:
        e = stack.pop()
        yield e
        stack.extend(reversed(expr_children(e)))


def is_test_path(path: str) -> bool:
    """True for a repo-relative path that lives in a test suite. Python:
    ``tests``/``test`` dirs, ``test_*.py`` / ``*_test.py``, ``conftest.py``.
    TS/JS (SPEC-4 W5): ``__tests__``/``__mocks__`` dirs, ``*.test.*`` /
    ``*.spec.*``, and Storybook (``stories``/``.storybook`` dirs, ``*.stories.*``)."""
    parts = path.split("/")
    base = parts[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    return (
        "tests" in parts
        or "test" in parts
        or "__tests__" in parts
        or "__mocks__" in parts
        or "stories" in parts
        or ".storybook" in parts
        or base.startswith("test_")
        or base.endswith("_test.py")
        or base == "conftest.py"
        or stem.endswith((".test", ".spec", ".stories"))
    )


def _shape_task(chain_hint: str | None, url_text: str) -> Task:
    """Task for a shape sink from its matched chain suffix / URL. Chat is the
    default; embeddings/audio are read off the suffix (their SDK-registry
    entries carry the same task, so both detection paths agree)."""
    hint = chain_hint or ""
    if "/embeddings" in url_text or hint.startswith("embeddings"):
        return "embedding"
    if "transcriptions" in hint:
        return "stt"
    if "audio.speech" in hint:
        return "tts"
    return "chat"


def path_language(path: str) -> Literal["python", "typescript", "javascript"]:
    """Source language from a file's extension (SPEC-4)."""
    lower = path.lower()
    if lower.endswith((".ts", ".tsx", ".mts", ".cts")):
        return "typescript"
    if lower.endswith((".js", ".jsx", ".mjs", ".cjs")):
        return "javascript"
    return "python"


def path_location(path: str) -> Literal["production", "test", "example"]:
    """Where a construct lives, for tagging inventory entries so test/example
    noise can be filtered without dropping anything: ``test`` | ``example`` |
    ``production``."""
    if is_test_path(path):
        return "test"
    parts = path.split("/")
    if {"examples", "example", "samples", "sample", "demo", "demos"} & set(parts):
        return "example"
    return "production"


def iter_call_sites(mod: ModuleIR, module_name: str) -> Iterator[CallSite]:
    """All call expressions with their enclosing region, deterministic order."""
    in_tests = is_test_path(mod.path)

    def walk(
        stmts: tuple[Stmt, ...],
        funcs: tuple[FuncIR, ...],
        owner: ClassIR | None,
        under_main: bool,
    ) -> Iterator[CallSite]:
        for s in stmts:
            for e in _expr_subtree(stmt_exprs(s)):
                if isinstance(e, CallE):
                    yield CallSite(
                        call=e,
                        module=module_name,
                        path=mod.path,
                        funcs=funcs,
                        owner=owner,
                        under_main=under_main,
                        in_tests=in_tests,
                    )
            if isinstance(s, FuncDefS):
                yield from walk(s.func.body, (*funcs, s.func), owner, under_main)
            elif isinstance(s, ClassDefS):
                for a in s.cls.body_assigns:
                    for e in _expr_subtree((a.value,)):
                        if isinstance(e, CallE):
                            yield CallSite(
                                call=e,
                                module=module_name,
                                path=mod.path,
                                funcs=funcs,
                                owner=s.cls,
                                under_main=under_main,
                                in_tests=in_tests,
                            )
                for m in s.cls.methods:
                    yield from walk(
                        m.body, (*funcs, m), s.cls if not funcs else owner, under_main
                    )
            else:
                child_main = under_main or (not funcs and _is_main_guard(s))
                yield from walk(stmt_child_stmts(s), funcs, owner, child_main)

    yield from walk(mod.body, (), None, False)


class SinkEngine:
    """Classifies every call site in the analysis set (SPEC §6.5 a-c)."""

    def __init__(
        self,
        resolver: Resolver,
        org_pack: OrgPack,
        repo_root: Path | None = None,
    ) -> None:
        self.resolver = resolver
        self.org_pack = org_pack
        self.repo_root = repo_root
        self.sdk_registry = load_sdk_registry()
        self.hosts = load_host_registry(org_pack)
        self._root_index: dict[str, SdkEntry] = {}
        for entry in self.sdk_registry:
            for root in entry.roots:
                self._root_index[root] = entry

    # -- public -------------------------------------------------------------

    def scan_all(self, analyze_only: frozenset[str] | None = None) -> SinkScanResult:
        """Classify every call site as a sink or not. ``analyze_only`` (SPEC-8)
        limits emission to the given modules for an incremental scan; resolution
        of any site still sees the whole program. ``None`` scans everything."""
        result = SinkScanResult()
        for module_name in sorted(self.resolver.graph.by_name):
            if analyze_only is not None and module_name not in analyze_only:
                continue
            mod = self.resolver.graph.by_name[module_name]
            for site in iter_call_sites(mod, module_name):
                self.classify_site(site, result)
        return result

    def classify_site(self, site: CallSite, result: SinkScanResult) -> Sink | None:
        scope = self._scope_for(site)
        base, path = self.resolver.chain_root(site.call.callee, site.module, scope)
        sink = self._match_sdk(site, scope, base, path)
        if sink is None:
            sink = self._match_http(site, scope, base, path, result)
        if sink is not None:
            result.sinks.append(sink)
        return sink

    def scope_for_site(self, site: CallSite) -> Scope | None:
        return self._scope_for(site)

    # -- helpers ------------------------------------------------------------

    def _scope_for(self, site: CallSite) -> Scope | None:
        if not site.funcs:
            return None
        # SPEC-7 Z3: a sink in a class method resolves ``this``/``self`` against
        # a def-site instance of its own class, so constructor-assigned defaults
        # resolve; ctor arguments stay honestly unbound. (Supersedes the P2
        # "self unbound at definition site" trim.)
        self_instance = None
        if site.owner is not None:
            self_instance = ClassInstance(class_fq=f"{site.module}.{site.owner.name}")
        return self.resolver.function_scope(
            site.module, site.funcs, site.call.span, self_instance=self_instance
        )

    def _match_sdk(
        self,
        site: CallSite,
        scope: Scope | None,
        base: ValueSet,
        path: tuple[str, ...],
    ) -> Sink | None:
        for v in sorted(base, key=repr):
            candidates: list[tuple[str, tuple[str, ...], ClassInstance | None]] = []
            if isinstance(v, ClassInstance):
                candidates.append((v.class_fq, path, v))
            elif isinstance(v, PackageRef | ModuleRef):
                parts = v.name.split(".")
                for k in range(len(parts), 0, -1):
                    root = ".".join(parts[:k])
                    chain = (*parts[k:], *path)
                    candidates.append((root, chain, None))
            for root, chain, instance in candidates:
                entry = self._root_index.get(root)
                if entry is None:
                    continue
                chain_s = ".".join(chain)
                if chain_s in entry.chains:
                    return self._build_sdk_sink(site, scope, entry, chain_s, instance)
        return None

    def _build_sdk_sink(
        self,
        site: CallSite,
        scope: Scope | None,
        entry: SdkEntry,
        chain: str,
        instance: ClassInstance | None,
    ) -> Sink:
        call = site.call
        task: Task = entry.task_by_chain.get(chain, "chat")
        model_expr = self._named_arg(call, entry.model_arg) if entry.model_arg else None
        model_vals = (
            self.resolver.resolve(model_expr, site.module, scope) if model_expr else None
        )
        if model_vals is None and instance is not None and (
            entry.model_ctor_arg is not None or entry.model_ctor_kwargs
        ):
            for k in entry.model_ctor_kwargs:
                model_vals = ctor_kwarg(instance, k)
                if model_vals:
                    break
            if model_vals is None and entry.model_ctor_arg is not None:
                model_vals = instance.ctor_args.get("", position=entry.model_ctor_arg)
        if model_vals is None and entry.model_arg:
            # SPEC-7 sweep fix: model inside a ``**params`` spread.
            spread = self._payload_from_kwargs(site, scope)
            if spread is not None:
                model_vals = spread.get(entry.model_arg)
        attribution = attribute_model(model_expr, model_vals)

        endpoint: JsonRepr = None
        if instance is not None:
            for k in entry.ctor_endpoint_kwargs:
                vals = ctor_kwarg(instance, k)
                if vals:
                    endpoint = to_repr(vals)
                    break
        if endpoint is None:
            for k in entry.call_endpoint_kwargs:
                e = self._kwarg(call, k)
                if e is not None:
                    endpoint = to_repr(self.resolver.resolve(e, site.module, scope))
                    break

        prompt = self._sdk_prompt(site, scope, entry)
        model_fact = self._model_fact(
            site,
            attribution,
            endpoint=endpoint,
            deployment=None,
            api_style=entry.api_style,
            provider=entry.provider,
            task=task,
            confidence="high",
        )
        return Sink(
            site=site,
            kind="sdk",
            api_style=entry.api_style,
            task=task,
            confidence="high",
            score=None,
            signals=(f"sdk:{entry.provider}:{chain}",),
            model=model_fact,
            prompt=prompt,
            endpoint=endpoint,
            payload=None,
            credential_headers=None,
        )

    def _sdk_prompt(
        self, site: CallSite, scope: Scope | None, entry: SdkEntry
    ) -> PromptDefF | None:
        call = site.call
        if entry.system_arg:
            e = self._named_arg(call, entry.system_arg)
            if e is not None:
                vals = self.resolver.resolve(e, site.module, scope)
                return self.build_prompt_fact(vals, site, method="sink:system_arg")
        if entry.messages_arg:
            e = self._named_arg(call, entry.messages_arg)
            if e is not None:
                vals = self.resolver.resolve(e, site.module, scope)
                return self._prompt_from_messages(vals, site)
        return None

    def _match_http(
        self,
        site: CallSite,
        scope: Scope | None,
        base: ValueSet,
        path: tuple[str, ...],
        result: SinkScanResult,
    ) -> Sink | None:
        # SPEC-4 W3: TS/JS HTTP clients (fetch/axios/got) carry the payload in
        # an options object, not a direct kwarg — extracted specially. Falls
        # back to the language-neutral module/instance-root path (requests etc.).
        chain_hint: str | None = None
        extracted = self._ts_http_inputs(site, scope)
        if extracted is None:
            chained = self._chain_shape_inputs(site, scope)
            if chained is not None:
                extracted, chain_hint = chained
        if extracted is None:
            http = self._http_shape_of(base, path)
            if http is None:
                return None
            tail = http
            call = site.call
            url_expr = self._url_expr(call, tail)
            url_vals = (
                self.resolver.resolve(url_expr, site.module, scope)
                if url_expr is not None
                else None
            )
            payload_expr = self._payload_expr(call)
            payload_vals = (
                self.resolver.resolve(payload_expr, site.module, scope)
                if payload_expr is not None
                else None
            )
            payload = _first_of(payload_vals, DictVal)
            payload_unresolved = payload is None and payload_vals is not None
            headers = self._headers(call, site, scope)
            stream_hint = tail == "stream"
        else:
            url_vals, payload, payload_unresolved, headers, stream_hint = extracted

        url_text = _url_text(url_vals)
        response_fields = self._response_fields(site)

        inputs = ShapeInputs(
            url_text=url_text,
            payload=payload,
            payload_unresolved=payload_unresolved,
            headers=headers,
            response_fields=response_fields,
            stream_hint=stream_hint,
            chain_hint=chain_hint,
        )
        shape = score_shape(inputs)
        host = host_of(url_vals)
        host_known = self.hosts.is_known(host)

        if shape.is_sink:
            kind: SinkKind = "shape"
            confidence = shape.confidence()
            signals = shape.signals + ((f"host:{host}",) if host_known else ())
        elif host_known:
            kind = "host"
            confidence = "high"
            signals = (f"host:{host}", *shape.signals)
        elif shape.is_suspect:
            result.suspected.append((site, shape.score))
            return None
        else:
            return None

        return self._build_http_sink(
            site,
            scope,
            kind,
            shape,
            confidence,
            signals,
            url_vals,
            url_text,
            payload,
            headers,
            chain_hint=chain_hint,
        )

    def _build_http_sink(
        self,
        site: CallSite,
        scope: Scope | None,
        kind: SinkKind,
        shape: ShapeScore,
        confidence: Confidence,
        signals: tuple[str, ...],
        url_vals: ValueSet | None,
        url_text: str,
        payload: DictVal | None,
        headers: DictVal | None,
        chain_hint: str | None = None,
    ) -> Sink:
        deployment = deployment_from_url(url_text)
        model_expr = self._payload_model_expr(site.call)
        model_vals = payload.get("model") if payload is not None else None
        attribution = attribute_model(model_expr, model_vals)
        if not attribution.resolved and deployment is not None:
            ext = azure_external_model(deployment)
            attribution = ModelAttribution(
                model=to_repr(frozenset({ext})), method=RUNG_EXTERNAL, resolved=True
            )
        host = host_of(url_vals)
        provider = self.hosts.provider_for(host)
        endpoint: JsonRepr = to_repr(url_vals) if url_vals is not None else None
        task: Task = _shape_task(chain_hint, url_text)
        api_style = shape.api_style if shape.api_style != "unknown" else "openai"

        prompt: PromptDefF | None = None
        if payload is not None:
            system_vals = payload.get("system")
            if system_vals is not None:
                prompt = self.build_prompt_fact(system_vals, site, method="sink:system_key")
            else:
                messages_vals = payload.get("messages")
                if messages_vals is not None:
                    prompt = self._prompt_from_messages(messages_vals, site)

        model_fact = self._model_fact(
            site,
            attribution,
            endpoint=endpoint,
            deployment=deployment,
            api_style=api_style,
            provider=provider,
            task=task,
            confidence=confidence,
        )
        return Sink(
            site=site,
            kind=kind,
            api_style=api_style,
            task=task,
            confidence=confidence,
            score=shape.score,
            signals=signals,
            model=model_fact,
            prompt=prompt,
            endpoint=endpoint,
            payload=payload,
            credential_headers=headers,
        )

    def _ts_http_inputs(
        self, site: CallSite, scope: Scope | None
    ) -> tuple[ValueSet | None, DictVal | None, bool, DictVal | None, bool] | None:
        """Extract (url_vals, payload, payload_unresolved, headers, stream_hint)
        from a fetch/axios/got call, or None when the callee isn't one.

        The callee is matched syntactically (fetch is a bare global; axios etc.
        are commonly untyped) — the endpoint-agnostic scorer then runs on the
        extracted url/payload exactly as for a Python HTTP sink."""
        call = site.call
        callee_name = dotted_name(call.callee)
        if not callee_name:
            return None

        def as_dict(expr: Expr | None) -> tuple[DictVal | None, bool]:
            if expr is None:
                return None, False
            vals = self.resolver.resolve(expr, site.module, scope)
            dv = _first_of(vals, DictVal)
            return dv, dv is None and bool(vals)

        def dict_member(dv: DictVal | None, key: str) -> DictVal | None:
            if dv is None:
                return None
            vals = dv.get(key)
            return _first_of(vals, DictVal) if vals is not None else None

        stream_hint = False

        if callee_name in _TS_FETCH_CALLEES:
            url_vals = (
                self.resolver.resolve(call.args[0], site.module, scope) if call.args else None
            )
            opts, _ = as_dict(call.args[1] if len(call.args) > 1 else None)
            body_vals = opts.get("body") if opts is not None else None
            payload = _first_of(body_vals, DictVal) if body_vals is not None else None
            payload_unresolved = payload is None and bool(body_vals)
            return url_vals, payload, payload_unresolved, dict_member(opts, "headers"), stream_hint

        root = callee_name.split(".")[0]
        method = callee_name.split(".")[-1] if "." in callee_name else ""
        if root in _TS_CLIENT_ROOTS and (
            method in _TS_CLIENT_METHODS or method in _TS_STREAM_METHODS or callee_name == root
        ):
            stream_hint = method in _TS_STREAM_METHODS
            if callee_name == root or method == "request":
                # axios(config) / axios.request(config): single config object.
                cfg, _ = as_dict(call.args[0] if call.args else None)
                url_vals = self._config_url(cfg, site, scope)
                data_vals = cfg.get("data") if cfg is not None else None
                payload = _first_of(data_vals, DictVal) if data_vals is not None else None
                return (
                    url_vals,
                    payload,
                    payload is None and bool(data_vals),
                    dict_member(cfg, "headers"),
                    stream_hint,
                )
            # axios.post(url, data, config)
            url_vals = (
                self.resolver.resolve(call.args[0], site.module, scope) if call.args else None
            )
            payload, payload_unresolved = as_dict(call.args[1] if len(call.args) > 1 else None)
            cfg, _ = as_dict(call.args[2] if len(call.args) > 2 else None)
            return url_vals, payload, payload_unresolved, dict_member(cfg, "headers"), stream_hint

        return None

    def _chain_shape_inputs(
        self, site: CallSite, scope: Scope | None
    ) -> (
        tuple[tuple[ValueSet | None, DictVal | None, bool, DictVal | None, bool], str]
        | None
    ):
        """A `<opaque>.chat.completions.create({model, messages})`-style call:
        matched by chain suffix, with the options object as the payload. The
        options object literally holds `model`/`messages`, so it IS the request
        shape the scorer reads — no URL, endpoint stays null. Returns the
        extracted inputs plus the matched suffix (SPEC-5 §3 chain hint)."""
        callee_name = dotted_name(site.call.callee)
        if not callee_name:
            return None
        suffix = next(
            (s for s in _CHAIN_SHAPE_SUFFIXES if callee_name.endswith(s)), None
        )
        arg = site.call.args[0] if site.call.args else None
        if suffix is None:
            last = callee_name.rsplit(".", 1)[-1]
            if "." in callee_name and last in _INVOKE_SUFFIXES and arg is not None:
                vals = self.resolver.resolve(arg, site.module, scope)
                named_messages = (
                    isinstance(arg, NameE) and arg.name in _MESSAGES_ARG_NAMES
                )
                if _is_role_messages(vals) or named_messages:
                    msg_payload = DictVal(entries=(("messages", vals),))
                    return (
                        (None, msg_payload, False, None, last in ("stream", "astream")),
                        last,
                    )
            return None
        if arg is None:
            # kwargs form (Python `create(model=..., messages=...)`) — synthesise.
            payload = self._payload_from_kwargs(site, scope)
        else:
            payload_vals = self.resolver.resolve(arg, site.module, scope)
            payload = _first_of(payload_vals, DictVal)
        if payload is None or payload.get("model") is None:
            return None
        # ``file`` is the audio-transcription analog of ``input`` (STT payloads
        # carry {model, file}); only reached once a known suffix has matched.
        has_content = any(
            payload.get(k) is not None for k in ("messages", "prompt", "input", "file")
        )
        if not has_content:
            return None
        stream_hint = callee_name.endswith(("stream",))
        return (None, payload, False, None, stream_hint), suffix

    def _payload_from_kwargs(self, site: CallSite, scope: Scope | None) -> DictVal | None:
        entries: list[tuple[str, ValueSet]] = []
        open_ = False
        for k in site.call.kwargs:
            if k.name is not None:
                entries.append((k.name, self.resolver.resolve(k.value, site.module, scope)))
            else:
                # SPEC-7 sweep fix: ``create(**params)`` — a spread whose value
                # resolves to a dict contributes its entries (the swarm idiom);
                # anything else opens the payload honestly.
                inner = _first_of(
                    self.resolver.resolve(k.value, site.module, scope), DictVal
                )
                if inner is not None:
                    entries.extend(inner.entries)
                    open_ = open_ or inner.open
                else:
                    open_ = True
        if not entries:
            return None
        return DictVal(entries=tuple(entries), open=open_)

    def _config_url(
        self, cfg: DictVal | None, site: CallSite, scope: Scope | None
    ) -> ValueSet | None:
        if cfg is None:
            return None
        base = cfg.get("baseURL") or cfg.get("baseUrl")
        url = cfg.get("url")
        if base is not None and url is not None:
            b, u = _url_text(base), _url_text(url)
            if b and u:
                return frozenset({Str(s=b.rstrip("/") + "/" + u.lstrip("/"))})
        return url if url is not None else base

    def _http_shape_of(self, base: ValueSet, path: tuple[str, ...]) -> str | None:
        """The HTTP method tail when this call is a scoreable HTTP sink."""
        for v in sorted(base, key=repr):
            root: str | None = None
            if isinstance(v, ClassInstance):
                root = v.class_fq
                full = path
                if root in _HTTP_INSTANCE_ROOTS and full and full[-1] in (
                    "post",
                    "request",
                    "stream",
                ):
                    return full[-1]
            elif isinstance(v, PackageRef | ModuleRef):
                fq = v.name
                parts = tuple(fq.split("."))
                full = (*parts, *path)
                for mod_root in _HTTP_MODULE_ROOTS:
                    mr = tuple(mod_root.split("."))
                    if (
                        len(full) > len(mr)
                        and full[: len(mr)] == mr
                        and full[-1] in _HTTP_MODULE_SINK_TAILS
                    ):
                        return full[-1]
        return None

    # -- extraction helpers --------------------------------------------------

    def _kwarg(self, call: CallE, name: str) -> Expr | None:
        for k in call.kwargs:
            if k.name == name:
                return k.value
        return None

    def _named_arg(self, call: CallE, name: str) -> Expr | None:
        """A named argument, Python-kwarg *or* JS single-options-object member:
        ``create(model=x)`` and ``create({ model: x })`` both yield ``x``."""
        kw = self._kwarg(call, name)
        if kw is not None:
            return kw
        if call.args and isinstance(call.args[0], DictE):
            for entry in call.args[0].entries:
                if isinstance(entry.key, StrE) and entry.key.value == name:
                    return entry.value
        return None

    def _url_expr(self, call: CallE, tail: str) -> Expr | None:
        url_kw = self._kwarg(call, "url")
        if url_kw is not None:
            return url_kw
        pos = 1 if tail in ("request", "stream") else 0
        if pos < len(call.args):
            return call.args[pos]
        return None

    def url_expr(self, call: CallE) -> Expr | None:
        """Best-effort URL argument of an HTTP-style call (public helper)."""
        return self._url_expr(call, "post")

    def _payload_expr(self, call: CallE) -> Expr | None:
        for name in ("json", "data", "content", "body"):
            e = self._kwarg(call, name)
            if e is not None:
                return e
        return None

    def named_arg(self, call: CallE, name: str) -> Expr | None:
        """Public: a named argument — Python kwarg or JS options-object member
        (SPEC-7 Z1 caller-binding reads payload entries through this)."""
        return self._named_arg(call, name)

    def model_expr_of(self, call: CallE) -> Expr | None:
        """The model expression of a call: ``model=`` kwarg, JS options-object
        member (``create({ model: x })``), or HTTP payload key."""
        named = self._named_arg(call, "model")
        if named is not None:
            return named
        return self._payload_model_expr(call)

    def _payload_model_expr(self, call: CallE) -> Expr | None:
        payload = self._payload_expr(call)
        if isinstance(payload, DictE):
            for entry in payload.entries:
                if isinstance(entry.key, StrE) and entry.key.value == "model":
                    return entry.value
        return None

    def _headers(self, call: CallE, site: CallSite, scope: Scope | None) -> DictVal | None:
        e = self._kwarg(call, "headers")
        if e is None:
            return None
        return _first_of(self.resolver.resolve(e, site.module, scope), DictVal)

    def _response_fields(self, site: CallSite) -> frozenset[str]:
        """Def-use: names derived from the sink call, then response-shaped
        accesses on them (SPEC §6.5b response signal)."""
        region: tuple[Stmt, ...]
        if site.funcs:
            region = site.funcs[-1].body
        else:
            mod = self.resolver.graph.by_name.get(site.module)
            region = mod.body if mod is not None else ()
        derived: set[str] = set()
        for stmt in iter_stmts(region):
            if not isinstance(stmt, AssignS):
                continue
            rhs_root = _root_name(stmt.value)
            contains_call = any(e is site.call for e in _expr_subtree((stmt.value,)))
            if contains_call or (rhs_root is not None and rhs_root in derived):
                for t in stmt.targets:
                    if isinstance(t, NameE):
                        derived.add(t.name)
        if not derived:
            derived = {"<inline>"}

        fields: set[str] = set()
        for e in _iter_region_exprs(region):
            root = _root_name(e)
            direct_call = any(x is site.call for x in _expr_subtree((e,)))
            if root not in derived and not direct_call:
                continue
            if isinstance(e, SubscriptE) and isinstance(e.base, SubscriptE | AttrE):
                inner = e.base
                key = (
                    inner.index.value
                    if isinstance(inner, SubscriptE) and isinstance(inner.index, StrE)
                    else inner.attr
                    if isinstance(inner, AttrE)
                    else None
                )
                if key in _RESPONSE_KEY_FIELDS:
                    fields.add(_RESPONSE_KEY_FIELDS[key])
            if (
                isinstance(e, SubscriptE)
                and isinstance(e.index, StrE)
                and e.index.value in _RESPONSE_ATTR_FIELDS
            ):
                fields.add(e.index.value)
            if isinstance(e, AttrE) and e.attr in _RESPONSE_ATTR_FIELDS:
                fields.add(e.attr)
        return frozenset(fields)

    # -- fact builders -------------------------------------------------------

    def _model_fact(
        self,
        site: CallSite,
        attribution: ModelAttribution,
        endpoint: JsonRepr,
        deployment: str | None,
        api_style: ApiStyle,
        provider: str | None,
        task: Task,
        confidence: Confidence,
    ) -> ModelRefF:
        return ModelRefF(
            id=stable_id("model", api_style, attribution.model, endpoint),
            evidence=(str(site.call.span),),
            confidence=confidence,
            method=attribution.method,
            source_files=(site.path,),
            provider=provider,
            model=attribution.model,
            endpoint=endpoint,
            deployment=deployment,
            api_style=api_style,
            task=task,
        )

    def build_prompt_fact(
        self, vals: ValueSet, site: CallSite, method: str
    ) -> PromptDefF | None:
        evidence = (str(site.call.span),)
        source = (site.path,)
        if not vals:
            return None
        forced_dynamic = False
        if len(vals) > 1:
            # SPEC-7 Z2: a union (branch-dependent prompt, `prompt += ...`).
            # Pick the richest prompt-shaped member deterministically and mark
            # the prompt dynamic — one honest variant beats a silent None.
            candidates = [c for c in vals if isinstance(c, Str | Template | Symbolic)]
            if not candidates:
                return None
            vals = frozenset(
                {max(candidates, key=lambda c: (len(_prompt_text(c)), repr(c)))}
            )
            forced_dynamic = True
        if len(vals) == 1:
            v = next(iter(vals))
            if forced_dynamic and isinstance(v, Str):
                digest = hashlib.sha256(v.s.encode("utf-8")).hexdigest()[:12]
                return PromptDefF(
                    id=f"prompt:{digest}",
                    evidence=evidence,
                    confidence="medium",
                    method=method,
                    source_files=source,
                    content=v.s,
                    dynamic=True,
                    origin="literal",
                    content_hash=digest,
                )
            if isinstance(v, Str):
                digest = hashlib.sha256(v.s.encode("utf-8")).hexdigest()[:12]
                return PromptDefF(
                    id=f"prompt:{digest}",
                    evidence=evidence,
                    confidence="high",
                    method=method,
                    source_files=source,
                    content=v.s,
                    dynamic=False,
                    origin="literal",
                    content_hash=digest,
                )
            if isinstance(v, Symbolic) and v.kind == "config" and v.key.startswith("file:"):
                rel = v.key[len("file:") :]
                content: JsonRepr = to_repr(vals)
                file_digest: str | None = None
                if self.repo_root is not None and (self.repo_root / rel).is_file():
                    text = (self.repo_root / rel).read_text(encoding="utf-8", errors="replace")
                    file_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
                    content = text
                return PromptDefF(
                    id=f"prompt:{file_digest or stable_id('p', v.key)[2:]}",
                    evidence=evidence,
                    confidence="high" if file_digest else "medium",
                    method=method,
                    source_files=source,
                    content=content,
                    dynamic=False,
                    origin="file",
                    file_ref=rel,
                    content_hash=file_digest,
                )
            if isinstance(v, Symbolic):
                return PromptDefF(
                    id=stable_id("prompt", v.kind, v.key),
                    evidence=evidence,
                    confidence="medium",
                    method=method,
                    source_files=source,
                    content=to_repr(vals),
                    dynamic=True,
                    origin="config",
                )
            if isinstance(v, Template):
                rendered = v.render()
                digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:12]
                return PromptDefF(
                    id=f"prompt:{digest}",
                    evidence=evidence,
                    confidence="medium",
                    method=method,
                    source_files=source,
                    content={"template": rendered, "dynamic": True},
                    dynamic=True,
                    origin="literal",
                    content_hash=digest,
                )
            if isinstance(v, Top):
                return None
        return None

    def prompt_from_messages(self, vals: ValueSet, site: CallSite) -> PromptDefF | None:
        """Public alias: first role=system message → PromptDef."""
        return self._prompt_from_messages(vals, site)

    def _prompt_from_messages(self, vals: ValueSet, site: CallSite) -> PromptDefF | None:
        lv = _first_of(vals, ListVal)
        if lv is None:
            return None
        for elem in lv.elems:
            dv = _first_of(elem, DictVal)
            if dv is None:
                continue
            role_vals = dv.get("role")
            if role_vals is not None and Str(s="system") in role_vals:
                content_vals = dv.get("content")
                if content_vals is not None:
                    fact = self.build_prompt_fact(
                        content_vals, site, method="sink:first_system_message"
                    )
                    if fact is not None and lv.open and not fact.dynamic:
                        return fact
                    return fact
        return None


def ctor_kwarg(instance: ClassInstance, key: str) -> ValueSet | None:
    """Constructor kwarg lookup, Python-kwarg *or* JS options-object member:
    ``OpenAI(base_url=x)`` and ``new OpenAI({ baseURL: x })`` both resolve. Lives
    in the sinks layer so the framework engine (which imports sinks) can share it
    instead of keeping a byte-identical copy."""
    vals = instance.ctor_args.get(key)
    if vals:
        return vals
    first = instance.ctor_args.get("", position=0)
    if first is not None:
        for v in first:
            if isinstance(v, DictVal):
                member = v.get(key)
                if member:
                    return member
    return None


def _is_role_messages(vals: ValueSet) -> bool:
    """First-argument shape check for the invoke family: a list with at least
    one role-tagged dict member."""
    lv = _first_of(vals, ListVal)
    if lv is None:
        return False
    return any(
        (dv := _first_of(elem, DictVal)) is not None and dv.get("role") is not None
        for elem in lv.elems
    )


def _prompt_text(v: object) -> str:
    """Comparable text length of a prompt-shaped value (SPEC-7 Z2 selection)."""
    if isinstance(v, Str):
        return v.s
    if isinstance(v, Template):
        return v.render()
    return ""


def _first_of[T](vals: ValueSet | None, kind: type[T]) -> T | None:
    """Deterministic best-member selection: SPEC-5 made unions the common case
    (env fallbacks, ternaries), so extraction must tolerate them — the first
    matching member in sorted order, never a silent bail (SPEC-7 Z2)."""
    if vals is None:
        return None
    for v in sorted(vals, key=repr):
        if isinstance(v, kind):
            return v
    return None


def _url_text(vals: ValueSet | None) -> str:
    if vals is None:
        return ""
    for v in vals:
        if isinstance(v, Str):
            return v.s
        if isinstance(v, Template):
            return v.render()
    return ""


def _root_name(expr: Expr) -> str | None:
    node = expr
    while True:
        if isinstance(node, AttrE | SubscriptE):
            node = node.base
        elif isinstance(node, CallE):
            node = node.callee
        elif isinstance(node, NameE):
            return node.name
        else:
            return None


def _iter_region_exprs(region: tuple[Stmt, ...]) -> Iterator[Expr]:
    yield from iter_exprs(region, into_defs=False)
