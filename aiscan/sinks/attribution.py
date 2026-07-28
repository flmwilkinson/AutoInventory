"""Model/endpoint attribution ladder (SPEC §6.5).

For every sink, the first rung yielding a non-Top value wins and the rung is
recorded in provenance. The foundation model behind an Azure deployment or a
gateway alias is *not in the code*: it is recorded as an external symbolic,
never guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatch
from urllib.parse import urlsplit

from aiscan.ir.nodes import Expr, StrE
from aiscan.ir.values import JsonRepr, Str, Symbolic, Template, Top, ValueSet, to_repr

_DEPLOYMENT_PATH = re.compile(r"/openai/deployments/([^/?#]+)/")

RUNG_LITERAL = "attribution:literal"
RUNG_CONSTANT = "attribution:constant"
RUNG_SYMBOLIC = "attribution:config_symbolic"
RUNG_WRAPPER = "attribution:wrapper_default"
RUNG_EXTERNAL = "attribution:external"
RUNG_UNRESOLVED = "attribution:unresolved"


@dataclass(frozen=True, slots=True)
class ModelAttribution:
    model: JsonRepr
    method: str
    resolved: bool  # False when the value is unresolved (Top)


def attribute_model(expr: Expr | None, vals: ValueSet | None) -> ModelAttribution:
    """Run the ladder over a model expression and its resolved values."""
    if vals is None or not vals:
        return ModelAttribution(
            model={"unresolved": "unbound"}, method=RUNG_UNRESOLVED, resolved=False
        )
    if isinstance(expr, StrE):
        return ModelAttribution(model=expr.value, method=RUNG_LITERAL, resolved=True)
    if len(vals) == 1:
        v = next(iter(vals))
        if isinstance(v, Str):
            return ModelAttribution(model=v.s, method=RUNG_CONSTANT, resolved=True)
        if isinstance(v, Symbolic):
            method = RUNG_WRAPPER if v.kind == "wrapper_default" else RUNG_SYMBOLIC
            if v.kind == "external":
                method = RUNG_EXTERNAL
            return ModelAttribution(model=to_repr(vals), method=method, resolved=True)
        if isinstance(v, Template):
            return ModelAttribution(model=to_repr(vals), method=RUNG_CONSTANT, resolved=True)
        if isinstance(v, Top):
            return ModelAttribution(model=to_repr(vals), method=RUNG_UNRESOLVED, resolved=False)
    repr_ = to_repr(vals)
    if isinstance(repr_, dict) and "unresolved" in repr_:
        return ModelAttribution(model=repr_, method=RUNG_UNRESOLVED, resolved=False)
    # Multi-value union (SPEC-5 §2.4): partial knowledge is knowledge. An
    # all-Top union collapses to a single unresolved marker; a union with any
    # known member is resolved, on the symbolic rung when config-shaped.
    if all(isinstance(v, Top) for v in vals):
        reason = sorted(v.reason for v in vals if isinstance(v, Top))[0]
        return ModelAttribution(
            model={"unresolved": reason}, method=RUNG_UNRESOLVED, resolved=False
        )
    if any(isinstance(v, Symbolic) for v in vals):
        return ModelAttribution(model=repr_, method=RUNG_SYMBOLIC, resolved=True)
    return ModelAttribution(model=repr_, method=RUNG_CONSTANT, resolved=True)


def deployment_from_url(url_text: str) -> str | None:
    """Azure deployment name from a ``.../openai/deployments/{name}/...`` path."""
    m = _DEPLOYMENT_PATH.search(url_text)
    return m.group(1) if m else None


def azure_external_model(deployment: str) -> Symbolic:
    return Symbolic(kind="external", key=f"azure:deployment:{deployment}")


def host_of(vals: ValueSet | None) -> str | None:
    """Host of a resolved URL. Template URLs match on literal prefix parts —
    the host is only accepted when it is provably complete (a '/' follows)."""
    if vals is None:
        return None
    for v in vals:
        if isinstance(v, Str):
            host = urlsplit(v.s).hostname
            if host:
                return host
        elif isinstance(v, Template):
            prefix = v.literal_prefix()
            if "://" in prefix:
                after = prefix.split("://", 1)[1]
                if "/" in after:
                    host = after.split("/", 1)[0].split("@")[-1].split(":")[0]
                    if host:
                        return host
    return None


class HostRegistry:
    """Provider hosts (exact + wildcard) plus per-org gateway hosts."""

    def __init__(
        self,
        hosts: dict[str, str],
        wildcards: dict[str, str],
        gateway_hosts: tuple[str, ...] = (),
    ) -> None:
        self.hosts = hosts
        self.wildcards = wildcards
        self.gateway_hosts = gateway_hosts

    def provider_for(self, host: str | None) -> str | None:
        if not host:
            return None
        if host in self.hosts:
            return self.hosts[host]
        for pattern, provider in sorted(self.wildcards.items()):
            if fnmatch(host, pattern):
                return provider
        if host in self.gateway_hosts:
            return "org-gateway"
        return None

    def is_known(self, host: str | None) -> bool:
        return self.provider_for(host) is not None
