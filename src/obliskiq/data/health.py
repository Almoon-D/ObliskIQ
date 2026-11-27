"""Each source's health from its last run, without the network: ``failing`` when its last run
failed outright, ``stale`` when it has not succeeded within its ``stale_after_hours`` (or never
has), ``ok`` otherwise. Read by the status, the cockpit and the digest.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping
from datetime import datetime, timedelta

from ..config.schema import ObliskConfig
from ..domain.sources import SourceHealth, SourceRun, SourceState

#: A run that failed outright.
FAILED = "error"


def source_health(
    settings: ObliskConfig, runs: Mapping[str, SourceRun], *, now: datetime
) -> list[SourceHealth]:
    """The health of every built source, in the order config lists them."""
    health: list[SourceHealth] = []
    for name, provider in settings.sources.built().items():
        run = runs.get(name)
        window = timedelta(hours=provider.stale_after_hours or 0)
        if run is None:
            state, reason = SourceState.STALE, "never run"
        elif run.last_status == FAILED:
            state = SourceState.FAILING
            reason = f"{run.consecutive_failures} failed runs in a row: {run.detail or 'no detail'}"
        elif run.last_success_at is None or now - run.last_success_at > window:
            state = SourceState.STALE
            reason = f"no success within {provider.stale_after_hours} hours"
        else:
            state, reason = SourceState.OK, run.detail or ""
        health.append(
            SourceHealth(
                source=name,
                state=state,
                last_attempt_at=None if run is None else run.last_attempt_at,
                last_success_at=None if run is None else run.last_success_at,
                rows=0 if run is None else run.last_rows,
                reason=reason,
            )
        )
    return health
