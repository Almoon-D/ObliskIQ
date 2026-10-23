"""``obliskiq universe capabilities [--probe]`` and ``obliskiq universe vehicles <exposure>``, run
in process: what they print, what they read and what they refuse (Session 9).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import io
import logging
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from obliskiq.cli import collect as collect_cli
from obliskiq.cli import vehicles as vehicles_cli
from obliskiq.cli.app import EXIT_INVALID, EXIT_NO_CREDENTIALS, EXIT_OK, run
from obliskiq.config.loader import load_config
from obliskiq.decision.swap_partners import Partner, SwapGroup
from obliskiq.decision.vehicle_selector import resolve_exposure
from obliskiq.domain.research import Frequency, Observation, PointInTime, ResearchSeries, Vendor
from obliskiq.errors import ObliskError
from obliskiq.execution.capability_probe import ProbeReport
from obliskiq.research.data import universe_series_id
from obliskiq.store import open_research_store, open_store, repos
from obliskiq.store.research import replace_research_series

NOW = datetime(2026, 10, 22, 20, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:
    yield
    logging.getLogger().handlers.clear()


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_dir: Path) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    monkeypatch.setenv("OBLISK_DATA_DIR", str(path))
    monkeypatch.setenv("OBLISK_CONFIG_DIR", str(config_dir))
    return path


def _run(*argv: str) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    status = run(list(argv), env_file=None, stdout=out, stderr=err)
    return status, out.getvalue() + err.getvalue()


# ── capabilities ──


def test_capabilities_prints_every_check_and_the_permissions(data_dir: Path) -> None:
    status, out = _run("universe", "capabilities")
    assert status == EXIT_OK, out
    assert "etf on XETR (proposable, probe XDWD):" in out
    assert "order verified CP 2026-10-21" in out
    assert "etf on XLON (not proposable, probe IWDA):" in out
    assert "trading permissions:" in out
    assert "XLON Stocks: United Kingdom: unverified (no accepted preview yet)" in out


def test_the_probe_needs_credentials(data_dir: Path) -> None:
    status, out = _run("universe", "capabilities", "--probe")
    assert status == EXIT_NO_CREDENTIALS
    assert "credentials" in out.lower() or "OBLISK_IBKR" in out


def test_the_probe_prints_its_report_with_the_account_masked(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Probe:
        def __init__(self, settings: Any, client: Any, orders: Any, *, day: date) -> None:
            self.day = day

        async def run(self, checks: Any) -> ProbeReport:
            report = ProbeReport(self.day)
            report.add("etf@XETR", "order", True, "previewed for U1234567")
            return report

    def with_path(loaded: Any, run_probe: Any) -> Any:
        import asyncio

        client = SimpleNamespace(guard=SimpleNamespace(account_id="U1234567"))
        return asyncio.run(run_probe(SimpleNamespace(client=client, orders=None)))

    monkeypatch.setattr("obliskiq.cli.app._broker_refusal", lambda _env, _out: None)
    monkeypatch.setattr(collect_cli, "_with_collection_path", with_path)
    monkeypatch.setattr(vehicles_cli, "CapabilityProbe", Probe)
    status, out = _run("universe", "capabilities", "--probe")
    assert status == EXIT_OK, out
    assert "whatif only, nothing placed" in out
    assert "U1234567" not in out


def test_a_probe_that_cannot_start_says_so(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def with_path(loaded: Any, run_probe: Any) -> Any:
        raise ObliskError("the Gateway did not log in")

    monkeypatch.setattr("obliskiq.cli.app._broker_refusal", lambda _env, _out: None)
    monkeypatch.setattr(collect_cli, "_with_collection_path", with_path)
    status, out = _run("universe", "capabilities", "--probe")
    assert status == EXIT_INVALID
    assert "probe stopped: the Gateway did not log in" in out


# ── vehicles ──


def test_vehicles_ranks_a_role_and_explains_every_rejection(data_dir: Path) -> None:
    status, out = _run("universe", "vehicles", "world_equity")
    assert status == EXIT_OK, out
    assert out.startswith("vehicles for world_equity (Global developed equity (WORLD))")
    assert "1. SPPW (etf on XETR in EUR)" in out or "1. XDWD (etf on XETR in EUR)" in out
    assert "tracking 0.00 x 0.25: unmeasured: no research store" in out
    assert "IWDA [capability_unverified]" in out
    assert "IWLE [hedge_not_allowed]" in out
    # The owner allows a same-index partner (config/owner.yaml): XDWD and SPPW both track MSCI
    # World, so without a research store the pair is refused only for its unmeasured correlation.
    assert "swap partners of" in out
    assert "tracks the same index" not in out
    assert "correlation with" in out
    assert "unmeasured: no research store" in out


def test_a_role_without_a_proposable_vehicle_says_so(data_dir: Path) -> None:
    status, out = _run("universe", "vehicles", "gold")
    assert status == EXIT_OK, out
    assert "none proposable: see the rejections" in out
    assert "IGLN [capability_unverified]" in out
    assert "4GLD [ineligible]" in out
    assert "swap partners of" not in out


def test_an_unknown_exposure_or_primary_is_refused(data_dir: Path) -> None:
    status, out = _run("universe", "vehicles", "nowhere")
    assert status == EXIT_INVALID
    assert "refused: 'nowhere' is neither a core role" in out
    status, out = _run("universe", "vehicles", "world_equity", "--primary", "IWDA")
    assert status == EXIT_INVALID
    assert "IWDA is not a vehicle the selector proposes" in out


def _series(series_id: str, currency: str, points: list[tuple[date, float]]) -> Any:
    dataset, _, field = series_id.partition(".")
    series = ResearchSeries(
        series_id=series_id, dataset_id=dataset, vendor=Vendor.YAHOO, field=field, units="price",
        currency=currency, frequency=Frequency.DAILY, point_in_time=PointInTime.AS_OBSERVED,
        source_url="x", downloaded_at=NOW, content_sha256="a" * 64, bias_notes=("x",),
    )  # fmt: skip
    rows = [Observation(observed_on=d, available_on=d, value=v) for d, v in points]
    return series, rows


def test_vehicles_reads_the_market_and_research_stores(
    data_dir: Path, config_dir: Path, make_instrument: Any, make_bar: Any
) -> None:
    settings = load_config(config_dir).settings
    today = date.today()
    xdwd = settings.universe.instrument("XDWD")
    vhve = settings.universe.instrument("VHVE")
    assert xdwd is not None
    assert vhve is not None
    with open_store(settings, data_dir) as store:
        repos.upsert_instrument(
            store,
            make_instrument(
                conid=xdwd.conid,
                symbol="XDWD",
                listing_exchange="IBIS2",
                currency="EUR",
                option_exchange=None,
                domicile="IE",
                ucits=True,
            ),
        )
        repos.upsert_bars(
            store,
            [
                make_bar(conid=xdwd.conid, session_date=today - timedelta(days=d), close=120.0,
                         volume=50_000.0)
                for d in range(1, 30)
            ],
        )  # fmt: skip
    rng = np.random.default_rng(3)
    months = [today.replace(day=1) - timedelta(days=30 * k) for k in range(60, 0, -1)]
    common = np.cumsum(rng.normal(0, 0.03, len(months)))
    with open_research_store(settings, data_dir) as store:
        for series_id, noise in (
            ("msci_world_eur.level", 0.0),
            (universe_series_id("XDWD"), 0.002),
            (universe_series_id("SPPW"), 0.002),
            (universe_series_id("VHVE"), 0.004),
        ):
            values = np.exp(common + rng.normal(0, noise, len(months))) * 100
            replace_research_series(
                store, *_series(series_id, "EUR", list(zip(months, values.tolist(), strict=True)))
            )
        replace_research_series(store, *_series("ecb_fx.USD", "USD", [(today, 1.1)]))
    status, out = _run("universe", "vehicles", "world_equity")
    assert status == EXIT_OK, out
    assert "average daily value 6,000,000 EUR over 29 sessions" in out
    assert "tracking" in out
    assert "monthly returns" in out
    status, out = _run("universe", "vehicles", "world_equity", "--primary", "XDWD")
    assert status == EXIT_OK, out
    assert "swap partners of XDWD:" in out


def test_the_output_helpers(config_dir: Path) -> None:
    settings = load_config(config_dir).settings
    cell = resolve_exposure(settings, "eurozone/financials")
    assert vehicles_cli.exposure_series(cell) == universe_series_id("EXV1")
    group = SwapGroup(
        "world_equity", "XDWD", (Partner("VHVE", 0.996, "issuer Vanguard"),), refused=()
    )
    assert vehicles_cli.group_lines(group) == [
        "swap partners of XDWD:",
        "  VHVE: issuer Vanguard",
    ]
    closed = SimpleNamespace(session_bounds=lambda *_a: None, next_session=lambda *_a: None)
    assert vehicles_cli.windows(settings, closed, ["XETR"], date(2026, 10, 22)) == {}
