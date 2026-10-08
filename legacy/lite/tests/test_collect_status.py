"""The morning-after report, and what it refuses to say.

The objective this exists for is "failure is reported, not inferred". An exit
code is only seen by whatever invoked the process, and on an unattended VM that
is systemd, which records it and moves on. So the second channel is a row in the
store — and this is the one command that reads it.

The channel is deliberately NOT a webhook or a notification service. A collector
that phones home is a new outbound path, a new secret and a new thing to get
wrong on the one host whose job is to need no attention.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from obliskiq.collect.runlog import CollectorRun, RunStatus, reap_interrupted_runs
from obliskiq.collect.sequence import SequenceRun
from obliskiq.collect.status import status_report
from obliskiq.util.timeutil import iso_date, utc_stamp, utc_today

TODAY = iso_date(utc_today())


def _sequence(store, *, session=TODAY, steps=("backfill", "chain"), verdicts=None, notes=None):
    with SequenceRun(store, "daily", session_date=session, steps=steps) as sequence:
        if notes:
            sequence.notes.update(notes)
        for step, (status, reason) in (verdicts or {}).items():
            sequence.step(step, status, reason=reason)
    return sequence


def test_an_empty_store_says_nothing_ran_rather_than_all_clear(store, config):
    """An empty report that read as success would be the failure this package
    exists to prevent, wearing a status command."""
    report = status_report(store, config)
    assert "none." in report
    assert "recorded a sequence in this window" in report


def test_a_clean_night_is_reported_without_a_marker(store, config):
    _sequence(store, verdicts={"backfill": (RunStatus.OK, None), "chain": (RunStatus.OK, None)})
    report = status_report(store, config)
    assert f"{TODAY}" in report
    assert " ok " in report
    assert "!" not in report.split("Runs a killed process")[0]


def test_the_step_that_degraded_and_the_reason_both_appear(store, config):
    _sequence(
        store,
        verdicts={
            "backfill": (RunStatus.OK, None),
            "chain": (RunStatus.DEGRADED, "FCX fell to the configured default sigma"),
        },
    )
    report = status_report(store, config)
    assert "chain: degraded" in report
    assert "FCX fell to the configured default sigma" in report


def test_the_step_that_failed_names_its_upstream(store, config):
    _sequence(
        store,
        steps=("chain", "vendor-chain"),
        verdicts={
            "chain": (RunStatus.OK, None),
            "vendor-chain": (RunStatus.ERROR, "UpstreamError('marketdata.app returned 503')"),
        },
    )
    assert "503" in status_report(store, config)


def test_an_incomplete_sequence_is_marked_as_such(store, config):
    """The half-collected day, said out loud rather than left to arithmetic."""
    sequence = SequenceRun(store, "daily", session_date=TODAY, steps=("backfill", "chain", "curve"))
    sequence.__enter__()
    sequence.step("backfill", RunStatus.OK)
    # killed here

    report = status_report(store, config)
    assert "1/3 steps" in report
    assert "INCOMPLETE" in report


def test_stale_bars_are_reported_because_they_changed_every_band_that_night(store, config):
    _sequence(
        store,
        verdicts={"backfill": (RunStatus.OK, None), "chain": (RunStatus.OK, None)},
        notes={"daily_bars_fresh": False, "freshness_days": 5},
    )
    report = status_report(store, config)
    assert "daily bars were STALE" in report


def test_fresh_bars_are_not_worth_a_line(store, config):
    _sequence(
        store,
        verdicts={"backfill": (RunStatus.OK, None), "chain": (RunStatus.OK, None)},
        notes={"daily_bars_fresh": True, "freshness_days": 5},
    )
    assert "STALE" not in status_report(store, config)


def test_a_run_a_killed_process_left_behind_is_surfaced(store, config):
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO collector_runs (collector, started_at, status) VALUES (?, ?, ?)",
            ("chain", utc_stamp(), RunStatus.RUNNING),
        )
    report = status_report(store, config)
    assert "Runs a killed process left behind" in report
    assert "chain" in report.split("Runs a killed process left behind")[1]


def test_a_reaped_run_still_appears_so_the_night_it_died_is_not_lost(store, config):
    window = config.require_int("collectors.run_stale_after_seconds")
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO collector_runs (collector, started_at, status) VALUES (?, ?, ?)",
            (
                "chain",
                utc_stamp(datetime.now(UTC) - timedelta(seconds=window + 60)),
                RunStatus.RUNNING,
            ),
        )
    reap_interrupted_runs(store, config)
    assert RunStatus.INTERRUPTED in status_report(store, config)


def test_the_last_run_of_every_collector_is_listed(store, config):
    for collector, rows in (("chain", 900), ("curve", 12), ("macro", 4100)):
        with CollectorRun(store, collector) as run:
            run.succeeded(collector, rows)
    report = status_report(store, config).split("Most recent run per collector")[1]
    for collector in ("chain", "curve", "macro"):
        assert collector in report


def test_the_window_comes_from_config_and_excludes_what_falls_outside_it(store, config):
    old = iso_date(utc_today() - timedelta(days=90))
    _sequence(store, session=old, verdicts={"chain": (RunStatus.OK, None)})
    _sequence(store, verdicts={"chain": (RunStatus.OK, None)})

    default = config.require_int("collectors.status_report_days")
    assert old not in status_report(store, config, days=default)
    assert old in status_report(store, config, days=365)


def test_the_report_reads_the_store_and_asks_no_upstream(repo_root):
    """It is the failure channel, so it must work when every upstream is down —
    and must never itself become a reason to open a socket."""
    source = (repo_root / "src" / "obliskiq" / "collect" / "status.py").read_text(encoding="utf-8")
    for forbidden in ("httpx", "requests", "socket", "urllib", "MarketData", "await "):
        assert forbidden not in source
