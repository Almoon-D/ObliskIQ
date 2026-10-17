"""``obliskiq research``: the research harness from the command line.

``research data load <dataset>...`` downloads datasets into the research store;
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
import math
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

import httpx

from ..config.schema_research import Dataset
from ..domain.research import TrialStage
from ..errors import ObliskError
from ..jurisdiction import TAX_POLICIES
from ..quant.costs import CostBook
from ..research.data import (
    Sources,
    coverage_by_year,
    http_fetch,
    load_dataset,
    store_loaded,
    survivorship,
    yfinance_history,
)
from ..research.prereg import Prereg, load_prereg
from ..research.registry import Git, TrialRegistry, verify_lock, write_lock
from ..research.report import write_report
from ..research.stats import detectable_effect, effective_n, power
from ..research.study import StudyRunner
from ..store import open_research_store
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


def _universe(loaded: Loaded) -> tuple[list[str], date, date]:
    args = loaded.args
    if args.prereg is not None:
        study = load_prereg(args.prereg).spec.study
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


def _prereg(loaded: Loaded, out: TextIO) -> Prereg | None:
    try:
        return load_prereg(loaded.args.prereg)
    except ObliskError as exc:
        print(f"refused: {exc}", file=out)
        return None


def _power(loaded: Loaded, out: TextIO) -> int:
    prereg = _prereg(loaded, out)
    if prereg is None:
        return EXIT_INVALID
    spec = prereg.spec
    defaults = loaded.config.settings.research.decision
    alpha = float(spec.alpha if spec.alpha is not None else defaults.alpha)
    target = float(spec.target_power if spec.target_power is not None else defaults.target_power)
    inputs = spec.power
    per_period = effective_n(inputs.names_per_period, float(inputs.rho))
    observations = inputs.periods * per_period
    se = float(inputs.sd_per_period) * inputs.periods_per_year / math.sqrt(observations)
    tests = spec.tests_in_confirmation
    print(f"power of {spec.id} before any data", file=out)
    print(f"  effective observations  {observations:g} ({per_period:g} per period)", file=out)
    print(f"  standard error          {se:.4%} a year", file=out)
    print(f"  theta                   {float(spec.theta):.4%} a year", file=out)
    print(
        f"  power to detect theta   {power(float(spec.theta), se, alpha=alpha, tests=tests):.3f}",
        file=out,
    )
    detectable = detectable_effect(se, alpha=alpha, tests=tests, target_power=target)
    print(f"  detectable at {target:g}      {detectable:.4%} a year", file=out)
    print(f"  alpha {alpha:g}, {tests} test(s) in the confirmation run", file=out)
    return EXIT_OK


def _run(loaded: Loaded, out: TextIO) -> int:
    prereg = _prereg(loaded, out)
    if prereg is None:
        return EXIT_INVALID
    explore: bool = loaded.args.explore
    settings = loaded.config.settings
    try:
        lock = None if explore else verify_lock(prereg.path)
        root = Git(prereg.path).root
        policy = TAX_POLICIES.resolve(loaded.profile, settings)
        runner = StudyRunner(settings, policy, CostBook.from_config(settings))
        with open_research_store(settings, loaded.env.require_data_dir()) as store:
            registry = TrialRegistry(store)
            if explore:
                registry.check_budget(prereg.spec.id, prereg.spec.trial_budget)
            else:
                registry.check_single_shot(prereg.spec.id)
            result = runner.run(prereg, store, registry, explore=explore, lock=lock, now=utc_now())
            path = write_report(result, root / settings.research.reports_dir)
            registry.record(
                study_id=prereg.spec.id,
                stage=TrialStage.EXPLORATION if explore else TrialStage.CONFIRMATION,
                at=result.run_at,
                spec_sha256=prereg.sha256,
                inputs=result.inputs_sha256,
                parameters={"report": path.name},
                metric=prereg.spec.primary_metric,
                estimate=result.estimate,
                sharpe=None if result.deflated is None else result.deflated.sharpe,
                observations=max(len(result.dates) - 1, 0),
                outcome=result.decision.outcome.value,
            )
    except ObliskError as exc:
        print(f"refused: {exc}", file=out)
        return EXIT_INVALID
    labels = ", ".join(label.value for label in result.decision.labels)
    print(
        f"{prereg.spec.id}: {result.decision.outcome.value}" + (f" ({labels})" if labels else ""),
        file=out,
    )
    if result.estimate is not None:
        print(f"  estimate {result.estimate:.4%} a year", file=out)
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
