"""The reconciliation freeze: when new risk waits (MASTER_PLAN §5.12, DECISIONS D179).

New risk needs proof that the store and the broker agree now: the run in force is green and no
older than ``execution.reconcile.max_age_seconds``. No run yet, a run that is not green and a green
run that has aged all freeze it. Sells that reduce risk are never frozen. One rule for every
reader: the pre-trade gate, the executor's staging and placing and the rebalancers' surveys.
VERIFY-LIVE(the reconciliation's age in service): 900 s against IBKR's partial answers.

A dry run writes nothing, so it cannot reconcile: its survey is a ``rehearsal`` of the cycle that
would reconcile first, and only a run that is not green blocks its plan. Nothing a rehearsal
plans is staged or cleared.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import datetime, timedelta

from ..domain.orders import ReconcileRun, ReconcileStatus


def new_risk_frozen(
    latest: ReconcileRun | None, *, now: datetime, max_age: timedelta, rehearsal: bool = False
) -> str | None:
    """Why new risk is frozen under the run in force (``latest``); ``None`` when it is not.
    ``rehearsal``: a dry run's survey, which no missing or aged run blocks."""
    if latest is None:
        if rehearsal:
            return None
        return "no reconciliation has run yet: new risk waits for a green one"
    if latest.status is not ReconcileStatus.GREEN:
        return (
            f"the latest reconciliation ({latest.at.isoformat()}) is {latest.status.value}: "
            "new risk waits for a green one"
        )
    age = now - latest.at
    if age > max_age and not rehearsal:
        return (
            f"the latest green reconciliation ({latest.at.isoformat()}) is "
            f"{age.total_seconds():.0f} s old, beyond {max_age.total_seconds():.0f} s: new risk "
            "waits for a fresh one"
        )
    return None
