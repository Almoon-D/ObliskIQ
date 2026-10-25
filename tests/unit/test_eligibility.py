"""The ``eu_priips`` eligibility policy (``jurisdiction/eligibility_eu_priips.py``) and its
registration: buy eligibility computed from the universe's attributes.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from typing import Any

import pytest
from traded import instrument, option

from obliskiq.config.schema_universe import UniverseConfig, UniverseInstrument
from obliskiq.domain.enums import AssetClass
from obliskiq.domain.instrument import TradedInstrument
from obliskiq.errors import PolicyError
from obliskiq.execution.instruments import UniverseDirectory, traded_instrument
from obliskiq.jurisdiction import ELIGIBILITY_POLICIES, SHIPPED_POLICY_KINDS, load_profile
from obliskiq.jurisdiction.eligibility import EligibilityProfile, EligibilityVerdict
from obliskiq.jurisdiction.eligibility_eu_priips import (
    EU_PRIIPS_POLICY_ID,
    ContangoVerdict,
    EuPriipsEligibility,
    UnavailableContangoCheck,
)

SPY, EWJ, EXV1, IWDA, EMIM, IBGM, FCX = (
    756733,
    253190534,
    89005163,
    78999785,
    153454120,
    43614383,
    7089,
)


class PassingContango:
    def assess(self, root: str) -> ContangoVerdict:
        return ContangoVerdict(True, f"{root} is within the contango limit")


def rules(**changes: Any) -> EligibilityProfile:
    fields: dict[str, Any] = {
        "kid_required": ["etf", "etc", "fund"],
        "ucits_required": ["etf", "fund"],
        "government_bonds": True,
        "corporate_bonds": False,
        "options_on_ineligible_etps": "mandatory_close",
        "source": "a test",
    }
    fields.update(changes)
    return EligibilityProfile.model_validate(fields)


def declared(universe: UniverseConfig, base_symbol: str, **changes: Any) -> UniverseInstrument:
    """A shipped instrument with some of its fields (``attributes`` merged) changed."""
    base = universe.instrument(base_symbol)
    assert base is not None
    data = base.model_dump(mode="json")
    attributes = changes.pop("attributes", {})
    data.update(changes)
    data["attributes"] = data["attributes"] | attributes
    return UniverseInstrument.model_validate(data)


def with_instruments(universe: UniverseConfig, *extra: UniverseInstrument) -> UniverseConfig:
    return universe.model_copy(update={"instruments": [*universe.instruments, *extra]})


def traded(universe: UniverseConfig, conid: int) -> TradedInstrument:
    found = universe.pinned(conid)
    assert found is not None
    described = traded_instrument(found)
    assert described is not None
    return described


@pytest.fixture
def policy(shipped_settings: Any) -> EuPriipsEligibility:
    return EuPriipsEligibility(shipped_settings.universe, rules(), UnavailableContangoCheck())


def test_the_shipped_profile_selects_eu_priips(repo_root: Any, shipped_settings: Any) -> None:
    profile = load_profile(repo_root / "config", "es")
    resolved = ELIGIBILITY_POLICIES.resolve(profile, shipped_settings)
    assert resolved.policy_id == EU_PRIIPS_POLICY_ID
    assert ELIGIBILITY_POLICIES in SHIPPED_POLICY_KINDS
    assert ELIGIBILITY_POLICIES.ids() == (EU_PRIIPS_POLICY_ID,)


def test_a_us_domiciled_etf_is_refused_and_the_verdict_says_why(
    policy: EuPriipsEligibility, shipped_settings: Any
) -> None:
    universe = shipped_settings.universe
    for conid in (SPY, EWJ):
        verdict = policy.assess(traded(universe, conid), underlying=None)
        assert not verdict.eligible
        assert "domiciled in US" in verdict.reason
        assert "UCITS False" in verdict.reason
        assert "key information document" in verdict.reason
        assert "UCITS form" in verdict.reason


def test_the_core_funds_and_shares_are_eligible(
    policy: EuPriipsEligibility, shipped_settings: Any
) -> None:
    universe = shipped_settings.universe
    for conid in (EXV1, IWDA, EMIM, IBGM):
        verdict = policy.assess(traded(universe, conid), underlying=None)
        assert verdict.eligible, verdict.reason
        assert verdict.policy == EU_PRIIPS_POLICY_ID
        assert "with its PRIIPs KID available" in verdict.reason
    share = policy.assess(traded(universe, FCX), underlying=None)
    assert share.eligible
    assert "not a packaged product" in share.reason


def test_every_tradable_candidate_with_a_verdict_of_ineligible_is_explained(
    policy: EuPriipsEligibility, shipped_settings: Any
) -> None:
    for item in shipped_settings.universe.instruments:
        verdict = policy.assess_declared(item)
        assert verdict.reason.startswith(item.symbol)


def test_an_unknown_fact_refuses_as_firmly_as_a_bad_one(
    policy: EuPriipsEligibility, shipped_settings: Any
) -> None:
    universe = shipped_settings.universe
    unknown = declared(universe, "IWDA", attributes={"kid": None})
    verdict = policy.assess_declared(unknown)
    assert not verdict.eligible
    assert "KID unknown" in verdict.reason
    not_ucits = declared(universe, "IWDA", attributes={"ucits": None})
    assert "UCITS form" in policy.assess_declared(not_ucits).reason
    measured_only = universe.instrument("1618")
    assert measured_only is not None
    assert not policy.assess_declared(measured_only).eligible  # a Japanese fund: no KID


def test_an_index_and_an_undeclared_conid_are_never_eligible(
    policy: EuPriipsEligibility, shipped_settings: Any
) -> None:
    index = shipped_settings.universe.instrument("SPX")
    assert index is not None
    assert "an index" in policy.assess_declared(index).reason
    stranger = policy.assess(instrument(424242), underlying=None)
    assert not stranger.eligible
    assert "attributes are unknown" in stranger.reason
    mismatched = traded(shipped_settings.universe, FCX).model_copy(
        update={"asset_class": AssetClass.ETF}
    )
    assert "the universe declares FCX a stock" in policy.assess(mismatched, underlying=None).reason


def test_a_futures_tracking_etc_waits_for_the_contango_rule(shipped_settings: Any) -> None:
    universe = shipped_settings.universe
    etc = declared(
        universe,
        "IWDA",
        symbol="TESTETC",
        conid=990001,
        asset_class="etc",
        roles=["tradable_candidate", "data"],
        exposures=universe.instrument("IWDA").exposures.model_dump() | {"core_roles": []},  # type: ignore[union-attr]
        attributes={"ucits": False, "replication": "futures", "futures_root": "CL"},
    )
    wider = with_instruments(universe, etc)
    closed = EuPriipsEligibility(wider, rules(), UnavailableContangoCheck()).assess_declared(etc)
    assert not closed.eligible
    assert "no reading of CL's curve is wired in" in closed.reason
    open_ = EuPriipsEligibility(wider, rules(), PassingContango()).assess_declared(etc)
    assert open_.eligible  # an ETC needs its KID, not the UCITS form
    with pytest.raises(ValueError, match="futures replication names its futures_root"):
        declared(universe, "IWDA", attributes={"replication": "futures"})


def test_bonds_follow_the_profiles_switches(shipped_settings: Any) -> None:
    universe = shipped_settings.universe
    government = declared(
        universe, "IWDA", symbol="GOVT1", conid=990002, asset_class="bond",
        attributes={"bond_issuer": "government"},
    )  # fmt: skip
    corporate = declared(
        universe, "IWDA", symbol="CORP1", conid=990003, asset_class="bond",
        attributes={"bond_issuer": "corporate"},
    )  # fmt: skip
    nameless = declared(
        universe, "IWDA", symbol="BOND1", conid=990004, asset_class="bond",
        attributes={"bond_issuer": None},
    )  # fmt: skip
    default = EuPriipsEligibility(universe, rules(), UnavailableContangoCheck())
    assert default.assess_declared(government).eligible
    assert "off by the profile" in default.assess_declared(corporate).reason
    assert "issuer kind is not declared" in default.assess_declared(nameless).reason
    flipped = EuPriipsEligibility(
        universe, rules(corporate_bonds=True, government_bonds=False), UnavailableContangoCheck()
    )
    assert flipped.assess_declared(corporate).eligible
    assert not flipped.assess_declared(government).eligible


def test_an_option_is_judged_with_its_underlying(
    policy: EuPriipsEligibility, shipped_settings: Any
) -> None:
    universe = shipped_settings.universe
    share, etf = traded(universe, FCX), traded(universe, SPY)
    on_share = policy.assess(option(800001, share), underlying=share)
    assert on_share.eligible
    assert not on_share.mandatory_close
    on_etf = policy.assess(option(800002, etf), underlying=etf)
    assert on_etf.eligible
    assert on_etf.mandatory_close
    assert "closed before expiry" in on_etf.reason
    assert not policy.assess(option(800001, share), underlying=None).eligible
    assert not policy.assess(option(800001, share), underlying=etf).eligible  # not its underlying
    refused = EuPriipsEligibility(
        universe, rules(options_on_ineligible_etps="refused"), UnavailableContangoCheck()
    )
    assert not refused.assess(option(800002, etf), underlying=etf).eligible
    index = traded_instrument(
        declared(universe, "SPX", description="S&P 500 INDEX", data_only_reason="a level")
    )
    assert index is not None
    on_index = policy.assess(option(800003, index), underlying=index)
    assert not on_index.eligible  # an index is not an ETP: its options are not held at all


def test_a_profile_needs_its_eligibility_section(repo_root: Any, shipped_settings: Any) -> None:
    profile = load_profile(repo_root / "config", "es").model_copy(update={"eligibility": None})
    with pytest.raises(PolicyError, match="carries no eligibility section"):
        ELIGIBILITY_POLICIES.resolve(profile, shipped_settings)
    with pytest.raises(ValueError, match="a packaged product and needs a KID"):
        rules(kid_required=["etc"], ucits_required=["etf"])
    with pytest.raises(ValueError, match="listed twice"):
        rules(kid_required=["etf", "etf"])


def test_the_directory_describes_every_pinned_and_resolved_instrument(
    shipped_settings: Any,
) -> None:
    directory = UniverseDirectory.from_settings(shipped_settings)
    assert directory.describe(FCX) is not None
    assert directory.describe(SPY) is not None  # ineligible, yet described: options name it
    assert directory.describe(416904) is None  # SPX: pinned, but no resolved description
    assert directory.describe(1) is None
    leveraged = declared(
        shipped_settings.universe, "IWDA", symbol="LEV", conid=990005,
        description="ProShares UltraPro QQQ",
    )  # fmt: skip
    universe = with_instruments(shipped_settings.universe, leveraged)
    narrowed = UniverseDirectory.from_settings(
        shipped_settings.model_copy(update={"universe": universe})
    )
    assert narrowed.describe(990005) is None  # rejected at ingestion by its description


def test_a_verdict_is_frozen() -> None:
    verdict = EligibilityVerdict(True, "r", "p")
    with pytest.raises(AttributeError):
        verdict.eligible = False  # type: ignore[misc]
