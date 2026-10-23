"""A contract's crowding as known on a date: the share, its percentile and its velocity.

Ported from the old ObliskIQ's ``cot_percentile_with_status`` (docs/COT_REAL.md) onto the share of
open interest lite stores (a percentile of the raw net count over years ranks how large the market
grew, not how crowded it is). The percentile is the inclusive rank of the newest share among the
trailing ``percentile_window_weeks`` reports, the newest included; until the window holds
``percentile_min_weeks`` reports it is ``None`` and the reading says it is warming up, so the
component casts no vote (honest degradation, never a rank of too little history). Velocity is the
share's change over ``velocity_weeks`` reports. Only reports released by ``as_of`` are given here:
the store reads them by release date.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Sequence
from datetime import date

from ...config.schema_sources import PositioningConfig
from ...domain.sources import CotReport, PositioningReading, PositioningStatus


def positioning_reading(
    contract_id: str,
    reports: Sequence[CotReport],
    *,
    as_of: date,
    config: PositioningConfig,
) -> PositioningReading:
    """The reading of ``contract_id`` on ``as_of`` from its reports released by then."""
    released = sorted(
        (
            (report, share)
            for report in reports
            if report.release_date <= as_of
            and (share := report.net_noncommercial_share) is not None
        ),
        key=lambda pair: pair[0].report_date,
    )
    if not released:
        return PositioningReading(
            contract_id=contract_id,
            as_of=as_of,
            status=PositioningStatus.NO_DATA,
            report_date=None,
            release_date=None,
            share=None,
            percentile=None,
            window=0,
            velocity=None,
            reason=f"no report of {contract_id} was released by {as_of}",
        )
    newest, share = released[-1]
    window = [value for _, value in released[-config.percentile_window_weeks :]]
    back = config.velocity_weeks
    velocity = share - released[-1 - back][1] if len(released) > back else None
    if len(window) < config.percentile_min_weeks:
        return PositioningReading(
            contract_id=contract_id,
            as_of=as_of,
            status=PositioningStatus.WARMING_UP,
            report_date=newest.report_date,
            release_date=newest.release_date,
            share=share,
            percentile=None,
            window=len(window),
            velocity=velocity,
            reason=f"warming up: {len(window)}/{config.percentile_min_weeks} weekly reports",
        )
    rank = sum(1 for value in window if value <= share) / len(window)
    return PositioningReading(
        contract_id=contract_id,
        as_of=as_of,
        status=PositioningStatus.OK,
        report_date=newest.report_date,
        release_date=newest.release_date,
        share=share,
        percentile=rank,
        window=len(window),
        velocity=velocity,
        reason=f"ranked among {len(window)} weekly reports",
    )
