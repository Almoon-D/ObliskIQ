"""Research history and the trial registry in the store.

A series is replaced whole: its metadata and every row in one transaction, so a reload can
neither leave rows the vendor dropped nor mix two downloads. A read of observations is bounded
by ``available_on <= as_of`` and, for a series with vintages, returns the latest vintage of each
period public by then. Series metadata and trials are the harness's own records and are read
whole (DECISIONS D46's rule for the system's own state).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import sqlite3
from collections.abc import Sequence
from datetime import date

from ..domain.research import (
    Frequency,
    Observation,
    PointInTime,
    ResearchSeries,
    Trial,
    TrialStage,
    Vendor,
)
from ..errors import StoreError
from .db import Store
from .mappers import date_text, read_date, read_stamp, stamp_text

_SERIES_COLUMNS = (
    "series_id, dataset_id, vendor, field, units, currency, frequency, point_in_time, "
    "source_url, downloaded_at, content_sha256, bias_notes"
)
_TRIAL_COLUMNS = (
    "trial_id, study_id, stage, recorded_at, spec_sha256, inputs_sha256, parameters, metric, "
    "estimate, sharpe, observations, outcome"
)


def _series_from_row(row: sqlite3.Row) -> ResearchSeries:
    try:
        return ResearchSeries(
            series_id=row["series_id"],
            dataset_id=row["dataset_id"],
            vendor=Vendor(row["vendor"]),
            field=row["field"],
            units=row["units"],
            currency=row["currency"],
            frequency=Frequency(row["frequency"]),
            point_in_time=PointInTime(row["point_in_time"]),
            source_url=row["source_url"],
            downloaded_at=read_stamp(row["downloaded_at"], "research_series", "downloaded_at"),
            content_sha256=row["content_sha256"],
            bias_notes=tuple(json.loads(row["bias_notes"])),
        )
    except ValueError as exc:
        raise StoreError(f"research_series row {row['series_id']!r} is invalid: {exc}") from None


def replace_research_series(
    store: Store, series: ResearchSeries, observations: Sequence[Observation]
) -> int:
    """Replace ``series`` and all its rows. Returns the number of rows written."""
    if not observations:
        raise StoreError(f"series {series.series_id} has no observations to store")
    keys = {(o.observed_on, o.available_on) for o in observations}
    if len(keys) != len(observations):
        raise StoreError(f"series {series.series_id} repeats an observation and vintage")
    with store.transaction():
        store.execute("DELETE FROM research_series WHERE series_id = ?", (series.series_id,))
        store.execute(
            f"INSERT INTO research_series ({_SERIES_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                series.series_id,
                series.dataset_id,
                series.vendor.value,
                series.field,
                series.units,
                series.currency,
                series.frequency.value,
                series.point_in_time.value,
                series.source_url,
                stamp_text(series.downloaded_at),
                series.content_sha256,
                json.dumps(list(series.bias_notes)),
            ),
        )
        store.executemany(
            "INSERT INTO research_history (series_id, observed_on, available_on, value) "
            "VALUES (?, ?, ?, ?)",
            [
                (series.series_id, date_text(o.observed_on), date_text(o.available_on), o.value)
                for o in observations
            ],
        )
    return len(observations)


def research_series_info(store: Store, series_id: str) -> ResearchSeries | None:
    """The metadata of one series, or ``None`` when it was never loaded."""
    row = store.execute(
        f"SELECT {_SERIES_COLUMNS} FROM research_series WHERE series_id = ?", (series_id,)
    ).fetchone()
    return None if row is None else _series_from_row(row)


def research_series_in(store: Store, dataset_id: str) -> list[ResearchSeries]:
    """Every series a dataset loaded, by id."""
    rows = store.execute(
        f"SELECT {_SERIES_COLUMNS} FROM research_series WHERE dataset_id = ? ORDER BY series_id",
        (dataset_id,),
    ).fetchall()
    return [_series_from_row(row) for row in rows]


def research_observations(store: Store, series_id: str, *, as_of: date) -> list[Observation]:
    """The observations of ``series_id`` public on ``as_of``: per period, its latest vintage."""
    rows = store.execute(
        "SELECT observed_on, available_on, value FROM research_history AS h "
        "WHERE series_id = ? AND available_on <= ? AND available_on = ("
        "  SELECT MAX(available_on) FROM research_history "
        "  WHERE series_id = h.series_id AND observed_on = h.observed_on AND available_on <= ?"
        ") ORDER BY observed_on",
        (series_id, date_text(as_of), date_text(as_of)),
    ).fetchall()
    return [
        Observation(
            observed_on=read_date(row["observed_on"], "research_history", "observed_on"),
            available_on=read_date(row["available_on"], "research_history", "available_on"),
            value=row["value"],
        )
        for row in rows
    ]


def record_trial(store: Store, trial: Trial) -> None:
    """Append ``trial`` to the registry; a trial id is written once."""
    with store.transaction():
        store.execute(
            f"INSERT INTO research_trials ({_TRIAL_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                trial.trial_id,
                trial.study_id,
                trial.stage.value,
                stamp_text(trial.recorded_at),
                trial.spec_sha256,
                trial.inputs_sha256,
                trial.parameters,
                trial.metric,
                trial.estimate,
                trial.sharpe,
                trial.observations,
                trial.outcome,
            ),
        )


def trials_for(store: Store, study_id: str) -> list[Trial]:
    """Every trial of ``study_id``, oldest first."""
    rows = store.execute(
        f"SELECT {_TRIAL_COLUMNS} FROM research_trials WHERE study_id = ? "
        "ORDER BY recorded_at, trial_id",
        (study_id,),
    ).fetchall()
    return [
        Trial(
            trial_id=row["trial_id"],
            study_id=row["study_id"],
            stage=TrialStage(row["stage"]),
            recorded_at=read_stamp(row["recorded_at"], "research_trials", "recorded_at"),
            spec_sha256=row["spec_sha256"],
            inputs_sha256=row["inputs_sha256"],
            parameters=row["parameters"],
            metric=row["metric"],
            estimate=row["estimate"],
            sharpe=row["sharpe"],
            observations=row["observations"],
            outcome=row["outcome"],
        )
        for row in rows
    ]
