"""The operational tools: what the machine did, and making it do it again.

``get_system_health`` reads; ``run_collectors`` spawns. They sit together
because both answer the same question from opposite ends — *is the pipeline
keeping up* — and a session that finds a thin grid should ask the first before
reaching for the second.

The collectors normally run on a timer with no session open and no human
present, because option-chain and futures-curve history cannot be retrieved
retrospectively and a day not collected is gone permanently. This tool exists
for the case where the timer did not fire, or where a session needs today's
chain before the evening run.

It **spawns** the standalone entrypoint rather than importing it. The separation
is not incidental: the collectors hold IBKR credentials and this process binds a
port, a chain pull runs for minutes, and the same collector image has to deploy
to a VM that runs no server at all.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ...collect.state import UPSTREAM_MARKETDATA_APP, active_cooldown, last_cp_handshake
from ...collect.status import CollectorStatus, collector_status
from ...compute.build_grid import coverage_report, latest_grid_date
from ...compute.frames import latest_nav_observation
from ...logging_setup import get_logger
from ...store import Store
from ...util.timeutil import iso_date, parse_stamp, utc_now, utc_stamp
from ..collectors import CollectorRunner, CollectorsDisabled, runs_since
from ..context import ServerContext, run_blocking
from ..encode import to_jsonable
from .base import READ_ONLY, envelope, refuse

log = get_logger("server.tools.operations")

#: Spawns a process that writes to the store. Append-only, but not idempotent:
#: two runs write two run-log rows.
SPAWNS_COLLECTOR = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
)


#: Nothing has run at all. Not ``ok``, and the distinction is the whole reason
#: this tool exists: an empty store that reported healthy would be a machine
#: saying "all clear" about work it has never attempted.
VERDICT_EMPTY = "empty"

#: Something is behind or failing and a session should say so before reasoning
#: over the gap.
VERDICT_ATTENTION = "attention"

#: Every recorded input is current. It is a claim about the COLLECTORS, never
#: about the market.
VERDICT_OK = "ok"

#: Seconds in a day, for turning an age into the unit an operator thinks in.
_SECONDS_PER_DAY = 86400.0


def _age_seconds(stamp: datetime | None, *, now: datetime) -> float | None:
    return None if stamp is None else max((now - stamp).total_seconds(), 0.0)


def _age_days(stamp: str | None, *, as_of: date) -> int | None:
    """Whole days between a recorded UTC stamp and ``as_of``, or ``None`` if unreadable.

    An unreadable stamp reports ``None`` rather than zero: a row whose date
    cannot be parsed is a row nothing is known about, and zero would be the claim
    that it is current.
    """
    parsed = parse_stamp(stamp) if stamp else None
    if parsed is None:
        return None
    return max((as_of - parsed.date()).days, 0)


def _grid_block(store: Store, config: Any, *, as_of: date) -> dict[str, Any]:
    """Grid coverage, read back from the table the build wrote.

    Composed from ``compute.build_grid.coverage_report`` rather than counted
    here: ``coverage_pct`` has one owner, and a tool that divided its own
    numerator by its own denominator would eventually disagree with the build
    that printed them.
    """
    newest = latest_grid_date(store)
    if newest is None:
        return {
            "as_of": None,
            "age_days": None,
            "below_coverage_floor": None,
            "note": (
                "no grid has ever been built in this store. Nothing here is thin — "
                "there is nothing. Run `obliskiq-compute all`; it is a separate "
                "entrypoint scheduled on the machine and has no tool of its own."
            ),
        }
    report = coverage_report(store, as_of=newest)
    floor = config.require_float("grid.min_coverage_pct")
    age = (as_of - newest).days
    return {
        "as_of": iso_date(newest),
        "age_days": age,
        "markets": report.markets,
        "sectors": report.sectors,
        "families": len(report.families),
        "cells_written": report.cells_written,
        "populated": report.populated,
        "absent": report.absent,
        "stale": report.stale,
        "inapplicable": report.inapplicable,
        "collectible": report.collectible,
        "coverage_pct": report.coverage_pct,
        "ambition_pct": report.ambition_pct,
        "min_coverage_pct": floor,
        "below_coverage_floor": report.coverage_pct < floor,
        "measured_cells": report.measured_cells,
        # The histogram, not the prose. A session that wants the sentence asks
        # get_grid_cell for the cell it cares about.
        "reasons": dict(report.reasons),
    }


def _nav_block(store: Store, *, as_of: date) -> dict[str, Any]:
    observation = latest_nav_observation(store)
    if observation is None:
        return {
            "as_of": None,
            "note": (
                "no NAV has ever been recorded. The drawdown circuit breaker is "
                "n/a until the `nav` step runs, and every affordability ceiling is "
                "computed without one."
            ),
        }
    return {
        "as_of": iso_date(observation.as_of),
        "age_days": (as_of - observation.as_of).days,
    }


def _ibkr_block(store: Store, config: Any, *, now: datetime) -> dict[str, Any]:
    """When a collector run last completed the OAuth 1.0a handshake.

    **This is the last successful HANDSHAKE, not a live session.** Collectors are
    ``Type=oneshot`` against an ``82,800``-second token, so every run re-does the
    Diffie-Hellman exchange from the PEM files and the token itself never
    outlives the process. Comparing the age against the TTL therefore answers
    "has anything authenticated inside the window a token would have lasted" —
    which is the operationally useful question — and never "is a token still
    valid". Saying which is which is the difference between a health tool and a
    reassuring one.
    """
    ttl = config.require_float("ibkr.cp_api.oauth.live_session_token_ttl_seconds")
    stamp = last_cp_handshake(store)
    if stamp is None:
        return {
            "last_authenticated_at": None,
            "ttl_seconds": ttl,
            "note": (
                "no collector run has recorded a Client Portal handshake. Either "
                "none has run against cp_api, or every attempt failed before "
                "authenticating. It is not the claim that the credentials work."
            ),
        }
    age = _age_seconds(stamp, now=now) or 0.0
    return {
        "last_authenticated_at": utc_stamp(stamp),
        "age_seconds": age,
        "ttl_seconds": ttl,
        "within_ttl": age <= ttl,
        "note": (
            "the last successful handshake, not a live token: collectors are "
            "oneshot and mint a fresh one per run. Past the TTL means nothing has "
            "authenticated for longer than a token would have lasted."
        ),
    }


def _concerns(
    status: CollectorStatus,
    grid: dict[str, Any],
    nav: dict[str, Any],
    ibkr: dict[str, Any],
    *,
    as_of: date,
    stale_after_days: int,
) -> list[str]:
    """Every reason this store is not simply healthy, named rather than scored.

    **AGE is a concern in its own right, and it is the one a health tool exists
    to catch.** ``CollectorStatus`` reports the newest run per collector with no
    window on it at all, so a collector that last ran cleanly ninety days ago
    arrives as a tidy ``ok`` row with an empty sequence list beside it — which is
    exactly what a machine whose timer stopped firing looks like, and what a
    verdict built only from run STATUSES would call healthy.

    The bound is ``server.stale_after_days``, the same one ``envelope`` applies
    to every tool response. It is the server's declared answer to "beyond this a
    read is stale", and the newest collector run and the newest grid build are
    reads like any other; giving this a second key would be one fact with two
    owners.

    Every age is measured against ``as_of`` — the session the caller asked about
    — and never against the wall clock, so asking about a past session reports
    what was true then. The one exception is the handshake, whose TTL is a
    property of a real token's real lifetime and is measured in elapsed seconds.
    """
    out: list[str] = []
    if status.needs_attention:
        out.extend(status.notes)

    for run in status.latest:
        age = _age_days(run.started_at, as_of=as_of)
        if age is not None and age > stale_after_days:
            out.append(
                f"the newest {run.collector} run is {age} day(s) old, beyond "
                f"server.stale_after_days ({stale_after_days}). Its status is "
                f"{run.status!r}, which describes that run and not the silence since."
            )

    if grid["as_of"] is None:
        out.append(str(grid["note"]))
    else:
        if grid["age_days"] > stale_after_days:
            out.append(
                f"the newest grid was built for {grid['as_of']}, {grid['age_days']} day(s) "
                f"ago, beyond server.stale_after_days ({stale_after_days}). Every ranked "
                "view and every cell read is that old."
            )
        if grid["below_coverage_floor"]:
            out.append(
                f"grid collection coverage is {grid['coverage_pct']:.0%} of what is "
                f"collectible, below the grid.min_coverage_pct floor "
                f"({grid['min_coverage_pct']:.0%})."
            )

    if nav["as_of"] is None:
        out.append(str(nav["note"]))
    elif nav["age_days"] > stale_after_days:
        out.append(
            f"the newest NAV is {nav['age_days']} day(s) old. The sleeve budget and every "
            "concentration cap are sized on it."
        )

    if ibkr["last_authenticated_at"] is None:
        out.append(str(ibkr["note"]))
    elif not ibkr["within_ttl"]:
        out.append(
            "no collector run has authenticated against Client Portal for longer than a "
            "live session token would have lasted, so nothing has proven the credentials "
            "still work."
        )
    return out


def _vendor_block(store: Store, status: CollectorStatus, *, now: datetime) -> dict[str, Any]:
    """marketdata.app credit spend in the status window, and any live block."""
    row = store.execute(
        "SELECT COUNT(*) AS fetches, SUM(COALESCE(credits, 0)) AS credits, "
        "MAX(fetched_at) AS newest FROM vendor_fetch_log WHERE trade_date >= ?",
        (status.since,),
    ).fetchone()
    fetches = 0 if row is None else int(row["fetches"] or 0)
    block: dict[str, Any] = {
        "since": status.since,
        "fetches": fetches,
        "credits_spent": 0 if row is None else int(row["credits"] or 0),
        "last_fetch_at": None if row is None else row["newest"],
    }
    if not fetches:
        block["note"] = (
            "the vendor chain route has bought nothing in this window. On a fresh "
            "install that is usually an empty MARKETDATA_API_TOKEN or an egress "
            "address the account has not authorised."
        )
    cooldown = active_cooldown(store, UPSTREAM_MARKETDATA_APP, now=now)
    if cooldown:
        block["cooldown_until"] = cooldown.get("until")
        block["cooldown_reason"] = cooldown.get("reason")
    return block


def register(mcp: FastMCP, context: ServerContext) -> None:
    @mcp.tool(
        name="get_system_health",
        description=(
            "How far behind the collectors and the grid build are, read out of the "
            "store: last run and status per collector, the last sequence and what it "
            "planned versus completed, grid coverage with its as_of, NAV age, the age "
            "of the last Client Portal handshake against its configured TTL, and "
            "vendor credit spend. Touches no network and works when every upstream is "
            "down. CALL THIS WHEN A GRID READ COMES BACK THIN OR EMPTY: it is what "
            "tells a missing grid apart from a stale one, which otherwise has to be "
            "inferred from absence. An EMPTY store reports empty, never healthy."
        ),
        annotations=READ_ONLY,
    )
    async def get_system_health(
        days: int | None = None, as_of: str | None = None
    ) -> dict[str, Any]:
        """Report what the pipeline has actually done lately.

        Args:
            days: How far back to read run history; defaults to
                collectors.status_report_days.
            as_of: Session date, YYYY-MM-DD, that ages are measured against.
        """
        session = context.as_of(as_of)
        now = utc_now()

        def read() -> dict[str, Any]:
            with context.store() as store:
                status = collector_status(store, context.config, days=days)
                grid = _grid_block(store, context.config, as_of=session)
                nav = _nav_block(store, as_of=session)
                ibkr = _ibkr_block(store, context.config, now=now)
                return {
                    "collectors": {
                        "since": status.since,
                        "window_days": status.window_days,
                        "sequences": [asdict(s) for s in status.sequences],
                        "orphaned_runs": [asdict(r) for r in status.orphaned],
                        "last_run_per_collector": [asdict(r) for r in status.latest],
                        "notes": list(status.notes),
                    },
                    "grid": grid,
                    "nav": nav,
                    "ibkr_session": ibkr,
                    "vendor": _vendor_block(store, status, now=now),
                    "_empty": status.is_empty and grid["as_of"] is None,
                    "_concerns": _concerns(
                        status,
                        grid,
                        nav,
                        ibkr,
                        as_of=session,
                        stale_after_days=context.stale_after_days,
                    ),
                }

        payload = await run_blocking(read)
        empty = payload.pop("_empty")
        concerns = payload.pop("_concerns")
        if empty:
            payload["verdict"] = VERDICT_EMPTY
            payload["verdict_note"] = (
                "this store is EMPTY, which is not the same as healthy: no collector "
                "run and no grid build has ever been recorded in it, so nothing here "
                "is evidence that the pipeline works. Say so rather than reasoning "
                "over the gap."
            )
            payload["concerns"] = concerns
        elif concerns:
            payload["verdict"] = VERDICT_ATTENTION
            payload["concerns"] = concerns
            payload["verdict_note"] = (
                f"{len(concerns)} thing(s) are behind or failing — `concerns` names each "
                "one. A reading taken now is indicative at best, and a grid that is thin "
                "for any of these reasons is a collection gap, not a market fact."
            )
        else:
            payload["verdict"] = VERDICT_OK
            payload["concerns"] = []
        return envelope(context, as_of=session, data=payload)

    @mcp.tool(
        name="run_collectors",
        description=(
            "Manually trigger a collector run: chain, curve, macro, bars, universe, or "
            "the whole daily sequence. Spawns the standalone obliskiq-collect entrypoint "
            "as a SEPARATE PROCESS — it is never imported here. Returns the child's exit "
            "code (0 ok, 1 failed, 2 degraded) and the run-log rows it wrote. Chain and "
            "CHAIN snapshots cannot be retrieved retrospectively, so a missed day is "
            "gone permanently; this is how a missed one gets caught before the market "
            "moves on. A missed CURVE day is recoverable from per-contract history."
        ),
        annotations=SPAWNS_COLLECTOR,
    )
    async def run_collectors(
        task: str = "daily",
        only: list[str] | None = None,
        skip: list[str] | None = None,
        dry_run: bool = False,
        force: bool = False,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        """Run one collector task and report what it did.

        Args:
            task: One of universe, backfill, chain, curve, macro, daily. Anything
                else is rejected against server.collectors.allowed_tasks.
            only: Restrict to these symbols, futures roots or macro series ids.
            skip: Steps to omit from the 'daily' sequence: backfill, chain, curve,
                macro.
            dry_run: Fetch and report, write nothing.
            force: Ignore the macro refetch cache and pull every series again.
            as_of: Session date, YYYY-MM-DD, for the response envelope only. The
                collectors always collect for the moment they run: a snapshot is
                of now, and there is no as-of endpoint for one.
        """
        session = context.as_of(as_of)
        runner = CollectorRunner(context.config)

        try:
            runner.build_argv(task, only=only, skip=skip, dry_run=dry_run, force=force)
        except CollectorsDisabled as exc:
            raise refuse(str(exc)) from exc
        except (ValueError, FileNotFoundError) as exc:
            raise refuse(f"run_collectors: {exc}") from exc

        try:
            result = await runner.run(task, only=only, skip=skip, dry_run=dry_run, force=force)
        except OSError as exc:
            raise refuse(
                f"run_collectors could not start {task!r}: {exc}. The collectors are a "
                "separate entrypoint; check that the package is installed in the "
                "environment this server runs in."
            ) from exc

        def read_runs() -> list[dict[str, Any]]:
            with context.store() as store:
                return runs_since(store, since=result.started_at, limit=context.max_rows)

        runs = await run_blocking(read_runs)
        payload: dict[str, Any] = {
            "task": result.task,
            "outcome": result.outcome,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "started_at": result.started_at,
            "finished_at": result.finished_at,
            "runs": to_jsonable(runs),
            # The resolved path, not the raw argv: the argv can carry symbols a
            # caller supplied, and echoing it back verbatim invites reading tool
            # input as server output.
            "executable": result.argv[0],
        }
        if result.timed_out:
            payload["timeout_note"] = (
                "the child exceeded server.collectors.timeout_seconds and was killed. "
                "Anything it had already written is committed — the collectors are "
                "append-only and idempotent, so re-running is safe."
            )
        elif not result.ok and result.stderr_tail:
            payload["stderr_tail"] = result.stderr_tail
        if not runs:
            payload["runs_note"] = (
                "the child wrote no run-log row. Either it failed before opening the "
                "store, or this was a dry run."
            )
        return envelope(context, as_of=session, data=payload)
