"""The replay's report (``research/reports/analyst-replay.md``) and its lines beside the forward
ledger (``obliskiq scoreboard ledger``). Every number is the grader's; the record is the
replay's own and is never summed with the forward one.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Sequence
from datetime import date

from ...scoreboard.grading import Record
from ..prereg_replay import AnalystReplaySpec
from .grade import Leak, ReplayBook, SegmentResult


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.2%}"


def _interval(found: tuple[float, float] | None) -> str:
    return "n/a" if found is None else f"[{found[0]:+.2%}, {found[1]:+.2%}]"


def _record(found: Record) -> str:
    verdict = "too few" if found.decision is None else found.decision.outcome.value
    return (
        f"{found.graded} calls graded in {found.cohorts} weeks (N_eff {found.effective:.1f}); "
        f"mean excess {_pct(found.estimate.mean)} a call, interval "
        f"{_interval(found.estimate.ci)}: {verdict}"
    )


def _leak(found: Leak) -> str:
    return (
        f"before minus after {_pct(found.difference)}, interval {_interval(found.interval)}"
        if found.difference is not None
        else "not measured (too few calls on one side)"
    )


def _better(skill: float | None) -> str:
    return "better" if (skill or 0.0) > 0 else "no better"


def _calibration(result: SegmentResult) -> str:
    found = result.calibration
    if found.brier is None or found.reference is None:
        return "no resolved call states a probability"
    if found.skill is None:
        return (
            f"Brier {found.brier:.3f}; every resolved call came out alike, so its climatology "
            "is certain and its skill undefined"
        )
    return (
        f"Brier {found.brier:.3f} against its climatology's {found.reference:.3f}, skill "
        f"{found.skill:+.3f}"
    )


def _plain(result: SegmentResult, book: ReplayBook) -> list[str]:
    """Three sentences a reader without statistics reads."""
    after = result.after.estimate.mean
    beat = (
        "We cannot yet tell whether its picks beat the stocks it could have picked: too few were "
        "graded."
        if result.after.decision is None
        else f"After its model's cutoff its picks did {abs(after or 0.0):.1%} a month "
        f"{'better' if (after or 0.0) > 0 else 'worse'} than the average stock it could have "
        f"picked, after costs, and that is {result.verdict} against the half percent the test "
        "asks for."
    )
    memory = (
        "It did clearly better on dates its model had seen than on dates it had not, so much of "
        "what looks like skill is memory."
        if result.remembering
        else "It did not do clearly better on dates its model had already seen, so the result "
        "is not memory alone."
    )
    calibration = (
        f"When it said how likely its target was, it was {_better(result.skill)} than always "
        "guessing its own average."
        if result.calibration_read
        else f"Its stated chances cannot be judged yet: {result.resolved} of its calls reached "
        "a level by now."
    )
    if result.segment in book.loses_share:
        calibration += " It is proposed to lose its share of the forward budget."
    return [beat, memory, calibration]


def segment_lines(
    result: SegmentResult, book: ReplayBook, *, minimum: str, alpha: float
) -> list[str]:
    lines = [
        f"### {result.label}",
        "",
        " ".join(_plain(result, book)),
        "",
        f"- Calls: {result.calls} valid drafts ({result.stock_calls} on stocks), {result.dropped} "
        "dropped by the forward checks.",
        f"- **After the cutoff ({result.cutoff.isoformat()}), the verdict that counts**: "
        f"{_record(result.after)}"
        + (
            f"; quarters trimmed by the probe: {', '.join(result.trimmed)}"
            if result.trimmed
            else ""
        )
        + f". Minimum {minimum}.",
        f"- After the cutoff, untrimmed: {_record(result.after_untrimmed)}.",
        f"- After the cutoff, without its best week: {_record(result.after_without_best)}.",
        f"- Before the cutoff (the leak control): {_record(result.before)}.",
        f"- Leak measure: {_leak(result.leak)}; "
        + ("**remembering**" if result.remembering else "not remembering")
        + ".",
        "- After the cutoff, over the equity reference (beside): "
        f"{_record(result.over_reference)}.",
        "- After the cutoff, at its own levels (beside): "
        + (", ".join(f"{n} {kind}" for kind, n in sorted(result.levels.items())) or "none resolved")
        + f"; {result.resolved} resolved by the prices' end.",
        "- Calibration: "
        + _calibration(result)
        + ("" if result.calibration_read else " (below the minimum: not read)")
        + ".",
    ]
    if result.calibration.bins:
        lines.append(
            "- Reliability: "
            + "; ".join(
                f"stated {b.low:.1f}-{b.high:.1f}: {b.count} calls, mean {b.mean_stated:.2f}, "
                f"observed {b.observed:.2f}"
                for b in result.calibration.bins
            )
            + "."
        )
    if result.not_evaluable:
        lines.append(
            "- Not evaluable: "
            + "; ".join(f"{n} {why}" for why, n in sorted(result.not_evaluable.items()))
            + "."
        )
    if result.probe:
        lines.append(
            "- Memory probe: "
            + "; ".join(
                f"{r.model_id} {r.quarter} {r.hits}/{r.answered} hits against a majority of "
                f"{r.majority:.0%}" + (" (recalled)" if r.recalled(alpha) else "")
                for r in result.probe
            )
            + "."
        )
    return [*lines, ""]


def _inherited(spec: AnalystReplaySpec) -> str:
    """The sentence a cutoff not stated for its own model owes every result."""
    found = [m for m in spec.models if m.cutoff_source == "inherited"]
    if not found:
        return ""
    named = ", ".join(f"{m.model_id} ({m.training_cutoff})" for m in found)
    return (
        f" The cutoff of {named} is inherited from its base model's card, not stated for it: "
        "a later cutoff would put some sheets read as after it before it."
    )


def report_text(book: ReplayBook, spec: AnalystReplaySpec, *, as_of: date) -> str:
    minimum = f"{spec.statistic.min_cohorts} weeks and N_eff {spec.statistic.min_effective}"
    proposal = (
        f"`{book.proposed_trading}` is proposed to the owner as the trading segment (now "
        f"`{book.current_trading}`); the owner decides."
        if book.proposed_trading is not None
        else f"No segment is proposed as the trading segment: `{book.current_trading}` stays."
    )
    losing = (
        f"Proposed to lose their forward budget share: {', '.join(book.loses_share)}."
        if book.loses_share
        else "No segment is proposed to lose its forward budget share."
    )
    lines = [
        "# The analyst replayed after its models' cutoff (H15)",
        "",
        f"Graded on {as_of.isoformat()} from the replay's own store, under "
        "`research/preregistrations/analyst-replay.md` (locked). The record is the replay's: "
        "never pooled with shadow, paper or live, never read by H13 or H14, and it moves no "
        "capital. Not investment advice.",
        "",
        f"{proposal} {losing}",
        "",
        f"{len(book.segments)} segments; the confidence level is split over {book.tests} (the "
        f"segments past the minimum, at least one). {book.units} sheet runs "
        f"({book.interrupted} interrupted); spent {book.spent:.2f} USD. Not replayed: "
        f"{', '.join(book.not_replayed) or 'none'}.{_inherited(spec)}",
        "",
    ]
    for result in book.segments:
        lines += segment_lines(result, book, minimum=minimum, alpha=float(spec.probe.alpha))
    return "\n".join(lines).rstrip() + "\n"


def ledger_lines(book: ReplayBook | str) -> list[str]:
    """The replay beside the forward ledger: one line per segment, never summed with it."""
    head = "The replay after the cutoff (H15), its own record, never summed with the above:"
    if isinstance(book, str):
        return [head, f"- {book}."]
    lines: Sequence[str] = [
        f"- **{r.label}**: after the cutoff {_record(r.after)}; leak {_leak(r.leak)}"
        + ("; remembering" if r.remembering else "")
        + "."
        for r in book.segments
    ]
    return [head, *lines]
