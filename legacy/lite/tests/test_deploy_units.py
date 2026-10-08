"""The units, checked against what the code actually does.

A systemd unit is the only configuration on the VM that nothing else validates:
it is not imported, not parsed by any test, and a directive in the wrong section
is logged once at load and then ignored forever. Three things were:

* ``StartLimitBurst`` sat in ``[Service]``, where systemd has not read it since
  v230, and with no ``StartLimitIntervalSec`` at all — so the restart limit the
  comment describes was not in force;
* the network-isolated compute unit read the collectors' environment file, which
  holds every IBKR credential and API key on the machine;
* ``OBLISK_IBKR_CA_BUNDLE`` was read by ``env.py`` and named in no example file,
  so the one supported way to point at a custom trust store was undiscoverable.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import configparser
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from obliskiq.collect.cli import DAILY_SEQUENCE, EXIT_DEGRADED, EXIT_OK, STEP_DEADLINE_SECTION

COLLECT_UNIT = "obliskiq-collect@.service"
COMPUTE_UNIT = "obliskiq-compute@.service"
SERVER_UNIT = "obliskiq-mcp.service"

TIMERS = (
    "obliskiq-collect@daily.timer",
    "obliskiq-collect@macro.timer",
    "obliskiq-collect@universe.timer",
    "obliskiq-compute@all.timer",
    "obliskiq-compute@census.timer",
)

#: Which timer is the equivalent of which cron line. Keyed by the entrypoint and
#: the subcommand, which is what both files agree on; the schedule is the thing
#: being compared and is deliberately not restated here.
CRON_TO_TIMER = {
    ("obliskiq-collect", "daily"): "obliskiq-collect@daily.timer",
    ("obliskiq-collect", "macro"): "obliskiq-collect@macro.timer",
    ("obliskiq-collect", "universe"): "obliskiq-collect@universe.timer",
    ("obliskiq-compute", "all"): "obliskiq-compute@all.timer",
    ("obliskiq-compute", "census"): "obliskiq-compute@census.timer",
}


def _directives(repo_root: Path, name: str) -> list[str]:
    """Every non-comment line of a unit, as systemd reads them.

    Parsed lookups keep only the last of a repeated key; systemd applies them
    all, so anything asserting a unit does NOT do something has to read the
    lines rather than the mapping.
    """
    return [
        line
        for line in (repo_root / "deploy" / name).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _unit(repo_root: Path, name: str) -> configparser.ConfigParser:
    """Parsed as systemd reads it: sections matter, and a key in the wrong one
    is not the same key."""
    parser = configparser.ConfigParser(strict=False, allow_no_value=True)
    parser.optionxform = str
    parser.read_string((repo_root / "deploy" / name).read_text(encoding="utf-8"))
    return parser


# ── restart behaviour ──


def test_the_restart_limit_is_in_the_section_systemd_reads_it_from(repo_root):
    """In [Service] it is silently ignored. The unit then retries forever on a
    dead upstream, which on a metered vendor route is a bill."""
    unit = _unit(repo_root, COLLECT_UNIT)
    assert not unit.has_option("Service", "StartLimitBurst")
    assert unit.has_option("Unit", "StartLimitBurst")
    assert unit.has_option("Unit", "StartLimitIntervalSec"), (
        "a burst with no interval is half a rule"
    )


def test_a_degraded_run_is_a_success_the_unit_does_not_retry(repo_root):
    """Re-running would spend the same API budget on the same weak inputs."""
    unit = _unit(repo_root, COLLECT_UNIT)
    assert unit["Service"]["SuccessExitStatus"] == f"{EXIT_OK} {EXIT_DEGRADED}"
    assert unit["Service"]["Restart"] == "on-failure"


# ── the network-isolated unit holds no credentials ──


def test_the_compute_unit_reads_no_file_that_holds_a_credential(repo_root):
    """PrivateNetwork stops it reaching an upstream; not handing it the keys
    stops it being the thing that leaks them. Two independent guards, which is
    the point."""
    unit = _unit(repo_root, COMPUTE_UNIT)
    assert unit["Service"]["PrivateNetwork"] == "true"
    assert unit["Service"]["EnvironmentFile"] == "/etc/obliskiq/compute.env"
    # Every directive, not just the one above: a second EnvironmentFile line
    # would be read too, and configparser keeps only the last of a repeated key.
    assert not any("collect.env" in line for line in _directives(repo_root, COMPUTE_UNIT))


def test_the_compute_environment_names_no_credential(repo_root):
    text = (repo_root / "deploy" / "compute.env.example").read_text(encoding="utf-8")
    for forbidden in ("IBKR", "FRED_API_KEY", "EIA_API_KEY", "MARKETDATA", "AUTH_TOKEN"):
        assert forbidden not in text, f"{forbidden} has no business in a unit that cannot connect"


#: One environment file per entrypoint, and which one each unit must read. The
#: split is the design: the server needs no IBKR credentials and the collectors
#: need no auth token, so a unit reading the wrong file hands a process every
#: secret on the machine to do a job that needs none of them.
UNIT_ENVIRONMENT = {
    COLLECT_UNIT: "/etc/obliskiq/collect.env",
    COMPUTE_UNIT: "/etc/obliskiq/compute.env",
    SERVER_UNIT: "/etc/obliskiq/server.env",
}


def test_each_unit_reads_its_own_environment_file_and_only_that_one(repo_root):
    """Three files, three units, asserted for all three.

    Only the compute unit's EnvironmentFile was ever pinned, so the 3-file
    pattern held by habit at two of its three corners: the collect unit could
    have been pointed at server.env, or the server unit at collect.env — which
    is the credential-splitting failure this file already documents, in the two
    directions nothing was watching.

    Every directive is read rather than the parsed value, because configparser
    keeps only the LAST of a repeated key and systemd reads them all: a second
    EnvironmentFile line would be in force and invisible to a parsed lookup.
    """
    for unit, expected in UNIT_ENVIRONMENT.items():
        declared = [
            line.split("=", 1)[1].strip()
            for line in _directives(repo_root, unit)
            if line.split("=", 1)[0].strip() == "EnvironmentFile"
        ]
        assert declared == [expected], (
            f"{unit} reads {declared} and must read exactly ['{expected}']"
        )

    assert sorted(UNIT_ENVIRONMENT.values()) == sorted(set(UNIT_ENVIRONMENT.values())), (
        "two units share an environment file, which is not the three-file split"
    )
    for path in UNIT_ENVIRONMENT.values():
        example = repo_root / "deploy" / (Path(path).name + ".example")
        assert example.is_file(), f"{example.name} is read by a unit and not shipped"


def test_no_network_isolated_unit_reads_a_file_that_holds_a_credential(repo_root):
    """Driven by PrivateNetwork=true, not by a filename.

    The rule is about the property, not about the compute unit: a process the
    kernel has cut off from the network has no use for an API key, and handing it
    one is a blast radius bought for nothing. Hardcoding `compute.env.example`
    made the guard true of today's units and silent about tomorrow's, so a second
    isolated unit pointed at collect.env would be caught by nothing here.
    """
    isolated = [
        unit
        for unit in UNIT_ENVIRONMENT
        if _unit(repo_root, unit)["Service"].get("PrivateNetwork") == "true"
    ]
    assert isolated, "no unit declares PrivateNetwork=true; this guard has lost its subject"

    for unit in isolated:
        for path in [
            line.split("=", 1)[1].strip()
            for line in _directives(repo_root, unit)
            if line.split("=", 1)[0].strip() == "EnvironmentFile"
        ]:
            example = repo_root / "deploy" / (Path(path).name + ".example")
            assert example.is_file(), f"{unit} reads {path} and no example ships for it"
            text = example.read_text(encoding="utf-8")
            for forbidden in ("IBKR", "FRED_API_KEY", "EIA_API_KEY", "MARKETDATA", "AUTH_TOKEN"):
                assert forbidden not in text, (
                    f"{unit} has PrivateNetwork=true and reads {example.name}, which names "
                    f"{forbidden}. A process that cannot connect has no use for a credential."
                )


def test_the_compute_environment_carries_what_the_entrypoint_actually_requires(repo_root):
    """OBLISK_DB_PATH is the one variable load_settings() refuses to default."""
    text = (repo_root / "deploy" / "compute.env.example").read_text(encoding="utf-8")
    for required in ("OBLISK_DB_PATH", "OBLISK_CONFIG_PATH"):
        assert f"{required}=" in text


def test_the_collector_environment_names_the_ca_bundle_env_py_reads(repo_root):
    """An unreadable bundle is a startup error and never a downgrade, so the
    variable has to be discoverable or the only supported route to a custom
    trust store is reading the source."""
    text = (repo_root / "deploy" / "collect.env.example").read_text(encoding="utf-8")
    assert "OBLISK_IBKR_CA_BUNDLE" in text


# ── the schedule ──


@pytest.mark.parametrize("name", TIMERS)
def test_every_timer_catches_up_a_missed_run(repo_root, name):
    """Persistent=true. For a series that cannot be backfilled, it is the
    difference between a gap and no gap."""
    assert _unit(repo_root, name)["Timer"]["Persistent"] == "true"


@pytest.mark.parametrize("name", TIMERS)
def test_every_timer_names_a_unit_that_exists(repo_root, name):
    unit = _unit(repo_root, name)["Timer"]["Unit"]
    template = unit.split("@")[0] + "@.service" if "@" in unit else unit
    assert (repo_root / "deploy" / template).is_file(), f"{name} points at a unit nobody ships"


def test_every_scheduled_job_is_a_timer_and_not_only_a_cron_line(repo_root):
    """crontab.example carried a weekly universe sync with no timer counterpart,
    so the documented fallback did MORE than the documented default. Cron cannot
    catch up a missed run and cannot tell exit 2 from exit 0."""
    cron = (repo_root / "deploy" / "crontab.example").read_text(encoding="utf-8")
    scheduled = {
        line.split("obliskiq-collect")[1].split()[0]
        for line in cron.splitlines()
        if "obliskiq-collect " in line and not line.lstrip().startswith("#")
    }
    timers = {
        name.split("@")[1].removesuffix(".timer")
        for name in TIMERS
        if name.startswith("obliskiq-collect@")
    }
    assert scheduled <= timers, f"cron runs {scheduled - timers} with no timer for it"


def test_the_daily_timer_describes_the_sequence_the_code_runs(repo_root):
    """A header comment naming the wrong order is how the reordering would be
    discovered by whoever debugs it at 03:00 rather than by whoever changed it."""
    header = (repo_root / "deploy" / "obliskiq-collect@daily.timer").read_text(encoding="utf-8")
    positions = [header.index(step) for step in DAILY_SEQUENCE]
    assert positions == sorted(positions), "the comment lists the steps in the wrong order"


# ── the cron fallback fires at the same TIME, not merely on the same job ──


def _timer_times(repo_root: Path, name: str) -> set[tuple[int, int]]:
    """Every (hour, minute) an OnCalendar line fires at.

    Narrow on purpose: it reads the two forms this repository actually ships —
    ``Mon..Fri 20:15 UTC`` and ``*-*-* 06,14:00:00 UTC`` — and raises on
    anything else rather than guessing. A parser that silently returned the empty
    set for a form it did not know would make this whole comparison vacuous,
    which is the failure mode of the test it sits beside.
    """
    spec = _unit(repo_root, name)["Timer"]["OnCalendar"].replace("UTC", "").strip()
    # Drop the weekday and date heads, whatever they are; what is left is the
    # time. It must be ONE token: this used to re-join `06,14 :00:00`, which
    # systemd rejects as Invalid argument, so the macro timer never loaded while
    # this test read it as 06:00 and 14:00 and called the schedule verified.
    time_spec = "".join(token for token in spec.split() if ":" in token)
    parts = time_spec.split(":")
    if len(parts) not in (2, 3) or not parts[0] or not parts[1]:
        raise AssertionError(f"{name}: cannot read a time out of OnCalendar={spec!r}")
    return {
        (int(hour), int(minute)) for hour in parts[0].split(",") for minute in parts[1].split(",")
    }


def _cron_jobs(repo_root: Path) -> dict[tuple[str, str], set[tuple[int, int]]]:
    """Every (entrypoint, subcommand) cron runs, and the times it runs it at."""
    jobs: dict[tuple[str, str], set[tuple[int, int]]] = {}
    for line in (repo_root / "deploy" / "crontab.example").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 7 or not fields[0][0].isdigit():
            continue
        minutes, hours = fields[0], fields[1]
        entrypoint, subcommand = fields[5], fields[6]
        times = {
            (int(hour), int(minute)) for hour in hours.split(",") for minute in minutes.split(",")
        }
        jobs.setdefault((entrypoint, subcommand), set()).update(times)
    return jobs


#: Weekday sets, as the two files spell them. cron counts 0-6 from Sunday and
#: systemd names them; both are normalised to Monday=0 so they can be compared.
_CRON_DAYS = {"0": 6, "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6}
_TIMER_DAYS = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}


def _cron_days(repo_root: Path) -> dict[tuple[str, str], frozenset[int]]:
    """Which weekdays each cron line fires on, Monday=0.

    Only the day-of-week field is read. Every line this repository ships leaves
    day-of-month and month at ``*``; a line that narrowed either would be a
    schedule neither this nor _timer_days can express, and the assertion below
    would fail rather than compare half of it.
    """
    days: dict[tuple[str, str], frozenset[int]] = {}
    for line in (repo_root / "deploy" / "crontab.example").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 7 or not fields[0][0].isdigit():
            continue
        day_of_month, month, weekday = fields[2], fields[3], fields[4]
        assert (day_of_month, month) == ("*", "*"), (
            f"{' '.join(fields[5:7])} narrows day-of-month or month, which no timer here "
            "expresses; compare it by hand or widen this parser deliberately"
        )
        if weekday == "*":
            chosen = frozenset(range(7))
        else:
            chosen = frozenset()
            for part in weekday.split(","):
                if "-" in part:
                    first, last = (_CRON_DAYS[bound] for bound in part.split("-"))
                    chosen |= frozenset(range(first, last + 1))
                else:
                    chosen |= {_CRON_DAYS[part]}
        days[(fields[5], fields[6])] = chosen
    return days


def _timer_days(repo_root: Path, name: str) -> frozenset[int]:
    """Which weekdays a timer's OnCalendar fires on, Monday=0.

    Narrow like :func:`_timer_times`, and raising for the same reason: a parser
    that returned "every day" for a form it did not recognise would make the
    comparison vacuous in the direction that hides a drift.
    """
    spec = _unit(repo_root, name)["Timer"]["OnCalendar"].replace("UTC", "").strip()
    head = spec.split()[0]
    if head.startswith("*-*-*") or head[0].isdigit():
        return frozenset(range(7))  # a date head, or none: every day
    chosen: frozenset[int] = frozenset()
    for part in head.split(","):
        if ".." in part:
            first, last = (_TIMER_DAYS[bound] for bound in part.split(".."))
            chosen |= frozenset(range(first, last + 1))
        elif part in _TIMER_DAYS:
            chosen |= {_TIMER_DAYS[part]}
        else:
            raise AssertionError(f"{name}: cannot read weekdays out of OnCalendar={spec!r}")
    return chosen


def test_every_cron_line_fires_on_its_timer_s_DAYS(repo_root):
    """The other half of the schedule, and it was compared by nobody.

    `test_every_cron_line_fires_at_its_timer_s_hour` reads the time fields and
    discards the rest, so `15 20 * * 1-5` and `15 20 * * *` are the same line to
    it — one collects on weekdays, the other adds Saturday and Sunday runs against
    closed markets. The same blind spot in reverse would turn the weekly universe
    sync into a nightly one. Verified by hand at the time of writing; asserted
    here so it stays that way.
    """
    days = _cron_days(repo_root)
    assert set(days) == set(CRON_TO_TIMER), (
        f"cron and the timer map disagree about which jobs exist: {set(days) ^ set(CRON_TO_TIMER)}"
    )
    for job, cron_weekdays in sorted(days.items()):
        timer = CRON_TO_TIMER[job]
        assert cron_weekdays == _timer_days(repo_root, timer), (
            f"cron runs {' '.join(job)} on weekdays {sorted(cron_weekdays)} and {timer} fires "
            f"on {sorted(_timer_days(repo_root, timer))} (Monday=0). One of them is wrong, and "
            "the cron file is the one that calls itself the equivalent."
        )


def test_every_shipped_timer_is_in_the_cron_map(repo_root):
    """The parity check runs off CRON_TO_TIMER, which is kept by hand.

    `test_every_cron_line_fires_at_its_timer_s_hour` asserts cron's jobs equal
    that map's keys, so a cron line with no timer is caught — but a TIMER with no
    cron line is not: it is absent from both the map and the crontab, and the two
    still agree with each other. Adding a sixth timer and forgetting the cron
    fallback would pass every test in this file. This is the missing direction.
    """
    assert set(CRON_TO_TIMER.values()) == set(TIMERS), (
        f"the cron map and the shipped timers disagree: {set(CRON_TO_TIMER.values()) ^ set(TIMERS)}"
    )
    for timer in TIMERS:
        assert (repo_root / "deploy" / timer).is_file(), f"{timer} is named but not shipped"


def test_every_cron_line_fires_at_its_timer_s_hour(repo_root):
    """The cron file calls itself the equivalent of the timers, and drifted.

    It fired `daily` at 22:30 while the timer fired at 20:15 — and the timer's own
    header records why 22:30 is wrong: US option prices collapse between 21:08 and
    23:04, so a no-systemd deployment following the shipped crontab collected a
    largely unpriced chain every night. It drifted precisely because
    ``test_every_scheduled_job_is_a_timer_and_not_only_a_cron_line`` above compares
    WHICH commands cron schedules and never their TIMES. This compares the times.
    """
    jobs = _cron_jobs(repo_root)
    assert set(jobs) == set(CRON_TO_TIMER), (
        f"cron and the timer map disagree about which jobs exist: {set(jobs) ^ set(CRON_TO_TIMER)}"
    )
    for job, cron_times in sorted(jobs.items()):
        timer = CRON_TO_TIMER[job]
        assert cron_times == _timer_times(repo_root, timer), (
            f"cron runs {' '.join(job)} at {sorted(cron_times)} and {timer} fires at "
            f"{sorted(_timer_times(repo_root, timer))}. One of them is wrong, and the "
            "cron file is the one that calls itself the equivalent."
        )


@pytest.mark.parametrize("name", TIMERS)
def test_systemd_itself_parses_every_oncalendar(repo_root, name):
    """Asked of systemd, because the parser above is ours.

    ``_timer_times`` once accepted ``*-*-* 06,14 :00:00 UTC``, which systemd
    answers with "Invalid argument": the macro timer would not load, `list-timers`
    would show four timers where the walkthrough promises five, and no test said so.
    Skipped where systemd-analyze is absent; CI's runners have it. Every timer is
    also required to name UTC, since ``_timer_times`` strips the word and a timer
    that lost it would otherwise still compare equal to a table that says UTC.
    """
    spec = _unit(repo_root, name)["Timer"]["OnCalendar"]
    assert spec.endswith(" UTC"), f"{name}: OnCalendar={spec!r} does not say UTC"
    analyze = shutil.which("systemd-analyze")
    if analyze is None:
        pytest.skip("systemd-analyze is not installed here")
    result = subprocess.run(
        [analyze, "calendar", spec], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"{name}: systemd rejects OnCalendar={spec!r}: {result.stderr}"


# ── the walkthrough's schedule table says what the timers say ──

#: Where the table sits, and the words it uses for days. Narrow on purpose, like
#: ``_timer_times``: a form this does not know raises instead of being guessed at.
_WALKTHROUGH = ("docs", "DEPLOY.md")
_TABLE_MARKER = "**What runs when**"
_TABLE_DAYS = {
    "Weekdays": frozenset(range(5)),
    "Saturdays": frozenset({5}),
    "daily": frozenset(range(7)),
}


#: A job's schedule as the walkthrough table states it: (times, weekdays), Monday=0.
_Schedule = tuple[set[tuple[int, int]], frozenset[int]]


def _walkthrough_schedule(repo_root: Path) -> dict[str, _Schedule]:
    """Each job in the walkthrough's *What runs when* table, as (times, weekdays).

    The job is the first column (`collect@daily`); the second says when, in the
    two words this repository uses — ``Weekdays 20:15``, ``Saturdays 05:00``,
    ``06:00 and 14:00 daily`` — and anything else is an AssertionError.
    """
    text = repo_root.joinpath(*_WALKTHROUGH).read_text(encoding="utf-8")
    assert text.count(_TABLE_MARKER) == 1, (
        f"{'/'.join(_WALKTHROUGH)} should hold exactly one {_TABLE_MARKER!r}; the table this "
        "test compares against the timers cannot be found, and passing without it would be "
        "the same blind spot the table already had"
    )
    rows = {}
    in_table = False
    for line in text.split(_TABLE_MARKER, 1)[1].splitlines():
        if line.startswith("|"):
            in_table = True
        elif in_table:
            break  # the first line that is not a row ends the table
        else:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        match = re.fullmatch(r"`([a-z]+@[a-z]+)`", cells[0])
        if match is None:
            continue  # the header and its rule
        when = cells[1]
        words = [word for word in _TABLE_DAYS if word in when]
        times = {(int(h), int(m)) for h, m in re.findall(r"\b(\d{2}):(\d{2})\b", when)}
        assert len(words) == 1 and times, (
            f"{match.group(1)}: cannot read days and times out of {when!r}; the table uses "
            f"{sorted(_TABLE_DAYS)} and HH:MM"
        )
        rows[match.group(1)] = (times, _TABLE_DAYS[words[0]])
    return rows


def test_the_walkthrough_schedule_table_matches_the_timers(repo_root):
    """The walkthrough said 22:30 for as long as the timer said 20:15.

    Session 9.5 moved the daily timer and the crontab, wrote a test that compares
    those two, and left the third copy of the schedule — the table in the document a
    newcomer follows — saying the hour the timer's header records as being on the
    wrong side of the price collapse. Nothing compared it. This does, for every job,
    on both the time and the days.
    """
    table = _walkthrough_schedule(repo_root)
    expected = {name.removeprefix("obliskiq-").removesuffix(".timer"): name for name in TIMERS}
    assert set(table) == set(expected), (
        f"the walkthrough table and the shipped timers disagree about which jobs exist: "
        f"{set(table) ^ set(expected)}"
    )
    for job, (times, days) in sorted(table.items()):
        timer = expected[job]
        assert times == _timer_times(repo_root, timer), (
            f"the walkthrough says {job} runs at {sorted(times)} and {timer} fires at "
            f"{sorted(_timer_times(repo_root, timer))}. The timer is the one that runs."
        )
        assert days == _timer_days(repo_root, timer), (
            f"the walkthrough says {job} runs on weekdays {sorted(days)} and {timer} fires on "
            f"{sorted(_timer_days(repo_root, timer))} (Monday=0)."
        )


# ── the sequence bounds itself before systemd bounds the process ──


def _step_budgets(repo_root: Path) -> dict[str, float]:
    with (repo_root / "config.yaml").open(encoding="utf-8") as handle:
        node = yaml.safe_load(handle)
    for part in STEP_DEADLINE_SECTION.split("."):
        node = node[part]
    return {str(step): float(seconds) for step, seconds in node.items()}


def test_every_step_of_the_daily_sequence_has_a_wall_clock_budget(repo_root):
    """An unbounded step is the condition the whole mechanism exists to remove.

    A step with no budget runs until the unit's TimeoutStartSec expires on it,
    which is a SIGTERM — process death, not an exception — so the per-step
    try/except in collect/cli.py never sees it and every step after it is lost.
    """
    budgets = _step_budgets(repo_root)
    assert set(budgets) == set(DAILY_SEQUENCE), (
        f"{STEP_DEADLINE_SECTION} and DAILY_SEQUENCE disagree: {set(budgets) ^ set(DAILY_SEQUENCE)}"
    )
    assert all(seconds > 0 for seconds in budgets.values())


def test_the_steps_are_bounded_inside_the_unit_s_own_timeout(repo_root):
    """The sum is the constraint, and it is what makes these one decision.

    The sequence has to reach its own deadline before systemd reaches
    TimeoutStartSec, or the budgets are decoration: whichever step is running
    when the unit gives up dies with the process and takes the rest with it.
    """
    timeout = int(_unit(repo_root, COLLECT_UNIT)["Service"]["TimeoutStartSec"])
    budgets = _step_budgets(repo_root)
    assert sum(budgets.values()) <= timeout, (
        f"the per-step budgets sum to {sum(budgets.values()):g}s against "
        f"TimeoutStartSec={timeout}. Lower one of them, or raise the unit."
    )


def test_the_irreplaceable_step_gets_the_largest_budget(repo_root):
    """`chain` is the three European listings, which no amount of money recovers.

    Every other step is re-collectible — bars at any time, curves rung by rung,
    the vendor chains re-bought, macro tomorrow for free — so a budget that let
    one of them spend the window `chain` needs would have the priority exactly
    backwards. This is DAILY_SEQUENCE's own ranking, asserted in seconds.
    """
    budgets = _step_budgets(repo_root)
    largest = max(budgets, key=lambda step: budgets[step])
    assert largest == "chain", (
        f"{largest!r} has a larger wall-clock budget than 'chain'. The European chains "
        "are the only series here that cannot be re-collected at any price."
    )


# ── nothing is hardcoded that the environment should own ──


@pytest.mark.parametrize("name", [COLLECT_UNIT, COMPUTE_UNIT, SERVER_UNIT])
def test_no_unit_carries_a_secret_or_a_port(repo_root, name):
    """Secrets, paths and ports come from the environment. A unit names the
    environment FILE and the install prefix, which is what a unit is for, and
    never a value from inside it."""
    text = (repo_root / "deploy" / name).read_text(encoding="utf-8")
    body = "\n".join(
        line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    )
    for forbidden in ("TOKEN=", "SECRET=", "API_KEY=", "--port", "--host"):
        assert forbidden not in body


@pytest.mark.parametrize("name", [COLLECT_UNIT, COMPUTE_UNIT])
def test_the_scheduled_units_stay_oneshot_and_bounded(repo_root, name):
    """Type=oneshot against a live-session-token TTL far longer than
    TimeoutStartSec is what keeps the missing token-renewal path from biting.
    A daemon here would need one."""
    unit = _unit(repo_root, name)["Service"]
    assert unit["Type"] == "oneshot"
    assert int(unit["TimeoutStartSec"]) > 0
