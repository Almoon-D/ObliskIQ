"""The scoreboard as the service and the CLI run it: the daily grading and pattern lifecycle, the
month-end looks and allocation, the promotion gate's verdict, and the reports' inputs.

The rules are the locked pre-registration's (``rules.py``): without them nothing is graded,
promoted, looked at or allocated, and the report says why. Reading the scoreboard never places an
order; its only writes are its own records.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from ..calendars.service import Calendars
from ..config.schema import ObliskConfig
from ..domain.orders import ExecutionMode
from ..domain.tax import TaxYearSummary
from ..errors import CalendarError, ObliskError
from ..reporting.alerts import Alert, Severity
from ..reporting.scoreboard import ReportInputs
from ..research.patterns import REGISTRY_PATH
from ..research.prereg import ForwardBookPrereg, ScoreboardPrereg
from ..store import scoreboard as repo
from ..store.h13 import H13Look, h13_looks
from ..store.worker import StoreWorker
from ..tax.book import TaxBook
from .h13_run import run_h13
from .layers import (
    Activation,
    RegistryLayer,
    Retest,
    no_activation,
    no_retest,
    registry_layers,
)
from .ledger import LedgerBook, grade_ledger, record_segments
from .promotion import Verdict, evaluate
from .rules import (
    H13_PATH,
    LEDGER_PATH,
    RULES_PATH,
    load_locked,
    load_locked_book,
    load_locked_ledger,
)
from .run import (
    Changes,
    GradeBook,
    executions,
    grade_book,
    performance,
    promotion_evidence,
    run_lifecycle,
    run_month,
)
from .tca import analyse

#: The next level of the ladder from each level.
NEXT_LEVEL = {
    ExecutionMode.SHADOW: ExecutionMode.PAPER,
    ExecutionMode.PAPER: ExecutionMode.LIVE_SMALL,
    ExecutionMode.LIVE_SMALL: ExecutionMode.LIVE,
}
#: The alert source of the scoreboard's runs.
SCOREBOARD = "scoreboard"


@dataclass(frozen=True, slots=True)
class Rules:
    """The locked rules: the scoreboard's and H13's."""

    scoreboard: ScoreboardPrereg
    h13: ForwardBookPrereg


def previous_month(day: date) -> str:
    """The calendar month before ``day``'s, as ``YYYY-MM``."""
    return (day.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")


class Scoreboard:
    """The scoreboard over the store, the repository's research files and the calendars."""

    def __init__(
        self,
        settings: ObliskConfig,
        worker: StoreWorker,
        *,
        root: Path | None,
        calendars: Calendars,
        tax: TaxBook | None,
        now: Callable[[], datetime],
        activation: Activation = no_activation,
        retest: Retest = no_retest,
    ) -> None:
        self._settings = settings
        self._worker = worker
        self._root = root
        self._calendars = calendars
        self._tax = tax
        self._now = now
        self._activation = activation
        self._retest = retest

    def rules(self) -> Rules | str:
        """The locked rules, or why they cannot be read."""
        if self._root is None:
            return "no repository holds the research files (an installed wheel)"
        try:
            locked = load_locked(self._root / RULES_PATH)
            h13 = load_locked_book(self._root / H13_PATH)
        except ObliskError as exc:
            return str(exc)
        return Rules(locked, h13)

    def layers(self) -> list[RegistryLayer]:
        return [] if self._root is None else registry_layers(self._root / REGISTRY_PATH)

    async def grades(self, as_of: date) -> GradeBook | str:
        rules = self.rules()
        if isinstance(rules, str):
            return rules
        return await self._worker.run(
            grade_book, self._settings, rules.scoreboard.spec, as_of=as_of
        )

    async def ledger(self, as_of: date) -> LedgerBook | str:
        """The idea ledger graded on ``as_of`` under H14's locked rules, or why it cannot be."""
        if self._root is None:
            return "no repository holds the research files (an installed wheel)"
        try:
            spec = load_locked_ledger(self._root / LEDGER_PATH).spec
        except ObliskError as exc:
            return str(exc)
        return await self._worker.run(
            grade_ledger, self._settings, spec, as_of=as_of, venue_of=self._venue
        )

    async def daily(self, as_of: date) -> tuple[Changes | str, list[Alert]]:
        """Grade every layer and the idea ledger (each segment's calibration is stored for the
        thesis book's slot order), and promote the shadow entries whose rule is met."""
        at = self._now()
        ledger = await self.ledger(as_of)
        if isinstance(ledger, LedgerBook):
            await self._worker.run(record_segments, ledger, at=at)
        rules = self.rules()
        if isinstance(rules, str):
            return rules, []
        spec = rules.scoreboard.spec
        book = await self._worker.run(grade_book, self._settings, spec, as_of=as_of)
        changes = await self._worker.run(
            run_lifecycle, spec, self.layers(), book, activation=self._activation, at=at
        )
        changes.notes.append(
            f"idea ledger not graded: {ledger}" if isinstance(ledger, str) else ledger.summary()
        )
        return changes, self._alerts(changes, at)

    def _venue(self, exchange: str) -> str | None:
        try:
            return self._calendars.venue_for_ibkr(exchange)
        except CalendarError:
            return None

    async def month_end(self, month: str) -> tuple[Changes | str, list[Alert]]:
        """H13's looks, the layers' looks and the allocation of ``month``'s end, once each."""
        rules = self.rules()
        if isinstance(rules, str):
            return rules, []
        at = self._now()
        h13 = await self._worker.run(
            run_h13,
            self._settings,
            rules.h13.spec,
            policy=None if self._tax is None else self._tax.policy,
            venue_of=self._venue,
            month=month,
            at=at,
        )
        changes = await self._worker.run(
            run_month,
            self._settings,
            rules.scoreboard.spec,
            self.layers(),
            month=month,
            h13=rules.h13.spec,
            activation=self._activation,
            retest=self._retest,
            at=at,
        )
        changes.h13 = h13
        return changes, self._alerts(changes, at)

    def _alerts(self, changes: Changes, at: datetime) -> list[Alert]:
        found: list[Alert] = []
        for event in changes.events:
            severity = Severity.CRITICAL if event.event in ("shadow", "freeze") else Severity.INFO
            found.append(
                Alert(
                    f"scoreboard.{event.event}",
                    f"scoreboard.{event.event}:{event.layer}",
                    f"{event.layer}: {event.detail}",
                    severity,
                    at,
                    condition=False,
                )
            )
        for look in changes.h13:
            if not (look.step or look.back):
                continue
            found.append(
                Alert(
                    "scoreboard.h13",
                    f"scoreboard.h13:{look.record}:{look.group}",
                    f"H13 {look.record} {look.group}: {look.detail}",
                    Severity.CRITICAL if look.back else Severity.INFO,
                    at,
                    condition=False,
                )
            )
        for share in changes.shares:
            found.append(
                Alert(
                    "scoreboard.allocation",
                    f"scoreboard.allocation:{share.layer}",
                    f"{share.layer} now has {share.share:.2%} of investable capital on the live "
                    f"account: {share.reason}",
                    Severity.INFO,
                    at,
                    condition=False,
                )
            )
        return found

    def sessions(self, first: date, last: date) -> list[date]:
        """The digest venue's trading days from ``first`` to ``last``."""
        venue = self._settings.runtime.digest.venue
        days: list[date] = []
        day = first
        while day <= last:
            if self._calendars.session_bounds(venue, day) is not None:
                days.append(day)
            day += timedelta(days=1)
        return days

    async def verdict(
        self, mode: ExecutionMode, target: ExecutionMode | None = None
    ) -> Verdict | None:
        """The promotion gate's verdict on ``target`` (the next level by default); ``None`` at the
        top of the ladder."""
        goal = target or NEXT_LEVEL.get(mode)
        if goal is None:
            return None
        evidence = await self._worker.run(
            promotion_evidence,
            self._settings,
            mode=mode,
            now=self._now(),
            sessions=self.sessions,
        )
        return evaluate(goal, evidence, self._settings.promotion)

    async def _tax_summary(self, year: int) -> TaxYearSummary | str:
        if self._tax is None:
            return "no tax book"
        try:
            engine = await self._tax.engine()
        except ObliskError as exc:
            return str(exc)
        settled = {s.tax_year: s for s in engine.ledger.settled()}
        return settled.get(year) or engine.ledger.provisional(year)

    async def inputs(
        self, *, mode: ExecutionMode, since: date, as_of: date, monthly: bool
    ) -> ReportInputs:
        """Everything a weekly or monthly report shows."""
        grades = await self.grades(as_of)
        view = await self._worker.run(performance, self._settings, since=since, as_of=as_of)
        measured = await self._worker.run(executions, self._settings, mode=mode, since=None)
        looks: Sequence[repo.Look] = []
        h13: Sequence[H13Look] = []
        allocation: Sequence[repo.AllocationEvent] = []
        if monthly:
            month = as_of.strftime("%Y-%m")
            looks = [
                look
                for look in await self._worker.run(repo.looks)
                if look.month in (month, previous_month(as_of))
            ]
            h13 = [
                look
                for look in await self._worker.run(h13_looks)
                if look.month in (month, previous_month(as_of))
            ]
            allocation = [
                event
                for event in await self._worker.run(repo.allocation_events)
                if event.at.date() >= since
            ]
        shares: dict[str, Decimal] = await self._worker.run(repo.current_shares)
        return ReportInputs(
            as_of=as_of,
            mode=mode,
            grades=None if isinstance(grades, str) else grades,
            grades_missing=grades if isinstance(grades, str) else None,
            performance=view,
            tca=analyse(measured),
            promotion=await self.verdict(mode),
            looks=looks,
            allocation=allocation,
            shares=shares,
            tax=await self._tax_summary(as_of.year) if monthly else None,
            notes=(),
            h13=h13,
            ledger=await self.ledger(as_of),
        )
