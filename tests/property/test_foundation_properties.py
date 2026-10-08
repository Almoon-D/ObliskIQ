"""Properties of the foundation helpers, checked over generated inputs.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import copy
import math
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from obliskiq.config.loader import flatten, merge_into
from obliskiq.util import safe_float, safe_int

_keys = st.text(alphabet="abcdefghij", min_size=1, max_size=3)
_leaves = st.one_of(st.integers(), st.text(max_size=5), st.booleans(), st.none())
_trees = st.recursive(
    st.dictionaries(_keys, _leaves, max_size=4),
    lambda children: st.dictionaries(_keys, st.one_of(_leaves, children), max_size=4),
    max_leaves=12,
)


@given(st.one_of(st.text(), st.floats(), st.integers(), st.none(), st.booleans()))
def test_safe_float_never_raises_and_never_returns_a_non_finite_number(value: Any):
    result = safe_float(value, None)
    assert result is None or math.isfinite(result)


@given(st.floats(allow_nan=False, allow_infinity=False))
def test_a_finite_float_survives_coercion_unchanged(value: float):
    assert safe_float(value, None) == value
    assert safe_float(repr(value), None) == value


@given(st.text())
def test_safe_int_never_raises(value: str):
    result = safe_int(value, None)
    assert result is None or isinstance(result, int)


def _at(tree: dict[str, Any], dotted: str) -> Any:
    node: Any = tree
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _is_noop(base: dict[str, Any], dotted: str, value: Any) -> bool:
    """An empty section laid over a mapping overrides nothing."""
    return (value is None or value == {}) and isinstance(_at(base, dotted), dict)


def _related(a: str, b: str) -> bool:
    return a == b or a.startswith(f"{b}.") or b.startswith(f"{a}.")


@given(_trees, _trees)
def test_the_overlay_always_wins_and_untouched_defaults_survive(
    base: dict[str, Any], overlay: dict[str, Any]
):
    merged = copy.deepcopy(base)
    sources = dict.fromkeys(flatten(base), "default")
    merge_into(merged, overlay, "owner", sources)
    flat_merged = flatten(merged)
    effective = {
        key: value for key, value in flatten(overlay).items() if not _is_noop(base, key, value)
    }
    for key, value in effective.items():
        assert flat_merged[key] == value
        assert sources[key] == "owner"
    for key, value in flatten(base).items():
        if not any(_related(key, changed) for changed in effective):
            assert flat_merged[key] == value
            assert sources[key] == "default"
    assert set(sources) == set(flat_merged)


@given(_trees)
def test_merging_into_nothing_reproduces_the_overlay(overlay: dict[str, Any]):
    merged: dict[str, Any] = {}
    sources: dict[str, str] = {}
    merge_into(merged, overlay, "owner", sources)
    assert merged == overlay
    assert set(sources) == set(flatten(overlay))
