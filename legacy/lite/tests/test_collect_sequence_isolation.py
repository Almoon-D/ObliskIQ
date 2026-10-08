"""A failing step does not take the ones after it down with it.

``docs/COLLECTORS.md`` said, in as many words, that it could: the sequence caught
only ``NoMatchingTargets``, so any other exception escaping a step abandoned
every step after it — and the backfill step sat outside the loop entirely, with
no guard at all. The order put ``vendor-chain`` before ``chain``, which meant a
marketdata.app outage could cost a European chain snapshot that cannot be
recovered at any price.

What is checked here is the property, not the plumbing: whatever a step does, the
steps after it still run, the sequence records what each one did, and the step
that cannot be re-collected tomorrow goes first.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from obliskiq.collect import cli
from obliskiq.collect.runlog import RunStatus
from obliskiq.config import Config
from obliskiq.errors import (
    NoMatchingTargets,
    ObliskError,
    StepDeadlineExceeded,
    StoreError,
    UpstreamError,
)

SESSION = "2026-08-24"


# ── the order ──


def test_the_irreplaceable_step_runs_before_the_ones_that_can_be_re_bought():
    """The EU chain is the only series a missed day loses permanently. Everything
    after it is recoverable: the curve rung by rung, the US chains from the
    vendor for credits, macro for free."""
    order = list(cli.DAILY_SEQUENCE)
    assert order.index("chain") < order.index("vendor-chain")
    assert order.index("chain") < order.index("macro")
    assert order.index("backfill") < order.index("chain"), (
        "the chain sizes its strike bands from daily bars, so the bars still run first"
    )
    assert order[-1] == "macro", "the only step that can be caught up tomorrow"


def test_every_step_of_the_sequence_can_be_skipped_by_name():
    """--skip has to name the same steps the sequence runs, or a step becomes
    unskippable during an incident."""
    parser = cli.build_parser()
    for step in cli.DAILY_SEQUENCE:
        assert parser.parse_args(["daily", "--skip", step]).skip == [step]


def test_a_step_verdict_carries_the_reason_the_run_gave(store, config, monkeypatch):
    """A step recorded 'degraded' with nothing saying which target fell back is a
    status somebody has to investigate rather than one that has been reported."""
    from obliskiq.collect.runlog import CollectorRun

    with CollectorRun(store, "chain") as run:
        run.succeeded("FCX", 12, degraded=True)
        run.succeeded("SPY", 400)
    verdict = cli.StepVerdict(run.status(), cli._reason_for(run))

    assert verdict.status == RunStatus.DEGRADED
    assert "FCX" in verdict.reason
    assert "SPY" not in verdict.reason, "only the target that fell back is named"


def test_a_failed_target_names_itself_and_its_upstream(store, config):
    from obliskiq.collect.runlog import CollectorRun

    with CollectorRun(store, "curve") as run:
        run.failed("CL", "UpstreamError('COMEX returned {}')")
        run.succeeded("NG", 8)
    reason = cli._reason_for(run)
    assert "CL" in reason and "COMEX" in reason


def test_a_clean_run_offers_no_reason_rather_than_an_empty_one(store, config):
    from obliskiq.collect.runlog import CollectorRun

    with CollectorRun(store, "curve") as run:
        run.succeeded("CL", 8)
    assert cli._reason_for(run) is None


def test_the_exit_codes_are_one_definition():
    """collect.sequence restates them because the CLI imports it and the cycle
    would run the wrong way. Pinned so the two cannot drift."""
    from obliskiq.collect import sequence

    assert (sequence.EXIT_OK, sequence.EXIT_FAILED, sequence.EXIT_DEGRADED) == (
        cli.EXIT_OK,
        cli.EXIT_FAILED,
        cli.EXIT_DEGRADED,
    )


# ── isolation ──


def _sequence_row(store):
    return store.execute("SELECT * FROM collector_sequences").fetchone()


def _steps_of(row):
    return {entry["step"]: entry["status"] for entry in json.loads(row["detail"])["steps"]}


@pytest.mark.parametrize(
    "raised",
    [
        StoreError("the universe table is empty"),
        UpstreamError("marketdata.app returned 503"),
        RuntimeError("something nobody wrapped"),
    ],
)
async def test_a_step_that_raises_anything_is_recorded_and_stepped_over(
    store, config, monkeypatch, raised
):
    """Including a bare RuntimeError. The sequence must not depend on every
    upstream failure having been wrapped in this package's own hierarchy."""
    ran: list[str] = []

    async def fake_step(command, context, **kwargs):
        ran.append(command)
        if command == "chain":
            raise raised
        return cli.StepVerdict(RunStatus.OK)

    monkeypatch.setattr(cli, "_run_single", fake_step)
    context = _context(store, config)
    await cli._run_daily(context)

    assert ran == list(cli.DAILY_SEQUENCE), "every later step still ran"
    row = _sequence_row(store)
    steps = _steps_of(row)
    assert steps["chain"] == RunStatus.ERROR
    assert steps["curve"] == RunStatus.OK
    assert row["status"] == RunStatus.PARTIAL
    assert row["exit_code"] == cli.EXIT_FAILED


async def test_the_reason_a_step_failed_reaches_the_store(store, config, monkeypatch):
    """The morning after, nobody has the terminal that printed the traceback."""

    async def fake_step(command, context, **kwargs):
        if command == "vendor-chain":
            raise UpstreamError("marketdata.app returned 503")
        return cli.StepVerdict(RunStatus.OK)

    monkeypatch.setattr(cli, "_run_single", fake_step)
    await cli._run_daily(_context(store, config))

    detail = json.loads(_sequence_row(store)["detail"])
    failed = next(entry for entry in detail["steps"] if entry["step"] == "vendor-chain")
    assert "503" in failed["reason"]


async def test_the_backfill_step_is_guarded_like_every_other_one(store, config, monkeypatch):
    """It used to sit outside the loop, so a StoreError there abandoned the whole
    day — including the one snapshot that cannot be taken again."""
    ran: list[str] = []

    async def fake_step(command, context, **kwargs):
        ran.append(command)
        if command == "backfill":
            raise StoreError("no bars table")
        return cli.StepVerdict(RunStatus.OK)

    monkeypatch.setattr(cli, "_run_single", fake_step)
    await cli._run_daily(_context(store, config))

    assert "chain" in ran, "the irreplaceable snapshot still happened"
    assert _steps_of(_sequence_row(store))["backfill"] == RunStatus.ERROR


async def test_a_filter_naming_none_of_a_steps_targets_is_a_skip_not_a_failure(
    store, config, monkeypatch
):
    """--only SPY is meaningful to the chain collector and meaningless to the
    curve collector, which tracks futures roots."""

    async def fake_step(command, context, **kwargs):
        if command == "curve":
            raise NoMatchingTargets("--only named no futures root")
        return cli.StepVerdict(RunStatus.OK)

    monkeypatch.setattr(cli, "_run_single", fake_step)
    await cli._run_daily(_context(store, config, only=["SPY"]))

    row = _sequence_row(store)
    assert _steps_of(row)["curve"] == RunStatus.SKIPPED
    assert row["status"] == RunStatus.OK, "a scoped run that collected what it could is not failed"
    assert row["exit_code"] == cli.EXIT_OK


async def test_a_skipped_step_does_not_count_as_collected(store, config, monkeypatch):
    async def fake_step(command, context, **kwargs):
        return cli.StepVerdict(RunStatus.OK)

    monkeypatch.setattr(cli, "_run_single", fake_step)
    await cli._run_daily(_context(store, config, skip={"macro", "vendor-chain"}))

    row = _sequence_row(store)
    assert row["steps_planned"] == len(cli.DAILY_SEQUENCE)
    assert row["steps_completed"] == len(cli.DAILY_SEQUENCE) - 2
    assert _steps_of(row)["macro"] == RunStatus.SKIPPED


async def test_the_bars_freshness_verdict_is_recorded_not_merely_logged(store, config, monkeypatch):
    """It decided the quality of every strike band collected that night and left
    no trace in the store, so months later nothing said why a band was wide."""

    async def fake_step(command, context, **kwargs):
        return cli.StepVerdict(RunStatus.OK)

    monkeypatch.setattr(cli, "_run_single", fake_step)
    monkeypatch.setattr(cli, "daily_bars_are_fresh", lambda store, config: False)
    await cli._run_daily(_context(store, config))

    notes = json.loads(_sequence_row(store)["detail"])["notes"]
    assert notes["daily_bars_fresh"] is False
    assert "freshness_days" in notes


# ── a route that cannot run says so, and one that was told not to does not ──


async def test_a_step_that_could_not_run_degrades_the_whole_sequence(store, config, monkeypatch):
    """What a fresh install does, asserted at the level the scheduler reads.

    deploy/collect.env.example ships MARKETDATA_API_TOKEN empty while config.yaml
    ships option_vendor.enabled true, so `vendor-chain` collects nothing every
    night. Recorded SKIPPED it exited 0 and the timer reported success on a
    machine photographing zero US option chains.
    """

    async def fake_step(command, context, **kwargs):
        if command == "vendor-chain":
            return cli.StepVerdict(RunStatus.DEGRADED, "MARKETDATA_API_TOKEN is unset")
        return cli.StepVerdict(RunStatus.OK)

    monkeypatch.setattr(cli, "_run_single", fake_step)
    await cli._run_daily(_context(store, config))

    row = _sequence_row(store)
    assert row["status"] == RunStatus.DEGRADED
    assert row["exit_code"] == cli.EXIT_DEGRADED, "the scheduler must not read this as success"


async def test_a_route_told_not_to_run_leaves_the_sequence_ok(store, config, monkeypatch):
    """The other half of the same distinction, and it is the half that must not move.

    `option_vendor.enabled: false` is a decision somebody took. A night that
    honoured it is healthy, and reporting it as degraded would make the flag
    useless — every run would need looking at for having done what it was told.
    """

    async def fake_step(command, context, **kwargs):
        if command == "vendor-chain":
            raise NoMatchingTargets("option_vendor.enabled is false")
        return cli.StepVerdict(RunStatus.OK)

    monkeypatch.setattr(cli, "_run_single", fake_step)
    await cli._run_daily(_context(store, config))

    row = _sequence_row(store)
    assert _steps_of(row)["vendor-chain"] == RunStatus.SKIPPED
    assert row["status"] == RunStatus.OK
    assert row["exit_code"] == cli.EXIT_OK


# ── the wall-clock budget ──


async def test_a_step_that_outruns_its_budget_is_abandoned_and_the_rest_still_run(
    store, config, monkeypatch
):
    """The guard above catches a step that RAISES. This one catches the step that
    simply never returns.

    Until the budget existed, the only thing bounding a hung step was the unit's
    TimeoutStartSec — and that expiring is a SIGTERM, which is process death
    rather than an exception, so the try/except never sees it, every step after
    it is lost, and Restart=on-failure re-runs the whole sequence from the top,
    re-spending marketdata.app credits each time.
    """
    ran: list[str] = []

    async def fake_step(command, context, *, deadline_seconds=None, **kwargs):
        ran.append(command)
        if command == "backfill":
            raise StepDeadlineExceeded(f"budget of {deadline_seconds}s")
        return cli.StepVerdict(RunStatus.OK)

    monkeypatch.setattr(cli, "_run_single", fake_step)
    await cli._run_daily(_context(store, config))

    assert ran == list(cli.DAILY_SEQUENCE), "every step after the abandoned one still ran"
    assert _steps_of(_sequence_row(store))["backfill"] == RunStatus.INTERRUPTED


async def test_an_abandoned_step_is_interrupted_and_never_reads_as_a_healthy_night(
    store, config, monkeypatch
):
    """The quietest possible failure, closed.

    INTERRUPTED is not in _COMPLETING, so an abandoned `chain` beside five healthy
    steps used to leave the sequence OK and the exit code 0 — the scheduler
    reporting success on the night it lost the one series no amount of money
    recovers. It still does not count toward steps_completed, because it reached
    no verdict and claims nothing about what it wrote.
    """

    async def fake_step(command, context, *, deadline_seconds=None, **kwargs):
        if command == "chain":
            raise StepDeadlineExceeded("budget")
        return cli.StepVerdict(RunStatus.OK)

    monkeypatch.setattr(cli, "_run_single", fake_step)
    await cli._run_daily(_context(store, config))

    row = _sequence_row(store)
    assert row["status"] == RunStatus.PARTIAL
    assert row["exit_code"] == cli.EXIT_FAILED
    assert row["steps_completed"] == len(cli.DAILY_SEQUENCE) - 1


async def test_every_step_is_handed_the_budget_config_declares_for_it(store, config, monkeypatch):
    """Read per step from config, never a single number shared by six of them.

    One shared budget would let `backfill` spend the window `chain` needs, which
    is DAILY_SEQUENCE's priority exactly backwards.
    """
    handed: dict[str, float | None] = {}

    async def fake_step(command, context, *, deadline_seconds=None, **kwargs):
        handed[command] = deadline_seconds
        return cli.StepVerdict(RunStatus.OK)

    monkeypatch.setattr(cli, "_run_single", fake_step)
    await cli._run_daily(_context(store, config))

    assert handed == cli._step_deadlines(config)
    assert handed["chain"] == max(handed.values()), "the irreplaceable step gets the most time"


async def test_a_step_with_no_declared_budget_refuses_before_the_sequence_starts(
    store, config, monkeypatch
):
    """Defaulting to unbounded would reinstate the exact condition being removed,
    and a default in code is a magic number that has learned to hide."""
    raw = copy.deepcopy(config.as_dict())
    raw["collectors"]["daily"]["step_deadline_seconds"].pop("chain")

    with pytest.raises(ObliskError, match="chain"):
        cli._step_deadlines(Config(raw))


async def test_the_budget_really_cancels_a_step_that_does_not_return(store, config, monkeypatch):
    """_run_single itself, not the sequence's handling of it.

    The tests above hand the sequence a StepDeadlineExceeded. This one hands the
    real code a collector that never finishes, which is the case the mechanism
    exists for and the one a raised exception cannot stand in for.
    """
    started = asyncio.Event()

    async def never_returns(context, run):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(cli, "_collect_macro", never_returns)

    with pytest.raises(StepDeadlineExceeded, match="macro"):
        await cli._run_single("macro", _context(store, config), deadline_seconds=0.05)

    assert started.is_set(), "the step ran before it was abandoned"
    row = store.connection.execute(
        "SELECT status FROM collector_runs WHERE collector = 'macro' ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == RunStatus.INTERRUPTED, (
        "an abandoned run is interrupted, never error: it reached no verdict at all"
    )


# ── the code systemd sees is the code the store holds ──
#
# docs/COLLECTORS.md states it as a property: "the thing systemd saw and the thing
# a human reads afterwards are one row and can be checked against each other."
# It was true of the STORE and false of the PROCESS. _run_daily recorded a raised
# or abandoned step on the sequence and never added it to the list _exit_code
# reads, so a night that lost its chain step stored exit_code 1 and exited 0 —
# systemd saw success, Restart=on-failure never fired, and the one series no
# amount of money recovers went missing quietly.
#
# Every test in this file called `await cli._run_daily(...)` and DISCARDED the
# return value, then asserted on the row read back out of SQLite. That is why the
# suite was green through all of it: the half that was broken was the half
# nothing looked at. These assert both halves, and that they agree.


async def _daily_codes(store, config, fake_step, *, skip=frozenset()):
    """Run the sequence and return (process exit code, stored exit code)."""
    monkeypatched = cli._run_single
    cli._run_single = fake_step
    try:
        statuses = await cli._run_daily(_context(store, config, skip=skip))
    finally:
        cli._run_single = monkeypatched
    return cli._exit_code(statuses), _sequence_row(store)["exit_code"]


async def test_a_raised_step_makes_the_process_exit_non_zero(store, config):
    """The defect, at the level it actually bit: the process code."""

    async def fake_step(command, context, **kwargs):
        if command == "chain":
            raise StoreError("the universe table is empty")
        return cli.StepVerdict(RunStatus.OK)

    process, stored = await _daily_codes(store, config, fake_step)

    assert process == cli.EXIT_FAILED, (
        "a sequence that lost a step must not report success to systemd"
    )
    assert process == stored, "the process code and the sequence row are one fact"


async def test_a_step_abandoned_at_its_budget_makes_the_process_exit_non_zero(store, config):
    """INTERRUPTED is failure in both readings, or the two disagree about a night.

    This is the case sequence.status() names in its own docstring: a chain step
    abandoned at its wall-clock budget beside five healthy ones "would otherwise
    return OK and exit 0".
    """

    async def fake_step(command, context, **kwargs):
        if command == "chain":
            raise StepDeadlineExceeded("chain exceeded its 3600s budget")
        return cli.StepVerdict(RunStatus.OK)

    process, stored = await _daily_codes(store, config, fake_step)

    assert process == cli.EXIT_FAILED
    assert process == stored


async def test_a_sequence_where_every_step_raises_does_not_exit_zero(store, config):
    """The worst case, and the one an empty status list used to report as clean:
    nothing collected, and `_exit_code([])` is EXIT_OK."""

    async def fake_step(command, context, **kwargs):
        raise StoreError("everything is broken")

    process, stored = await _daily_codes(store, config, fake_step)

    assert process == cli.EXIT_FAILED
    assert process == stored
    assert _sequence_row(store)["status"] == RunStatus.ERROR


async def test_a_clean_sequence_still_exits_zero(store, config):
    """The fix must not turn every run red: exit 0 still has to be reachable."""

    async def fake_step(command, context, **kwargs):
        return cli.StepVerdict(RunStatus.OK)

    process, stored = await _daily_codes(store, config, fake_step)

    assert process == cli.EXIT_OK
    assert process == stored


async def test_a_degraded_sequence_still_exits_two(store, config):
    """Degraded stays distinguishable from failed. A scheduler alerts on 2 without
    treating it as an outage, and SuccessExitStatus=0 2 depends on it."""

    async def fake_step(command, context, **kwargs):
        if command == "vendor-chain":
            return cli.StepVerdict(RunStatus.DEGRADED, "no token")
        return cli.StepVerdict(RunStatus.OK)

    process, stored = await _daily_codes(store, config, fake_step)

    assert process == cli.EXIT_DEGRADED
    assert process == stored


async def test_a_skipped_step_still_exits_zero(store, config):
    """A step filtered out collected nothing and was never asked to. SKIPPED is
    deliberately not failure — only ERROR, PARTIAL and INTERRUPTED are."""

    async def fake_step(command, context, **kwargs):
        return cli.StepVerdict(RunStatus.OK)

    process, stored = await _daily_codes(store, config, fake_step, skip={"macro"})

    assert process == cli.EXIT_OK
    assert process == stored


async def test_a_step_with_no_matching_targets_still_exits_zero(store, config):
    """--only scoping a step out is not a failed collection, and the sequence has
    always stepped over it. Pinned so the fix above does not widen into it."""

    async def fake_step(command, context, **kwargs):
        if command == "curve":
            raise NoMatchingTargets("no futures root matches --only SPY")
        return cli.StepVerdict(RunStatus.OK)

    process, stored = await _daily_codes(store, config, fake_step)

    assert process == cli.EXIT_OK
    assert process == stored


def test_the_two_exit_code_readings_map_every_status_the_same_way():
    """_exit_code and SequenceRun.exit_code are two readings of one fact.

    Asserted over the statuses rather than by inspection, because the drift that
    started this was exactly one status being handled in one of them and not the
    other.
    """
    from obliskiq.collect.sequence import SequenceRun

    for status in (
        RunStatus.OK,
        RunStatus.DEGRADED,
        RunStatus.PARTIAL,
        RunStatus.ERROR,
        RunStatus.INTERRUPTED,
    ):
        # Called unbound against a stub, because exit_code() reads nothing but
        # status() — constructing a real SequenceRun would need a store and would
        # test the constructor rather than the mapping.
        sequence_code = SequenceRun.exit_code(_StubSequence(status))  # type: ignore[arg-type]
        assert cli._exit_code([status]) == sequence_code, (
            f"{status!r} is {cli._exit_code([status])} to the process and "
            f"{sequence_code} to the store"
        )


class _StubSequence:
    """Just enough of SequenceRun for exit_code(): it reads only status()."""

    def __init__(self, status: str) -> None:
        self._status = status

    def status(self) -> str:
        return self._status


# ── the harness ──


def _context(store, config, *, only=None, skip=frozenset()):
    """A Context with no market data and no HTTP client.

    Neither is reached: every test here replaces _run_single, because what is
    under test is the sequence's own handling and not any collector's.
    """
    return cli.Context(
        config=config,
        settings=None,
        store=store,
        market_data=None,
        http=None,
        dry_run=False,
        only=only,
        force=False,
        skip=frozenset(skip),
    )
