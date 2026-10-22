"""``obliskiq collect`` and ``obliskiq universe``.

``collect <universe|candidates|backfill|snapshots|chains|curves|daily>`` runs one collector (or
every step, ``daily``) by hand over the venues named with ``--venue`` (every venue by default), as
one run record in the run log, under the service lock: one process talks to the broker at a
time (stop a running service first). ``collect candidates`` refreshes the issuers' holdings files
on their cadence (``--force``: now) and runs the declared scanners; without IBKR credentials it
refreshes the files alone and says the scanners were not run.

``universe validate`` prints the universe: its grid and macro board, every pinned instrument's
eligibility under the profile's policy and why, the venue profiles with the market-data plan and
its cost, and the candidates' coverage of the grid as of today. ``universe pin <SYMBOL>``
resolves an instrument the file declares without a conid and writes it into
``config/universe.yaml``; ``universe pin --candidate <KEY> --thesis <ID>`` resolves a stock
candidate and pins it, so it is seeded and collected.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

import httpx

from ..calendars.profiles import venue_profiles
from ..config.schema import ObliskConfig
from ..config.schema_collectors import COLLECT_STEPS, CollectStep
from ..data.candidates import (
    CandidateCollector,
    Cell,
    candidate_cells,
    coverage_problems,
    http_fetch,
    resolve_candidate,
)
from ..data.market.daily import COLLECT_COMMAND, MarketCollector
from ..data.market.plumbing import Collection, Pacer
from ..data.market.targets import collection_venues, declared_targets
from ..data.market.universe import SEARCH_SEC_TYPE, pin_in_file
from ..errors import ObliskError
from ..execution.instruments import traded_instrument
from ..execution.runtime import OrderPath, open_order_path
from ..ibkr.identity import resolve_identity
from ..ibkr.transport import missing_credentials, transport_refusal
from ..jurisdiction import ELIGIBILITY_POLICIES
from ..jurisdiction.profile import JurisdictionProfile
from ..orchestration.lock import ServiceLock
from ..orchestration.runlog import exit_code
from ..store import open_store
from ..store import universe as universe_repo
from ..store.worker import StoreWorker
from ..util.http import RetryPolicy
from ..util.timeutil import utc_now
from .vehicles import add_vehicle_commands

if TYPE_CHECKING:
    from .app import Loaded

EXIT_OK = 0
EXIT_INVALID = 1
CANDIDATES = "candidates"
DAILY = "daily"


def _with_collection_path[T](loaded: Loaded, run: Callable[[OrderPath], Awaitable[T]]) -> T:
    """``run`` over a read session opened for this command, its calendars covering every venue
    the universe collects on, under the service lock."""
    settings = loaded.config.settings
    data_dir = loaded.env.require_data_dir()

    async def go() -> T:
        async with open_order_path(
            loaded.env,
            settings,
            loaded.profile,
            data_dir=data_dir,
            extra_venues=collection_venues(settings),
        ) as path:
            return await run(path)

    with ServiceLock(data_dir / settings.paths.service_lock, command=str(loaded.args.command)):
        return asyncio.run(go())


def _report(collection: Collection, out: TextIO) -> None:
    outcome = collection.judge()
    print(f"{collection.step}: {outcome.status} ({collection.rows} rows)", file=out)
    for target in collection.outcomes:
        if not target.ok or target.degraded:
            state = "failed" if not target.ok else "degraded"
            print(f"  {target.target}: {state}: {target.reason}", file=out)
    if collection.empty_reason and not collection.outcomes:
        print(f"  {collection.empty_reason}", file=out)


def _collect(loaded: Loaded, out: TextIO) -> int:
    from .app import _broker_refusal

    step: str = loaded.args.step
    if step == CANDIDATES:
        return _collect_candidates(loaded, out)
    refused = _broker_refusal(loaded.env, out)
    if refused is not None:
        return refused
    settings = loaded.config.settings
    venues = set(loaded.args.venue) or None
    steps: tuple[CollectStep, ...] = tuple(s for s in COLLECT_STEPS if step in (DAILY, s))

    async def run(path: OrderPath) -> str:
        collector = MarketCollector(
            settings,
            client=path.client,
            worker=path.worker,
            calendars=path.calendars,
            now=utc_now,
            sleep=path.client.session.clock.sleep,
        )
        record = await collector.run(
            COLLECT_COMMAND, utc_now().date(), venues=venues, steps=steps,
            dry_run=loaded.args.dry_run,
        )  # fmt: skip
        for outcome in record.outcomes:
            print(f"{outcome.name}: {outcome.status}", file=out)
            if outcome.reason:
                print(f"  {outcome.reason}", file=out)
        return record.status

    try:
        status = _with_collection_path(loaded, run)
    except ObliskError as exc:
        print(f"collect stopped: {exc}", file=out)
        return EXIT_INVALID
    print(f"run: {status}", file=out)
    return exit_code(status)


def _collect_candidates(loaded: Loaded, out: TextIO) -> int:
    settings = loaded.config.settings
    data_dir = loaded.env.require_data_dir()
    policy = RetryPolicy.from_settings(settings.http)

    async def refresh(path: OrderPath | None) -> Collection:
        timeout = httpx.Timeout(settings.http.timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            worker = path.worker if path is not None else StoreWorker(
                lambda: open_store(settings, data_dir)
            )  # fmt: skip
            try:
                collector = CandidateCollector(
                    settings,
                    fetch=http_fetch(client, policy),
                    scanner=path.client.scanner if path is not None else None,
                    worker=worker,
                    pacer=Pacer(settings.collectors, asyncio.sleep),
                    now=utc_now,
                )
                return await collector.run(dry_run=loaded.args.dry_run, force=loaded.args.force)
            finally:
                if path is None:
                    await worker.aclose()

    env = loaded.env
    reachable = transport_refusal(env) is None and not missing_credentials(env)
    if env.ibkr_profile is not None and reachable:
        try:
            collection = _with_collection_path(loaded, refresh)
        except ObliskError as exc:
            print(f"collect stopped: {exc}", file=out)
            return EXIT_INVALID
    else:
        print("no IBKR credentials: the holdings files alone are refreshed", file=out)
        collection = asyncio.run(refresh(None))
    _report(collection, out)
    return exit_code(collection.status)


# ── universe ──


def _grid_lines(settings: ObliskConfig) -> list[str]:
    grid = settings.universe.grid
    measured = {key: cell.symbol for key, cell in settings.universe.cells().items() if cell.symbol}
    lines = [f"grid: {len(grid.markets)} markets x {len(grid.sectors)} sectors"]
    for market in grid.markets:
        held = [f"{s} {measured[(market, s)]}" for s in grid.sectors if (market, s) in measured]
        unmapped = [s for s in grid.sectors if (market, s) not in measured]
        line = f"  {market}: " + ", ".join(held)
        lines.append(line + (f"; unmapped: {', '.join(unmapped)}" if unmapped else ""))
    notes = [i for i in settings.universe.instruments if i.coverage_note and i.measured_cell]
    lines += [f"  note on {i.symbol}: {i.coverage_note}" for i in notes]
    return lines


def _root_lines(settings: ObliskConfig) -> list[str]:
    """Each futures root: what it measures, how it can be expressed and what it will feed."""
    lines = [f"futures roots (data only): {len(settings.universe.futures_roots)}"]
    for root in settings.universe.futures_roots:
        series = [
            f"{name} {value}"
            for name, value in (
                ("positioning", root.cot_series),
                ("inventory", root.inventory_series),
                ("history", root.history_series),
            )
            if value
        ]
        traits = [root.bucket] + (["seasonal"] if root.seasonal else [])
        tracked = "" if root.tracked else " (not tracked)"
        lines.append(
            f"  {root.root} on {root.exchange}{tracked}: {root.description}, "
            f"{', '.join(traits)}; {'; '.join(series) or 'no series'}; {root.expression}"
        )
    return lines


def _eligibility_lines(settings: ObliskConfig, profile: JurisdictionProfile) -> list[str]:
    policy = ELIGIBILITY_POLICIES.resolve(profile, settings)
    source = getattr(policy, "source", "")
    lines = [f"eligibility: policy {policy.policy_id}" + (f" ({source})" if source else "")]
    for declared in settings.universe.instruments:
        verdict = policy.assess_declared(declared)
        state = "eligible" if verdict.eligible else "refused"
        pending = []
        if declared.conid is None:
            pending.append("not pinned")
        elif traded_instrument(declared) is None:
            pending.append("no resolved description")
        note = f" [never ordered until resolved: {', '.join(pending)}]" if pending else ""
        lines.append(f"  {declared.symbol}: {state}: {verdict.reason}{note}")
    return lines


def _venue_lines(settings: ObliskConfig) -> list[str]:
    profiles = venue_profiles(settings)
    plan = settings.market_data
    lines = ["venues the universe collects on:"]
    for mic in collection_venues(settings):
        profile = profiles[mic]
        feeds = [f.id for f in plan.planned() if mic in f.venues]
        lot = f", board lot {profile.board_lot}" if profile.board_lot else ""
        unverified = (
            f" [VERIFY-LIVE: {', '.join(profile.unverified)}]" if profile.unverified else ""
        )
        lines.append(
            f"  {mic} {profile.name}: {'/'.join(profile.currencies)}, "
            f"{profile.trading_permission}{lot}; "
            f"{'real time: ' + ', '.join(feeds) if feeds else 'delayed: no feed of the plan'}"
            f"{unverified}"
        )
    cost = ", ".join(f"{amount} {currency}" for currency, amount in plan.plan_cost().items())
    lines.append(
        f"market-data plan: {', '.join(plan.plan)}; {cost or 'nothing'} a month before waivers"
    )
    return lines


def _coverage_lines(cells: list[Cell]) -> list[str]:
    problems = coverage_problems(cells)
    covered = sum(1 for cell in cells if cell.candidates)
    gaps = sum(1 for cell in cells if not cell.candidates and cell.gap)
    lines = [f"candidates: {covered} cells hold candidates, {gaps} declare a gap"]
    lines += [f"  problem: {problem}" for problem in problems]
    return lines


def _universe_validate(loaded: Loaded, out: TextIO) -> int:
    settings = loaded.config.settings
    universe = settings.universe
    tradable = sum(1 for i in universe.instruments if i.tradable)
    chains = sum(1 for i in universe.instruments if i.collects_chain)
    print(
        f"universe is valid: {len(universe.instruments)} instruments ({tradable} tradable "
        f"candidates, {chains} option underlyings), {len(universe.futures_roots)} futures roots, "
        f"{len(declared_targets(settings))} collected",
        file=out,
    )
    lines = [
        *_grid_lines(settings),
        f"macro board: {len(universe.macro_board.regions)} regions x "
        f"{len(universe.macro_board.asset_classes)} asset classes",
        *_root_lines(settings),
        *_eligibility_lines(settings, loaded.profile),
        *_venue_lines(settings),
    ]
    data_dir = loaded.env.data_dir
    if data_dir is not None and (data_dir / settings.paths.database).exists():
        today = utc_now().date()
        with open_store(settings, data_dir) as store:
            holdings = universe_repo.candidate_holdings(store, as_of=today)
            scanned = universe_repo.scanner_rows(store, as_of=today)
        lines += _coverage_lines(candidate_cells(settings, holdings, scanned))
    else:
        lines.append("candidates: no store yet (`obliskiq collect candidates` fills it)")
    for line in lines:
        print(line, file=out)
    return EXIT_OK


def _config_dir(loaded: Loaded) -> Path:
    chosen: Path | None = loaded.args.config_dir
    return (chosen if chosen is not None else loaded.env.resolved_config_dir()).expanduser()


def _universe_pin(loaded: Loaded, out: TextIO) -> int:
    from .app import _broker_refusal

    args = loaded.args
    if (args.symbol is None) == (args.candidate is None):
        print("refused: name a SYMBOL, or a --candidate with its --thesis", file=out)
        return EXIT_INVALID
    if args.candidate is not None and not args.thesis:
        print("refused: a candidate is pinned for a thesis (--thesis)", file=out)
        return EXIT_INVALID
    refused = _broker_refusal(loaded.env, out)
    if refused is not None:
        return refused
    settings = loaded.config.settings
    if args.symbol is not None:
        declared = settings.universe.instrument(args.symbol)
        if declared is None or declared.conid is not None:
            state = "not declared" if declared is None else f"already pinned to {declared.conid}"
            print(f"refused: {args.symbol} is {state}", file=out)
            return EXIT_INVALID

    async def pin(path: OrderPath) -> str:
        if args.symbol is not None:
            declared = settings.universe.instrument(args.symbol)
            assert declared is not None  # checked above
            identity = await resolve_identity(
                path.client.secdef,
                declared.symbol,
                listing_exchange=declared.listing_exchange,
                currency=declared.currency,
                sec_type=SEARCH_SEC_TYPE[declared.asset_class],
            )
            pin_in_file(_config_dir(loaded) / "universe.yaml", declared.symbol, identity.conid)
            return f"{declared.symbol} pinned to conid {identity.conid} in universe.yaml"
        today = utc_now().date()
        holdings = await path.worker.run(universe_repo.candidate_holdings, as_of=today)
        scanned = await path.worker.run(universe_repo.scanner_rows, as_of=today)
        found = {
            c.key: c
            for cell in candidate_cells(settings, holdings, scanned)
            for c in cell.candidates
        }
        candidate = found.get(args.candidate)
        if candidate is None:
            return f"refused: no candidate {args.candidate!r} as of {today}"
        resolution = await resolve_candidate(
            path.client.secdef, candidate, at=utc_now(), pinned_for=args.thesis
        )
        await path.worker.run(universe_repo.record_resolution, resolution)
        if resolution.conid is None:
            return f"refused: {candidate.key} did not resolve: {resolution.failure}"
        return f"{candidate.key} pinned to conid {resolution.conid} for {args.thesis}"

    try:
        said = _with_collection_path(loaded, pin)
    except ObliskError as exc:
        print(f"pin stopped: {exc}", file=out)
        return EXIT_INVALID
    print(said, file=out)
    return EXIT_INVALID if said.startswith("refused") else EXIT_OK


def add_collect_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    collect = commands.add_parser("collect", help="run a market-data collector by hand")
    collect.add_argument(
        "step", choices=[*COLLECT_STEPS, CANDIDATES, DAILY], help="what to collect"
    )
    collect.add_argument("--venue", action="append", default=[], help="a MIC (repeatable)")
    collect.add_argument("--dry-run", action="store_true", help="collect and write nothing")
    collect.add_argument("--force", action="store_true", help="candidates: download every file now")
    collect.set_defaults(handler=_collect)

    universe = commands.add_parser("universe", help="the universe")
    actions = universe.add_subparsers(dest="action", required=True, metavar="ACTION")
    validate = actions.add_parser("validate", help="the universe, its eligibility and coverage")
    validate.set_defaults(handler=_universe_validate)
    pin = actions.add_parser("pin", help="pin an instrument or a stock candidate")
    pin.add_argument("symbol", nargs="?", help="a declared instrument without a conid")
    pin.add_argument("--candidate", help="a stock candidate's key (country:name)")
    pin.add_argument("--thesis", help="the thesis that needs the candidate")
    pin.set_defaults(handler=_universe_pin)
    add_vehicle_commands(actions)
