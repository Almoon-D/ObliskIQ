"""``obliskiq-collect`` — the standalone collector entrypoint.

Runs entirely separately from the MCP server process. It binds no port, holds no
session and needs no human: everything comes from the environment and from
config.yaml, so the same image deploys unchanged to a small always-on cloud VM.

Subcommands::

    obliskiq-collect universe    seed universe.yaml + the live account into the store
    obliskiq-collect backfill    historical and incremental bars
    obliskiq-collect vendor-chain  US option chains, today or history
    obliskiq-collect chain         EU option-chain snapshot     (irreplaceable)
    obliskiq-collect curve         daily futures term structure (recoverable)
    obliskiq-collect macro       FRED / ECB / CFTC / EIA
    obliskiq-collect daily       the scheduled sequence, in order

``daily`` exists because the order matters, and ``DAILY_SEQUENCE`` below states
it once. Every step runs inside its own guard: a step that fails is recorded and
stepped over, never allowed to abandon the ones after it. That is not a
convenience — the ordering exists precisely so the collection that cannot be
redone happens before the ones that can, and a sequence that gave up halfway
would defeat it.

``status`` reads the store and collects nothing. It is the morning-after channel:
one command that says which steps ran last night, which degraded and why, and
which runs a killed process left behind.

Exit codes: 0 every collector reported ok, 1 something failed, 2 a run was
degraded. A scheduler can alert on 2 without treating it as an outage. The same
code is written into ``collector_sequences.exit_code``, so the thing systemd saw
and the thing a human reads afterwards are one row.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import date

import httpx

from ..config import Config, load_config
from ..env import Settings, load_settings
from ..errors import NoMatchingTargets, ObliskError, StepDeadlineExceeded
from ..ibkr.connector import probe_connector
from ..ibkr.preflight import run_preflight
from ..ibkr.session import connect_cp_client
from ..logging_setup import configure_logging, get_logger
from ..macro.fetchers import build_fetchers
from ..store import Store, open_store
from ..util.timeutil import iso_date, utc_today
from .backfill import Backfill
from .chain_collector import ChainCollector, daily_bars_are_fresh
from .curve_collector import CurveCollector
from .macro_pull import MacroPull
from .nav import NavCollector
from .option_vendor import VendorChainCollector, VendorClient, eastern_session
from .runlog import DETAIL_EXCERPT_CHARS, CollectorRun, RunStatus, reap_interrupted_runs
from .sequence import SequenceRun
from .source import SOURCE_CP_API, SOURCE_MCP, FixtureMarketData, MarketData
from .source import CpApiMarketData as CpMarketData
from .state import record_cp_handshake
from .status import status_report
from .universe import UniverseSync

log = get_logger("collect.cli")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_DEGRADED = 2

#: How much of a failure message the sequence row keeps per step. The detail
#: column is a record, not a log sink; the traceback stays in the journal.
REASON_CHARS = DETAIL_EXCERPT_CHARS

#: How many failed targets a step's one-line reason names before it stops. The
#: reason is a pointer to `collector_runs.detail`, which holds every one of them;
#: a line naming forty dead underlyings is a line nobody finishes reading.
REASON_TARGETS_NAMED = 3

# SOURCE_CP_API is imported from ``collect.source`` rather than restated here.
# It is both the live route and the value written to every row's ``source``
# column, and two definitions of one tag is how the column and the route drift
# apart. The two below are replay routes and exist only at this layer.

#: Recorded Client Portal responses. Touches no network.
SOURCE_FIXTURE = "fixture"
#: A directory of live MCP responses translated by ``collect.mcp_capture``. Replays
#: like a fixture, but the rows it writes are real market data and are tagged
#: ``source='mcp'`` rather than ``cp_api``. See that module for why the bridge
#: exists and when it should stop being used.
SOURCE_MCP_CAPTURE = "mcp-capture"

#: Not a collector. Walks the OAuth handshake and reports which step failed,
#: for the first run after IBKR approve the application.
COMMAND_PREFLIGHT = "preflight"

#: Not a collector either. Reads collector_sequences and collector_runs and
#: reports what the machine did while nobody was watching. Opens no market-data
#: provider and touches no network.
COMMAND_STATUS = "status"

#: Not a collector, and not a route. Walks the IBKR Custom Connector's OAuth 2.0
#: discovery documents and reports which grants its authorization server accepts,
#: so the question "is there a headless route that needs no OAuth 1.0a approval"
#: is answered by asking IBKR rather than by assuming. Sends no credentials,
#: registers nothing, opens no store.
COMMAND_CONNECTOR = "connector"

#: The scheduled sequence, in order. This is architecture and not a threshold, so
#: it lives here rather than in config.yaml — BUILD-BRIEF.md and docs/COLLECTORS.md
#: own it, and a config key that could reorder it would make the guarantee below
#: switchable by a setting.
#:
#: The order is a ranking by what a missed day costs, irrecoverable first:
#:
#:   nav           one reading of the account's net liquidation value. First
#:                 because /portfolio/{id}/ledger serves TODAY's figure and no
#:                 other, so a missed day is a missed observation that cannot be
#:                 asked for again — and because it is a single call that cannot
#:                 meaningfully delay anything behind it.
#:   backfill      daily bars. Second because `chain` sizes its strike bands from
#:                 them; recoverable at any time, so its FAILURE costs a worse
#:                 band rather than a lost day.
#:   chain         the three European listings. The only series in this package
#:                 that cannot be retrieved retrospectively at any price, so it
#:                 runs before anything that could keep it from running.
#:   curve         futures term structure. Reconstructable rung by rung from
#:                 per-contract history, but a snapshot is one call against many.
#:   vendor-chain  the US chains, from marketdata.app. The only step that spends
#:                 money and the only one whose data can be re-bought later, so
#:                 it is the one that yields when something ahead of it is at
#:                 stake. It used to run FIRST, which meant a marketdata.app
#:                 outage could cost a European snapshot; see docs/DECISIONS.md.
#:   macro         last, because it is the only part of the sequence that can be
#:                 caught up tomorrow for free.
DAILY_SEQUENCE = ("nav", "backfill", "chain", "curve", "vendor-chain", "macro")

#: Where each step's wall-clock budget comes from. A section rather than a flat
#: key because the budgets are a RANKING — the same one DAILY_SEQUENCE states —
#: and one number shared by six steps would let `backfill` spend the window
#: `chain` needs, which is the priority exactly backwards.
STEP_DEADLINE_SECTION = "collectors.daily.step_deadline_seconds"

#: The commands that read market data from IBKR, and therefore need a route
#: authenticated against it. Stated once, here, because the fact was previously
#: implicit in _build_market_data being called for every command: `macro` reaches
#: FRED, ECB, CFTC and EIA and no IBKR endpoint at all, yet could not start
#: without OBLISK_IBKR_CP_BASE_URL. deploy/obliskiq-collect@macro.timer runs it as
#: a unit of its own, so that coupling made the whole macro layer unreachable
#: whenever the IBKR credentials were absent, expired or not yet issued.
#:
#: `vendor-chain` is absent deliberately: it builds its own VendorClient against
#: marketdata.app and never touches context.market_data.
NEEDS_MARKET_DATA = frozenset({"universe", "nav", "backfill", "chain", "curve", "daily"})


@dataclass
class Context:
    """Everything a subcommand needs, built once."""

    config: Config
    settings: Settings
    store: Store
    #: None for the commands in no NEEDS_MARKET_DATA, which never build a route.
    #: Deliberately Optional rather than a null object: a collector that silently
    #: receives a stub reports success over nothing, which is the failure mode this
    #: package exists to prevent. Read it through require_market_data().
    market_data: MarketData | None
    http: httpx.AsyncClient
    dry_run: bool
    only: list[str] | None
    force: bool
    skip: frozenset[str]
    #: vendor-chain only: walk option_vendor.backfill_sessions back instead of
    #: collecting today. History is cheap on this route and irrecoverable on the
    #: other, so the flag names the intent rather than a session count.
    backfill_chains: bool = False
    #: vendor-chain only: the newest session to collect, defaulting to today in
    #: New York. It reaches the collector as ``as_of`` and NOT as ``today``, which
    #: is that collector's tier reference; see VendorChainCollector.run().
    as_of: date | None = None

    def require_market_data(self) -> MarketData:
        """The market-data route, or a refusal naming the command that lacks one."""
        if self.market_data is None:
            raise ObliskError(
                "this collector needs a market-data route and none was built. Only the "
                f"commands in {sorted(NEEDS_MARKET_DATA)} build one; a collector reaching "
                "IBKR from outside that set is a wiring error, not a configuration one."
            )
        return self.market_data


def _session_date(raw: str) -> date:
    """An ISO calendar date, or an argparse refusal naming what was wrong.

    ``date.fromisoformat`` accepts other ISO 8601 spellings on 3.11+ — week dates
    and basic format among them — and a session is written one way everywhere
    else in this package, so the shape is pinned rather than inferred.
    """
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a YYYY-MM-DD date: {exc}") from exc
    if raw != parsed.isoformat():
        raise argparse.ArgumentTypeError(f"{raw!r} must be written as YYYY-MM-DD")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obliskiq-collect",
        description=(
            "Unattended collectors for obliskiq-lite. Option-chain history is "
            "retrievable retrospectively for US listings through 'vendor-chain', and "
            "NOT for the European ones, where 'chain' is the only route and a day not "
            "collected is gone permanently."
        ),
    )
    parser.add_argument(
        "command",
        choices=[
            "universe",
            "nav",
            "backfill",
            "chain",
            "vendor-chain",
            "curve",
            "macro",
            "daily",
            COMMAND_PREFLIGHT,
            COMMAND_STATUS,
            COMMAND_CONNECTOR,
        ],
        help=(
            "which collector to run; 'daily' runs the scheduled sequence in order, "
            "'nav' records one account NAV observation and collects nothing else, "
            "'preflight' walks the OAuth handshake step by step and collects nothing, "
            "'status' reports what the last runs did and collects nothing either, "
            "'connector' reports what the IBKR Custom Connector's authorization "
            "server accepts and collects nothing either"
        ),
    )
    parser.add_argument(
        "--source",
        choices=[SOURCE_CP_API, SOURCE_FIXTURE, SOURCE_MCP_CAPTURE],
        default=None,
        help=(
            "data route; defaults to collectors.source in config.yaml. 'fixture' "
            "replays recorded responses and touches no network; 'mcp-capture' "
            "replays a live capture taken through the IBKR MCP connector and tags "
            "its rows source='mcp'"
        ),
    )
    parser.add_argument(
        "--fixtures",
        help="recorded-response directory, required with --source fixture or mcp-capture",
    )
    parser.add_argument(
        "--only",
        action="append",
        metavar="SYMBOL",
        help=(
            "restrict to these symbols, roots or series ids; repeatable. Refused "
            "on 'universe', which reconciles the whole file in one pass"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and report, write nothing",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "ignore every refetch cache and pull again. For macro that is free; for "
            "vendor-chain it RE-BUYS every session already held, so it costs real "
            "credits."
        ),
    )
    parser.add_argument(
        "--backfill-chains",
        action="store_true",
        help=(
            "vendor-chain only: walk option_vendor.backfill_sessions of history "
            "instead of collecting today. Newest session first, resumable, and it "
            "stops at option_vendor.max_credits_per_run rather than spending the "
            "day's allowance and failing on the rest."
        ),
    )
    parser.add_argument(
        "--as-of",
        metavar="YYYY-MM-DD",
        type=_session_date,
        help=(
            "vendor-chain only: the newest session to COLLECT, defaulting to today "
            "in New York. This chooses the credit tier as well as the date: the "
            "current session is requested without 'date=' and billed per option "
            "symbol, while any earlier session carries 'date=' and is billed as "
            "history. Without it the walk always starts at today, so collecting one "
            "past session meant paying for the live one first"
        ),
    )
    parser.add_argument(
        "--skip",
        action="append",
        choices=list(DAILY_SEQUENCE),
        metavar="STEP",
        help=(
            "omit a step from the 'daily' sequence; repeatable. --source fixture "
            "replays recorded IBKR responses; macro is unaffected either way, "
            "because it reaches its own sources and never the IBKR route"
        ),
    )
    parser.add_argument(
        "--days",
        type=int,
        help=("status only: how far back to report; defaults to collectors.status_report_days"),
    )
    parser.add_argument("--config", help="path to config.yaml; defaults to $OBLISK_CONFIG_PATH")
    parser.add_argument("--log-level", help="override OBLISK_LOG_LEVEL")
    parser.add_argument("--log-format", choices=["json", "text"], help="override OBLISK_LOG_FORMAT")
    return parser


async def _build_market_data(
    args: argparse.Namespace,
    context_stack: AsyncExitStack,
    config: Config,
    settings: Settings,
    client: httpx.AsyncClient,
    store: Store,
) -> MarketData:
    # An omitted --source is the unattended case, which is the one config
    # decides. Only cp_api can be authenticated with nobody present — the Custom
    # Connector reaches the MCP tools from a headless host but takes a token this
    # build cannot yet mint — so the choice list has one member: setting
    # collectors.source to anything else fails here, loudly, rather than being
    # silently ignored.
    route = args.source or config.require_choice("collectors.source", [SOURCE_CP_API])

    if route in (SOURCE_FIXTURE, SOURCE_MCP_CAPTURE):
        if not args.fixtures:
            raise ObliskError(f"--source {route} requires --fixtures pointing at a directory")
        # The tag is the whole difference. A fixture replay is recorded CP
        # traffic and stays 'cp_api'; a capture is live market data taken through
        # the MCP connector, and a row that cannot say which is which is a row
        # nobody can audit later.
        tag = SOURCE_MCP if route == SOURCE_MCP_CAPTURE else SOURCE_CP_API
        return FixtureMarketData(args.fixtures, source=tag)

    cp_client = await connect_cp_client(settings, config, client)
    await context_stack.enter_async_context(cp_client)
    # The handshake worked, so note WHEN — never the token, which stays in this
    # process and dies with it. The server holds no IBKR credential by design and
    # so cannot observe this for itself; without the stamp, `get_system_health`
    # could not tell a route that has been failing to authenticate for a week
    # from one nobody has asked to.
    record_cp_handshake(store)
    return CpMarketData(cp_client)


async def _run_command(args: argparse.Namespace, context: Context) -> list[str]:
    """Run the requested command. Returns the status of every run performed."""
    command = args.command
    # Refused rather than ignored. Only vendor-chain reads it, so accepting it on
    # `daily` would pin one step of five and leave an operator believing the whole
    # sequence had been asked for a past session.
    if context.as_of is not None and command != "vendor-chain":
        raise ObliskError(
            f"--as-of applies to vendor-chain and {command!r} does not read it. "
            "It selects the marketdata.app credit tier for that one step; no other "
            "collector takes a session to collect for."
        )
    # Checked here as well as in the collector, which raises on the same
    # condition against its own clock reading. This one refuses before a target
    # list is built, so the message names the flag the operator typed.
    if context.as_of is not None and context.as_of > eastern_session():
        raise ObliskError(
            f"--as-of {context.as_of.isoformat()} is in the future; the New York "
            f"session is {eastern_session().isoformat()}. A session that has not "
            "happened cannot be collected."
        )
    # Refused rather than ignored, for the reason --as-of is. `universe` is a
    # whole-file RECONCILIATION: it resolves every declared row, retires the ones
    # a corrected conid supersedes and ages out portfolio rows it did not see. A
    # subset changes what "did not see" means, so a filtered run would make
    # retirement decisions on partial evidence — and it silently walked all 91
    # targets while an operator read the flag as having narrowed it.
    if context.only and command == "universe":
        raise ObliskError(
            "--only does not apply to universe. The sync reconciles the whole "
            "file against the account in one pass — it retires rows a corrected "
            "conid supersedes and ages out portfolio rows it did not see this "
            "run — so a subset would retire on evidence it never gathered. Run "
            "it whole, and use --dry-run to see what it would change."
        )
    if command == "daily":
        return await _run_daily(context)
    return [(await _run_single(command, context)).status]


@dataclass(frozen=True)
class StepVerdict:
    """What a step reported, and why.

    The reason travels with the status because the sequence row is read the
    morning after by somebody who no longer has the terminal that printed the
    warning. A step recorded as ``degraded`` with nothing saying which target
    fell back is a status that has to be investigated instead of one that has
    been reported.
    """

    status: str
    reason: str | None = None


def _reason_for(run: CollectorRun) -> str | None:
    """One line naming what went wrong or what fell back, from the run's targets."""
    failed = [outcome for outcome in run.outcomes if not outcome.ok]
    if failed:
        named = ", ".join(
            f"{o.target}: {o.reason}" for o in failed[:REASON_TARGETS_NAMED] if o.reason
        )
        return f"{len(failed)} target(s) failed — {named}"[:REASON_CHARS] if named else None
    degraded = run.degraded_targets
    if degraded:
        return f"collected on a weaker input than intended: {', '.join(degraded)}"[:REASON_CHARS]
    return None


async def _run_single(
    command: str,
    context: Context,
    *,
    sequence: SequenceRun | None = None,
    step_index: int | None = None,
    deadline_seconds: float | None = None,
) -> StepVerdict:
    """One collector, optionally under a wall-clock budget.

    ``deadline_seconds`` is passed only by the ``daily`` sequence. A hand-run
    ``obliskiq-collect chain`` is invoked by somebody who is watching it and can
    stop it, so it is never killed on a timer; the budget exists for the
    unattended case, where the alternative is systemd's SIGTERM taking the whole
    process and every step after it.

    The budget wraps the RUNNER and not the ``CollectorRun``, so the row this run
    opened is closed out by the same ``with`` that opened it. ``wait_for``
    cancels at an await point, which on these collectors is a network call rather
    than the middle of a transaction; whatever committed before that point stays
    committed, and ``interrupted`` is precisely the status that claims nothing
    about how much of it there was.
    """
    runner = {
        "universe": _collect_universe,
        "nav": _collect_nav,
        "backfill": _collect_backfill,
        "chain": _collect_chain,
        "vendor-chain": _collect_vendor_chain,
        "curve": _collect_curve,
        "macro": _collect_macro,
    }[command]
    with CollectorRun(
        context.store,
        command,
        dry_run=context.dry_run,
        sequence=sequence,
        step_index=step_index,
    ) as run:
        if deadline_seconds is None:
            await runner(context, run)
        else:
            try:
                await asyncio.wait_for(runner(context, run), deadline_seconds)
            except TimeoutError as exc:
                raise StepDeadlineExceeded(
                    f"the {command!r} step outran its wall-clock budget of "
                    f"{deadline_seconds:g}s from {STEP_DEADLINE_SECTION}, so it was "
                    "abandoned and the steps after it still ran. What it wrote before "
                    "that point is in the collector's own tables; this run claims "
                    "nothing about how much that was. Raise the budget there, and the "
                    "unit's TimeoutStartSec with it, or find out why the step is slow."
                ) from exc
    return StepVerdict(run.status(), _reason_for(run))


async def _run_daily(context: Context) -> list[str]:
    """The scheduled sequence, in the order the data depends on and cannot be redone.

    ``DAILY_SEQUENCE`` states the order and why. What this function adds is the
    guarantee that the order is worth stating: **every step runs inside its own
    guard**. A step that raises is recorded against the sequence and stepped
    over, because the alternative — the behaviour this replaced — was that an
    upstream nobody had wrapped could abandon the European chain snapshot, which
    is the one thing here that no amount of money recovers.

    The bars-freshness check runs between the backfill and the chain and does not
    gate anything: a worse strike band is a bad day, a missed chain snapshot is a
    permanent hole. It is recorded on the sequence rather than only logged, so
    months later something says why a band was sized the way it was.

    **The guard is now two guards, because they catch different things.** The
    ``try/except`` below catches a step that RAISES. It cannot catch a step that
    simply does not return — and when the unit's ``TimeoutStartSec`` expires on
    one of those, systemd sends SIGTERM, which is process death rather than an
    exception, so every step after it is lost and ``Restart=on-failure`` re-runs
    the whole sequence from the top. ``_step_deadlines`` gives each step its own
    wall-clock budget, summing below the unit's, so a hung step is abandoned here
    instead of taking the sequence with it.
    """
    statuses: list[str] = []
    session = iso_date(utc_today())
    deadlines = _step_deadlines(context.config)

    with SequenceRun(
        context.store,
        "daily",
        session_date=session,
        steps=DAILY_SEQUENCE,
        dry_run=context.dry_run,
    ) as sequence:
        for index, command in enumerate(DAILY_SEQUENCE):
            if command in context.skip:
                log.info("step skipped by request", extra={"step": command})
                sequence.step(command, RunStatus.SKIPPED, reason="omitted by --skip")
                if command == "backfill":
                    log.warning(
                        "backfill SKIPPED by request. The chain collector sizes its strike "
                        "bands from daily bars, so expect fallback sigmas and a degraded run."
                    )
                _note_freshness_after(command, context, sequence)
                continue

            try:
                verdict = await _run_single(
                    command,
                    context,
                    sequence=sequence,
                    step_index=index,
                    deadline_seconds=deadlines[command],
                )
            except StepDeadlineExceeded as exc:
                # Ahead of the broad guard, and INTERRUPTED rather than ERROR:
                # the step reached no verdict at all, which is a different fact
                # from reaching one and reporting failure. It does not count
                # toward steps_completed and it does move the sequence's own
                # verdict, so the exit code says something happened.
                log.error(
                    "step abandoned at its wall-clock budget; the sequence continues "
                    "with the steps after it",
                    extra={"step": command, "deadline_seconds": deadlines[command]},
                )
                statuses.append(RunStatus.INTERRUPTED)
                sequence.step(command, RunStatus.INTERRUPTED, reason=str(exc)[:REASON_CHARS])
            except NoMatchingTargets as exc:
                # --only scopes the whole sequence, but the collectors do not share a
                # namespace: an equity symbol means something to chain and backfill and
                # nothing to curve, which tracks futures roots. Inside the sequence that
                # is the filter not naming this step's targets, not a failed collection,
                # so it is logged and stepped over. Asked for directly the same
                # condition still raises — see NoMatchingTargets.
                log.info(
                    "step has no targets matching --only; skipping it rather than "
                    "failing the sequence",
                    extra={"step": command, "only": context.only, "detail": str(exc)},
                )
                sequence.step(command, RunStatus.SKIPPED, reason=str(exc)[:REASON_CHARS])
            except Exception as exc:
                # Broad on purpose. The point of this guard is the failure NOBODY
                # anticipated: an upstream that returned something unparseable, a
                # library raising its own type, a bug in a collector. Narrowing it
                # to ObliskError would leave exactly those escaping, which is the
                # behaviour being fixed.
                log.error(
                    "step failed; the sequence continues with the steps after it",
                    extra={"step": command, "error": repr(exc)},
                )
                statuses.append(RunStatus.ERROR)
                sequence.step(command, RunStatus.ERROR, reason=repr(exc)[:REASON_CHARS])
            else:
                statuses.append(verdict.status)
                sequence.step(command, verdict.status, reason=verdict.reason)

            _note_freshness_after(command, context, sequence)

    return statuses


def _step_deadlines(config: Config) -> dict[str, float]:
    """Every step's wall-clock budget, refused rather than defaulted.

    A step with no declared budget raises here, before the sequence starts,
    rather than silently running unbounded: an unbounded step is exactly the
    condition this whole mechanism exists to remove, and a default in code would
    be a magic number that had learned to hide.
    """
    declared = config.get(STEP_DEADLINE_SECTION)
    if not isinstance(declared, dict):
        raise ObliskError(f"{STEP_DEADLINE_SECTION} must be a mapping of step name to seconds")
    missing = [step for step in DAILY_SEQUENCE if step not in declared]
    extra = [step for step in declared if step not in DAILY_SEQUENCE]
    if missing or extra:
        raise ObliskError(
            f"{STEP_DEADLINE_SECTION} declares no budget for {missing} and a budget for "
            f"{extra}, which DAILY_SEQUENCE does not run. Every step needs one and only "
            "the steps: a step running unbounded is what lets the unit's TimeoutStartSec "
            "expire on it and take the steps after it down with it, and a budget for a "
            "step nobody runs is a decision that looks made and is not."
        )
    return {step: float(declared[step]) for step in DAILY_SEQUENCE}


def _note_freshness_after(command: str, context: Context, sequence: SequenceRun) -> None:
    """Record the daily-bar freshness once the backfill has had its turn.

    Checked rather than assumed: a backfill that failed quietly would otherwise
    push every underlying onto a fallback sigma with nothing saying why. It is a
    note on the sequence and not a step of its own, because it collects nothing —
    counting it would make a sequence look like it did more work than it did.
    """
    if command != "backfill":
        return
    fresh = daily_bars_are_fresh(context.store, context.config)
    window = context.config.require_int("collectors.chain.bars_freshness_days")
    sequence.notes["daily_bars_fresh"] = fresh
    sequence.notes["freshness_days"] = window
    if fresh:
        log.info("daily bars are fresh; chain strike bands will use realised vol")
    else:
        log.warning(
            "DAILY BARS ARE STALE after the backfill step. The chain collector will "
            "fall back to a weaker sigma and report the run as degraded. Check the "
            "backfill outcome above before trusting today's strike bands.",
            extra={"freshness_days": window},
        )


async def _collect_universe(context: Context, run: CollectorRun) -> None:
    await UniverseSync(context.store, context.config, context.require_market_data()).run(
        run=run, dry_run=context.dry_run
    )


async def _collect_nav(context: Context, run: CollectorRun) -> None:
    """One NAV observation, on its own, every trading day.

    The same reading the weekly `universe` sync takes, on the cadence the
    rolling-month drawdown breaker actually needs: it measures a drawdown across
    ``risk.portfolio_circuit_breaker.window_days`` and refuses to report on fewer
    than ``min_observations`` readings, which a weekly write clears only barely.
    Recording it here as well costs one call a day and turns a breaker that could
    only ever say `n/a` into one that says something.
    """
    await NavCollector(context.store, context.config, context.require_market_data()).run(
        run, dry_run=context.dry_run
    )


async def _collect_backfill(context: Context, run: CollectorRun) -> None:
    await Backfill(context.store, context.config, context.require_market_data()).run(
        run, only=context.only, dry_run=context.dry_run
    )


async def _collect_chain(context: Context, run: CollectorRun) -> None:
    await ChainCollector(context.store, context.config, context.require_market_data()).run(
        run, only=context.only, dry_run=context.dry_run
    )


async def _collect_vendor_chain(context: Context, run: CollectorRun) -> None:
    """US option chains from marketdata.app.

    Runs AFTER the IBKR ``chain`` step in the daily sequence, and the ordering is
    deliberate: this is the only step that spends money and the only one whose
    data can be re-bought later, so it yields to the European snapshot that
    cannot. ``DAILY_SEQUENCE`` states the order; the comment above it says why it
    was moved off the front.

    The two routes split on listing exchange and never collect the same
    underlying, so ordering is about cost and not precedence.
    """
    client = VendorClient(context.config, context.http, context.settings.marketdata_api_token)
    collector = VendorChainCollector(context.store, context.config, client)
    sessions = (
        context.config.require_int("option_vendor.backfill_sessions")
        if context.backfill_chains
        else 1
    )
    await collector.run(
        run,
        only=context.only,
        sessions=sessions,
        dry_run=context.dry_run,
        force=context.force,
        # as_of, NOT today. `today` is the collector's tier reference -- the
        # session it treats as current -- and setting that to a past date would
        # make the pinned session look current again, sending an undated
        # live-tier request and stamping the CURRENT chain with a past date.
        # `as_of` moves the end of the walk and leaves the reference alone, which
        # is what puts the request on the historical tier. None keeps the
        # collector's own default of today in New York.
        as_of=context.as_of,
    )


async def _collect_curve(context: Context, run: CollectorRun) -> None:
    await CurveCollector(context.store, context.config, context.require_market_data()).run(
        run, only=context.only, dry_run=context.dry_run
    )


async def _collect_macro(context: Context, run: CollectorRun) -> None:
    fetchers = build_fetchers(
        context.config,
        context.http,
        fred_api_key=context.settings.fred_api_key,
        eia_api_key=context.settings.eia_api_key,
    )
    await MacroPull(context.store, context.config, fetchers).run(
        run, only=context.only, dry_run=context.dry_run, force=context.force
    )


def _exit_code(statuses: Sequence[str]) -> int:
    """The code the PROCESS returns, and it must agree with the sequence row.

    ``docs/COLLECTORS.md`` promises that "the thing systemd saw and the thing a
    human reads afterwards are one row and can be checked against each other", so
    this and :meth:`SequenceRun.exit_code` are two readings of one fact and the
    same statuses have to map the same way. They did not: a step that RAISED, or
    one abandoned at its wall-clock budget, was recorded on the sequence and never
    added to the list this reads — so a `daily` run that lost its chain step
    stored ``exit_code = 1`` and exited **0**, which is the quietest possible
    failure and the one ``Restart=on-failure`` exists to catch.

    ``INTERRUPTED`` is therefore failure here exactly as it is there. A step that
    reached no verdict at all is not a step that succeeded, and the two codes
    disagreeing is worse than either of them being wrong on its own: it makes the
    store and the scheduler tell an operator different things about one night.

    ``SKIPPED`` is deliberately absent. A step filtered out by ``--only`` or
    ``--skip`` collected nothing and was never meant to, which is exit 0.
    """
    failed = (RunStatus.ERROR, RunStatus.PARTIAL, RunStatus.INTERRUPTED)
    if any(status in failed for status in statuses):
        return EXIT_FAILED
    if any(status == RunStatus.DEGRADED for status in statuses):
        return EXIT_DEGRADED
    return EXIT_OK


async def _main(args: argparse.Namespace) -> int:
    settings = load_settings()
    config = load_config(args.config or settings.config_path)
    configure_logging(
        level=args.log_level or settings.log_level,
        fmt=args.log_format or settings.log_format,
        utc=config.require_bool("runtime.logging.utc_timestamps"),
    )

    if args.command == COMMAND_PREFLIGHT:
        # Deliberately ahead of everything else. Preflight opens no store and
        # builds no market-data provider: building one would run the very
        # handshake this command exists to diagnose, and fail before it could
        # report which step went wrong.
        async with httpx.AsyncClient() as client:
            report = await run_preflight(settings, config, client)
        print(report.render())
        return EXIT_OK if report.ok else EXIT_FAILED

    if args.command == COMMAND_CONNECTOR:
        # Ahead of the store for the same reason preflight is: this command needs
        # neither a database nor a market-data provider, and building one would
        # run the OAuth 1.0a handshake whose absence is the very thing that makes
        # asking about a second route worth doing.
        endpoint = settings.ibkr_connector_url
        if not endpoint:
            print(
                "OBLISK_IBKR_CONNECTOR_URL is unset, so there is no endpoint to probe.\n"
                "See .env.template for the value, and docs/DECISIONS.md for what this "
                "route is and is not."
            )
            return EXIT_FAILED
        async with httpx.AsyncClient() as client:
            model = await probe_connector(endpoint, config, client)
        print(model.render())
        return EXIT_OK if model.ok else EXIT_DEGRADED

    if args.command == COMMAND_STATUS:
        # Reads the store and nothing else: no market-data provider is built, so
        # asking what happened last night never spends a credit or opens a
        # socket. It reaps first, so a run a killed process left saying 'running'
        # is reported as interrupted rather than as still working.
        with open_store(settings.db_path, config) as store:
            reap_interrupted_runs(store, config)
            print(status_report(store, config, days=args.days))
        return EXIT_OK

    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(httpx.AsyncClient())
        store = stack.enter_context(open_store(settings.db_path, config))
        # Before anything else touches the store. A process that was killed
        # cannot record its own death, so the next run is the only thing in a
        # position to close its row out — and leaving it saying 'running' is what
        # made a run that died last Tuesday indistinguishable from a live one.
        reap_interrupted_runs(store, config)
        # Only for the commands that read market data. `macro` reaches five public
        # macro sources and no IBKR endpoint, so building a route for it ran an
        # OAuth 1.0a handshake it had no use for and refused to start without.
        market_data = (
            await _build_market_data(args, stack, config, settings, client, store)
            if args.command in NEEDS_MARKET_DATA
            else None
        )
        context = Context(
            config=config,
            settings=settings,
            store=store,
            market_data=market_data,
            http=client,
            dry_run=bool(args.dry_run),
            only=list(args.only) if args.only else None,
            force=bool(args.force),
            skip=frozenset(args.skip or ()),
            backfill_chains=bool(getattr(args, "backfill_chains", False)),
            as_of=getattr(args, "as_of", None),
        )
        statuses = await _run_command(args, context)

    log.info("collector command finished", extra={"command": args.command, "statuses": statuses})
    return _exit_code(statuses)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_main(args))
    except ObliskError as exc:
        # A configuration or store problem: print it plainly. Nobody is watching a
        # traceback on a VM, and the message is written to be actionable.
        configure_logging()
        log.error("collector could not run", extra={"error": str(exc)})
        return EXIT_FAILED


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    sys.exit(main())
