"""The operational promotion ladder L0 → L3 (MASTER_PLAN §5.19; ``config/defaults/scoreboard.yaml``
``promotion``).

| Level | Entry criteria |
|---|---|
| L0 shadow | the default |
| L1 paper | consecutive trading days of shadow with no unhandled error, clean reconciliation, no stuck intent, every daily reset re-logged unattended |
| L2 live-small | weeks of paper with no rail violation, order success and slippage inside their bounds, the breaker, kill and cash-flow drills passed, on a capped book |
| L3 live | months of live-small, a monthly scoreboard report, the owner's command |

The criteria are operational, not alpha: a pass says the machine ran cleanly, never that it
earns anything. Every criterion is passed, failed or **unmeasured**; an unmeasured one fails the
level, so **promotion never passes on missing data**. Every upgrade is still an owner command
(``obliskiq mode set``); this module only says whether the level's criteria are met.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from ..config.schema_scoreboard import PromotionConfig
from ..domain.orders import ExecutionMode, ModeChange
from ..store.scoreboard import DRILL_KINDS, Drill
from .tca import TcaReport

#: Days in a week.
DAYS_PER_WEEK = 7
#: The run statuses that are an unhandled error.
ERROR_STATUSES = frozenset({"error", "interrupted"})
#: The command of the monthly scoreboard run (``orchestration/jobs.py``).
MONTHLY_REPORT_COMMAND = "scoreboard:monthly"


@dataclass(frozen=True, slots=True)
class Criterion:
    """One entry criterion: ``passed`` is ``None`` when it could not be measured."""

    name: str
    passed: bool | None
    detail: str


@dataclass(frozen=True, slots=True)
class Verdict:
    """Whether ``target``'s criteria are met; an unmeasured criterion fails it."""

    target: ExecutionMode
    criteria: tuple[Criterion, ...]

    @property
    def passed(self) -> bool:
        return bool(self.criteria) and all(c.passed is True for c in self.criteria)

    def summary(self) -> str:
        if self.passed:
            return f"{self.target.value}: every criterion is met"
        failing = [c.name for c in self.criteria if c.passed is not True]
        return f"{self.target.value}: not met ({', '.join(failing)})"


@dataclass(frozen=True, slots=True)
class RunOutcome:
    session_date: date
    command: str
    status: str


@dataclass(frozen=True, slots=True)
class Evidence:
    """What the store says about the current mode's period, read at ``now``."""

    now: datetime
    mode: ExecutionMode
    changes: tuple[ModeChange, ...]
    sessions: tuple[date, ...]
    runs: tuple[RunOutcome, ...]
    reconciliations: tuple[tuple[datetime, str], ...]
    stuck_intents: int
    relogins_recorded: bool
    placed: int
    filled: int
    fills_without_clearance: int
    tca: TcaReport | None
    drills: tuple[Drill, ...]
    capped_book: bool
    hard_breaker_tripped: bool


def mode_since(changes: Sequence[ModeChange], mode: ExecutionMode) -> datetime | None:
    """When the account last entered ``mode`` and has stayed in it since; ``None`` when the audit
    does not show it entering (the initial mode has no change to date it from)."""
    if not changes or changes[-1].to_mode is not mode:
        return None
    return changes[-1].at


def _months_between(first: datetime, last: datetime) -> int:
    months = (last.year - first.year) * 12 + last.month - first.month
    return months - (1 if last.day < first.day else 0)


def _clean_period(evidence: Evidence, since: datetime) -> list[Criterion]:
    days = [d for d in evidence.sessions if d >= since.date()]
    in_period = [r for r in evidence.runs if r.session_date >= since.date()]
    errors = [r for r in in_period if r.status in ERROR_STATUSES]
    reconciled = [(at, status) for at, status in evidence.reconciliations if at >= since]
    dirty = [status for _, status in reconciled if status != "green"]
    return [
        Criterion(
            "no unhandled error",
            None if not in_period else not errors,
            "no run recorded in the period"
            if not in_period
            else f"{len(errors)} of {len(in_period)} runs ended in error or were interrupted",
        ),
        Criterion(
            "clean reconciliation",
            None if not reconciled else not dirty,
            "no reconciliation in the period"
            if not reconciled
            else f"{len(dirty)} of {len(reconciled)} reconciliations were not green",
        ),
        Criterion(
            "no stuck intent",
            evidence.stuck_intents == 0,
            f"{evidence.stuck_intents} intents still working after their expiry",
        ),
        Criterion(
            "a run every trading day",
            None if not days else {r.session_date for r in in_period} >= set(days),
            f"{len({r.session_date for r in in_period} & set(days))} of {len(days)} sessions "
            "had a run",
        ),
    ]


def evaluate(target: ExecutionMode, evidence: Evidence, config: PromotionConfig) -> Verdict:
    """The verdict on an upgrade to ``target`` from the evidence of the current mode."""
    criteria: list[Criterion] = []
    previous = {
        ExecutionMode.PAPER: ExecutionMode.SHADOW,
        ExecutionMode.LIVE_SMALL: ExecutionMode.PAPER,
        ExecutionMode.LIVE: ExecutionMode.LIVE_SMALL,
    }.get(target)
    if previous is None:
        return Verdict(target, (Criterion("a level above shadow", False, "shadow is L0"),))
    if evidence.mode is not previous:
        return Verdict(
            target,
            (
                Criterion(
                    f"the account is in {previous.value}",
                    False,
                    f"it is in {evidence.mode.value}: the ladder is climbed one level at a time",
                ),
            ),
        )
    if evidence.hard_breaker_tripped:
        criteria.append(Criterion("the hard breaker is not latched", False, "it is latched"))
    since = mode_since(evidence.changes, previous)
    if target is ExecutionMode.PAPER:
        days = [] if since is None else [d for d in evidence.sessions if d >= since.date()]
        criteria.append(
            Criterion(
                f"{config.shadow_days} trading days of shadow",
                None if since is None else len(days) >= config.shadow_days,
                "the audit does not date the entry into shadow"
                if since is None
                else f"{len(days)} trading days since {since.date().isoformat()}",
            )
        )
        if since is not None:
            criteria += _clean_period(evidence, since)
        criteria.append(
            Criterion(
                "every daily reset re-logged unattended",
                None if not evidence.relogins_recorded else True,
                "the broker session's re-logins are not recorded in the store"
                if not evidence.relogins_recorded
                else "re-logged every day",
            )
        )
        return Verdict(target, tuple(criteria))
    if target is ExecutionMode.LIVE_SMALL:
        weeks = None if since is None else (evidence.now - since).days // DAYS_PER_WEEK
        criteria.append(
            Criterion(
                f"{config.paper_weeks} weeks of paper",
                None if weeks is None else weeks >= config.paper_weeks,
                "the audit does not date the entry into paper"
                if weeks is None
                else f"{weeks} weeks since {since.date().isoformat() if since else ''}",
            )
        )
        if since is not None:
            criteria += _clean_period(evidence, since)
        criteria.append(
            Criterion(
                "no rail violation",
                evidence.fills_without_clearance == 0,
                f"{evidence.fills_without_clearance} broker fills of an intent with no spent "
                "clearance",
            )
        )
        enough = evidence.placed >= config.min_orders
        rate = Decimal(evidence.filled) / evidence.placed if enough else None
        criteria.append(
            Criterion(
                f"order success at least {config.order_success_min}",
                None if rate is None else rate >= config.order_success_min,
                f"{evidence.filled} of {evidence.placed} orders placed filled"
                + ("" if enough else f" (at least {config.min_orders} needed)"),
            )
        )
        tca = evidence.tca
        criteria.append(
            Criterion(
                "slippage within the cost model's band",
                None
                if tca is None or tca.excess is None or tca.fills < config.min_fills
                else Decimal(str(tca.excess)) <= config.slippage_excess_max,
                "no fill measured"
                if tca is None or tca.excess is None
                else f"realised less modelled {tca.excess:+.4%} a leg over {tca.fills} fills "
                f"(at most {config.slippage_excess_max}, at least {config.min_fills} fills)",
            )
        )
        for kind in DRILL_KINDS:
            mine = [
                d
                for d in evidence.drills
                if d.kind == kind and d.mode is ExecutionMode.PAPER
                and (since is None or d.at >= since)
            ]
            latest = mine[-1] if mine else None
            criteria.append(
                Criterion(
                    f"the {kind.replace('_', '-')} drill",
                    None if latest is None else latest.passed,
                    "never run in paper"
                    if latest is None
                    else f"{'passed' if latest.passed else 'failed'} on "
                    f"{latest.at.date().isoformat()}: {latest.detail}",
                )
            )
        criteria.append(
            Criterion(
                "a capped book",
                evidence.capped_book,
                "capital.mode is cap: live-small trades a book of a stated size"
                if evidence.capped_book
                else "capital.mode is not cap: live-small would manage the whole account",
            )
        )
        return Verdict(target, tuple(criteria))
    months = None if since is None else _months_between(since, evidence.now)
    criteria.append(
        Criterion(
            f"{config.live_small_months} months of live-small",
            None if months is None else months >= config.live_small_months,
            "the audit does not date the entry into live-small"
            if months is None
            else f"{months} months since {since.date().isoformat() if since else ''}",
        )
    )
    reports = [
        r
        for r in evidence.runs
        if r.command == MONTHLY_REPORT_COMMAND
        and r.status == "ok"
        and since is not None
        and r.session_date >= since.date()
    ]
    criteria.append(
        Criterion(
            "a monthly scoreboard report",
            bool(reports),
            f"{len(reports)} monthly reports in live-small",
        )
    )
    if since is not None:
        criteria += _clean_period(evidence, since)
    return Verdict(target, tuple(criteria))


def stuck_cutoff(now: datetime, config: PromotionConfig) -> datetime:
    """An intent still working that expired before this is stuck."""
    return now - timedelta(hours=config.stuck_after_hours)
