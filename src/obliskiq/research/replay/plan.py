"""What the replay may do, from its locked pre-registration and the configuration (H15).

**The models.** A segment is replayed only if each of its models is registered and configured
alike: the same provider id, and a documented training cutoff (``training_cutoff`` and its
``cutoff_source`` in ``config/llm.yaml``) equal to the registered one. A model whose cutoff is not
documented, or whose id is an alias a provider may refresh (``-latest``, a preview or an
experimental id), is ``no_cutoff`` and refused.

**The dates.** The cutoff day is the last day of a segment's cutoff month (the later of its
models'). The sheets after it are the Mondays from the first one at least ``margin_days`` after
the cutoff day, as many as registered; the leak control is as many Mondays ending at the last
one at least ``margin_days`` before the cutoff month's first day. Both are recomputed here and
must be the registered ones. Any other date is refused: one between the windows is inside the
margin, and one before the leak control is a replay before the cutoff that no rule allows.

**The settings.** A segment runs on the forward configuration, with the sheet's registered
sections in its hash (a replay segment is never the forward one) and a budget that is the
replay's: its whole cap, for the one segment running.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Final, Literal

from ...config.schema import ObliskConfig
from ...errors import ReplayRefusedError
from ..prereg_replay import AnalystReplaySpec, ReplaySegmentSpec
from ..prereg_replay import WindowSpec as Window

#: Ids a provider may point at another model without notice.
ALIAS: Final = re.compile(r"(-latest$|preview|-exp\b|experimental)")
WEEK: Final = timedelta(days=7)
MONDAY: Final = 0

type Side = Literal["after", "before"]


def cutoff_day(month: str) -> date:
    """The last day of a ``YYYY-MM`` month."""
    year, number = (int(part) for part in month.split("-"))
    return date(year, number, calendar.monthrange(year, number)[1])


def _monday_on_or_after(day: date) -> date:
    return day + timedelta(days=(MONDAY - day.weekday()) % 7)


def _monday_on_or_before(day: date) -> date:
    return day - timedelta(days=(day.weekday() - MONDAY) % 7)


@dataclass(frozen=True, slots=True)
class Dates:
    """A segment's sheet dates: after the cutoff, and the leak control before it."""

    cutoff: date
    after: tuple[date, ...]
    before: tuple[date, ...]

    def side(self, day: date) -> Side:
        """Which window ``day`` is in; any other date is refused."""
        if day in self.after:
            return "after"
        if day in self.before:
            return "before"
        if self.before[-1] < day < self.after[0]:
            raise ReplayRefusedError(
                f"{day.isoformat()} is inside the margin around the cutoff "
                f"{self.cutoff.isoformat()} (between {self.before[-1].isoformat()} and "
                f"{self.after[0].isoformat()}): refused"
            )
        raise ReplayRefusedError(
            f"{day.isoformat()} is not a registered sheet date (Mondays {self.before[0]} to "
            f"{self.before[-1]} and {self.after[0]} to {self.after[-1]}): refused"
        )

    @property
    def all(self) -> tuple[date, ...]:
        return self.before + self.after


def _window(first: date, sheets: int) -> tuple[date, ...]:
    return tuple(first + WEEK * n for n in range(sheets))


def _check(name: str, found: tuple[date, ...], registered: Window) -> None:
    if (found[0], found[-1], len(found)) != (registered.first, registered.last, registered.sheets):
        raise ReplayRefusedError(
            f"the {name} window the rule gives ({found[0]} to {found[-1]}, {len(found)} sheets) "
            f"is not the registered one ({registered.first} to {registered.last}, "
            f"{registered.sheets} sheets)"
        )


def sheet_dates(spec: AnalystReplaySpec, segment: ReplaySegmentSpec) -> Dates:
    """The segment's sheet dates by the registered rule, checked against the registered windows."""
    month = spec.segment_cutoff(segment)
    cutoff = cutoff_day(month)
    margin = timedelta(days=spec.dates.margin_days)
    after = _window(_monday_on_or_after(cutoff + margin), spec.dates.after.sheets)
    month_start = cutoff.replace(day=1)
    last_before = _monday_on_or_before(month_start - margin)
    before = _window(last_before - WEEK * (spec.dates.before.sheets - 1), spec.dates.before.sheets)
    _check("after", after, spec.dates.after)
    _check("before", before, spec.dates.before)
    return Dates(cutoff, after, before)


def model_status(spec: AnalystReplaySpec, settings: ObliskConfig, name: str) -> str | None:
    """``None`` when the model may be replayed; else why not (``no_cutoff``, or a mismatch)."""
    registered = spec.model(name)
    configured = settings.llm.models.get(name)
    if configured is None:
        return f"{name} is not a configured model"
    if configured.model_id != registered.model_id:
        return f"{name} is configured as {configured.model_id}, registered as {registered.model_id}"
    if configured.training_cutoff is None or ALIAS.search(configured.model_id):
        return f"no_cutoff: {configured.model_id} has no documented training cutoff of its own id"
    if (configured.training_cutoff, configured.cutoff_source) != (
        registered.training_cutoff,
        registered.cutoff_source,
    ):
        return (
            f"{name}'s configured cutoff {configured.training_cutoff} ({configured.cutoff_source})"
            f" is not the registered {registered.training_cutoff} ({registered.cutoff_source})"
        )
    return None


def check_segment(
    spec: AnalystReplaySpec, settings: ObliskConfig, segment: ReplaySegmentSpec
) -> None:
    """Refuse a segment whose models or configuration are not the registered ones."""
    configured = settings.llm.analyst.segments.get(segment.name)
    if configured is None:
        raise ReplayRefusedError(f"segment {segment.name} is not configured")
    if (configured.scan_model, configured.dossier_model) != (
        segment.scan_model,
        segment.dossier_model,
    ):
        raise ReplayRefusedError(
            f"segment {segment.name} is configured with {configured.scan_model} and "
            f"{configured.dossier_model}, registered with {segment.scan_model} and "
            f"{segment.dossier_model}"
        )
    for name in (segment.scan_model, segment.dossier_model):
        problem = model_status(spec, settings, name)
        if problem is not None:
            raise ReplayRefusedError(f"segment {segment.name} is refused: {problem}")
    if Decimal(repr(settings.llm.analyst.temperature)) != spec.call.temperature:
        raise ReplayRefusedError(
            f"the analyst's temperature {settings.llm.analyst.temperature} is not the registered "
            f"{spec.call.temperature}"
        )
    if settings.llm.analyst.scan.max_candidates != spec.call.max_candidates:
        raise ReplayRefusedError("the scan's candidates are not the registered number")


def replay_settings(settings: ObliskConfig, segment: str) -> ObliskConfig:
    """The forward configuration with the replay's budget: its cap for the month, all of it the
    running segment's (the run checks the replay's whole spend before each sheet). Only the
    running segment's share is read by its analyst."""
    llm = settings.llm
    analyst = llm.analyst
    segments = dict(analyst.segments)
    segments[segment] = segments[segment].model_copy(update={"budget_share": Decimal(1)})
    return settings.model_copy(
        update={
            "llm": llm.model_copy(
                update={
                    "budget": llm.budget.model_copy(update={"monthly_usd": llm.replay.max_usd}),
                    "analyst": analyst.model_copy(update={"segments": segments}),
                }
            )
        }
    )
