"""Tool side-effect classification (SPEC §6.5, multi-label).

Classifies a function body by the calls it makes: reads, writes, external
sends, code execution, admin mutation. Also extracts the external target host
and the credential *reference* (never a value — secret literals were redacted
at IR time)."""

from __future__ import annotations

from dataclasses import dataclass

from aiscan.context import OrgPack
from aiscan.facts.models import SideEffect
from aiscan.ir.nodes import AttrE, CallE, FuncIR, NameE, Span, StrE
from aiscan.ir.values import (
    ClassInstance,
    DictVal,
    FuncRef,
    Hole,
    JsonRepr,
    PackageRef,
    Str,
    Symbolic,
    Template,
    ValueSet,
    to_repr,
)
from aiscan.ir.walk import dotted_name, iter_calls
from aiscan.resolve.engine import Resolver, Scope
from aiscan.sinks.attribution import host_of

_HTTP_MODULE_ROOTS = ("requests", "httpx", "aiohttp", "urllib.request")
_HTTP_INSTANCE_ROOTS = (
    "requests.Session",
    "httpx.Client",
    "httpx.AsyncClient",
    "aiohttp.ClientSession",
)
_HTTP_METHODS = ("get", "post", "put", "delete", "patch", "request", "stream", "urlopen")

_CODE_EXEC_ROOTS = ("subprocess", "docker", "kubernetes", "child_process")
_CODE_EXEC_FQS = ("os.system", "os.popen", "os.execv", "os.spawnl")

# SPEC-5 §4: TS/JS HTTP callees, matched syntactically like the sink engine's
# (fetch is a bare global; axios etc. are commonly untyped) — the SPEC-4 W5
# side-effect coverage for TS tool functions that was never actually wired.
_TS_FETCH_CALLEES = frozenset({"fetch", "globalThis.fetch", "window.fetch", "$fetch"})
_TS_CLIENT_ROOTS = frozenset({"axios", "got", "ky"})
_TS_CLIENT_METHODS = frozenset({"get", "post", "put", "patch", "delete", "request"})

# SPEC-5 §4: bounded transitive classification — a tool implementation that
# delegates to an internal helper (executeGenerateChart → sandbox-client
# generateChart → fetch) still classifies. Two hops, cycle-guarded.
_CLASSIFY_HOPS = 2
_EXTERNAL_SEND_ROOTS = ("smtplib", "kafka", "confluent_kafka", "slack_sdk", "pika")
_SEND_BOTO_SERVICES = ("ses", "sqs", "sns", "sesv2")
_ADMIN_BOTO_SERVICES = ("iam", "identitystore", "sso-admin")
_AUTH_HEADER_KEYS = ("authorization", "x-api-key", "api-key")


@dataclass(frozen=True, slots=True)
class SideEffectReport:
    effects: tuple[SideEffect, ...]
    external_target: JsonRepr
    credential_ref: JsonRepr
    http_verb: str | None  # verb of the external_send call, for declared_authorisation


def extract_credential(headers: DictVal | None) -> JsonRepr:
    """The auth *reference* from a headers dict: a symbolic (env/config) when
    one is present, else the (already-redacted) literal."""
    if headers is None:
        return None
    for key, vals in headers.entries:
        if key.lower() not in _AUTH_HEADER_KEYS:
            continue
        for v in vals:
            if isinstance(v, Symbolic):
                return to_repr(frozenset({v}))
            if isinstance(v, Template):
                for part in v.parts:
                    if isinstance(part, Hole) and ":" in part.expr:
                        kind = part.expr.partition(":")[0]
                        if kind in ("env", "config"):
                            return {"symbolic": part.expr}
                return to_repr(frozenset({v}))
            if isinstance(v, Str):
                return v.s
    return None


def classify_body(
    fn: FuncIR,
    module: str,
    resolver: Resolver,
    org_pack: OrgPack,
    llm_sink_spans: frozenset[Span] = frozenset(),
    _depth: int = 0,
    _visited: set[str] | None = None,
) -> SideEffectReport:
    """Classify one function body (SPEC §6.5 table)."""
    effects: set[SideEffect] = set()
    external_target: JsonRepr = None
    credential: JsonRepr = None
    http_verb: str | None = None
    visited = _visited if _visited is not None else set()

    scope = resolver.function_scope(module, (fn,), None)

    def record_http(
        verb: str, url_vals: ValueSet | None, headers: DictVal | None, call: CallE
    ) -> None:
        nonlocal external_target, credential, http_verb
        host = host_of(url_vals)
        sensitive = host is not None and host in org_pack.sensitive_hosts
        if verb == "GET" and not sensitive:
            effects.add("read")
            return
        effects.add("external_send")
        if external_target is None and host is not None:
            external_target = host
            http_verb = verb
            if headers is not None:
                credential = credential or extract_credential(headers)
            credential = credential or _headers_credential(call, resolver, module, scope)

    for call in iter_calls(fn.body, into_defs=True):
        if call.span in llm_sink_spans:
            continue
        effects |= _builtin_effects(call)
        ts_http = _ts_http_call(call, resolver, module, scope)
        if ts_http is not None:
            record_http(*ts_http, call)
            continue
        base, path = resolver.chain_root(call.callee, module, scope)
        if _depth < _CLASSIFY_HOPS and not path:
            # Bounded descent into internal helpers (SPEC-5 §4).
            for v in sorted(base, key=repr):
                if isinstance(v, FuncRef) and v.fq not in visited:
                    entry = resolver.lookup_def(v.fq)
                    if entry is None:
                        continue
                    visited.add(v.fq)
                    sub = classify_body(
                        entry[1],
                        entry[0],
                        resolver,
                        org_pack,
                        llm_sink_spans,
                        _depth + 1,
                        visited,
                    )
                    effects.update(sub.effects)
                    if external_target is None and sub.external_target is not None:
                        external_target = sub.external_target
                        http_verb = sub.http_verb
                        credential = credential or sub.credential_ref
        full = _full_chain(base, path)
        if full is None:
            continue
        root_fq, chain = full
        top_pkg = root_fq.split(".")[0]
        tail = (root_fq.split(".") + list(chain))[-1]

        if root_fq in _CODE_EXEC_FQS or top_pkg in _CODE_EXEC_ROOTS:
            effects.add("code_exec")
        elif top_pkg in _EXTERNAL_SEND_ROOTS:
            effects.add("external_send")
        elif root_fq.startswith("boto3.") and root_fq.endswith("-client"):
            service = root_fq[len("boto3.") : -len("-client")]
            if service in _ADMIN_BOTO_SERVICES:
                effects.add("admin_mutation")
            elif service in _SEND_BOTO_SERVICES:
                effects.add("external_send")
        elif _is_http_call(root_fq, chain):
            verb = _http_verb(tail, call, resolver, module, scope)
            url_vals = _http_url(tail, call, resolver, module, scope)
            record_http(verb, url_vals, None, call)

    return SideEffectReport(
        effects=tuple(sorted(effects)),
        external_target=external_target,
        credential_ref=credential,
        http_verb=http_verb,
    )


def _ts_http_call(
    call: CallE, resolver: Resolver, module: str, scope: Scope | None
) -> tuple[str, ValueSet | None, DictVal | None] | None:
    """(verb, url values, headers) for a fetch/axios/got/ky call, else None."""
    name = dotted_name(call.callee)
    if not name:
        return None
    if name in _TS_FETCH_CALLEES:
        url_vals = resolver.resolve(call.args[0], module, scope) if call.args else None
        verb = "GET"
        headers: DictVal | None = None
        if len(call.args) > 1:
            for v in resolver.resolve(call.args[1], module, scope):
                if not isinstance(v, DictVal):
                    continue
                method_vals = v.get("method")
                if method_vals is not None:
                    for mv in method_vals:
                        if isinstance(mv, Str):
                            verb = mv.s.upper()
                header_vals = v.get("headers")
                if header_vals is not None:
                    for hv in header_vals:
                        if isinstance(hv, DictVal):
                            headers = hv
        return verb, url_vals, headers
    root, _, method = name.partition(".")
    if root in _TS_CLIENT_ROOTS and method in _TS_CLIENT_METHODS:
        url_vals = resolver.resolve(call.args[0], module, scope) if call.args else None
        return ("REQUEST" if method == "request" else method.upper()), url_vals, None
    return None


def _full_chain(base: ValueSet, path: tuple[str, ...]) -> tuple[str, tuple[str, ...]] | None:
    for v in sorted(base, key=repr):
        if isinstance(v, PackageRef):
            return v.name, path
        if isinstance(v, ClassInstance):
            return v.class_fq, path
    return None


def _is_http_call(root_fq: str, chain: tuple[str, ...]) -> bool:
    parts = root_fq.split(".")
    full = tuple(parts) + chain
    if not full or full[-1] not in _HTTP_METHODS:
        return False
    for mod_root in _HTTP_MODULE_ROOTS:
        if root_fq == mod_root or root_fq.startswith(mod_root + "."):
            return True
    return root_fq in _HTTP_INSTANCE_ROOTS


def _http_verb(
    tail: str, call: CallE, resolver: Resolver, module: str, scope: Scope | None
) -> str:
    if tail in ("get", "post", "put", "delete", "patch"):
        return tail.upper()
    if tail in ("request", "stream") and call.args:
        first = resolver.resolve(call.args[0], module, scope)
        for v in first:
            if isinstance(v, Str):
                return v.s.upper()
        return "REQUEST"
    if tail == "urlopen":
        has_data = any(k.name == "data" for k in call.kwargs)
        return "POST" if has_data else "GET"
    return tail.upper() if tail else "CALL"


def _http_url(
    tail: str, call: CallE, resolver: Resolver, module: str, scope: Scope | None
) -> ValueSet | None:
    pos = 1 if tail in ("request", "stream") else 0
    for k in call.kwargs:
        if k.name == "url":
            return resolver.resolve(k.value, module, scope)
    if pos < len(call.args):
        return resolver.resolve(call.args[pos], module, scope)
    return None


def _headers_credential(
    call: CallE, resolver: Resolver, module: str, scope: Scope | None
) -> JsonRepr:
    for k in call.kwargs:
        if k.name == "headers":
            vals = resolver.resolve(k.value, module, scope)
            for v in vals:
                if isinstance(v, DictVal):
                    return extract_credential(v)
    return None


def _builtin_effects(call: CallE) -> set[SideEffect]:
    """open()/pathlib/exec-eval effects that need no chain resolution."""
    out: set[SideEffect] = set()
    callee = call.callee
    if isinstance(callee, NameE):
        if callee.name in ("exec", "eval"):
            out.add("code_exec")
        elif callee.name == "open":
            mode = ""
            if len(call.args) >= 2 and isinstance(call.args[1], StrE):
                mode = call.args[1].value
            for k in call.kwargs:
                if k.name == "mode" and isinstance(k.value, StrE):
                    mode = k.value.value
            if any(c in mode for c in "wa+x"):
                out.add("write")
            else:
                out.add("read")
    if isinstance(callee, AttrE):
        if callee.attr in ("write_text", "write_bytes"):
            out.add("write")
        elif callee.attr in ("read_text", "read_bytes"):
            out.add("read")
        elif callee.attr == "write" and isinstance(callee.base, NameE):
            out.add("write")
    return out
