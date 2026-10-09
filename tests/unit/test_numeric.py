"""Tolerant numeric coercion. Ported from ObliskIQ-lite-MCP's tests/test_util.py.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import pytest

from obliskiq.util import safe_float, safe_int


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 0.0),
        ("", 0.0),
        (".", 0.0),  # FRED's missing-observation placeholder
        ("12.5", 12.5),
        (" 12.5 ", 12.5),
        ("1,234.56", 1234.56),  # thousands separators
        ("100", 100.0),  # IBKR returns strikes as strings
        (7, 7.0),
        (7.5, 7.5),
        (True, 1.0),
        ("C228.50", 228.50),  # IBKR prefixes a previous close with C
        ("H10", 10.0),
        ("L9.5", 9.5),
        ("31.42%", 31.42),  # IBKR's percent-suffixed implied vol
        ("%", 0.0),
        ("garbage", 0.0),
    ],
)
def test_safe_float_handles_the_shapes_the_apis_actually_return(raw, expected):
    assert safe_float(raw) == expected


def test_safe_float_can_distinguish_absent_from_zero():
    """Collapsing both to 0.0 would silently turn a missing bid into a real one."""
    assert safe_float(None, None) is None
    assert safe_float("", None) is None
    assert safe_float("garbage", None) is None
    assert safe_float("0", None) == 0.0
    assert safe_float(0, None) == 0.0


def test_safe_int_goes_through_float_so_decimal_strings_parse():
    assert safe_int("1,234.0") == 1234
    assert safe_int("12.9") == 12
    assert safe_int(None, None) is None
    assert safe_int("nope") == 0


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "Infinity", "-inf", "1e999", "-1e999"])
def test_safe_float_treats_a_non_finite_parse_as_absent(raw):
    """JSON admits these literals and Python's float() parses all of them; a non-finite
    parse is "unusable", not a real quote."""
    assert safe_float(raw) == 0.0
    assert safe_float(raw, None) is None


@pytest.mark.parametrize("raw", [float("nan"), float("inf"), float("-inf")])
def test_safe_float_treats_a_non_finite_python_float_as_absent(raw):
    """A value that arrives already decoded takes the numeric fast path and is checked too."""
    assert safe_float(raw, None) is None


def test_safe_float_still_parses_ordinary_scientific_notation():
    """The guard must not confuse a large-but-real magnitude with an overflow."""
    assert safe_float("1e6") == 1_000_000.0
    assert safe_float(1e6) == 1_000_000.0


def test_safe_int_rejects_non_finite_strings():
    assert safe_int("nan", None) is None
    assert safe_int("inf", None) is None
    assert safe_int("1e999", None) is None
