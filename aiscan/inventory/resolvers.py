"""External-resolution interfaces (SPEC-3 §4.4) — typed stubs, nothing wired.

The record's [X] fields hold *refs* (``credential_ref``, ``cmdb_app_id``,
``effective_entitlement``). Resolving them against a client's IAM, config
store, or directory is a later phase; these Protocols define the seams and the
defaults honestly return :class:`Unresolved`.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from aiscan.ir.values import JsonRepr


class Unresolved(BaseModel):
    """The resolver could not (or does not) resolve the ref — never a guess."""

    model_config = ConfigDict(frozen=True)

    reason: str = "not_wired"


class Entitlement(BaseModel):
    """A resolved credential: what identity it is and what it may do."""

    model_config = ConfigDict(frozen=True)

    identity: str
    granted_scopes: tuple[str, ...] = ()
    source_system: str | None = None


class AccountableOwner(BaseModel):
    """A resolved owner candidate: a real person/team in the directory."""

    model_config = ConfigDict(frozen=True)

    name: str
    directory_id: str | None = None


class EntitlementResolver(Protocol):
    def resolve(self, credential_ref: JsonRepr) -> Entitlement | Unresolved: ...


class ConfigResolver(Protocol):
    """Dereferences a config/gateway alias (e.g. an Azure deployment name)."""

    def resolve(self, symbol: str, environment: str) -> str | Unresolved: ...


class OwnerResolver(Protocol):
    def resolve(self, candidate: str) -> AccountableOwner | Unresolved: ...


class NullEntitlementResolver:
    def resolve(self, credential_ref: JsonRepr) -> Entitlement | Unresolved:
        return Unresolved()


class NullConfigResolver:
    def resolve(self, symbol: str, environment: str) -> str | Unresolved:
        return Unresolved()


class NullOwnerResolver:
    def resolve(self, candidate: str) -> AccountableOwner | Unresolved:
        return Unresolved()
