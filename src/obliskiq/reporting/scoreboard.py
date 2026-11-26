"""The scoreboard's reports: a weekly one and a monthly one, as markdown, and a short summary of
each for Telegram (MASTER_PLAN §5.19; research/preregistrations/scoreboard.md).

A report states every layer's record with its count of graded calls beside its effective
observations, its verdict only once its minimum is reached, calibration, the weekly panel, the
account's growth beside the references and the owner's aspiration (a distance, never a forecast,
D153), each sleeve's profit, transaction costs and the promotion gate. The monthly one adds the
monitoring looks, the capital each layer was given and the year's realised tax. A part that could
not be computed says so: an empty report must never read as a healthy one.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..domain.orders import ExecutionMode
from ..domain.tax import TaxYearSummary
from ..quant.inference import MeanEstimate
from ..scoreboard.calibration import CalibrationState
from ..scoreboard.grading import PanelRecord, Record
from ..scoreboard.ledger import LedgerBook
from ..scoreboard.performance import Series
from ..scoreboard.promotion import Verdict
from ..scoreboard.run import GradeBook, PerformanceView
from ..scoreboard.tca import TcaReport
from ..store.h13 import H13Look
from ..store.scoreboard import AllocationEvent, Look
from .bar import BAR_GROWTH
from .ledger import ledger_lines

#: The owner's aspiration, a year (DECISIONS D153): shown as a distance, never a forecast.
ASPIRATION = BAR_GROWTH
#: Lines of the Telegram summary's record list before "and N more".
SUMMARY_ROWS = 6


@dataclass(frozen=True, slots=True)
class ReportInputs:
    """Everything a report shows, gathered on ``as_of``."""

    as_of: date
    mode: ExecutionMode
    grades: GradeBook | None
    grades_missing: str | None
    performance: PerformanceView
    tca: TcaReport
    promotion: Verdict | None
    looks: Sequence[Look]
    allocation: Sequence[AllocationEvent]
    shares: dict[str, Decimal]
    tax: TaxYearSummary | str | None
    notes: Sequence[str]
    #: H13's looks taken this month (the thesis book's record against its twin).
    h13: Sequence[H13Look] = ()
    #: The idea ledger (H14), or why it was not graded.
    ledger: LedgerBook | str = "not read"


def _pct(value: float | Decimal | None, places: int = 2) -> str:
    return "n/a" if value is None else f"{float(value):+.{places}%}"


def _number(value: float | None, places: int) -> str:
    return "n/a" if value is None else f"{value:.{places}f}"


def _estimate(found: MeanEstimate) -> str:
    if found.mean is None or found.ci is None:
        return "n/a"
    return f"{found.mean:+.4f} [{found.ci[0]:+.4f}, {found.ci[1]:+.4f}]"


def _verdict(item: Record) -> str:
    if item.decision is None:
        return "pending (below the minimum: nothing concluded)"
    labels = (
        f" ({', '.join(label.value for label in item.decision.labels)})"
        if item.decision.labels
        else ""
    )
    return f"{item.decision.outcome.value}{labels}"


def _records(grades: GradeBook) -> list[str]:
    if not grades.records:
        return ["No layer has a call yet."]
    lines = [
        "| Layer | Calls | Graded | Pending | Unmarked | Cohorts | Effective | Per 28 days "
        "| Verdict |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name, item in grades.records.items():
        lines.append(
            f"| {name} | {item.calls} | {item.graded} | {item.pending} | {item.missing} | "
            f"{item.cohorts} | {item.effective:.1f} | {_estimate(item.estimate)} | "
            f"{_verdict(item)} |"
        )
    return lines


def _calibration(states: dict[str, CalibrationState]) -> list[str]:
    if not states:
        return ["No layer states probabilities: none sizes from them."]
    return [
        f"- {name}: {'calibrated' if s.calibrated else 'uncalibrated'}, shrink {s.shrink:.2f}. "
        f"{s.reason}."
        for name, s in states.items()
    ]


def _panel(panel: PanelRecord | None) -> list[str]:
    if panel is None:
        return ["No forecast of the weekly panel has been graded."]
    verdict = (
        "pending (below the minimum)" if panel.decision is None else panel.decision.outcome.value
    )
    score = panel.probabilities
    return [
        f"- {panel.names} graded forecasts in {panel.cohorts} cohorts: {panel.effective:.1f} "
        "effective observations (forecasts in one week share the market's move).",
        f"- Mean rank IC {_estimate(panel.ic)}; verdict {verdict}.",
        f"- 12-1 momentum's IC {_estimate(panel.baseline_ic)}; the model's increment "
        f"{_estimate(panel.incremental_ic)}; top less bottom decile {_estimate(panel.spread)}.",
        f"- Brier {_number(score.brier, 4)} against {score.base} {_number(score.reference, 4)}: "
        f"skill {_number(score.skill, 3)}.",
    ]


def _series(item: Series) -> str:
    growth = item.growth
    note = f" ({item.reason})" if item.reason else ""
    return f"{item.name}: {_pct(growth)}{note}"


def performance_lines(view: PerformanceView) -> list[str]:
    lines = [f"From {view.since.isoformat()} to {view.as_of.isoformat()}, time-weighted:"]
    lines.append(f"- {_series(view.account)}")
    lines += [f"- {_series(reference)}" for reference in view.references]
    account = view.account.growth
    days = (
        (view.account.points[-1].on - view.account.points[0].on).days
        if len(view.account.points) > 1
        else 0
    )
    if account is not None and days > 0:
        aspired = (1 + ASPIRATION) ** Decimal(days / 365) - 1
        lines.append(
            f"- the owner's aspiration of {ASPIRATION:.0%} a year would be {_pct(aspired)} over "
            f"these days: {_pct(account - aspired)} from it (a distance, not a forecast)"
        )
    if isinstance(view.sleeves, str):
        lines.append(f"- sleeves: not computed ({view.sleeves})")
    else:
        for sleeve in view.sleeves:
            unmarked = f", {len(sleeve.unmarked)} unmarked" if sleeve.unmarked else ""
            lines.append(
                f"- {sleeve.sleeve.value}: realised {sleeve.realised:.2f}, unrealised "
                f"{sleeve.unrealised:.2f}, held {sleeve.held_value:.2f}, commissions "
                f"{sleeve.commissions:.2f}{unmarked}"
            )
    return lines


def _tca(report: TcaReport) -> list[str]:
    lines = [
        f"{report.fills} broker fills measured; realised less modelled "
        f"{_pct(report.excess, 3)} a leg"
        + (
            ""
            if report.interval is None
            else f" [{_pct(report.interval[0], 3)}, {_pct(report.interval[1], 3)}]"
        )
    ]
    lines += [
        f"- {g.venue} {g.order_type} ({g.quote_status}): {g.fills} fills, realised "
        f"{_pct(g.realised, 3)}, modelled {_pct(g.modelled, 3)}"
        for g in report.groups
    ]
    lines += [f"- not measured: {count} ({why})" for why, count in report.unmeasured.items()]
    return lines


def _promotion(verdict: Verdict | None) -> list[str]:
    if verdict is None:
        return ["The account is at the top of the ladder."]
    lines = [f"Next level {verdict.summary()}."]
    for criterion in verdict.criteria:
        mark = {True: "met", False: "not met", None: "unmeasured"}[criterion.passed]
        lines.append(f"- {criterion.name}: {mark} ({criterion.detail})")
    return lines


def _capital(inputs: ReportInputs) -> list[str]:
    lines = [
        f"- {layer}: {share:.2%} of investable capital on the live account"
        for layer, share in sorted(inputs.shares.items())
    ] or ["- no layer has a share on the live account: the reserve holds it all"]
    lines += [
        f"- {e.at.date().isoformat()} {e.layer} -> {e.share:.2%}: {e.reason}"
        for e in inputs.allocation
    ]
    return lines


def _looks(looks: Sequence[Look]) -> list[str]:
    if not looks:
        return ["No active layer had a look this month."]
    return [
        f"- {look.layer} {look.boundary} ({look.month}): "
        + ("not evaluable" if not look.evaluable else ("BREACH" if look.breached else "inside"))
        + f" — {look.detail}"
        for look in looks
    ]


def _h13(looks: Sequence[H13Look]) -> list[str]:
    if not looks:
        return ["No look of H13 fell this month."]
    lines = [f"{len(looks)} records and groups were looked at; the groups are not adjusted for one "
             "another."]  # fmt: skip
    for look in looks:
        verdict = (
            "not evaluable"
            if not look.evaluable
            else "STEP"
            if look.step
            else "BACK TO SHADOW"
            if look.back
            else "no change"
        )
        lines.append(f"- {look.record} {look.group}, look {look.look}: {verdict} — {look.detail}")
    return lines


def _tax(tax: TaxYearSummary | str | None) -> list[str]:
    if tax is None:
        return ["Not computed."]
    if isinstance(tax, str):
        return [f"Not computed: {tax}."]
    return [
        f"{tax.tax_year}: gains {tax.gains:.2f}, income {tax.income:.2f}, tax due "
        f"{tax.tax_due:.2f} (paid {tax.payment_date.isoformat()})"
        + ("" if tax.rate_table_covers_year else "; the rate table does not cover the year")
    ]


def _grades(inputs: ReportInputs) -> list[str]:
    if inputs.grades is None:
        return [f"Not graded: {inputs.grades_missing}."]
    return [
        "## Forward records",
        "",
        *_records(inputs.grades),
        "",
        "## Calibration",
        "",
        *_calibration(inputs.grades.calibration),
        "",
        "## Weekly forecast panel",
        "",
        *_panel(inputs.grades.panel),
    ]


def weekly(inputs: ReportInputs) -> str:
    """The weekly report, as markdown."""
    lines = [
        f"# Scoreboard, week to {inputs.as_of.isoformat()}",
        "",
        f"Mode {inputs.mode.value}. Not investment advice; a forward record decides nothing before "
        "its pre-registered minimum.",
        "",
        "## Performance",
        "",
        *performance_lines(inputs.performance),
        "",
        *_grades(inputs),
        "",
        "## Idea ledger",
        "",
        *ledger_lines(inputs.ledger),
        "",
        "## Transaction costs",
        "",
        *_tca(inputs.tca),
        "",
        "## Promotion",
        "",
        *_promotion(inputs.promotion),
    ]
    if inputs.notes:
        lines += ["", "## Notes", "", *[f"- {note}" for note in inputs.notes]]
    return "\n".join(lines) + "\n"


def monthly(inputs: ReportInputs) -> str:
    """The monthly report, as markdown: the weekly one with the month's looks, capital and tax."""
    body = weekly(inputs).replace(
        f"# Scoreboard, week to {inputs.as_of.isoformat()}",
        f"# Scoreboard, month to {inputs.as_of.isoformat()}",
        1,
    )
    extra = [
        "## Monitoring",
        "",
        *_looks(inputs.looks),
        "",
        "## The thesis book (H13)",
        "",
        *_h13(inputs.h13),
        "",
        "## Capital",
        "",
        *_capital(inputs),
        "",
        "## Realised tax",
        "",
        *_tax(inputs.tax),
    ]
    return body + "\n" + "\n".join(extra) + "\n"


def summary(inputs: ReportInputs, *, kind: str) -> tuple[str, str]:
    """The Telegram summary: a subject and a few lines."""
    perf = inputs.performance
    lines = [f"Mode {inputs.mode.value}. {_series(perf.account)}."]
    lines += [f"{_series(reference)}." for reference in perf.references]
    if inputs.grades is not None:
        records = list(inputs.grades.records.items())
        for name, item in records[:SUMMARY_ROWS]:
            lines.append(
                f"{name}: {item.graded} graded, {item.effective:.1f} effective, {_verdict(item)}"
            )
        if len(records) > SUMMARY_ROWS:
            lines.append(f"and {len(records) - SUMMARY_ROWS} more layers")
    else:
        lines.append(f"Not graded: {inputs.grades_missing}.")
    if inputs.promotion is not None:
        lines.append(f"Promotion: {inputs.promotion.summary()}.")
    breaches = [look for look in inputs.looks if look.breached]
    if breaches:
        lines.append(f"{len(breaches)} monitoring breaches: see the report.")
    for event in inputs.allocation:
        lines.append(f"{event.layer} -> {event.share:.2%}: {event.reason}")
    return f"ObliskIQ {kind} scoreboard {inputs.as_of.isoformat()}", "\n".join(lines)
