"""A guard is verified by mutation, not by being green (lite's DECISIONS, "A guard is verified by
mutation"): three invariants of the candidate gates are broken on a copy of ``decision/gates.py``
and the test that guards each must fail against the mutant while it passes on the original.

* ``crossed_book_waived``: a crossed book fails outright, before the depth waiver; killed by
  ``test_a_crossed_book_fails_and_no_depth_waives_it``.
* ``commitments_ignored``: the pool subtracts the premium open positions commit; killed by
  ``test_the_pool_is_the_budget_less_the_penalty_and_the_commitments``.
* ``tenor_floor_at_the_hold``: the entry window starts above the minimum hold; killed by
  ``test_an_entry_at_the_minimum_hold_is_refused``.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import importlib.util
import inspect
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

import pytest

import obliskiq.decision.gates as original
from obliskiq.config import ObliskConfig


def _guards() -> ModuleType:
    """The gates' own test module, loaded by path (tests import by path, not by name)."""
    path = Path(__file__).with_name("test_decision_gates.py")
    spec = importlib.util.spec_from_file_location("gate_guards", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guards = _guards()

#: name -> (the original text, its mutant, the guarding test)
MUTATIONS: dict[str, tuple[str, str, Callable[..., None]]] = {
    "crossed_book_waived": (
        "if spread_pct_of_mid is not None and spread_pct_of_mid < 0:",
        "if False:",
        guards.test_a_crossed_book_fails_and_no_depth_waives_it,
    ),
    "commitments_ignored": (
        "available=max(ZERO, budget - penalty - committed),",
        "available=max(ZERO, budget - penalty),",
        guards.test_the_pool_is_the_budget_less_the_penalty_and_the_commitments,
    ),
    "tenor_floor_at_the_hold": (
        "if dte < self._entry_dte_min:",
        "if dte < self._hold_days_min:",
        guards.test_an_entry_at_the_minimum_hold_is_refused,
    ),
}


def mutant(original_text: str, mutated_text: str) -> ModuleType:
    """A copy of ``decision/gates.py`` with one textual mutation, loaded beside the original."""
    source = inspect.getsource(original)
    assert source.count(original_text) == 1, f"the mutation site {original_text!r} moved"
    spec = importlib.util.spec_from_loader("obliskiq.decision._gates_mutant", loader=None)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "obliskiq.decision"
    sys.modules[spec.name] = module
    exec(
        compile(source.replace(original_text, mutated_text), original.__file__ or "gates", "exec"),
        module.__dict__,
    )
    return module


@pytest.fixture
def cleanup() -> Iterator[None]:
    yield
    sys.modules.pop("obliskiq.decision._gates_mutant", None)


def call(test: Callable[..., None], gates: object, settings: ObliskConfig) -> None:
    parameters = inspect.signature(test).parameters
    kwargs = {"gates": gates, "shipped_settings": settings}
    test(**{name: kwargs[name] for name in parameters})


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_each_guard_kills_its_mutation(
    name: str, shipped_settings: ObliskConfig, cleanup: None
) -> None:
    original_text, mutated_text, guard = MUTATIONS[name]
    call(guard, original.CandidateGates(shipped_settings), shipped_settings)
    broken = mutant(original_text, mutated_text).CandidateGates(shipped_settings)
    with pytest.raises(AssertionError):
        call(guard, broken, shipped_settings)
