"""Jurisdiction profiles and the policy registry: every failure branch fails loudly.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from obliskiq.config import ConfigInvalid
from obliskiq.errors import ConfigError, PolicyError
from obliskiq.jurisdiction import (
    SHIPPED_POLICY_KINDS,
    JurisdictionProfile,
    PolicyRegistry,
    check_declared_policies,
    load_profile,
)
from obliskiq.jurisdiction.profile import available_profiles


@dataclass(frozen=True)
class FakePolicy:
    """A stand-in policy: records which profile built it."""

    name: str
    profile_id: str


#: The configuration a factory is built with; the fake policies never read it.
SETTINGS: Any = object()


def _registry(kind: str = "eligibility") -> PolicyRegistry[FakePolicy]:
    return PolicyRegistry(
        kind,
        [
            ("strict", lambda profile, _settings: FakePolicy("strict", profile.id)),
            ("lenient", lambda profile, _settings: FakePolicy("lenient", profile.id)),
        ],
    )


def _profile(**policies: str) -> JurisdictionProfile:
    return JurisdictionProfile(id="es", name="Spain", policies=policies)


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


# ── profiles ──


def test_the_shipped_spanish_profile_loads(repo_root):
    profile = load_profile(repo_root / "config", "es")
    assert profile.id == "es"
    assert profile.name == "Spain"
    assert profile.policies == {"eligibility": "skeleton", "tax": "es_irpf"}
    assert profile.tax is not None


def test_available_profiles(config_dir, tmp_path):
    assert available_profiles(config_dir) == ("es",)
    assert available_profiles(tmp_path / "nowhere") == ()


def test_a_missing_profile_fails_and_lists_the_available_ones(config_dir):
    with pytest.raises(ConfigInvalid) as raised:
        load_profile(config_dir, "ch")
    assert str(raised.value.problems[0]) == (
        "config/jurisdictions/ch.yaml: no profile for jurisdiction 'ch' (available: es)"
    )


def test_a_profile_whose_id_disagrees_with_its_file_fails(config_dir):
    _write(config_dir / "jurisdictions" / "es.yaml", "id: pt\nname: Portugal\n")
    with pytest.raises(ConfigInvalid, match=r"is 'pt' but the file is es\.yaml"):
        load_profile(config_dir, "es")


def test_a_profile_with_an_unknown_key_fails(config_dir):
    _write(config_dir / "jurisdictions" / "es.yaml", "id: es\nname: Spain\ntax_rate: 0.19\n")
    with pytest.raises(ConfigInvalid) as raised:
        load_profile(config_dir, "es")
    assert str(raised.value.problems[0]) == (
        "config/jurisdictions/es.yaml: tax_rate: is not a known setting"
    )


def test_a_profile_without_a_name_fails(config_dir):
    _write(config_dir / "jurisdictions" / "es.yaml", "id: es\nname: '  '\n")
    with pytest.raises(ConfigInvalid, match="name: must not be empty"):
        load_profile(config_dir, "es")


def test_a_profile_is_frozen():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _profile().name = "Portugal"  # type: ignore[misc]


# ── the registry ──


def test_a_declared_policy_resolves_to_its_registered_factory():
    policy = _registry().resolve(_profile(eligibility="strict"), SETTINGS)
    assert policy == FakePolicy("strict", "es")


def test_a_profile_that_declares_no_policy_for_the_kind_fails_loudly():
    with pytest.raises(PolicyError, match="declares no eligibility policy; set policies"):
        _registry().resolve(_profile(), SETTINGS)


def test_an_unregistered_policy_id_fails_loudly_and_lists_the_registered_ones():
    with pytest.raises(
        PolicyError, match=r"'eu_priips' .* not registered \(registered: lenient, strict\)"
    ):
        _registry().resolve(_profile(eligibility="eu_priips"), SETTINGS)


def test_an_empty_registry_says_so():
    with pytest.raises(PolicyError, match=r"registered: none"):
        PolicyRegistry[FakePolicy]("tax", []).resolve(_profile(tax="es_irpf"), SETTINGS)


def test_a_policy_error_is_a_configuration_error():
    assert issubclass(PolicyError, ConfigError)


def test_a_duplicate_registration_is_refused():
    factory = lambda profile, _settings: FakePolicy("x", profile.id)  # noqa: E731
    with pytest.raises(PolicyError, match="registered twice"):
        PolicyRegistry("tax", [("es_irpf", factory), ("es_irpf", factory)])


@pytest.mark.parametrize(("kind", "policy_id"), [("Tax", "es_irpf"), ("tax", "ES-IRPF")])
def test_kinds_and_ids_must_be_identifiers(kind, policy_id):
    with pytest.raises(PolicyError, match="lower-case identifier"):
        PolicyRegistry(kind, [(policy_id, lambda profile, _settings: FakePolicy("x", profile.id))])


def test_the_registry_reports_its_kind_and_ids():
    registry = _registry("tax")
    assert registry.kind == "tax"
    assert registry.ids() == ("lenient", "strict")


def test_resolution_is_per_kind():
    profile = _profile(eligibility="strict", tax="lenient")
    assert _registry("eligibility").resolve(profile, SETTINGS).name == "strict"
    assert _registry("tax").resolve(profile, SETTINGS).name == "lenient"


# ── checking a profile's declarations up front ──


def test_a_profile_declaring_registered_policies_passes():
    check_declared_policies(_profile(eligibility="strict"), [_registry()])


def test_a_profile_declaring_nothing_passes_with_no_registries():
    check_declared_policies(_profile(), SHIPPED_POLICY_KINDS)


def test_an_unknown_kind_is_refused():
    with pytest.raises(PolicyError, match=r"no policy kind 'tax' exists \(known: eligibility\)"):
        check_declared_policies(_profile(tax="es_irpf"), [_registry()])


def test_an_unknown_kind_with_no_registries_is_refused():
    with pytest.raises(PolicyError, match=r"known: none"):
        check_declared_policies(_profile(tax="es_irpf"), ())


def test_an_unshipped_kind_is_refused_and_the_shipped_ones_listed():
    with pytest.raises(
        PolicyError, match=r"no policy kind 'pension' exists \(known: eligibility, tax\)"
    ):
        check_declared_policies(_profile(pension="es_plan"), SHIPPED_POLICY_KINDS)


def test_the_shipped_profile_declares_only_shipped_policies(repo_root):
    check_declared_policies(load_profile(repo_root / "config", "es"), SHIPPED_POLICY_KINDS)


def test_an_unregistered_id_is_refused_up_front():
    with pytest.raises(PolicyError, match=r"'eu_priips' is not a registered eligibility policy"):
        check_declared_policies(_profile(eligibility="eu_priips"), [_registry()])


def test_an_unregistered_id_in_an_empty_kind_is_refused_up_front():
    empty = PolicyRegistry[FakePolicy]("eligibility", [])
    with pytest.raises(PolicyError, match=r"registered: none"):
        check_declared_policies(_profile(eligibility="eu_priips"), [empty])


def test_policies_declared_in_yaml_are_read(config_dir):
    _write(
        config_dir / "jurisdictions" / "es.yaml",
        "id: es\nname: Spain\npolicies:\n  eligibility: strict\n",
    )
    profile = load_profile(config_dir, "es")
    assert _registry().resolve(profile, SETTINGS).name == "strict"


def test_a_profiles_policies_cannot_be_changed_after_loading(config_dir):
    _write(
        config_dir / "jurisdictions" / "es.yaml",
        "id: es\nname: Spain\npolicies:\n  eligibility: strict\n",
    )
    profile = load_profile(config_dir, "es")
    with pytest.raises(TypeError):
        profile.policies["eligibility"] = "lenient"  # type: ignore[index]
    assert profile.model_dump()["policies"] == {"eligibility": "strict"}
    assert _registry().resolve(profile, SETTINGS).name == "strict"


def test_a_profile_survives_pickling_and_deep_copies():
    """Worker processes receive profiles by pickle."""
    import copy
    import pickle

    profile = _profile(eligibility="strict")
    for clone in (pickle.loads(pickle.dumps(profile)), copy.deepcopy(profile)):
        assert clone == profile
        assert _registry().resolve(clone, SETTINGS).name == "strict"
        with pytest.raises(TypeError):
            clone.policies["eligibility"] = "lenient"  # type: ignore[index]
    assert repr(profile.policies) == "PolicyIds({'eligibility': 'strict'})"


def test_a_bare_policies_header_declares_no_policies(config_dir):
    _write(config_dir / "jurisdictions" / "es.yaml", "id: es\nname: Spain\npolicies:\n")
    assert load_profile(config_dir, "es").policies == {}
