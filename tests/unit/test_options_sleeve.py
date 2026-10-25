"""The options sleeve: its budget from ``capital.budgets``, its candidates through the gates, its
activation condition (H8 sufficient, the budget buying the pre-registered number of median
premiums, each within the friction gate) and the shadow log it keeps while that condition fails.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from options_rig import AS_OF, EURUSD, SPY, position, seed_options

from obliskiq.calendars.service import Calendars
from obliskiq.config import ObliskConfig
from obliskiq.decision.gates import Gate
from obliskiq.domain.enums import AssetClass
from obliskiq.domain.instrument import Instrument
from obliskiq.domain.market import ChainRow
from obliskiq.domain.research import RegisteredHypothesis
from obliskiq.engines.options_sleeve.activation import (
    BUDGET,
    EVIDENCE,
    FRICTION,
    FrictionGate,
    FrictionReading,
    SleeveActivation,
)
from obliskiq.engines.options_sleeve.candidates import CandidateBook, Pool
from obliskiq.engines.options_sleeve.sleeve import OptionsSleeve
from obliskiq.research.patterns import RegistryError, registered_hypothesis
from obliskiq.store import Store, repos
from obliskiq.store import options as repo

USD_TO_EUR = Decimal(1) / Decimal(repr(EURUSD))
AT = datetime(2026, 10, 22, 22, 0, tzinfo=UTC)


def sufficient(premiums: int | None = 3) -> RegisteredHypothesis:
    return RegisteredHypothesis(
        hypothesis="H8",
        status="shadow",
        decision="sufficient",
        min_median_premiums=premiums,
        source="pattern_registry.yaml: options-h8",
    )


def friction(share: str | None) -> FrictionReading:
    return FrictionReading(
        Decimal(100),
        Decimal(3),
        Decimal(0),
        None if share is None else Decimal(share),
        "priced" if share else "no rate",
    )


# ── the activation condition ──


def test_without_a_registered_h8_the_sleeve_stays_in_shadow(shipped_settings: ObliskConfig) -> None:
    activation = SleeveActivation(shipped_settings).evaluate(
        evidence=None,
        budget=Decimal(100000),
        budget_gap=None,
        median_premium=Decimal(100),
        median_gap=None,
        friction=friction("0.01"),
        friction_holds=True,
    )
    assert not activation.active
    legs = {leg.name: leg.holds for leg in activation.legs}
    assert legs == {EVIDENCE: False, BUDGET: None, FRICTION: True}
    assert activation.reason().startswith("shadow: evidence fails: H8 is not in the research")


@pytest.mark.parametrize("decision", ["insufficient", "inconclusive", None])
def test_an_outcome_short_of_sufficient_keeps_the_sleeve_in_shadow(
    shipped_settings: ObliskConfig, decision: str | None
) -> None:
    evidence = sufficient().model_copy(update={"decision": decision})
    activation = SleeveActivation(shipped_settings).evaluate(
        evidence=evidence,
        budget=Decimal(100000),
        budget_gap=None,
        median_premium=Decimal(100),
        median_gap=None,
        friction=friction("0.01"),
        friction_holds=True,
    )
    assert not activation.active
    assert activation.legs[0].holds is False


def test_every_leg_holding_activates_the_sleeve(shipped_settings: ObliskConfig) -> None:
    activation = SleeveActivation(shipped_settings).evaluate(
        evidence=sufficient(3),
        budget=Decimal(300),
        budget_gap=None,
        median_premium=Decimal(100),
        median_gap=None,
        friction=friction("0.05"),
        friction_holds=True,
    )
    assert activation.active
    assert activation.reason().startswith("active: H8 is sufficient")


def test_a_budget_short_of_the_pre_registered_count_or_unknown_is_shadow(
    shipped_settings: ObliskConfig,
) -> None:
    activation = SleeveActivation(shipped_settings)
    short = activation.evaluate(
        evidence=sufficient(3),
        budget=Decimal(299),
        budget_gap=None,
        median_premium=Decimal(100),
        median_gap=None,
        friction=friction("0.05"),
        friction_holds=True,
    )
    assert short.legs[1].holds is False
    assert not short.active
    unknown = activation.evaluate(
        evidence=sufficient(3),
        budget=None,
        budget_gap="no account read",
        median_premium=Decimal(100),
        median_gap=None,
        friction=friction("0.05"),
        friction_holds=True,
    )
    assert unknown.legs[1].holds is None
    assert unknown.legs[1].detail == "no account read"
    no_median = activation.evaluate(
        evidence=sufficient(3),
        budget=Decimal(1000),
        budget_gap=None,
        median_premium=None,
        median_gap="nothing passed",
        friction=None,
        friction_holds=None,
    )
    assert no_median.legs[1].holds is None
    assert no_median.legs[2].holds is None
    assert "nothing passed" in no_median.reason()


def test_friction_beyond_the_ceiling_keeps_the_sleeve_in_shadow(
    shipped_settings: ObliskConfig,
) -> None:
    activation = SleeveActivation(shipped_settings).evaluate(
        evidence=sufficient(1),
        budget=Decimal(1000),
        budget_gap=None,
        median_premium=Decimal(100),
        median_gap=None,
        friction=friction("0.40"),
        friction_holds=False,
    )
    assert activation.legs[2].holds is False
    assert not activation.active


# ── the friction gate ──


def test_a_base_currency_premium_pays_commission_only(shipped_settings: ObliskConfig) -> None:
    gate = FrictionGate(shipped_settings)
    reading = gate.assess(
        premium_per_unit=Decimal("2.00"),
        multiplier=Decimal(100),
        contracts=1,
        venue="XEUR",
        currency="EUR",
        rate_to_base=lambda _: None,
    )
    assert reading.conversion_base == 0
    # Eurex: EUR 1.50 a contract, twice: 3.00 on a 200 premium.
    assert reading.commission_base == Decimal("3.00")
    assert reading.share == Decimal("0.015")
    assert gate.holds(reading) is True


def test_a_foreign_premium_pays_the_conversion_both_ways(shipped_settings: ObliskConfig) -> None:
    gate = FrictionGate(shipped_settings)
    reading = gate.assess(
        premium_per_unit=Decimal("0.30"),
        multiplier=Decimal(100),
        contracts=1,
        venue="XNYS",
        currency="USD",
        rate_to_base=lambda currency: USD_TO_EUR if currency == "USD" else None,
    )
    assert reading.conversion_base is not None
    assert reading.conversion_base > 0
    assert reading.share is not None
    assert reading.share > gate.max_share
    assert gate.holds(reading) is False


def test_friction_that_cannot_be_priced_is_untested(shipped_settings: ObliskConfig) -> None:
    gate = FrictionGate(shipped_settings)
    no_rate = gate.assess(
        premium_per_unit=Decimal(1),
        multiplier=Decimal(100),
        contracts=1,
        venue="XNYS",
        currency="USD",
        rate_to_base=lambda _: None,
    )
    assert no_rate.share is None
    assert gate.holds(no_rate) is None
    no_schedule = gate.assess(
        premium_per_unit=Decimal(1),
        multiplier=Decimal(100),
        contracts=1,
        venue="XTKS",
        currency="EUR",
        rate_to_base=lambda _: None,
    )
    assert no_schedule.share is None
    assert "commission cannot be priced" in no_schedule.reason


# ── the research record ──


def test_the_registry_reader(tmp_path: Path) -> None:
    path = tmp_path / "pattern_registry.yaml"
    assert registered_hypothesis(path, "H8") is None, "no registry, no outcome"
    path.write_text("patterns:\n  - {id: a, hypothesis: H1, status: retired}\n")
    assert registered_hypothesis(path, "H8") is None
    path.write_text(
        "patterns:\n"
        "  - id: options-h8\n    hypothesis: H8\n    status: shadow\n"
        "    outcome: {decision: sufficient}\n    activation: {median_premiums: 3}\n"
    )
    found = registered_hypothesis(path, "H8")
    assert found == RegisteredHypothesis(
        hypothesis="H8",
        status="shadow",
        decision="sufficient",
        min_median_premiums=3,
        source="pattern_registry.yaml: options-h8",
    )


@pytest.mark.parametrize(
    "text",
    [
        "patterns: {}\n",
        "patterns:\n  - {id: a, hypothesis: H8, status: shadow}\n"
        "  - {id: b, hypothesis: H8, status: idea}\n",
        "patterns:\n  - {id: a, hypothesis: H8, status: maybe}\n",
        "patterns: [\n",
    ],
)
def test_a_registry_that_cannot_be_read_raises(tmp_path: Path, text: str) -> None:
    path = tmp_path / "pattern_registry.yaml"
    path.write_text(text)
    with pytest.raises(RegistryError):
        registered_hypothesis(path, "H8")


def test_the_shipped_registry_holds_no_h8(repo_root: Path) -> None:
    assert registered_hypothesis(repo_root / "research" / "pattern_registry.yaml", "H8") is None


# ── the sleeve ──


@pytest.fixture
def sleeve(store: Store, shipped_settings: ObliskConfig, calendars: Calendars) -> OptionsSleeve:
    return OptionsSleeve(
        store, shipped_settings, calendars=calendars, evidence=None, now=lambda: AT
    )


def test_the_budget_is_the_option_share_of_investable_capital(
    shipped_settings: ObliskConfig, sleeve: OptionsSleeve
) -> None:
    share = shipped_settings.capital.budgets.option_premium
    assert sleeve.budget(Decimal(1000)) == Decimal(1000) * share
    assert sleeve.penalty.status(budget=Decimal(1000), as_of=AS_OF).available == Decimal(1000)


def test_the_sleeve_stays_in_shadow_and_logs_its_would_be_entries(
    store: Store, shipped_settings: ObliskConfig, sleeve: OptionsSleeve
) -> None:
    """The acceptance case: the activation condition fails (no H8), so the sleeve trades
    nothing and every contract that failed no gate is logged for H8."""
    seed_options(store, shipped_settings)
    run = sleeve.evaluate(as_of=AS_OF, investable=Decimal(100000))
    assert not run.activation.active
    assert run.scanned == 18
    assert run.survivors, run.report()
    assert run.status is not None
    assert run.status.budget == sleeve.budget(Decimal(100000))
    assert run.median_premium is not None
    assert run.friction is not None
    logged = sleeve.log_shadow(run)
    assert logged == len(run.survivors)
    entries = repo.shadow_entries(store, as_of=AS_OF)
    assert len(entries) == logged
    assert all(entry.activation.startswith("shadow:") for entry in entries)
    assert all(entry.underlying_conid == SPY for entry in entries)
    assert all(entry.premium_base is not None for entry in entries)
    report = "\n".join(run.report())
    assert "shadow: evidence fails" in report
    assert "pool: budget" in report


def test_a_relog_replaces_the_session(
    store: Store, shipped_settings: ObliskConfig, sleeve: OptionsSleeve
) -> None:
    seed_options(store, shipped_settings)
    run = sleeve.evaluate(as_of=AS_OF, investable=Decimal(100000))
    sleeve.log_shadow(run)
    again = sleeve.log_shadow(run)
    assert len(repo.shadow_entries(store, as_of=AS_OF)) == again


def test_without_a_budget_the_pool_is_untested_and_still_logged(
    store: Store, shipped_settings: ObliskConfig, sleeve: OptionsSleeve
) -> None:
    seed_options(store, shipped_settings)
    run = sleeve.evaluate(as_of=AS_OF, investable=None, investable_gap="no account read")
    assert run.status is None
    assert run.budget_gap == "no account read"
    assert run.untested[Gate.PRICE_CEILING] == run.scanned
    assert run.survivors
    assert not any(item.result.fully_tested for item in run.survivors)
    assert "pool: untested (no account read)" in "\n".join(run.report())
    entries = repo.shadow_entries(store, as_of=AS_OF)
    assert entries == ()
    sleeve.log_shadow(run)
    assert all(
        Gate.PRICE_CEILING.value in entry.untested
        for entry in repo.shadow_entries(store, as_of=AS_OF)
    )


def test_a_sleeve_at_its_cap_logs_nothing_it_could_not_enter(
    store: Store, shipped_settings: ObliskConfig, sleeve: OptionsSleeve
) -> None:
    seed_options(store, shipped_settings)
    for _ in range(shipped_settings.option_sleeve.max_concurrent_positions):
        repo.open_position(store, position(opened_on=date(2026, 10, 1)))
    run = sleeve.evaluate(as_of=AS_OF, investable=Decimal(100000))
    assert run.failed[Gate.CONCURRENCY] == run.scanned
    assert run.survivors == ()
    assert run.median_premium is None


def test_without_a_multiplier_no_premium_is_priced(
    store: Store, shipped_settings: ObliskConfig, sleeve: OptionsSleeve
) -> None:
    seed_options(store, shipped_settings, multiplier=None)
    run = sleeve.evaluate(as_of=AS_OF, investable=Decimal(100000))
    assert run.median_premium is None
    assert run.friction is None
    assert "could be priced" in run.activation.reason()


def test_a_median_contract_on_an_unmapped_exchange_leaves_the_friction_untested(
    store: Store, shipped_settings: ObliskConfig, sleeve: OptionsSleeve
) -> None:
    seed_options(store, shipped_settings)
    store.execute("UPDATE chain_snapshots SET option_exchange = 'NOWHERE'")
    run = sleeve.evaluate(as_of=AS_OF, investable=Decimal(100000))
    assert run.friction is not None
    assert run.friction.share is None
    assert "NOWHERE maps to no venue" in run.friction.reason
    assert run.activation.legs[2].holds is None


def test_an_active_sleeve_logs_no_shadow_entry(
    store: Store, shipped_settings: ObliskConfig, calendars: Calendars
) -> None:
    seed_options(store, shipped_settings)
    sleeve = OptionsSleeve(
        store, shipped_settings, calendars=calendars, evidence=sufficient(1), now=lambda: AT
    )
    run = sleeve.evaluate(as_of=AS_OF, investable=Decimal(1_000_000))
    assert run.activation.active, run.activation.reason()
    assert sleeve.log_shadow(run) == 0
    assert repo.shadow_entries(store, as_of=AS_OF) == ()


def test_an_empty_store_scans_nothing_and_says_so(store: Store, sleeve: OptionsSleeve) -> None:
    run = sleeve.evaluate(as_of=AS_OF, investable=None, investable_gap="x")
    assert run.scanned == 0
    assert run.skipped == ()
    assert "no contract failed no gate" in run.activation.reason()
    quiet = "\n".join(sleeve.evaluate(as_of=AS_OF, investable=None).report())
    assert "pool:" not in quiet
    assert "refused by" not in quiet
    assert "the sleeve's budget is unknown" not in quiet, "the budget leg waits on H8's count"


def test_a_chain_of_no_pinned_underlying_is_skipped_with_why(
    store: Store,
    sleeve: OptionsSleeve,
    make_instrument: Callable[..., Instrument],
    make_chain_row: Callable[..., ChainRow],
) -> None:
    stray = 990002
    repos.upsert_instrument(store, make_instrument(conid=stray, symbol="STRAY"))
    repos.upsert_chain_rows(store, [make_chain_row(underlying_conid=stray, snapshot_date=AS_OF)])
    run = sleeve.evaluate(as_of=AS_OF, investable=None, investable_gap="x")
    assert run.scanned == 0
    (skipped,) = run.skipped
    assert skipped.underlying_conid == stray
    assert "no instrument the universe pins" in skipped.reason
    assert f"skipped {stray}:" in "\n".join(run.report())


def test_a_stale_chain_is_skipped_and_a_stale_surface_read_as_absent(
    store: Store, shipped_settings: ObliskConfig, sleeve: OptionsSleeve
) -> None:
    window = shipped_settings.grid.freshness_days.option_chain
    seed_options(store, shipped_settings)
    later = AS_OF + timedelta(days=window + 1)
    stale = sleeve.evaluate(as_of=later, investable=Decimal(100000))
    assert stale.scanned == 0
    (skipped,) = stale.skipped
    assert f"{window + 1} days old" in skipped.reason
    seed_options(store, shipped_settings, as_of=later, iv_percentile=None)
    fresh_chain = sleeve.evaluate(as_of=later, investable=Decimal(100000))
    assert fresh_chain.scanned
    assert fresh_chain.untested[Gate.IV_PERCENTILE] == fresh_chain.scanned
    (candidate, *_) = CandidateBook(store, shipped_settings).candidates(
        as_of=later, pool=Pool(None, "x", 0, None)
    )
    assert "the newest option surface is from" in (candidate.iv_percentile_gap or "")


def test_a_conversion_nothing_prices_leaves_the_friction_untested(
    shipped_settings: ObliskConfig,
) -> None:
    usd = {"USD": USD_TO_EUR}
    costs = shipped_settings.costs
    no_fx = costs.model_copy(
        update={
            "commissions": [s for s in costs.commissions if AssetClass.FX not in s.asset_classes]
        }
    )
    gate = FrictionGate(shipped_settings.model_copy(update={"costs": no_fx}))
    reading = gate.assess(
        premium_per_unit=Decimal("2.00"),
        multiplier=Decimal(100),
        contracts=1,
        venue="XNYS",
        currency="USD",
        rate_to_base=usd.get,
    )
    assert reading.commission_base is not None
    assert reading.share is None
    assert "no commission schedule prices a currency conversion" in reading.reason
    tiny = [
        s.model_copy(update={"currency": "CHF"}) if AssetClass.FX in s.asset_classes else s
        for s in costs.commissions
    ]
    chf = FrictionGate(
        shipped_settings.model_copy(
            update={"costs": costs.model_copy(update={"commissions": tiny})}
        )
    ).assess(
        premium_per_unit=Decimal("2.00"),
        multiplier=Decimal(100),
        contracts=1,
        venue="XNYS",
        currency="USD",
        rate_to_base=usd.get,
    )
    assert "no rate values the conversion in CHF" in chf.reason
    elsewhere = [
        s.model_copy(update={"venues": ["XNYS"]}) if AssetClass.FX in s.asset_classes else s
        for s in costs.commissions
    ]
    unpriced = FrictionGate(
        shipped_settings.model_copy(
            update={"costs": costs.model_copy(update={"commissions": elsewhere})}
        )
    ).assess(
        premium_per_unit=Decimal("2.00"),
        multiplier=Decimal(100),
        contracts=1,
        venue="XNYS",
        currency="USD",
        rate_to_base=usd.get,
    )
    assert "the conversion cannot be priced" in unpriced.reason


def test_the_activation_names_its_hypothesis(shipped_settings: ObliskConfig) -> None:
    assert SleeveActivation(shipped_settings).hypothesis == "H8"
