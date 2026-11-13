"""The thesis book's share of capital, its configuration and its pre-registration (H13).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from obliskiq.capital.budgets import SleeveBudgets, book_allocation, thesis_share
from obliskiq.config import ObliskConfig
from obliskiq.config.schema import ObliskConfig as Schema
from obliskiq.domain.orders import ExecutionMode, Sleeve
from obliskiq.domain.thesis import Conviction, ThesisSource
from obliskiq.errors import PreregistrationError
from obliskiq.research.patterns import registered_hypothesis
from obliskiq.research.prereg import FORWARD_KINDS, ForwardBookPrereg, load_any
from obliskiq.research.registry import verify_lock

D = Decimal


def h13(repo_root: Path) -> Path:
    return repo_root / "research" / "preregistrations" / "thesis-book.md"


# ── the budget reader ──


@pytest.mark.parametrize("mode", [ExecutionMode.SHADOW, ExecutionMode.PAPER])
def test_the_book_takes_its_share_of_the_satellite_s_budget_in_shadow_and_paper(
    shipped_settings: ObliskConfig, mode: ExecutionMode
) -> None:
    assert thesis_share(shipped_settings, mode) == D("0.10")
    allocation = book_allocation(shipped_settings, D(1000), mode)
    # The core keeps its 70 %, the options' 5 % and the 10 % of the satellite's the book leaves.
    assert (allocation.core, allocation.satellite, allocation.options) == (D(850), D(100), D(0))
    assert allocation.cash_floor == D(50)
    assert allocation.of(Sleeve.SATELLITE) == D(100)


@pytest.mark.parametrize("mode", [ExecutionMode.LIVE_SMALL, ExecutionMode.LIVE])
def test_the_book_has_no_share_on_the_live_account(
    shipped_settings: ObliskConfig, mode: ExecutionMode
) -> None:
    assert thesis_share(shipped_settings, mode) == D(0)
    allocation = book_allocation(shipped_settings, D(1000), mode)
    assert (allocation.core, allocation.satellite) == (D(950), D(0))


def test_a_share_of_nothing_leaves_everything_in_the_reserve(
    shipped_settings: ObliskConfig,
) -> None:
    thesis = shipped_settings.thesis.model_copy(update={"capital_share": D(0)})
    alone = shipped_settings.model_copy(update={"thesis": thesis})
    assert book_allocation(alone, D(1000), ExecutionMode.SHADOW).core == D(950)


def test_a_layer_never_takes_more_than_the_satellite_s_budget(
    shipped_settings: ObliskConfig,
) -> None:
    budgets = SleeveBudgets(shipped_settings.capital)
    with pytest.raises(ValueError, match="at most the satellite's budget"):
        budgets.allocate(D(1000), active={Sleeve.CORE, Sleeve.SATELLITE}, satellite_share=D("0.21"))


# ── the configuration ──


def test_the_configuration_refuses_a_share_above_the_budget_and_an_unknown_role(
    shipped_settings: ObliskConfig,
) -> None:
    data = shipped_settings.model_dump()
    data["thesis"]["capital_share"] = "0.25"
    data["thesis"]["defensive_roles"] = ["gold", "bunker"]
    with pytest.raises(ValidationError) as caught:
        Schema.model_validate(data)
    assert "thesis.capital_share: 0.25 is above the equity satellite's budget 0.20" in str(
        caught.value
    )
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
    # The step starts from the share the book works on and never passes the ceiling.
    assert shipped_settings.thesis.capital_share <= spec.step.share_ceiling
    lock = verify_lock(h13(repo_root))
    assert lock.sha256 == prereg.sha256
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
