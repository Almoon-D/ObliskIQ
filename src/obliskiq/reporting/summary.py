"""The run summary: what one decision cycle did, in a message the owner reads.

Ported in spirit from the legacy trade sheet (legacy/old modules/notifications/run_summary.py):
a subject that counts the buys and sells, then the trades with what they needed, what was held
back and why, and a line that never lets the mode be mistaken. ObliskIQ places its own orders,
so the summary reports what was done rather than what to do. Pure formatting.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from dataclasses import dataclass

from ..decision.core_rebalancer import BUY, SELL, SKIP, RebalancePlan
from ..domain.orders import ExecutionMode, OrderIntent, OrderSide
from ..execution.executor import CycleReport

#: Steps of the executor's report worth a line in the summary; routine ones are left out.
QUIET_ACTIONS = frozenset({"waiting", "not repriced"})


@dataclass(frozen=True, slots=True)
class CycleSummary:
    """One cycle: its name, mode, how it ended, its plan, what it made and what the executor did."""

    name: str
    mode: ExecutionMode
    status: str
    plan: RebalancePlan | None
    made: tuple[OrderIntent, ...]
    report: CycleReport | None
    errors: tuple[str, ...] = ()

    @property
    def eventful(self) -> bool:
        """Worth a message: it made, placed or filled something, or something failed."""
        acted = self.report is not None and any(
            step.action not in QUIET_ACTIONS for step in self.report.steps
        )
        return bool(self.made) or acted or bool(self.errors) or self.status != "ok"


def format_cycle(summary: CycleSummary) -> tuple[str, str]:
    """``(subject, body)`` for one cycle."""
    buys = sum(1 for intent in summary.made if intent.side is OrderSide.BUY)
    sells = len(summary.made) - buys
    headline = f"{buys} BUY / {sells} SELL" if summary.made else "no new trades"
    subject = f"ObliskIQ {summary.name} cycle ({summary.mode.value}): {headline}"
    lines = [f"cycle {summary.name}: {summary.status}"]
    plan = summary.plan
    if plan is not None:
        if plan.capital is not None:
            lines.append(
                f"managed capital {plan.capital.managed:.2f} ({plan.capital.detail}), "
                f"may spend {plan.spendable or 0:.2f}"
            )
        if plan.blocked is not None:
            lines.append(f"no rebalance: {plan.blocked}")
        for decision in plan.decisions:
            if decision.action in (BUY, SELL):
                icon = "🟢" if decision.action == BUY else "🔴"
                lines.append(
                    f"{icon} {decision.action.upper()} {decision.quantity} {decision.symbol} "
                    f"({decision.role}) ≈ {decision.notional:.2f}"
                )
            elif decision.action == SKIP:
                lines.append(f"  held back {decision.symbol} ({decision.role}): {decision.reason}")
    if summary.report is not None:
        steps = [s for s in summary.report.steps if s.action not in QUIET_ACTIONS]
        for step in steps:
            lines.append(f"  {step.action}: {step.detail}")
        for conversion in summary.report.conversions:
            lines.append(
                f"  conversion into {conversion.currency} ({conversion.amount}): "
                f"{conversion.status}, {conversion.detail}"
            )
        run = summary.report.reconcile
        if run is not None:
            lines.append(f"reconciliation {run.status.value}")
            lines += [f"  [{f.kind}] {f.detail}" for f in run.findings if f.freezes]
    lines += [f"error: {error}" for error in summary.errors]
    lines.append("")
    lines.append(_mode_line(summary.mode))
    return subject, "\n".join(lines)


def _mode_line(mode: ExecutionMode) -> str:
    if mode is ExecutionMode.SHADOW:
        return "Shadow mode: fills are simulated; nothing was sent to the broker."
    if mode is ExecutionMode.PAPER:
        return "Paper mode: orders go to the IBKR paper account."
    return f"Mode {mode.value}: real orders."
