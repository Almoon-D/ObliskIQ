"""The policy registry.

Each policy kind (eligibility, tax, …) has one :class:`PolicyRegistry` listing the policies
ObliskIQ ships for it, by id. A profile selects one id per kind; the registry resolves that
id when a policy is requested. A profile that declares no id for the kind, or declares an
id nobody registered, fails loudly — there is no fallback policy.

Registries are built from immutable entry tuples, so nothing is registered at import time
and there is no mutable module state.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable, Iterable, Mapping
from types import MappingProxyType
from typing import Protocol

from ..config.schema import check_identifier
from ..errors import PolicyError
from .profile import JURISDICTIONS_DIR, JurisdictionProfile

type PolicyFactory[P] = Callable[[JurisdictionProfile], P]


class PolicyKind(Protocol):
    """What :func:`check_declared_policies` needs from a registry of any policy type."""

    @property
    def kind(self) -> str: ...

    def ids(self) -> tuple[str, ...]: ...


class PolicyRegistry[P]:
    """The registered policies of one kind, by id."""

    def __init__(self, kind: str, entries: Iterable[tuple[str, PolicyFactory[P]]]) -> None:
        try:
            check_identifier(kind)
        except ValueError as exc:
            raise PolicyError(f"policy kind {kind!r} {exc}") from None
        factories: dict[str, PolicyFactory[P]] = {}
        for policy_id, factory in entries:
            try:
                check_identifier(policy_id)
            except ValueError as exc:
                raise PolicyError(f"{kind} policy id {policy_id!r} {exc}") from None
            if policy_id in factories:
                raise PolicyError(f"{kind} policy '{policy_id}' is registered twice")
            factories[policy_id] = factory
        self._kind = kind
        self._factories: Mapping[str, PolicyFactory[P]] = MappingProxyType(factories)

    @property
    def kind(self) -> str:
        return self._kind

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))

    def resolve(self, profile: JurisdictionProfile) -> P:
        """The policy ``profile`` selects for this kind."""
        policy_id = profile.policies.get(self._kind)
        where = f"{JURISDICTIONS_DIR}/{profile.id}.yaml"
        if policy_id is None:
            raise PolicyError(
                f"jurisdiction profile '{profile.id}' declares no {self._kind} policy; "
                f"set policies.{self._kind} in {where}"
            )
        factory = self._factories.get(policy_id)
        if factory is None:
            registered = ", ".join(self.ids()) or "none"
            raise PolicyError(
                f"{self._kind} policy '{policy_id}' selected by {where} is not registered "
                f"(registered: {registered})"
            )
        return factory(profile)


def check_declared_policies(profile: JurisdictionProfile, kinds: Iterable[PolicyKind]) -> None:
    """Fail loudly if ``profile`` declares a policy kind or id that is not registered."""
    known = {registry.kind: registry for registry in kinds}
    where = f"{JURISDICTIONS_DIR}/{profile.id}.yaml"
    problems: list[str] = []
    for kind, policy_id in sorted(profile.policies.items()):
        registry = known.get(kind)
        if registry is None:
            listed = ", ".join(sorted(known)) or "none"
            problems.append(f"policies.{kind}: no policy kind '{kind}' exists (known: {listed})")
        elif policy_id not in registry.ids():
            listed = ", ".join(registry.ids()) or "none"
            problems.append(
                f"policies.{kind}: '{policy_id}' is not a registered {kind} policy "
                f"(registered: {listed})"
            )
    if problems:
        raise PolicyError(f"{where}: " + "; ".join(problems))


#: The policy kinds ObliskIQ ships, each with its registered policies. A kind joins this
#: tuple together with its first policy.
SHIPPED_POLICY_KINDS: tuple[PolicyKind, ...] = ()
