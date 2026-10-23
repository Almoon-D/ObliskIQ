"""The vocabulary every measurement shares: the fifteen indicator families, what each reads, the
units a leaf is quoted in, and the reason codes a leaf that is not ``ok`` is grouped on.

Ported from lite's ``compute/indicators.py`` (its constants). A family is one of SKILL.md's
fifteen; it emits one or more leaves, because "returns: 5d, 21d, 63d, YTD, 12m, local and base"
is one indicator in the mandate and ten numbers in the store.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class Family(StrEnum):
    """The fifteen families, in SKILL.md's order. Exactly fifteen, and a test says so."""

    RETURNS = "returns"
    DISTANCE_FROM_HIGH = "distance_from_high"
    DISTANCE_FROM_LOW = "distance_from_low"
    DRAWDOWN = "drawdown"
    REALISED_VOL = "realised_vol"
    RV_TERM_STRUCTURE = "rv_term_structure"
    IMPLIED_VOL = "implied_vol"
    IV_PERCENTILE = "iv_percentile"
    IV_HV_SPREAD = "iv_hv_spread"
    CORRELATION = "correlation"
    CURRENCY_RETURN = "currency_return"
    POLICY_RATE = "policy_rate"
    YIELD_CURVE = "yield_curve"
    TURNOVER = "turnover"
    POSITIONING = "positioning"


class Input(StrEnum):
    """The stores a family reads. Freshness belongs to the input that was read, not the cell."""

    DAILY_BARS = "daily_bars"
    OPTION_CHAIN = "option_chain"
    UNDERLYING_SNAPSHOT = "underlying_snapshot"
    MACRO = "macro"


#: Family -> what it needs, as requirement GROUPS: every group must be satisfied, and within a
#: group any one source will do. A family is as fresh as its worst group, and a group as fresh as
#: its best member: ``implied_vol`` reads the chain and falls back to the snapshot, so either being
#: current is enough; ``iv_hv_spread`` needs an implied vol from one of those AND a realised vol
#: from bars, so an old chain makes the spread indicative against today's bars.
FAMILY_INPUTS: Final[Mapping[Family, tuple[tuple[Input, ...], ...]]] = MappingProxyType(
    {
        Family.RETURNS: ((Input.DAILY_BARS,),),
        Family.DISTANCE_FROM_HIGH: ((Input.DAILY_BARS,),),
        Family.DISTANCE_FROM_LOW: ((Input.DAILY_BARS,),),
        Family.DRAWDOWN: ((Input.DAILY_BARS,),),
        Family.REALISED_VOL: ((Input.DAILY_BARS,),),
        Family.RV_TERM_STRUCTURE: ((Input.DAILY_BARS,),),
        Family.IMPLIED_VOL: ((Input.OPTION_CHAIN, Input.UNDERLYING_SNAPSHOT),),
        Family.IV_PERCENTILE: ((Input.UNDERLYING_SNAPSHOT,),),
        Family.IV_HV_SPREAD: (
            (Input.OPTION_CHAIN, Input.UNDERLYING_SNAPSHOT),
            (Input.DAILY_BARS,),
        ),
        # Own bars against the benchmark's bars: one source, read twice.
        Family.CORRELATION: ((Input.DAILY_BARS,),),
        Family.CURRENCY_RETURN: ((Input.MACRO,),),
        Family.POLICY_RATE: ((Input.MACRO,),),
        Family.YIELD_CURVE: ((Input.MACRO,),),
        Family.TURNOVER: ((Input.DAILY_BARS,),),
        # CFTC reports are a published weekly series, dated by release like the macro store.
        Family.POSITIONING: ((Input.MACRO,),),
    }
)

#: Families that read the market's declarations and the published series and nothing else, and so
#: compute for a market no instrument measures. Derived, so a family that gains a bar input leaves.
MACRO_ONLY_FAMILIES: Final = frozenset(
    family
    for family, groups in FAMILY_INPUTS.items()
    if groups and all(group == (Input.MACRO,) for group in groups)
)


class Units(StrEnum):
    DECIMAL = "decimal"
    PERCENTILE = "percentile"
    RATIO = "ratio"
    INDEX = "index"
    SIGMA = "sigma"


class Reason(StrEnum):
    """Why a leaf is absent, as a short groupable code; the leaf's reason names the specifics."""

    NO_INSTRUMENT = "no_instrument_mapped"
    NOT_SEEDED = "instrument_not_seeded"
    SHORT_SAMPLE = "sample_too_short"
    SERIES_ABSENT = "macro_series_absent"
    NO_FX = "fx_series_absent"
    NO_CHAIN = "no_option_chain"
    NO_IV_PERCENTILE = "iv_percentile_unavailable"
    NO_BENCHMARK = "no_benchmark_series"
    NO_DISPERSION = "no_dispersion"
    NO_VOLUME = "no_volume_history"
    NO_POSITIONING = "no_futures_contract"
    MIXED_UNITS = "mixed_units_in_window"
    MIXED_FREQUENCY = "mixed_frequency"
    UNITS_MISMATCH = "units_not_as_declared"
    STALE = "inputs_stale"
    NON_FINITE = "non_finite_value"
    NO_BAR_RANGE = "no_high_low_range"
    OPEN_RESTATED = "open_restates_previous_close"
    NO_OPTIONS = "options_not_available"
    SERIES_UNDECLARED = "macro_series_undeclared"
    NO_FX_DECLARED = "fx_series_undeclared"
    IS_BENCHMARK = "cell_is_the_benchmark"
    INDEX_HAS_NO_VOLUME = "index_carries_no_volume"
    CELL_UNMAPPED = "board_cell_unmapped"
    NO_CURVE = "no_futures_curve"
    NO_FORECAST = "no_forecast"
    NO_EARNINGS = "no_earnings_reported"
    NO_EARNINGS_DATES = "earnings_dates_absent"


#: The structural codes: each says no collector could ever fill the leaf, and each is declared
#: somewhere a person wrote it rather than inferred from an absence. A coverage report leaves
#: them out of its denominator, and only these make a leaf ``inapplicable``.
INAPPLICABLE_CODES: Final = frozenset(
    {
        Reason.NO_INSTRUMENT,
        Reason.NO_OPTIONS,
        Reason.SERIES_UNDECLARED,
        Reason.NO_FX_DECLARED,
        Reason.NO_POSITIONING,
        Reason.IS_BENCHMARK,
        Reason.INDEX_HAS_NO_VOLUME,
        Reason.CELL_UNMAPPED,
        Reason.NO_EARNINGS,
    }
)

#: A leaf absent with no code is a bug in the family that produced it; the histogram names it.
UNCLASSIFIED: Final = "unclassified"
