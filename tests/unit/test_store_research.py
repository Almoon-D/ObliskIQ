"""Research history, the trial registry and tax lots in the store: whole replacement, reads
bounded by availability with the latest vintage, an append-only registry, refused bad rows.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import sqlite3
from datetime import UTC, date, datetime
from typing import Any

import pytest

from obliskiq.domain.research import (
    Frequency,
    Observation,
    PointInTime,
    ResearchSeries,
    Trial,
    TrialStage,
    Vendor,
)
from obliskiq.errors import StoreError
from obliskiq.store import Store
from obliskiq.store.lots import tax_lots
from obliskiq.store.research import (
    record_trial,
    replace_research_series,
    research_observations,
    research_series_in,
    research_series_info,
    trials_for,
)

DOWNLOADED = datetime(2026, 10, 16, 20, 0, tzinfo=UTC)
SHA = "a" * 64


def series(**overrides: Any) -> ResearchSeries:
    fields: dict[str, Any] = {
        "series_id": "alfred_unrate.value",
        "dataset_id": "alfred_unrate",
        "vendor": Vendor.ALFRED,
        "field": "value",
        "units": "percent",
        "currency": None,
        "frequency": Frequency.MONTHLY,
        "point_in_time": PointInTime.VINTAGES,
        "source_url": "https://api.stlouisfed.org/fred/series/observations?series_id=UNRATE",
        "downloaded_at": DOWNLOADED,
        "content_sha256": SHA,
        "bias_notes": ("revised by the BLS; every vintage kept",),
    }
    fields.update(overrides)
    return ResearchSeries(**fields)


def obs(observed: str, available: str, value: float) -> Observation:
    return Observation(
        observed_on=date.fromisoformat(observed),
        available_on=date.fromisoformat(available),
        value=value,
    )


VINTAGES = [
    obs("2023-10-01", "2023-11-03", 3.9),
    obs("2023-10-01", "2024-01-05", 3.8),
    obs("2023-11-01", "2023-12-08", 3.7),
]


def test_a_read_sees_the_latest_vintage_public_on_its_date(store: Store) -> None:
    assert replace_research_series(store, series(), VINTAGES) == 3
    before = research_observations(store, "alfred_unrate.value", as_of=date(2023, 11, 30))
    assert [(o.observed_on.isoformat(), o.value) for o in before] == [("2023-10-01", 3.9)]
    december = research_observations(store, "alfred_unrate.value", as_of=date(2023, 12, 31))
    assert [o.value for o in december] == [3.9, 3.7]
    later = research_observations(store, "alfred_unrate.value", as_of=date(2024, 6, 30))
    assert [(o.value, o.available_on) for o in later] == [
        (3.8, date(2024, 1, 5)),
        (3.7, date(2023, 12, 8)),
    ]
    assert research_observations(store, "alfred_unrate.value", as_of=date(2023, 11, 2)) == []


def test_a_reload_replaces_the_series_whole(store: Store) -> None:
    replace_research_series(store, series(), VINTAGES)
    replace_research_series(
        store, series(content_sha256="b" * 64), [obs("2024-01-01", "2024-02-02", 3.7)]
    )
    info = research_series_info(store, "alfred_unrate.value")
    assert info is not None
    assert info.content_sha256 == "b" * 64
    rows = research_observations(store, "alfred_unrate.value", as_of=date(2025, 1, 1))
    assert [o.observed_on for o in rows] == [date(2024, 1, 1)]
    assert research_series_info(store, "missing") is None
    assert [s.series_id for s in research_series_in(store, "alfred_unrate")] == [
        "alfred_unrate.value"
    ]
    assert research_series_in(store, "other") == []


def test_an_empty_or_repeated_load_is_refused_and_leaves_the_old_series(store: Store) -> None:
    replace_research_series(store, series(), VINTAGES)
    with pytest.raises(StoreError, match="has no observations"):
        replace_research_series(store, series(), [])
    with pytest.raises(StoreError, match="repeats an observation"):
        replace_research_series(store, series(), [VINTAGES[0], VINTAGES[0]])
    assert len(research_observations(store, "alfred_unrate.value", as_of=date(2025, 1, 1))) == 2


def test_an_observation_cannot_be_public_before_its_date() -> None:
    with pytest.raises(ValueError, match="public before the date it describes"):
        obs("2024-01-31", "2024-01-30", 1.0)


@pytest.mark.parametrize(
    "overrides",
    [
        {"vendor": "bloomberg"},
        {"frequency": "weekly"},
        {"point_in_time": "guessed"},
        {"currency": "eur"},
        {"content_sha256": "A" * 64},
        {"content_sha256": "a" * 63},
        {"bias_notes": "[]"},
        {"bias_notes": "not json"},
        {"downloaded_at": "2026-10-16"},
    ],
)
def test_a_bad_series_row_is_refused(store: Store, overrides: dict[str, Any]) -> None:
    row: dict[str, Any] = {
        "series_id": "s",
        "dataset_id": "d",
        "vendor": "yahoo",
        "field": "close",
        "units": "price",
        "currency": "EUR",
        "frequency": "daily",
        "point_in_time": "as_observed",
        "source_url": "https://example.org",
        "downloaded_at": "2026-10-16T20:00:00Z",
        "content_sha256": SHA,
        "bias_notes": '["note"]',
        **overrides,
    }
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    with pytest.raises(sqlite3.IntegrityError):
        store.execute(
            f"INSERT INTO research_series ({columns}) VALUES ({marks})", list(row.values())
        )


def test_a_stored_series_that_fails_the_domain_is_a_store_error(store: Store) -> None:
    replace_research_series(store, series(), VINTAGES)
    store.execute("UPDATE research_series SET bias_notes = '[\"  \"]'")
    with pytest.raises(StoreError, match=r"research_series row 'alfred_unrate\.value' is invalid"):
        research_series_info(store, "alfred_unrate.value")


def test_a_history_row_cannot_precede_its_observation(store: Store) -> None:
    replace_research_series(store, series(), VINTAGES)
    with pytest.raises(sqlite3.IntegrityError):
        store.execute(
            "INSERT INTO research_history VALUES ('alfred_unrate.value', '2024-02-01', "
            "'2024-01-01', 1.0)"
        )


# ── the trial registry ──


def trial(trial_id: str, **overrides: Any) -> Trial:
    fields: dict[str, Any] = {
        "trial_id": trial_id,
        "study_id": "toy",
        "stage": TrialStage.EXPLORATION,
        "recorded_at": DOWNLOADED,
        "spec_sha256": SHA,
        "inputs_sha256": "c" * 64,
        "parameters": '{"seed": 7}',
        "metric": "log_growth_difference",
        "estimate": 0.01,
        "sharpe": 0.2,
        "observations": 36,
        "outcome": None,
    }
    fields.update(overrides)
    return Trial(**fields)


def test_trials_are_appended_and_never_changed(store: Store) -> None:
    record_trial(store, trial("t1"))
    record_trial(
        store,
        trial(
            "t2",
            stage=TrialStage.CONFIRMATION,
            recorded_at=datetime(2026, 10, 17, tzinfo=UTC),
            outcome="sufficient",
        ),
    )
    assert [(t.trial_id, t.stage) for t in trials_for(store, "toy")] == [
        ("t1", TrialStage.EXPLORATION),
        ("t2", TrialStage.CONFIRMATION),
    ]
    assert trials_for(store, "other") == []
    with pytest.raises(StoreError):
        record_trial(store, trial("t1"))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.execute("UPDATE research_trials SET estimate = 1")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.execute("DELETE FROM research_trials")


# ── tax lots ──


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("quantity", "abc", "tax_lots.quantity holds 'abc', which is not a decimal"),
        ("quantity", "-1", "tax_lots row 'L1' is invalid"),
    ],
)
def test_a_corrupt_lot_is_a_store_error(store: Store, column: str, value: str, message: str):
    store.execute(
        "INSERT INTO tax_lots VALUES ('L1', 'WORLD', '2025-01-02', '1', '1', '0', '0', "
        "'2025-01-02T00:00:00Z')"
    )
    store.execute(f"UPDATE tax_lots SET {column} = ?", (value,))
    with pytest.raises(StoreError, match=message):
        tax_lots(store)
