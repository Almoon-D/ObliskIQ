"""The report of a study run: what was read and with which hashes and seeds, whether the data
were adequate, the outcome and its labels, power, the robustness checks, the biases, the tax
rules applied, and what the test cannot conclude (DECISIONS D62, D64).

An exploratory run says so at the top and is never evidence: only a confirmation run of a merged,
locked pre-registration counts.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from typing import Final

from ..util.timeutil import utc_stamp
from .simulate import SimulationResult
from .study import SIMULATION_NOTES, StrategyStats, StudyResult

#: The note a report carries about the universe it measured (D62's "survivorship measured").
SURVIVORSHIP_NOTE: Final = (
    "A universe listed today cannot show members that disappeared before it was listed; the "
    "report measures what the list can show."
)


#: Decimals of a reported statistic, and of a probability or a multiple.
DIGITS: Final = 4
SHORT_DIGITS: Final = 3


def _number(value: float | None, digits: int = DIGITS) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2%}"


def _money(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value:,.2f}"


def _table(header: Iterable[str], rows: Iterable[Iterable[str]]) -> list[str]:
    head = list(header)
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def _strategy_rows(stats: StrategyStats) -> list[str]:
    run: SimulationResult | None = stats.historical
    return [
        stats.name,
        _money(None if run is None else run.liquidation_value),
        _percent(None if run is None else run.log_growth),
        _percent(None if run is None else run.max_drawdown),
        _money(None if run is None else run.costs),
        _money(None if run is None else run.tax_paid),
        "n/a" if run is None else str(run.trades),
        _percent(stats.median_log_growth),
        _number(stats.terminal_wealth_p5, SHORT_DIGITS),
    ]


def render(result: StudyResult) -> str:
    """The report as Markdown."""
    spec = result.prereg.spec
    decision = result.decision
    tests = spec.tests_in_confirmation
    level = 1 - 2 * result.alpha / tests
    stage = "EXPLORATORY — never evidence (DECISIONS D64)" if result.explore else "Confirmation run"
    lines = [f"# {spec.title}", "", f"**{stage}.** Study `{spec.id}` ({spec.kind}).", ""]
    lock = result.lock
    lines += [
        "## Inputs",
        "",
        f"- Pre-registration: `{result.prereg.path.name}`, SHA-256 `{result.prereg.sha256}`",
        f"- Lock: merge commit `{lock.merge_commit}`" if lock else "- Lock: none (exploratory)",
        f"- Run at: {utc_stamp(result.run_at)}",
        f"- Inputs SHA-256: `{result.inputs_sha256}`",
        f"- Bootstrap: {spec.bootstrap.n_boot} paths of {spec.bootstrap.block_months}-month "
        f"blocks, seed {spec.bootstrap.seed}",
        f"- alpha {result.alpha} ({result.alpha_source}), {tests} test(s) in the confirmation "
        f"run; target power {result.target_power} ({result.target_power_source})",
        f"- Horizon: {spec.study.start.isoformat()} to {spec.study.end.isoformat()}, "
        f"{len(result.dates)} rebalance dates; initial capital "
        f"{_money(spec.study.initial_capital)}",
        "",
        "## Outcome",
        "",
        f"**{decision.outcome.value}**"
        + (f" ({', '.join(label.value for label in decision.labels)})" if decision.labels else ""),
        "",
        f"- Primary effect: {spec.primary_metric}, "
        f"{spec.study.candidate.name} minus {spec.study.reference.name}",
        f"- Estimate: {_percent(result.estimate)} a year",
        f"- Interval at level {level:.4f}: "
        + (
            "n/a"
            if decision.interval is None
            else f"[{_percent(decision.interval[0])}, {_percent(decision.interval[1])}]"
        ),
        f"- theta: {_percent(decision.theta)} ({spec.theta_justification})",
        f"- Power to detect theta: {_number(decision.power, SHORT_DIGITS)} (target "
        f"{decision.target_power}); effect detectable at that power: {_percent(result.detectable)}",
        f"- Bootstrap standard error: {_percent(result.se)}",
        "",
        "## What this test cannot conclude",
        "",
        *(f"- {line}" for line in decision.cannot_conclude),
        "- Anything about a period, market or cost regime outside this sample.",
        "",
        "## Adequacy checks",
        "",
        *_table(
            ("Check", "Passed", "Detail"),
            ((c.name, "yes" if c.passed else "**no**", c.detail) for c in decision.checks),
        ),
        "",
        "## Strategies",
        "",
        *_table(
            (
                "Strategy",
                "Liquidation value",
                "Log growth / yr",
                "Max drawdown",
                "Costs",
                "Tax paid",
                "Trades",
                "Median bootstrap growth",
                "P5 terminal wealth multiple",
            ),
            (_strategy_rows(result.candidate), _strategy_rows(result.reference)),
        ),
        "",
    ]
    for stats in (result.candidate, result.reference):
        if stats.breach:
            breaches = ", ".join(f"{float(limit):.0%}: {p:.1%}" for limit, p in stats.breach)
            lines.append(f"- {stats.name}: probability of a drawdown at or past {breaches}")
        run = stats.historical
        if run is not None and run.taxes:
            paid = ", ".join(
                f"{t.tax_year} {_money(t.amount)} on {t.paid_on.isoformat()}" for t in run.taxes
            )
            lines.append(f"- {stats.name}: tax paid {paid}")
    lines += ["", "## Robustness", ""]
    robustness = result.robustness
    if robustness is None:
        lines.append("Not run: the outcome is not evaluable.")
    else:
        lines += _table(
            ("Subperiod", "Estimate", "Sign holds"),
            (
                (
                    f"{p.start.isoformat()} to {p.end.isoformat()}",
                    _percent(p.estimate),
                    "n/a" if p.sign_holds is None else ("yes" if p.sign_holds else "**no**"),
                )
                for p in robustness.subperiods
            ),
        )
        breakeven = robustness.breakeven
        found = (
            f"{breakeven.multiplier:.3f}"
            if breakeven.multiplier is not None
            else (
                f"above {breakeven.bound:g}"
                if breakeven.status == "above_search"
                else "none (below theta even without costs)"
            )
        )
        lines += [
            "",
            f"- Break-even cost multiplier: {found}; declared cost uncertainty "
            f"{robustness.cost_uncertainty}: "
            + ("exceeded" if robustness.breakeven_exceeds_uncertainty else "**not exceeded**"),
            f"- Placebo: {robustness.placebo}",
        ]
    deflated = result.deflated
    lines += [
        "",
        "## Selection across trials",
        "",
        f"- Trials of this study, this one included: {result.trials}",
        (
            "- Deflated Sharpe: n/a"
            if deflated is None
            else f"- Deflated Sharpe: monthly Sharpe {deflated.sharpe:.4f} against "
            f"{deflated.benchmark:.4f} expected from the best of {result.trials} trial(s); "
            f"probability {deflated.probability:.3f}"
        ),
        "",
        "## Data",
        "",
        *_table(
            ("Series", "Vendor", "Point in time", "Downloaded", "SHA-256", "Source"),
            (
                (
                    s.series_id,
                    s.vendor.value,
                    s.point_in_time.value,
                    utc_stamp(s.downloaded_at),
                    f"`{s.content_sha256}`",
                    s.source_url,
                )
                for s in result.series
            ),
        ),
        "",
        f"- Coverage: {_percent(result.coverage)} of rebalance dates fully priced",
    ]
    report = result.survivorship
    lines += [
        f"- Without data: {', '.join(report.without_data) or 'none'}",
        f"- Entering after the start: {', '.join(report.late_entries) or 'none'}",
        f"- Ending before the horizon: {', '.join(report.early_exits) or 'none'}",
        f"- {SURVIVORSHIP_NOTE}",
    ]
    notes = sorted({note for s in result.series for note in s.bias_notes})
    lines += [
        "",
        "## Bias notes",
        "",
        *(f"- {note}" for note in notes),
        *(f"- {note}" for note in SIMULATION_NOTES),
        "",
        "Unverified figures the costs rest on: "
        + (", ".join(f"`{label}`" for label in result.cost_labels) or "none")
        + ".",
        "",
        "## Tax rules applied",
        "",
        *(f"- {rule}" for rule in result.tax_rules),
        "",
        "Not tax advice.",
        "",
    ]
    return "\n".join(lines)


def report_name(result: StudyResult) -> str:
    """``<id>.md`` for the confirmation run (there is one); exploratory runs are stamped."""
    if not result.explore:
        return f"{result.prereg.spec.id}.md"
    stamp = utc_stamp(result.run_at).replace("-", "").replace(":", "")
    return f"{result.prereg.spec.id}-exploration-{stamp}.md"


def write_report(result: StudyResult, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / report_name(result)
    path.write_text(render(result), encoding="utf-8")
    return path
