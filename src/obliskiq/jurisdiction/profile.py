"""Jurisdiction profiles.

``config/owner.yaml: jurisdiction`` selects ``config/jurisdictions/<id>.yaml``: the rules of
the owner's tax residence as data. A profile names the policy it uses for each kind (what
may be bought, how gains are taxed) under ``policies``; the policies themselves are code,
resolved through :mod:`obliskiq.jurisdiction.registry`. Changing residence is a profile
switch plus, at most, a new policy module.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Annotated, Any

from pydantic import AfterValidator, BeforeValidator, Field, PlainSerializer, ValidationError

from ..config.base import FrozenModel, Identifier
from ..config.loader import (
    ConfigInvalid,
    ConfigProblem,
    display_path,
    explain_validation_error,
    flatten,
    read_yaml_mapping,
)
from ..domain.base import NonEmptyText
from .eligibility import EligibilityProfile
from .tax_profile import TaxProfile

JURISDICTIONS_DIR = "jurisdictions"
PROFILE_SUFFIX = ".yaml"


class PolicyIds(Mapping[str, str]):
    """A profile's policy ids by kind: read-only, and picklable for worker processes."""

    __slots__ = ("_ids",)

    def __init__(self, ids: Mapping[str, str]) -> None:
        self._ids = dict(ids)

    def __getitem__(self, kind: str) -> str:
        return self._ids[kind]

    def __iter__(self) -> Iterator[str]:
        return iter(self._ids)

    def __len__(self) -> int:
        return len(self._ids)

    def __repr__(self) -> str:
        return f"PolicyIds({self._ids!r})"


def _none_is_empty(value: Any) -> Any:
    """A bare ``policies:`` header declares no policies."""
    return {} if value is None else value


class JurisdictionProfile(FrozenModel):
    """One jurisdiction's rules as data.

    ``policies`` maps a policy kind to the id of the registered policy the profile uses for
    it, read-only once loaded. A profile that declares none has nothing to resolve; asking
    it for a policy fails. ``tax`` holds the values the tax policy applies; a profile without
    it cannot resolve a tax policy; ``eligibility`` holds the values the eligibility policy
    applies.
    """

    id: Identifier
    name: NonEmptyText
    policies: Annotated[
        Mapping[Identifier, Identifier],
        BeforeValidator(_none_is_empty),
        AfterValidator(PolicyIds),
        PlainSerializer(dict, return_type=dict[str, str]),
    ] = Field(default_factory=dict, validate_default=True)
    tax: TaxProfile | None = None
    eligibility: EligibilityProfile | None = None


def available_profiles(config_dir: Path) -> tuple[str, ...]:
    """Ids of the profiles in ``config/jurisdictions/``."""
    directory = config_dir / JURISDICTIONS_DIR
    if not directory.is_dir():
        return ()
    return tuple(sorted(path.stem for path in directory.glob(f"*{PROFILE_SUFFIX}")))


def load_profile(config_dir: Path, jurisdiction_id: str) -> JurisdictionProfile:
    """Load and validate ``config/jurisdictions/<jurisdiction_id>.yaml``.

    Fails loudly when the file is missing, invalid, or names a different id than its file.
    """
    path = config_dir / JURISDICTIONS_DIR / f"{jurisdiction_id}{PROFILE_SUFFIX}"
    display = display_path(path, config_dir)
    if not path.is_file():
        available = ", ".join(available_profiles(config_dir)) or "none"
        raise ConfigInvalid(
            [
                ConfigProblem(
                    display,
                    "",
                    f"no profile for jurisdiction '{jurisdiction_id}' (available: {available})",
                )
            ]
        )
    data = read_yaml_mapping(path, display)
    sources = dict.fromkeys(flatten(data), display)
    try:
        profile = JurisdictionProfile.model_validate(data)
    except ValidationError as exc:
        raise ConfigInvalid(
            explain_validation_error(exc, JurisdictionProfile, sources, display)
        ) from None
    if profile.id != jurisdiction_id:
        raise ConfigInvalid(
            [
                ConfigProblem(
                    display,
                    "id",
                    f"is '{profile.id}' but the file is {path.name}; they must agree",
                )
            ]
        )
    return profile
