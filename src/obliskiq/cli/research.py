"""``obliskiq research``: the research harness from the command line.

``research data load <dataset>...`` downloads datasets into the research store; ``research data
load-universe`` the universe's tradable candidates from Yahoo (``--candidates N``: and the N
largest stock candidates of each grid cell), each with its first and last close;
``research data coverage`` reports, by year, the share of a universe without a price and who
enters late or ends early; ``research power <prereg>`` states a pre-registration's power to
detect theta and the effect detectable at its target power, before any data; ``research run
<prereg>`` runs a locked pre-registration (or, with ``--explore``, an exploratory trial that is
never evidence) and writes its report; ``research lock <prereg> --commit <sha>`` writes the lock
of a merged pre-registration.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import argparse
import asyncio
from collections.abc import Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

import httpx

from ..config.schema import ObliskConfig
from ..config.schema_research import Dataset
from ..data.candidates import Candidate, Quoted, candidate_cells
from ..domain.research import TrialStage
from ..errors import DataSourceError, ObliskError
from ..research import cross_section_report
from ..research.cross_section import CrossSectionResult, CrossSectionRunner, sample_months
from ..research.cross_section import declared_se as cross_section_se
from ..research.data import (
    CANDIDATE_PREFIX,
    UNIVERSE_PREFIX,
    Listing,
    Sources,
    coverage_by_year,
    derive_dataset,
    http_fetch,
    is_derived,
    load_dataset,
    store_loaded,
    survivorship,
    universe_datasets,
    universe_listings,
    yfinance_history,
)
from ..research.prereg import CrossSectionPrereg, Prereg, load_any
from ..research.registry import Git, TrialRegistry, verify_lock, write_lock
from ..research.report import write_report
from ..research.stats import detectable_effect, effective_n, power
from ..research.study import StudyResult, StudyRunner, declared_se
from ..store import open_research_store, open_store
from ..store import universe as universe_repo
from ..util.http import RetryPolicy
from ..util.timeutil import utc_now

if TYPE_CHECKING:
    from .app import Loaded

EXIT_OK = 0
EXIT_INVALID = 1
ONE_DAY = timedelta(days=1)


def _datasets(loaded: Loaded, out: TextIO) -> list[Dataset] | None:
    research = loaded.config.settings.research
    if loaded.args.all:
        return list(research.datasets)
    chosen: list[Dataset] = []
    for dataset_id in loaded.args.datasets:
        dataset = research.dataset(dataset_id)
        if dataset is None:
            known = ", ".join(d.id for d in research.datasets)
            print(f"refused: no dataset {dataset_id!r} (known: {known})", file=out)
            return None
        chosen.append(dataset)
    if not chosen:
        print("refused: name the datasets to load, or pass --all", file=out)
        return None
    return chosen


def _data_load(loaded: Loaded, out: TextIO) -> int:
    datasets = _datasets(loaded, out)
    if datasets is None:
        return EXIT_INVALID
    settings = loaded.config.settings
    key = loaded.env.fred_api_key
    policy = RetryPolicy.from_settings(settings.http)

    async def load() -> list[tuple[str, int | str]]:
        done: list[tuple[str, int | str]] = []
        timeout = httpx.Timeout(settings.research.data.timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            sources = Sources(
                fetch=http_fetch(client, policy),
                yahoo=yfinance_history,
                fred_api_key=None if key is None else key.get_secret_value(),
            )
            with open_research_store(settings, loaded.env.require_data_dir()) as store:
                for dataset in datasets:
                    try:
                        if is_derived(dataset):
                            series = derive_dataset(dataset, settings, store, now=utc_now())
                        else:
                            series = await load_dataset(dataset, settings, sources, now=utc_now())
                        done.append((dataset.id, store_loaded(store, series)))
                    except ObliskError as exc:
                        done.append((dataset.id, str(exc)))
        return done

    results = asyncio.run(load())
    failed = False
    for dataset_id, outcome in results:
        if isinstance(outcome, int):
            print(f"  {dataset_id}: {outcome} rows", file=out)
        else:
            failed = True
            print(f"  {dataset_id}: FAILED: {outcome}", file=out)
    return EXIT_INVALID if failed else EXIT_OK


def _home_listing(settings: ObliskConfig, candidate: Candidate) -> Quoted | None:
    """The candidate's listing on its home market's venue (the venue whose local currency is its
    country's grid market currency), else its first listing on a configured venue."""
    listings = candidate.venue_listings()
    home = next(
        (
            m.currency
            for m in settings.universe.grid.markets.values()
            if m.country == candidate.country
        ),
        None,
    )
    for listing in listings:
        entry = settings.ibkr_exchanges.get(listing.exchange or "")
        if entry is not None and settings.venues[entry.venue].currencies[0] == home:
            return listing
    return listings[0] if listings else None


def _candidate_listings(loaded: Loaded, top: int) -> list[Listing]:
    """The ``top`` largest candidates of each grid cell, as of today, from the store."""
    settings = loaded.config.settings
    today = utc_now().date()
    with open_store(settings, loaded.env.require_data_dir()) as store:
        holdings = universe_repo.candidate_holdings(store, as_of=today)
        scanned = universe_repo.scanner_rows(store, as_of=today)
    chosen: dict[str, Listing] = {}
    for cell in candidate_cells(settings, holdings, scanned):
        for candidate in cell.candidates[:top]:
            listing = _home_listing(settings, candidate)
            if listing is None or listing.exchange is None or listing.currency is None:
                continue
            chosen.setdefault(
                candidate.key,
                Listing(
                    key=candidate.key,
                    symbol=listing.ticker,
                    exchange=listing.exchange,
                    currency=listing.currency,
                    inception=None,
                    note=f"A stock candidate of {cell.market} ({candidate.name}), from "
                    f"{', '.join(candidate.sources)}.",
                ),
            )
    return list(chosen.values())


def _data_load_universe(loaded: Loaded, out: TextIO) -> int:
    """The tradable candidates' research history, and the top stock candidates' on request."""
    settings = loaded.config.settings
    datasets, skipped = universe_datasets(
        settings, universe_listings(settings), prefix=UNIVERSE_PREFIX
    )
    if loaded.args.candidates:
        more, not_listed = universe_datasets(
            settings, _candidate_listings(loaded, loaded.args.candidates), prefix=CANDIDATE_PREFIX
        )
        datasets += more
        skipped += not_listed

    async def load() -> list[tuple[str, str]]:
        done: list[tuple[str, str]] = []
        sources = Sources(fetch=_no_fetch, yahoo=yfinance_history, fred_api_key=None)
        with open_research_store(settings, loaded.env.require_data_dir()) as store:
            for dataset in datasets:
                try:
                    series = await load_dataset(dataset, settings, sources, now=utc_now())
                except ObliskError as exc:
                    done.append((dataset.id, f"FAILED: {exc}"))
                    continue
                store_loaded(store, series)
                closes = next(s for s in series if s.series.field == "close").observations
                first, last = closes[0].observed_on, closes[-1].observed_on
                done.append((dataset.id, f"{len(closes)} closes, {first} to {last}"))
        return done

    results = asyncio.run(load())
    for dataset_id, outcome in results:
        print(f"  {dataset_id}: {outcome}", file=out)
    for reason in skipped:
        print(f"  skipped {reason}", file=out)
    failed = sum(1 for _, outcome in results if outcome.startswith("FAILED"))
    print(f"loaded {len(results) - failed} of {len(results)} listings", file=out)
    return EXIT_INVALID if failed else EXIT_OK


async def _no_fetch(url: str, _params: Mapping[str, str] | None) -> bytes:
    raise DataSourceError(f"{url}: the universe's history is read from Yahoo alone")


def _universe(loaded: Loaded) -> tuple[list[str], date, date]:
    args = loaded.args
    if args.prereg is not None:
        prereg = load_any(args.prereg)
        if isinstance(prereg, CrossSectionPrereg):
            members = prereg.spec.study
            ids = [m.price_series for m in members.served() if m.price_series is not None]
            return ids, members.development.start, members.confirmation.end
        study = prereg.spec.study
        return [i.price_series for i in study.instruments], study.start, study.end
    return list(args.series), date(args.first_year, 1, 1), date(args.last_year + 1, 1, 1) - ONE_DAY


def _data_coverage(loaded: Loaded, out: TextIO) -> int:
    args = loaded.args
    if args.prereg is None and (
        not args.series or args.first_year is None or args.last_year is None
    ):
        print("refused: give --prereg, or --series with --from and --to", file=out)
        return EXIT_INVALID
    try:
        members, start, end = _universe(loaded)
    except ObliskError as exc:
        print(f"refused: {exc}", file=out)
        return EXIT_INVALID
    as_of: date = args.as_of or end
    settings = loaded.config.settings
    with open_research_store(settings, loaded.env.require_data_dir()) as store:
        years = coverage_by_year(
            store, members, first_year=start.year, last_year=end.year, as_of=as_of
        )
        report = survivorship(store, members, start=start, end=end, as_of=as_of)
    print(f"coverage of {len(members)} series as of {as_of.isoformat()}", file=out)
    for year in years:
        missing = ", ".join(year.missing) or "none"
        print(f"  {year.year}  {year.missing_share:.1%} without a price ({missing})", file=out)
    print(f"  without data: {', '.join(report.without_data) or 'none'}", file=out)
    print(
        f"  entering after {start.isoformat()}: {', '.join(report.late_entries) or 'none'}",
        file=out,
    )
    print(f"  ending before {end.isoformat()}: {', '.join(report.early_exits) or 'none'}", file=out)
    print("  a universe listed today cannot show what disappeared before it was listed", file=out)
    return EXIT_OK


def _prereg(loaded: Loaded, out: TextIO) -> Prereg | CrossSectionPrereg | None:
    try:
        return load_any(loaded.args.prereg)
    except ObliskError as exc:
        print(f"refused: {exc}", file=out)
        return None


def _power(loaded: Loaded, out: TextIO) -> int:
    prereg = _prereg(loaded, out)
    if prereg is None:
        return EXIT_INVALID
    defaults = loaded.config.settings.research.decision
    alpha = float(prereg.spec.alpha if prereg.spec.alpha is not None else defaults.alpha)
    target = float(
        prereg.spec.target_power if prereg.spec.target_power is not None else defaults.target_power
    )
    if isinstance(prereg, CrossSectionPrereg):
        return _cross_section_power(prereg, alpha, target, out)
    spec = prereg.spec
    inputs = spec.power
    per_period = effective_n(inputs.names_per_period, float(inputs.rho))
    observations = inputs.periods * per_period
    tests = spec.tests_in_confirmation
    theta = float(spec.theta)
    print(f"power of {spec.id} before any data", file=out)
    print(f"  effective observations  {observations:g} ({per_period:g} per period)", file=out)
    print(f"  theta                   {theta:.4%} a year", file=out)
    print(f"  alpha {alpha:g}, {tests} test(s) in the confirmation run", file=out)
    print(
        f"  {'candidate':<24} {'se a year':>10} {'power':>7} {f'detectable at {target:g}':>18}",
        file=out,
    )
    for candidate in spec.study.candidates:
        se = declared_se(prereg, candidate.name)
        detectable = detectable_effect(se, alpha=alpha, tests=tests, target_power=target)
        chance = power(theta, se, alpha=alpha, tests=tests)
        print(f"  {candidate.name:<24} {se:>10.4%} {chance:>7.3f} {detectable:>18.4%}", file=out)
    width = max([len("comparison"), *(len(c.name) for c in spec.comparisons)])
    if spec.comparisons:
        print(
            f"  {'comparison':<{width}} {'theta':>7} {'se a year':>10} {'power':>7} "
            f"{f'detectable at {target:g}':>18}",
            file=out,
        )
    for comparison in spec.comparisons:
        se = declared_se(prereg, comparison.name)
        detectable = detectable_effect(se, alpha=alpha, tests=tests, target_power=target)
        chance = power(float(comparison.theta), se, alpha=alpha, tests=tests)
        print(
            f"  {comparison.name:<{width}} {float(comparison.theta):>7.2%} {se:>10.4%} "
            f"{chance:>7.3f} {detectable:>18.4%}",
            file=out,
        )
    return EXIT_OK


def _cross_section_power(
    prereg: CrossSectionPrereg, alpha: float, target: float, out: TextIO
) -> int:
    spec = prereg.spec
    tests = spec.tests_in_confirmation
    months = sample_months(spec.study.confirmation)
    print(f"power of {spec.id} before any data", file=out)
    print(f"  confirmation months     {months} (one observation a month)", file=out)
    print(f"  alpha {alpha:g}, {tests} test(s) in the confirmation run", file=out)
    print(
        f"  {'test':<32} {'theta':>7} {'se a year':>10} {'power':>7} "
        f"{f'detectable at {target:g}':>18}",
        file=out,
    )
    for test in spec.tests:
        se = cross_section_se(spec, test)
        detectable = detectable_effect(se, alpha=alpha, tests=tests, target_power=target)
        chance = power(float(test.theta), se, alpha=alpha, tests=tests)
        print(
            f"  {test.name:<32} {float(test.theta):>7.2%} {se:>10.4%} {chance:>7.3f} "
            f"{detectable:>18.4%}",
            file=out,
        )
    return EXIT_OK


type _Outcomes = list[tuple[str, str, str, float | None, float | None]]


def _outcomes(result: StudyResult | CrossSectionResult) -> _Outcomes:
    """Per test: its name, outcome, labels, estimate and Sharpe."""
    rows: _Outcomes = []
    for test in result.tests:
        labels = ", ".join(label.value for label in test.decision.labels)
        sharpe = None if test.deflated is None else test.deflated.sharpe
        rows.append((test.name, test.decision.outcome.value, labels, test.estimate, sharpe))
    return rows


def _run(loaded: Loaded, out: TextIO) -> int:
    prereg = _prereg(loaded, out)
    if prereg is None:
        return EXIT_INVALID
    explore: bool = loaded.args.explore
    settings = loaded.config.settings
    spec = prereg.spec
    try:
        lock = None if explore else verify_lock(prereg.path)
        root = Git(prereg.path).root
        reports = root / settings.research.reports_dir
        with open_research_store(settings, loaded.env.require_data_dir()) as store:
            registry = TrialRegistry(store)
            if explore:
                registry.check_budget(spec.id, spec.trial_budget)
            else:
                registry.check_single_shot(spec.id)
            result: StudyResult | CrossSectionResult
            if isinstance(prereg, CrossSectionPrereg):
                result = CrossSectionRunner(settings, loaded.profile).run(
                    prereg, store, registry, explore=explore, lock=lock, now=utc_now()
                )
                path = cross_section_report.write_report(result, reports)
            else:
                result = StudyRunner(settings, loaded.profile).run(
                    prereg, store, registry, explore=explore, lock=lock, now=utc_now()
                )
                path = write_report(result, reports)
            outcomes = _outcomes(result)
            key = "test" if isinstance(prereg, CrossSectionPrereg) else "candidate"
            for name, outcome, _labels, estimate, sharpe in outcomes:
                registry.record(
                    study_id=spec.id,
                    stage=TrialStage.EXPLORATION if explore else TrialStage.CONFIRMATION,
                    at=result.run_at,
                    spec_sha256=prereg.sha256,
                    inputs=result.inputs_sha256,
                    parameters={"report": path.name, key: name},
                    metric=spec.primary_metric,
                    estimate=estimate,
                    sharpe=sharpe,
                    observations=max(len(result.dates) - 1, 0),
                    outcome=outcome,
                )
    except ObliskError as exc:
        print(f"refused: {exc}", file=out)
        return EXIT_INVALID
    print(f"{spec.id}:", file=out)
    for name, outcome, labels, estimate, _sharpe in outcomes:
        text = "" if estimate is None else f", estimate {estimate:.4%} a year"
        print(f"  {name}: {outcome}" + (f" ({labels})" if labels else "") + text, file=out)
    print(f"  report {path}", file=out)
    return EXIT_OK


def _lock(loaded: Loaded, out: TextIO) -> int:
    try:
        lock = write_lock(loaded.args.prereg, loaded.args.commit)
    except ObliskError as exc:
        print(f"refused: {exc}", file=out)
        return EXIT_INVALID
    print(f"locked {lock.file} at {lock.merge_commit} (SHA-256 {lock.sha256})", file=out)
    return EXIT_OK


def add_research_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    research = commands.add_parser("research", help="the research harness")
    actions = research.add_subparsers(dest="action", required=True, metavar="ACTION")

    data = actions.add_parser("data", help="research history")
    data_actions = data.add_subparsers(dest="data_action", required=True, metavar="ACTION")
    load = data_actions.add_parser("load", help="download datasets into the research store")
    load.add_argument("datasets", nargs="*", help="dataset ids from research.datasets")
    load.add_argument("--all", action="store_true", help="every configured dataset")
    load.set_defaults(handler=_data_load)
    universe = data_actions.add_parser(
        "load-universe", help="the tradable candidates' history (and the top stock candidates')"
    )
    universe.add_argument(
        "--candidates", type=int, default=0, help="also the N largest stock candidates per cell"
    )
    universe.set_defaults(handler=_data_load_universe)
    coverage = data_actions.add_parser(
        "coverage", help="the share of a universe without a price, by year, and its survivorship"
    )
    coverage.add_argument("--prereg", type=Path, help="the universe of a pre-registration")
    coverage.add_argument("--series", nargs="*", default=[], help="series ids")
    coverage.add_argument("--from", dest="first_year", type=int, help="first year")
    coverage.add_argument("--to", dest="last_year", type=int, help="last year")
    coverage.add_argument("--as-of", type=date.fromisoformat, help="read as of (YYYY-MM-DD)")
    coverage.set_defaults(handler=_data_coverage)

    power_parser = actions.add_parser("power", help="power and the detectable effect, before data")
    power_parser.add_argument("prereg", type=Path, help="the pre-registration file")
    power_parser.set_defaults(handler=_power)

    run = actions.add_parser("run", help="run a locked pre-registration and write its report")
    run.add_argument("prereg", type=Path, help="the pre-registration file")
    run.add_argument(
        "--explore",
        action="store_true",
        help="an exploratory trial: no lock needed, counted in the registry, never evidence",
    )
    run.set_defaults(handler=_run)

    lock = actions.add_parser("lock", help="lock a merged pre-registration at its merge commit")
    lock.add_argument("prereg", type=Path, help="the pre-registration file")
    lock.add_argument("--commit", required=True, help="the merge commit")
    lock.set_defaults(handler=_lock)
