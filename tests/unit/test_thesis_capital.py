"""The thesis book's share of capital, its configuration and its pre-registration (H13).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from thesis_rig import EMIM_CONID, approved, instrument

from obliskiq.capital.allocation import LiveShares, live_allocation, thesis_commitment
from obliskiq.capital.budgets import SleeveBudgets, book_allocation, thesis_share
from obliskiq.capital.managed import BookHolding, CapitalView
from obliskiq.capital.valuation import UnitValue
from obliskiq.config import ObliskConfig
from obliskiq.config.schema import ObliskConfig as Schema
from obliskiq.domain.orders import ExecutionMode, Sleeve
from obliskiq.domain.thesis import Conviction, ThesisSource
from obliskiq.errors import PreregistrationError
from obliskiq.research.patterns import registered_hypothesis
from obliskiq.research.prereg import FORWARD_KINDS, ForwardBookPrereg, load_any
from obliskiq.research.registry import lock_path
from obliskiq.scoreboard.layers import ThesisBookCapital, thesis_book_share

D = Decimal


def h13(repo_root: Path) -> Path:
    return repo_root / "research" / "preregistrations" / "thesis-book.md"


# ── the budget reader ──


@pytest.mark.parametrize("mode", [ExecutionMode.SHADOW, ExecutionMode.PAPER])
def test_the_book_hunts_with_the_owner_s_share_and_the_reserve_holds_the_rest(
    shipped_settings: ObliskConfig, mode: ExecutionMode
) -> None:
    # The owner's ruling: the whole account hunts (config/owner.yaml, D185).
    assert thesis_share(shipped_settings, mode) == D(1)
    allocation = book_allocation(shipped_settings, D(1000), mode)
    # The book may take all that the cash floor leaves; the reserve is budgeted the same until
    # the book commits it (capital/allocation.py: reserve_budget).
    assert (allocation.thesis_book, allocation.core, allocation.options) == (D(950), D(950), D(0))
    assert allocation.cash_floor == D(50)
    assert allocation.of(Sleeve.SATELLITE) == D(950)


def test_a_smaller_share_is_that_share_of_investable_capital(
    shipped_settings: ObliskConfig,
) -> None:
    thesis = shipped_settings.thesis.model_copy(update={"capital_share": D("0.25")})
    quarter = shipped_settings.model_copy(update={"thesis": thesis})
    allocation = book_allocation(quarter, D(1000), ExecutionMode.SHADOW)
    assert (allocation.thesis_book, allocation.core) == (D(250), D(950))


@pytest.mark.parametrize("mode", [ExecutionMode.LIVE_SMALL, ExecutionMode.LIVE])
def test_the_book_has_no_share_on_the_live_account_without_the_rule(
    shipped_settings: ObliskConfig, mode: ExecutionMode
) -> None:
    assert thesis_share(shipped_settings, mode) == D(0)
    allocation = book_allocation(shipped_settings, D(1000), mode)
    assert (allocation.core, allocation.satellite, allocation.thesis_book) == (D(950), D(0), D(0))


def test_on_the_live_account_the_reserve_keeps_the_book_s_share_until_it_is_committed(
    shipped_settings: ObliskConfig,
) -> None:
    shares = LiveShares(satellite=D("0.20"), options=D(0), thesis=D("0.20"))
    allocation = live_allocation(shipped_settings, D(1000), shares)
    assert (allocation.thesis_book, allocation.core) == (D(200), D(950))


def test_a_share_of_nothing_leaves_everything_in_the_reserve(
    shipped_settings: ObliskConfig,
) -> None:
    thesis = shipped_settings.thesis.model_copy(update={"capital_share": D(0)})
    alone = shipped_settings.model_copy(update={"thesis": thesis})
    allocation = book_allocation(alone, D(1000), ExecutionMode.SHADOW)
    assert (allocation.core, allocation.thesis_book) == (D(950), D(0))


def test_a_layer_never_takes_more_than_the_satellite_s_budget(
    shipped_settings: ObliskConfig,
) -> None:
    budgets = SleeveBudgets(shipped_settings.capital)
    with pytest.raises(ValueError, match="at most the satellite's budget"):
        budgets.allocate(D(1000), active={Sleeve.CORE, Sleeve.SATELLITE}, satellite_share=D("0.21"))


def _view(*holdings: BookHolding) -> CapitalView:
    return CapitalView(
        mode="cap", nav=D(1000), managed=D(1000), unmanaged=D(0), reserved=D(0),
        investable=D(1000), book_value=D(900), book_cash=D(100), holdings=holdings, detail="t",
    )  # fmt: skip


def _held(conid: int, sleeve: Sleeve, units: str, unit: str) -> BookHolding:
    return BookHolding(conid, sleeve, D(units), UnitValue(conid, None, D(unit), D(1)))


def test_the_reserve_gives_up_what_the_book_holds_or_has_approved() -> None:
    view = _view(
        _held(1, Sleeve.CORE, "10", "50"),
        _held(EMIM_CONID, Sleeve.SATELLITE, "2", "45"),  # half of an approved thesis's 180
        _held(3, Sleeve.OPTIONS, "1", "30"),
    )
    waiting = approved(size_base=D(180))
    other = approved(thesis_id=2, size_base=D(100), instrument=instrument(conid=99))
    # Held: 90 of the satellite and 30 of an option; approved and not yet held: 90 and 100.
    assert thesis_commitment(view, [waiting, other]) == D(310)
    assert thesis_commitment(_view(_held(1, Sleeve.CORE, "10", "50")), []) == D(0)


# ── the configuration ──


def test_the_configuration_takes_the_whole_account_and_refuses_an_unknown_role(
    shipped_settings: ObliskConfig,
) -> None:
    data = shipped_settings.model_dump()
    data["thesis"]["capital_share"] = "1"
    assert Schema.model_validate(data).thesis.capital_share == D(1)
    data["thesis"]["capital_share"] = "1.5"
    data["thesis"]["defensive_roles"] = ["gold", "bunker"]
    with pytest.raises(ValidationError) as caught:
        Schema.model_validate(data)
    assert "capital_share" in str(caught.value)
    data["thesis"]["capital_share"] = "1"
    with pytest.raises(ValidationError) as caught:
        Schema.model_validate(data)
    assert "thesis.defensive_roles: bunker is not a role the core declares" in str(caught.value)


@pytest.mark.parametrize(
    ("section", "values", "words"),
    [
        ("conviction", {"normal": "2", "high": "1.5", "max": "3"}, "never sizes below"),
        (None, {"defensive_roles": ["gold", "gold"]}, "lists a role twice"),
    ],
)
def test_the_book_s_own_values_hold_together(
    shipped_settings: ObliskConfig, section: str | None, values: dict[str, object], words: str
) -> None:
    data = shipped_settings.thesis.model_dump()
    if section is None:
        data.update(values)
    else:
        data[section] = values
    with pytest.raises(ValidationError, match=words):
        type(shipped_settings.thesis).model_validate(data)


# ── the pre-registration ──


def test_h13_loads_as_a_forward_book_and_is_locked(
    repo_root: Path, shipped_settings: ObliskConfig
) -> None:
    prereg = load_any(h13(repo_root))
    assert isinstance(prereg, ForwardBookPrereg)
    assert isinstance(prereg, FORWARD_KINDS)
    spec = prereg.spec
    assert (spec.hypothesis, spec.trial_budget, spec.theta) == ("H13", 0, D("0.02"))
    assert set(spec.sources) == set(ThesisSource)
    assert set(spec.convictions) == set(Conviction)
    assert spec.reference.role in shipped_settings.core.roles
    assert (spec.looks.every_months, spec.looks.count) == (6, 10)
    assert (spec.step.record, spec.step.alpha) == (ExecutionMode.LIVE, D("0.05"))
    assert spec.back_to_shadow.records == [ExecutionMode.PAPER, ExecutionMode.LIVE]
    # On the live account the owner's share is H13's exploration share, never past its ceiling
    # (scoreboard/layers.py: thesis_book_share); H13 is not edited (D185, D195).
    capital = ThesisBookCapital(
        exploration=shipped_settings.thesis.capital_share,
        share_step=spec.step.share_step,
        share_ceiling=spec.step.share_ceiling,
        looks=(),
    )
    assert thesis_book_share(capital).share == min(
        shipped_settings.thesis.capital_share, spec.step.share_ceiling
    )
    # The lock records the text merged in its own pull request; `verify_lock` also asks git for
    # that commit, which a shallow checkout does not hold.
    lock = json.loads(lock_path(h13(repo_root)).read_text(encoding="utf-8"))
    assert (lock["file"], lock["sha256"]) == ("thesis-book.md", prereg.sha256)
    assert len(lock["merge_commit"]) == 40
    registered = registered_hypothesis(repo_root / "research" / "pattern_registry.yaml", "H13")
    assert registered is not None
    assert registered.status == "pre-registered"


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('  share_step: "0.10"', '  share_step: "0.60"', "share_step is above"),
        ("  record: live", "  record: live-small", "a record the registration does not keep"),
        ("trial_budget: 0", "trial_budget: 2", "trial_budget"),
        ("  count: 10", "  count: 0", "looks.count"),
    ],
)
def test_a_forward_book_out_of_shape_is_refused(
    repo_root: Path, tmp_path: Path, old: str, new: str, message: str
) -> None:
    text = h13(repo_root).read_text(encoding="utf-8")
    assert old in text
    path = tmp_path / "book.md"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    with pytest.raises(PreregistrationError, match=message):
        load_any(path)
