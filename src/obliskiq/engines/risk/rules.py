"""The equity side's two entry rules: averaging down and the pre-earnings decision (lite's risk
engine and its ``equity_risk`` keys).

**Averaging down** needs a logged re-underwrite citing a **new, dated** catalyst that did not
exist at entry and has not yet passed, while ``equity_risk.allow_averaging_down`` is false: a
falling price is not a catalyst, and an undated one is a hope with a timestamp. A catalyst that
passed without the move is an invalidation, not a reason to buy more.

**Earnings inside the hold window** get an explicit decision, never silent exposure
(``equity_risk.pre_earnings_rule``): ``hold`` carries the gap, ``flag_and_size`` flags it and
sizes for the gap distribution across earnings (DECISIONS D67, Session 17), ``force_reduce``
reduces before the report. Above ``pre_earnings_range_decile_trigger`` of its long-run range the
position needs one of reduce, hedge or a stated reason to carry the full gap. Earnings dates are
the collected ones (``data/events/earnings.py``): unofficial and possibly estimated, so a dated
report is a reason to decide, and an absent one is reported as unknown, never as clear.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from ...config.schema import ObliskConfig


@dataclass(frozen=True, slots=True)
class Reunderwrite:
    """A logged re-underwrite of a position: the catalyst it cites and when it is due."""

    logged_on: date
    catalyst: str
    catalyst_date: date | None


@dataclass(frozen=True, slots=True)
class AveragingDown:
    allowed: bool
    reason: str
    catalyst_dates: tuple[date, ...] = ()


class EarningsAction(StrEnum):
    """What the pre-earnings rule asks for."""

    NONE = "none"
    HOLD = "hold"
    FLAG_AND_SIZE = "flag_and_size"
    DECIDE = "decide"
    FORCE_REDUCE = "force_reduce"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PreEarnings:
    action: EarningsAction
    reason: str
    report_on: date | None = None


class EntryRules:
    """Averaging down and the pre-earnings rule, bound to configuration once."""

    def __init__(self, settings: ObliskConfig) -> None:
        risk = settings.equity_risk
        self._allow_averaging_down = risk.allow_averaging_down
        self._rule = risk.pre_earnings_rule
        self._trigger = risk.pre_earnings_range_decile_trigger

    def averaging_down(
        self, *, opened_on: date, reunderwrites: Sequence[Reunderwrite], as_of: date
    ) -> AveragingDown:
        """May a losing position be added to on ``as_of``?"""
        if self._allow_averaging_down:
            return AveragingDown(True, "equity_risk.allow_averaging_down is true")
        dated = tuple(
            entry.catalyst_date
            for entry in reunderwrites
            if entry.catalyst_date is not None and entry.catalyst.strip()
        )
        new = [
            entry
            for entry in reunderwrites
            if entry.catalyst_date is not None
            and entry.catalyst.strip()
            and entry.catalyst_date > opened_on
            and entry.logged_on <= as_of
            and entry.catalyst_date >= as_of
        ]
        if new:
            return AveragingDown(
                True,
                f"a re-underwrite logged on {new[0].logged_on} cites a new catalyst dated "
                f"{new[0].catalyst_date}, still ahead of {as_of}: {new[0].catalyst}",
                dated,
            )
        return AveragingDown(
            False,
            "averaging down is refused while equity_risk.allow_averaging_down is false: it needs "
            "a logged re-underwrite citing a new, dated catalyst that did not exist at entry and "
            "has not passed. A falling price is not a catalyst",
            dated,
        )

    def pre_earnings(
        self,
        *,
        as_of: date,
        hold_until: date,
        reports: Sequence[date] | None,
        entry_range_pctile: float | None,
    ) -> PreEarnings:
        """The decision a holding needs for a report inside ``as_of``..``hold_until``.
        ``reports`` ``None`` means the dates were never collected (a share's dates are unknown,
        which is not the same as none)."""
        if reports is None:
            return PreEarnings(
                EarningsAction.UNKNOWN,
                "no earnings dates are collected for the holding, so whether a report falls in "
                "its hold window is unknown",
            )
        inside = sorted(day for day in reports if as_of <= day <= hold_until)
        if not inside:
            return PreEarnings(EarningsAction.NONE, f"no report falls before {hold_until}")
        report = inside[0]
        if self._rule == "force_reduce":
            return PreEarnings(
                EarningsAction.FORCE_REDUCE,
                f"a report on {report} falls in the hold window: equity_risk.pre_earnings_rule "
                "reduces before it",
                report,
            )
        if self._rule == "hold":
            return PreEarnings(
                EarningsAction.HOLD,
                f"a report on {report} falls in the hold window; the rule carries the gap",
                report,
            )
        if entry_range_pctile is not None and entry_range_pctile >= self._trigger:
            return PreEarnings(
                EarningsAction.DECIDE,
                f"a report on {report} falls in the hold window and the position sits at "
                f"{entry_range_pctile:.0%} of its long-run range, at or above the "
                f"{self._trigger:.0%} trigger: reduce, hedge, or state the reason for carrying "
                "the full gap",
                report,
            )
        untested = (
            "; where the position sits in its range is not measured, so the decision trigger "
            "is untested"
            if entry_range_pctile is None
            else ""
        )
        return PreEarnings(
            EarningsAction.FLAG_AND_SIZE,
            f"a report on {report} falls in the hold window: flagged, and sized for the gap "
            f"distribution across earnings{untested}",
            report,
        )
