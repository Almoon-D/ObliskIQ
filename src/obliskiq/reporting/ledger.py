"""The idea ledger in plain words (H14; DECISIONS D187): for each segment, source and event tag,
how many ideas are open and resolved and how they resolved, how much each earned over the equity
reference after costs with its interval, and the verdict or "too few"; for each analyst segment
what its calls cost in all and one resolved idea, how far its stated probabilities can be trusted
and how its weekly reviews scored. Shown by ``obliskiq scoreboard ledger`` and in the
weekly report.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping

from ..research.decide import Outcome
from ..scoreboard.grading import Record
from ..scoreboard.ledger import SEGMENT, Calibration, Group, LedgerBook

#: What each verdict means, for a reader who is not a statistician.
MEANING = {
    Outcome.SUFFICIENT: "its ideas beat the reference by at least the bar, with confidence",
    Outcome.INSUFFICIENT: "its ideas do not clear the bar, with confidence",
    Outcome.INCONCLUSIVE: "the record does not decide yet",
    Outcome.NOT_EVALUABLE: "the record cannot be read",
}


def _pct(value: float) -> str:
    return f"{value:+.1%}"


def _excess(item: Record) -> str:
    estimate = item.estimate
    if estimate.mean is None:
        return "no mean yet"
    interval = (
        ""
        if estimate.ci is None
        else f" (likely between {_pct(estimate.ci[0])} and {_pct(estimate.ci[1])})"
    )
    return f"{_pct(estimate.mean)} a call over the reference after costs{interval}"


def _verdict(item: Record, rules: str) -> str:
    if item.decision is None:
        return f"too few to judge: {item.cohorts} weeks and {item.effective:.1f} effective " + (
            f"observations of the {rules} needed"
        )
    outcome = item.decision.outcome
    return f"{outcome.value}: {MEANING[outcome]} ({_pct(item.theta)} a call)"


def cost_line(spent: float, resolved: int) -> str:
    """What a segment's calls cost in all, and one resolved idea."""
    each = (
        "no idea resolved yet" if resolved == 0 else f"{spent / resolved:.2f} USD a resolved idea"
    )
    return f"  Its calls cost {spent:.2f} USD in all: {each}."


def group_lines(
    group: Group, *, minimum: str, spend: Mapping[str, float] | None = None
) -> list[str]:
    resolved = sum(group.outcomes.values())
    record = group.record
    lines = [
        f"- **{group.name}**: {group.calls} ideas, {group.open} open, {resolved} resolved "
        f"({group.outcomes['target']} reached the target first, {group.outcomes['invalidation']} "
        f"the invalidation, {group.outcomes['horizon']} the horizon), {group.missing} not "
        f"evaluable; held about {group.held_weeks} weeks.",
        f"  Earned {_excess(record)}. Verdict: {_verdict(record, minimum)}.",
    ]
    label = group.name.removeprefix(SEGMENT)
    if spend is not None and group.name.startswith(SEGMENT) and label in spend:
        lines.append(cost_line(spend[label], resolved))
    return lines


def calibration_lines(found: Calibration, *, minimum: str) -> list[str]:
    score = found.score
    state = found.state
    if score.count == 0:
        return [f"- **{found.segment}**: no resolved idea states a probability yet."]
    base = "n/a" if found.base_rate is None else f"{found.base_rate:.0%}"
    lines = [
        f"- **{found.segment}**: {score.count} resolved ideas with a probability; their targets "
        f"came first {base} of the time.",
    ]
    if not found.mature:
        lines.append(
            f"  Too few to read its probabilities: {state.cohorts} weeks and "
            f"{state.effective:.1f} effective observations of the {minimum} needed."
        )
        return lines
    skill = "n/a" if score.skill is None else f"{score.skill:+.2f}"
    trusted = (
        "trusted: its ideas may be offered free slots first"
        if state.calibrated
        else ("not trusted: its ideas wait in order of arrival")
    )
    lines.append(
        f"  Brier skill {skill} against that rate; {trusted}. Believed share of the stated edge "
        f"{state.shrink:.2f}."
    )
    lines += [
        f"  - stated {b.low:.0%} to {b.high:.0%}: {b.count} ideas, said {b.mean_stated:.0%}, "
        f"happened {b.observed:.0%}"
        for b in score.bins
    ]
    return lines


def ledger_lines(book: LedgerBook | str) -> list[str]:
    """The ledger as a report section; ``book`` a reason when it could not be graded."""
    if isinstance(book, str):
        return [f"Not graded: {book}."]
    if not book.groups:
        return [
            "No idea has been written into the ledger yet "
            f"({book.unwritten} ideas never passed their own checks)."
        ]
    lines = [
        f"Every idea that passed its own checks, traded or not, graded at its own levels as of "
        f"{book.as_of.isoformat()}. {book.tests} groups past the minimum share the confidence "
        "level; a verdict moves no capital.",
        "",
    ]
    for group in book.groups.values():
        lines += group_lines(group, minimum=book.minimum, spend=book.spend)
    lines += ["", "Stated probabilities, per segment:", ""]
    for found in book.calibration.values():
        lines += calibration_lines(found, minimum=book.calibration_minimum)
    if book.reviews:
        lines += ["", "Weekly reviews, graded against the ideas they reviewed:", ""]
        lines += [
            f"- **{score.segment}**: {score.graded} reviews graded, mean Brier score "
            f"{score.brier:.3f} (0 is a certain, right call; 0.25 a coin's)."
            for score in book.reviews
        ]
    beside = [
        *(f"{count} not evaluable: {why}" for why, count in sorted(book.not_evaluable.items())),
        *(
            [f"{book.overlapping} written while an earlier idea of the segment was still open"]
            if book.overlapping
            else []
        ),
        f"{book.unwritten} ideas never passed their own checks",
    ]
    lines += ["", "Beside the record: " + "; ".join(beside) + "."]
    return lines
