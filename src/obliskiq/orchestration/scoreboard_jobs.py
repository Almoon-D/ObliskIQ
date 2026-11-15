"""The scoreboard's place in the service (MASTER_PLAN §5.19): after each day's digest it grades
every layer and runs the pattern lifecycle; the first digest of an ISO week writes and sends the
weekly report; the first digest of a month runs the month before it (the looks and the
allocation) and writes and sends the monthly report. Each is a run record of its own, so a week or
a month is reported once, and a run that fails is retried at the next digest.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path

from ..execution.modes import Modes
from ..reporting import scoreboard as report
from ..reporting.reporter import Reporter
from ..scoreboard.promotion import MONTHLY_REPORT_COMMAND
from ..scoreboard.run import Changes
from ..scoreboard.service import SCOREBOARD, Scoreboard, previous_month
from ..store import runs as run_repo
from ..store.worker import StoreWorker
from .runlog import DEGRADED, OK, SKIPPED, RunRecord, StepOutcome

DAILY = "scoreboard:daily"
WEEKLY = "scoreboard:weekly"
MONTHLY = MONTHLY_REPORT_COMMAND
#: The directory under the data directory the reports are written to.
REPORTS_DIR = "reports"
WEEK_DAYS = 7


def write_report(data_dir: Path, kind: str, as_of: date, text: str) -> Path:
    """Write a report under the data directory's ``reports/`` and return its path."""
    folder = data_dir / REPORTS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"scoreboard-{kind}-{as_of.isoformat()}.md"
    path.write_text(text, encoding="utf-8")
    return path


def _judge(name: str) -> Callable[[Changes | str], StepOutcome]:
    def judge(found: Changes | str) -> StepOutcome:
        if isinstance(found, str):
            return StepOutcome(name, SKIPPED, found)
        return StepOutcome(
            name,
            OK,
            f"{len(found.events)} events, {len(found.looks)} looks, {len(found.shares)} shares",
        )

    return judge


def _judge_report(name: str) -> Callable[[bool], StepOutcome]:
    def judge(sent: bool) -> StepOutcome:
        return StepOutcome(
            name, OK if sent else DEGRADED, "sent" if sent else "the summary could not be sent"
        )

    return judge


class ScoreboardJobs:
    """The scoreboard's daily, weekly and monthly runs after the digest."""

    def __init__(
        self,
        board: Scoreboard,
        *,
        worker: StoreWorker,
        modes: Modes,
        reporter: Reporter,
        data_dir: Path,
        now: Callable[[], datetime],
    ) -> None:
        self._board = board
        self._worker = worker
        self._modes = modes
        self._reporter = reporter
        self._data_dir = data_dir
        self._now = now

    async def _done(self, command: str, since: date) -> bool:
        runs = await self._worker.run(run_repo.runs_since, since=since)
        return any(r.command == command and r.status in (OK, DEGRADED) for r in runs)

    async def after_digest(self, session_date: date) -> None:
        await self.daily(session_date)
        monday = session_date - timedelta(days=session_date.weekday())
        if not await self._done(WEEKLY, monday):
            await self.weekly(session_date)
        if not await self._done(MONTHLY, session_date.replace(day=1)):
            await self.monthly(session_date)

    async def daily(self, session_date: date) -> str:
        record = RunRecord(
            self._worker, command=DAILY, session_date=session_date, steps=("grade",), now=self._now
        )
        await record.begin()

        async def grade() -> Changes | str:
            changes, alerts = await self._board.daily(session_date)
            for alert in alerts:
                self._reporter.push(alert, SCOREBOARD)
            await self._reporter.flush()
            return changes

        await record.step("grade", grade, _judge("grade"))
        return await record.finish()

    async def weekly(self, session_date: date) -> str:
        record = RunRecord(
            self._worker,
            command=WEEKLY,
            session_date=session_date,
            steps=("report",),
            now=self._now,
        )
        await record.begin()

        async def send() -> bool:
            inputs = await self._board.inputs(
                mode=await self._modes.current(),
                since=session_date - timedelta(days=WEEK_DAYS),
                as_of=session_date,
                monthly=False,
            )
            write_report(self._data_dir, "weekly", session_date, report.weekly(inputs))
            return await self._reporter.report(*report.summary(inputs, kind="weekly"))

        await record.step("report", send, _judge_report("report"))
        return await record.finish()

    async def monthly(self, session_date: date) -> str:
        record = RunRecord(
            self._worker,
            command=MONTHLY,
            session_date=session_date,
            steps=("month_end", "report"),
            now=self._now,
        )
        await record.begin()

        async def month_end() -> Changes | str:
            changes, alerts = await self._board.month_end(previous_month(session_date))
            for alert in alerts:
                self._reporter.push(alert, SCOREBOARD)
            await self._reporter.flush()
            return changes

        async def send() -> bool:
            first = (session_date.replace(day=1) - timedelta(days=1)).replace(day=1)
            inputs = await self._board.inputs(
                mode=await self._modes.current(), since=first, as_of=session_date, monthly=True
            )
            write_report(self._data_dir, "monthly", session_date, report.monthly(inputs))
            return await self._reporter.report(*report.summary(inputs, kind="monthly"))

        await record.step("month_end", month_end, _judge("month_end"))
        await record.step("report", send, _judge_report("report"))
        return await record.finish()
