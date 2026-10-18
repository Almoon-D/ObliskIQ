"""The report of a study run: what was read and with which hashes and seeds, whether the data
were adequate, each candidate's outcome with its labels and power, the comparisons, the frontier,
the owner's point, every portfolio before and after tax and year by year, the robustness checks,
the block lengths, the structure, forecast and long-history checks, the biases, the tax rules
applied, and what the test cannot conclude (DECISIONS D60, D62, D64, D92).

An exploratory run says so at the top and is never evidence: only a confirmation run of a merged,
locked pre-registration counts.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from collections.abc import Iterable, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Final

from ..util.timeutil import utc_stamp
from .decide import Decision
from .forecast import ForecastCheck
from .long_history import HistoryResult
from .simulate import MONTHS_PER_YEAR, SimulationResult
from .structure import StructureArm, StructureResult
from .study import CandidateTest, StrategyStats, StudyResult, simulation_notes

#: The note a report carries about the universe it measured (D62's "survivorship measured").
SURVIVORSHIP_NOTE: Final = (
    "A universe listed today cannot show members that disappeared before it was listed; the "
    "report measures what the list can show."
)
#: What a portfolio comparison cannot conclude whatever its outcome.
COMPARISON_LIMITS: Final = (
    "That a portfolio will keep its place after the sample: past growth after tax is no promise "
    "of future superiority.",
    "How a portfolio behaves in a crisis unlike the episodes in the sample; the bootstrap "
    "reshuffles the sample's own months and cannot create a new kind of crisis.",
    "Anything about a period, market or cost regime outside this sample.",
)

#: Decimals of a reported statistic, and of a probability or a multiple.
DIGITS: Final = 4
SHORT_DIGITS: Final = 3


def _number(value: float | None, digits: int = DIGITS) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2%}"


def _grid(value: Decimal) -> str:
    """A pre-registered fraction (a limit, a probability, an aspiration) as a percent, with
    the digits it was written with (2.5 % stays 2.5 %)."""
    return f"{value.scaleb(2).normalize():f}%"


def _money(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value:,.2f}"


def _table(header: Iterable[str], rows: Iterable[Iterable[str]]) -> list[str]:
    head = list(header)
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def _outcome(decision: Decision) -> str:
    labels = ", ".join(label.value for label in decision.labels)
    return f"**{decision.outcome.value}**" + (f" ({labels})" if labels else "")


def _interval(decision: Decision) -> str:
    if decision.interval is None:
        return "n/a"
    low, high = decision.interval
    return f"[{_percent(low)}, {_percent(high)}]"


def _run_row(
    stats: StrategyStats, run: SimulationResult | None, *, daily: bool = False
) -> list[str]:
    """A run's row; with ``daily`` its drawdown is the one on daily closes (the study's marks)."""
    worst = None if run is None else run.max_drawdown
    if daily:
        worst = None if stats.marks is None else stats.marks.worst
    return [
        stats.name,
        _money(None if run is None else run.liquidation_value),
        _percent(None if run is None else run.log_growth),
        _percent(worst),
        _money(None if run is None else run.costs),
        _money(None if run is None else run.tax_paid),
        "n/a" if run is None else str(run.trades),
    ]


def yearly_returns(run: SimulationResult) -> dict[int, float]:
    """Each calendar year's change of mark-to-market value; the first year from the initial
    capital, the last to the horizon's value before the terminal sale."""
    year_end: dict[int, float] = {}
    for day, value in zip(run.dates, run.nav, strict=True):
        year_end[day.year] = value
    out: dict[int, float] = {}
    previous = float(run.initial)
    for year in sorted(year_end):
        out[year] = year_end[year] / previous - 1
        previous = year_end[year]
    return out


def _aspiration_distance(stats: StrategyStats, aspiration: Decimal) -> str:
    if stats.median_log_growth is None:
        return "n/a"
    return _percent(stats.median_log_growth - math.log1p(float(aspiration)))


def _unsold(stats: StrategyStats) -> str:
    historical = "n/a" if stats.historical is None else str(stats.historical.unsold)
    paths = "n/a" if stats.unsold_paths is None else str(stats.unsold_paths)
    return f"{stats.name} {historical} on the historical path, on {paths} bootstrap paths"


def _frontier_lines(result: StudyResult) -> list[str]:
    if result.prereg.spec.frontier is None:
        return ["No frontier was pre-registered."]
    if result.development is not None:
        return ["Not computed in an exploration on the development window."]
    if not result.frontier:
        return ["Not computed: the bootstrap did not run (see the adequacy checks)."]
    tests = {test.name: test for test in result.tests}

    def against(name: str | None) -> str:
        if name is None:
            return "none respects this point"
        if name == result.reference.name:
            return "the reference itself"
        return _outcome(tests[name].decision)

    rows = (
        (
            _grid(point.limit),
            _grid(point.probability),
            point.chosen or "none",
            against(point.chosen),
            point.best or "none",
            ", ".join(point.indistinguishable) or "none",
            str(len(point.eligible)),
        )
        for point in result.frontier
    )
    return [
        "For each drawdown limit and largest acceptable probability of breaching it: the "
        "portfolio with the highest median after-tax log growth among those that respect it "
        "(best), and the one it resolves to (chosen) — the simplest, then the lowest-turnover, "
        "of the best and the portfolios whose growth the bootstrap cannot tell apart from it "
        "(their paired interval at the confirmation level holds zero). Choosing a point is the "
        "owner's authorisation (DECISIONS D60), not this report's.",
        "",
        *_table(
            (
                "Drawdown limit",
                "Breach probability at most",
                "Chosen",
                "Its outcome against the reference",
                "Best by median growth",
                "Cannot be told apart from the best",
                "Respecting it",
            ),
            rows,
        ),
    ]


def _test_rows(tests: Sequence[CandidateTest], *, against: bool) -> list[str]:
    head = ["Candidate" if not against else "Comparison"]
    if against:
        head += ["Candidate minus", "theta"]
    head += [
        "Outcome",
        "Estimate / yr",
        "Interval",
        "Power to detect theta",
        "Detectable at target power",
        "Before data: power / detectable",
    ]
    return _table(
        head,
        (
            (
                test.name,
                *((f"{test.candidate} minus {test.against}", _percent(test.decision.theta))
                  if against else ()),
                _outcome(test.decision),
                _percent(test.estimate),
                _interval(test.decision),
                _number(test.decision.power, SHORT_DIGITS),
                _percent(test.detectable),
                f"{test.declared_power:.3f} / {_percent(test.declared_detectable)}",
            )
            for test in tests
        ),
    )  # fmt: skip


def _comparison_lines(result: StudyResult) -> list[str]:
    spec = result.prereg.spec
    if not spec.comparisons:
        return []
    justifications = "".join(
        f"\n- {c.name}: theta {_percent(float(c.theta))}, {c.theta_justification}"
        for c in spec.comparisons
    )
    return [
        "## Comparisons",
        "",
        "Secondary tests, in the same Bonferroni count: each portfolio's annualised log growth of "
        "liquidation value after costs and tax minus the other's, paired on the same paths."
        + justifications,
        "",
        *_test_rows([t for t in result.tests if t.comparison], against=True),
        "",
    ]


def _point_lines(result: StudyResult) -> list[str]:
    spec = result.prereg.spec
    point = spec.owner_point
    if point is None or result.development is not None:
        return []
    marks = "daily closes" if spec.marks == "daily" else "month-end marks"
    horizons = spec.breach_horizons_years
    at = list(spec.drawdown_limits).index(point.limit)

    def respects(stats: StrategyStats) -> str:
        if stats.point is None:
            return "n/a"
        return "yes" if stats.point <= float(point.probability) else "**no**"

    def rate(stats: StrategyStats) -> tuple[str, str]:
        if not stats.rates:
            return "n/a", "n/a"
        _, per_decade, share = stats.rates[at]
        return f"{per_decade:.2f}", f"{share:.2%}"

    years = None if result.reference.marks is None else result.reference.marks.years
    length = (
        "Each path is as long as the sample"
        if years is None
        else f"Each path is {years:.2f} years long"
    )
    if years is not None and years < point.horizon_years:
        length += f", **shorter than the owner's {point.horizon_years} years**"
    rows = (
        (
            s.name,
            "n/a" if s.point is None else f"{s.point:.1%}",
            respects(s),
            *([f"{p:.1%}" for _, p in s.horizons] or ["n/a"] * len(horizons)),
            *rate(s),
            _percent(None if s.marks is None else s.marks.worst),
        )
        for s in result.portfolios
    )
    return [
        "## The owner's point",
        "",
        f"The owner's point (DECISIONS D91): a drawdown of {_grid(point.limit)} from the running "
        f"peak reached on at most {_grid(point.probability)} of {point.horizon_years}-year paths, "
        f"measured on {marks}. A candidate that does not respect it cannot be sufficient "
        "(D92). Beside it, the share of paths reaching the limit within each horizon, and its "
        "rate: drawdown episodes reaching it per decade and the share of time beyond it, over "
        f"the paths. {length}.",
        "",
        *_table(
            (
                "Portfolio",
                f"Reaches {_grid(point.limit)} within {point.horizon_years} years",
                "Respects the point",
                *(f"within {years} years" for years in horizons),
                "Episodes per decade",
                "Share of time beyond",
                "Deepest fall on the historical path",
            ),
            rows,
        ),
        "",
    ]


def _block_lines(result: StudyResult) -> list[str]:
    rule = result.block_rule
    spec = result.prereg.spec
    if rule is None:
        return []
    if not rule.possible:
        verdict = (
            "twice it exceeds a third of the sample, so no block length can respect it (D92 rule 3)"
        )
    elif rule.respected:
        verdict = "the blocks are at least twice it, as D92 rule 3 asks"
    else:
        verdict = "**the blocks are shorter than twice it** (D92 rule 3)"
    lines = [
        "## Block lengths",
        "",
        f"The longest lookback a portfolio reads is {rule.lookback_months:.1f} months (an EWMA "
        f"counts three half-lives, a window its length); the bootstrap's blocks are "
        f"{rule.block_months} months in a sample of {rule.sample_months}: {verdict}.",
        "",
    ]
    if not result.sensitivity:
        return [*lines, "No block-length sensitivity was registered or run.", ""]
    names = [name for name, _ in result.sensitivity[0].intervals]
    portfolios = [name for name, _ in result.sensitivity[0].points]
    point = spec.owner_point
    lines += [
        "The bootstrap repeated with each registered block length (same seed): each test's "
        "interval at the confirmation level"
        + (
            f", and each portfolio's share of paths reaching {_grid(point.limit)} within "
            f"{point.horizon_years} years."
            if point is not None
            else "."
        ),
        "",
        *_table(
            ("Blocks (months)", "Paths", *names, *(f"{name} reaches it" for name in portfolios)),
            (
                (
                    str(row.block_months),
                    str(row.n_boot),
                    *(f"[{_percent(low)}, {_percent(high)}]" for _, (low, high) in row.intervals),
                    *(f"{p:.1%}" for _, p in row.points),
                )
                for row in result.sensitivity
            ),
        ),
        "",
    ]
    return lines


def _forecast_lines(forecasts: Sequence[ForecastCheck], registered: bool) -> list[str]:
    if not registered:
        return []
    lines = [
        "## Forecast check",
        "",
        "A check that each estimator works, **never an outcome** (DECISIONS D92): its forecast "
        "of the base's next-month variance from each rebalance date, against the variance of "
        "the base's daily returns over that month, scored by QLIKE (r/f - ln(r/f) - 1, zero for "
        "a perfect forecast), beside the declared matrix's constant forecast. A forecast that "
        "follows recent volatility beats a constant one almost by construction, so this says "
        "nothing about growth at the owner's point. The interval is a moving-block bootstrap's: "
        "the first and last months fall in fewer blocks, so on a short sample it can sit beside "
        "the mean.",
        "",
    ]
    if not forecasts:
        return [*lines, "Not run (see the adequacy checks).", ""]
    return [
        *lines,
        *_table(
            (
                "Strategy",
                "Estimator",
                "Months",
                "Mean loss",
                "Declared's mean loss",
                "Difference",
                "Interval",
                "Months it did better",
            ),
            (
                (
                    f.name,
                    f.estimator,
                    str(f.months),
                    f"{f.mean_loss:.4f}",
                    f"{f.mean_declared:.4f}",
                    f"{f.difference:.4f}",
                    f"[{f.interval[0]:.4f}, {f.interval[1]:.4f}]",
                    f"{f.share_better:.1%}",
                )
                for f in forecasts
            ),
        ),
        "",
    ]


def _history_lines(histories: Sequence[HistoryResult], result: StudyResult) -> list[str]:
    spec = result.prereg.spec
    if not spec.long_history:
        return []
    lines = [
        "## Long-history check",
        "",
        "Descriptive, before tax and never an outcome (DECISIONS D92 rule 4): each total-risk "
        "strategy on the longest history its roles have, at a flat cost a leg, its estimator's "
        "lengths converted to the history's observations; the share of moving-block paths of "
        "whole months reaching each limit within each horizon.",
        "",
    ]
    if result.development is not None:
        return [*lines, "Not run in an exploration on the development window.", ""]
    for history in histories:
        hs = history.spec
        lines += [
            f"### {hs.name}",
            "",
            f"{hs.frequency}, {hs.currency}, data from {hs.data_start.isoformat()}, evaluated "
            f"from {hs.start.isoformat()} to {hs.end.isoformat()}; {hs.n_boot} paths of "
            f"{history.path_months} months in {hs.block_months}-month blocks; "
            f"{_percent(float(hs.cost_per_leg))} a leg.",
            "",
            *(f"- Bias: {bias}" for bias in hs.biases),
            "",
            *_table(
                ("Check", "Passed", "Detail"),
                ((c.name, "yes" if c.passed else "**no**", c.detail) for c in history.checks),
            ),
            "",
        ]
        if history.skipped:
            lines += [
                f"- Not run (its roles lack a history here): {', '.join(history.skipped)}",
                "",
            ]
        if not history.runs:
            lines += ["Not run.", ""]
            continue
        first = "n/a" if history.first is None else history.first.isoformat()
        last = "n/a" if history.last is None else history.last.isoformat()
        columns = [(limit, h) for limit, h, _ in history.runs[0].breach]
        lines += [
            f"From {first} to {last} ({history.months} months).",
            "",
            *_table(
                (
                    "Strategy",
                    "Log growth / yr",
                    "Deepest fall",
                    "Median path growth / yr",
                    *(
                        f"{_grid(limit)} {'anywhere' if h is None else f'within {h} y'}"
                        for limit, h in columns
                    ),
                ),
                (
                    (
                        run.name,
                        _percent(run.growth),
                        _percent(run.stats.worst),
                        _percent(run.median_growth),
                        *(f"{p:.1%}" for _, _, p in run.breach),
                    )
                    for run in history.runs
                ),
            ),
            "",
        ]
    return lines


def _structure_arm(name: str, arm: StructureArm) -> list[str]:
    breach = ", ".join(f"{_grid(limit)}: {p:.1%}" for limit, p in arm.breach)
    return [name, _percent(arm.log_growth), _percent(arm.max_drawdown), breach or "n/a"]


def _structure_lines(structure: StructureResult | None) -> list[str]:
    if structure is None:
        return ["No structure check was pre-registered."]
    spec = structure.spec
    lines = [
        "Descriptive, before tax and never an outcome: the tilt with this study's constants on "
        f"{' + '.join(spec.market_series)} against {spec.cash_series}, monthly, "
        f"{float(spec.cost_per_leg):.2%} a leg, against holding the market.",
        "",
        *_table(
            ("Check", "Passed", "Detail"),
            ((c.name, "yes" if c.passed else "**no**", c.detail) for c in structure.checks),
        ),
        "",
    ]
    if structure.overlay is None or structure.held is None:
        return [*lines, "Not run: a check failed."]
    first = "n/a" if structure.first is None else structure.first.isoformat()
    last = "n/a" if structure.last is None else structure.last.isoformat()
    interval = structure.interval
    lines += [
        f"- From {first} to {last}; the tilt held less than the whole market in "
        f"{structure.months_reduced} months.",
        "",
        *_table(
            ("Arm", "Log growth / yr", "Max drawdown", "Breach probability by limit"),
            (
                _structure_arm("tilt", structure.overlay),
                _structure_arm("held", structure.held),
            ),
        ),
        "",
        "- Difference in log growth, tilt minus held: "
        + _percent(structure.overlay.log_growth - structure.held.log_growth)
        + (
            ""
            if interval is None
            else f", block-bootstrap interval of the realised returns "
            f"[{_percent(interval[0])}, {_percent(interval[1])}]"
        ),
        "",
        *_table(
            ("Subperiod", "Growth difference / yr", "Max drawdown difference"),
            (
                (
                    f"{p.start.isoformat()} to {p.end.isoformat()}",
                    _percent(p.growth_difference),
                    _percent(p.drawdown_difference),
                )
                for p in structure.subperiods
            ),
        ),
    ]
    return lines


def _robustness_lines(test: CandidateTest, development: bool) -> list[str]:
    robustness = test.robustness
    if robustness is None and development:
        return [f"- {test.name}: not run in an exploration on the development window."]
    if robustness is None:
        return [f"- {test.name}: not run (the outcome is not evaluable)."]
    parts = ", ".join(
        f"{p.start.isoformat()} to {p.end.isoformat()} {_percent(p.estimate)}"
        + ("" if p.sign_holds is None else (" sign holds" if p.sign_holds else " **sign fails**"))
        for p in robustness.subperiods
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
    exceeded = "exceeded" if robustness.breakeven_exceeds_uncertainty else "**not exceeded**"
    return [
        f"- {test.name}: subperiods {parts}; break-even cost multiplier {found} "
        f"(declared cost uncertainty {robustness.cost_uncertainty}: {exceeded})."
    ]


def _years_table(portfolios: Sequence[StrategyStats], before_tax: bool) -> list[str]:
    runs = [(s.name, s.pre_tax if before_tax else s.historical) for s in portfolios]
    yearly = {name: yearly_returns(run) for name, run in runs if run is not None}
    years = sorted({year for table in yearly.values() for year in table})
    if not years:
        return ["Not run."]
    return _table(
        ("Year", *(name for name, _ in runs)),
        (
            (
                str(year),
                *(_percent(yearly.get(name, {}).get(year)) for name, _ in runs),
            )
            for year in years
        ),
    )


def _taxes_table(portfolios: Sequence[StrategyStats]) -> list[str]:
    paid: dict[str, dict[int, Decimal]] = {}
    for stats in portfolios:
        run = stats.historical
        if run is None:
            continue
        by_year: dict[int, Decimal] = {}
        for payment in run.taxes:
            by_year[payment.paid_on.year] = by_year.get(payment.paid_on.year, Decimal(0)) + (
                payment.amount
            )
        paid[stats.name] = by_year
    years = sorted({year for table in paid.values() for year in table})
    if not years:
        return ["No tax was paid."]
    return _table(
        ("Paid in", *paid),
        ((str(y), *(_money(paid[n].get(y)) for n in paid)) for y in years),
    )


def _weights_table(portfolios: Sequence[StrategyStats]) -> list[str]:
    weighted = [s for s in portfolios if s.static_weights is not None]
    if not weighted:
        return ["No core strategy."]
    roles = list(dict.fromkeys(role for s in weighted for role in s.static_weights or {}))
    return _table(
        ("Role", *(s.name for s in weighted)),
        (
            (
                role,
                *(
                    _percent(
                        None if (w := (s.static_weights or {}).get(role)) is None else float(w)
                    )
                    for s in weighted
                ),
            )
            for role in roles
        ),
    )


def render(result: StudyResult) -> str:
    """The report as Markdown."""
    spec = result.prereg.spec
    study = spec.study
    tests = spec.tests_in_confirmation
    level = 1 - 2 * result.alpha / tests
    stage = "EXPLORATORY — never evidence (DECISIONS D64)" if result.explore else "Confirmation run"
    window = result.development
    first, last = (study.start, study.end) if window is None else (window.start, window.end)
    primary = [test for test in result.tests if not test.comparison]
    daily = spec.marks == "daily"
    lock = result.lock
    aspirations = spec.aspiration or []
    lines = [f"# {spec.title}", "", f"**{stage}.** Study `{spec.id}` ({spec.kind}).", ""]
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
        f"run (Bonferroni), each interval at level {level:.4f}; target power "
        f"{result.target_power} ({result.target_power_source})",
        *(
            [
                f"- Development window: {window.start.isoformat()} to {window.end.isoformat()}; "
                "the comparisons' portfolios only, each comparison one exploratory trial"
            ]
            if window is not None
            else []
        ),
        f"- Marks: {'daily closes' if spec.marks == 'daily' else 'month ends'}",
        f"- Horizon: {first.isoformat()} to {last.isoformat()}, "
        f"{len(result.dates)} rebalance dates"
        + (
            f" ({result.dates[0].isoformat()} to {result.dates[-1].isoformat()})"
            if result.dates
            else ""
        )
        + f"; initial capital {_money(study.initial_capital)}",
        "",
        "## Outcomes",
        "",
        f"Each candidate's primary effect is {spec.primary_metric}: its annualised log growth of "
        f"liquidation value after costs and tax minus {study.reference.name}'s. theta "
        f"{_percent(float(spec.theta))}: {spec.theta_justification}",
        "",
        *(
            _test_rows(primary, against=False)
            if primary
            else [
                "None in this run: an exploration on the development window tests the "
                "comparisons only."
            ]
        ),
        "",
        *_comparison_lines(result),
        "## The frontier",
        "",
        *_frontier_lines(result),
        "",
        *_point_lines(result),
        "## What this test cannot conclude",
        "",
        *(
            f"- {line}"
            for line in dict.fromkeys(
                line for test in result.tests for line in test.decision.cannot_conclude
            )
        ),
        *(f"- {line}" for line in COMPARISON_LIMITS),
        "",
        "## Adequacy checks",
        "",
        *_table(
            ("Check", "Passed", "Detail"),
            ((c.name, "yes" if c.passed else "**no**", c.detail) for c in result.checks),
        ),
        "",
        "## Portfolios after tax",
        "",
        *_table(
            (
                "Portfolio",
                "Liquidation value",
                "Log growth / yr",
                "Max drawdown (daily closes)" if daily else "Max drawdown",
                "Costs",
                "Tax paid",
                "Trades",
                "Turnover / yr",
                "Median bootstrap growth",
                "P5 terminal wealth multiple",
                *(f"Median minus ln(1 + {_grid(a)})" for a in aspirations),
            ),
            (
                (
                    *_run_row(stats, stats.historical, daily=daily),
                    _percent(stats.turnover),
                    _percent(stats.median_log_growth),
                    _number(stats.terminal_wealth_p5, SHORT_DIGITS),
                    *(_aspiration_distance(stats, a) for a in aspirations),
                )
                for stats in result.portfolios
            ),
        ),
        "",
    ]
    if aspirations:
        lines += [
            "The owner's stated aspiration (DECISIONS D60) is reported as a distance from each "
            "portfolio's median after-tax log growth, never as a gate.",
            "",
        ]
    lines += [
        "Sales not made because their costs would exceed what they raise (the units stay held, "
        "and count as nothing at the horizon): "
        + "; ".join(_unsold(stats) for stats in result.portfolios)
        + ".",
        "",
    ]
    limits = spec.drawdown_limits
    within = "" if spec.owner_point is None else f" within {spec.owner_point.horizon_years} years"
    path_months = max(len(result.dates) - 1, 0)
    if (
        spec.owner_point is not None
        and path_months < spec.owner_point.horizon_years * MONTHS_PER_YEAR
    ):
        within = (
            f" within a path of {path_months} months (shorter than the owner's horizon of "
            f"{spec.owner_point.horizon_years} years)"
        )
    lines += [
        f"Probability that a bootstrap path's mark-to-market drawdown reaches each limit{within}:",
        "",
        *_table(
            ("Portfolio", *(_grid(limit) for limit in limits)),
            (
                (s.name, *([f"{p:.1%}" for _, p in s.breach] or ["n/a"] * len(limits)))
                for s in result.portfolios
            ),
        ),
        "",
        "## Portfolios before tax",
        "",
        "The same strategies, trading the same way, with no tax withheld, settled or paid.",
        "",
        *_table(
            (
                "Portfolio",
                "Liquidation value",
                "Log growth / yr",
                "Max drawdown (month ends)" if daily else "Max drawdown",
                "Costs",
                "Tax paid",
                "Trades",
            ),
            (_run_row(stats, stats.pre_tax) for stats in result.portfolios),
        ),
        "",
        "## Static weights",
        "",
        "Each core strategy's weights by role before the tilt (a total-risk strategy's at its "
        "declared share).",
        "",
        *_weights_table(result.portfolios),
        "",
        "## Year by year",
        "",
        "Mark-to-market value, after tax paid as it arose (the last year ends at the horizon "
        "before the terminal sale and its tax):",
        "",
        *_years_table(result.portfolios, before_tax=False),
        "",
        "Before tax:",
        "",
        *_years_table(result.portfolios, before_tax=True),
        "",
        "Tax paid, by the year it was paid (the horizon's includes the terminal sale's):",
        "",
        *_taxes_table(result.portfolios),
        "",
        "## Robustness",
        "",
        *(
            line
            for test in result.tests
            for line in _robustness_lines(test, result.development is not None)
        ),
        "",
        f"- Placebo: {result.tests[0].robustness.placebo}"
        if result.tests and result.tests[0].robustness is not None
        else "- Placebo: not run",
        "",
        *_block_lines(result),
        *_forecast_lines(result.forecasts, spec.forecast_check is not None),
        *_history_lines(result.long_history, result),
        "## Selection across trials",
        "",
        f"- Trials of this study, this run's tests included: {result.trials}",
        *(
            f"- {test.name}: deflated Sharpe n/a"
            if test.deflated is None
            else f"- {test.name}: monthly Sharpe {test.deflated.sharpe:.4f} against "
            f"{test.deflated.benchmark:.4f} expected from the best of {result.trials} trial(s); "
            f"probability {test.deflated.probability:.3f}"
            for test in result.tests
        ),
        "",
        "## Structure check",
        "",
        *_structure_lines(result.structure),
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
        *(f"- {note}" for note in simulation_notes(spec.marks)),
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
