"""Shared fixtures.

Every test runs with the variables ObliskIQ reads removed from the environment, so a
developer's own credentials or overrides can never change a result.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import os
import shutil
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from hypothesis import settings

from obliskiq.calendars.service import Calendars
from obliskiq.config import ObliskConfig, load_config
from obliskiq.domain.enums import (
    AssetClass,
    InstrumentRole,
    KidStatus,
    MarketDataStatus,
    OptionRight,
)
from obliskiq.domain.instrument import Instrument
from obliskiq.domain.macro import MacroObservation
from obliskiq.domain.market import Bar, ChainRow, CurvePoint, Snapshot
from obliskiq.env import variable_names
from obliskiq.ibkr.session import BrokerSession
from obliskiq.ibkr.transport.base import TransportKind
from obliskiq.store import Store, open_database
from obliskiq.store.worker import StoreWorker
from obliskiq.util.http import RetryPolicy

ROOT = Path(__file__).resolve().parents[1]

# Generated cases are never judged on speed (a slow runner is not a failing property); in CI
# the examples are derandomised so a red run reproduces from the commit alone.
settings.register_profile("default", deadline=None)
settings.register_profile("ci", deadline=None, derandomize=True)
settings.load_profile("ci" if os.environ.get("CI") else "default")


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in variable_names():
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return ROOT


class DateRules:
    """Calendar rules written out independently of the library under test."""

    @staticmethod
    def easter_sunday(year: int) -> date:
        """Western Easter by the anonymous Gregorian (computus) algorithm."""
        a, b, c = year % 19, year // 100, year % 100
        d, e = b // 4, b % 4
        f = (b + 8) // 25
        g = (b - f + 1) // 3
        h = (19 * a + b - d - g + 15) % 30
        i, k = c // 4, c % 4
        weekday_offset = (32 + 2 * e + 2 * i - h - k) % 7
        m = (a + 11 * h + 22 * weekday_offset) // 451
        month = (h + weekday_offset - 7 * m + 114) // 31
        day = (h + weekday_offset - 7 * m + 114) % 31 + 1
        return date(year, month, day)

    @staticmethod
    def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
        """The ``n``-th ``weekday`` (Monday is 0) of a month, counting from 1."""
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))

    @staticmethod
    def last_weekday(year: int, month: int, weekday: int) -> date:
        following = date(year + month // 12, month % 12 + 1, 1)
        return following - timedelta(days=(following.weekday() - weekday) % 7 or 7)


@pytest.fixture(scope="session")
def rules() -> type[DateRules]:
    return DateRules


@pytest.fixture(scope="session")
def shipped_settings() -> ObliskConfig:
    """The shipped configuration, loaded once. Read-only: models are frozen."""
    return load_config(ROOT / "config").settings


#: The reference date every calendar test builds around (the session's date).
CALENDAR_REFERENCE = date(2026, 10, 9)


@pytest.fixture(scope="session")
def calendars(shipped_settings: ObliskConfig) -> Calendars:
    """Every shipped venue, built once for the session. Tests must not change its incidents;
    a test that does builds its own through ``build_calendars``."""
    return Calendars.from_settings(shipped_settings, reference=CALENDAR_REFERENCE)


@pytest.fixture
def build_calendars(shipped_settings: ObliskConfig) -> Callable[..., Calendars]:
    """A fresh facade over a subset of the shipped venues (fast: build only what is named)."""

    def build(venues: tuple[str, ...], **kwargs: Any) -> Calendars:
        return Calendars.from_settings(
            shipped_settings, reference=CALENDAR_REFERENCE, venues=venues, **kwargs
        )

    return build


@pytest.fixture
def open_test_store(shipped_settings: ObliskConfig) -> Iterator[Callable[..., Store]]:
    """Open stores with the shipped pragmas; every one is closed when the test ends."""
    opened: list[Store] = []

    def open_one(path: Path | str, **overrides: Any) -> Store:
        pragmas: dict[str, Any] = {
            "busy_timeout_ms": shipped_settings.store.busy_timeout_ms,
            "synchronous": shipped_settings.store.synchronous,
        }
        pragmas.update(overrides)
        store = open_database(path, **pragmas)
        opened.append(store)
        return store

    yield open_one
    for store in opened:
        store.close()


@pytest.fixture
def store(open_test_store: Callable[..., Store], tmp_path: Path) -> Store:
    """A migrated file database; the file is what makes write-ahead logging real."""
    return open_test_store(tmp_path / "store.db")


@pytest.fixture
async def store_worker(store: Store, shipped_settings: ObliskConfig) -> AsyncIterator[StoreWorker]:
    """A store worker on its own connection to the test's store file (a store belongs to the
    thread that opened it); the test reads through ``store``."""
    worker = StoreWorker(
        lambda: open_database(
            store.path,
            busy_timeout_ms=shipped_settings.store.busy_timeout_ms,
            synchronous=shipped_settings.store.synchronous,
        )
    )
    yield worker
    await worker.aclose()


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """A writable copy of the shipped ``config/`` directory."""
    target = tmp_path / "config"
    shutil.copytree(ROOT / "config", target)
    return target


# ── domain object factories ──
#
# Each factory builds a valid object and takes keyword overrides, so a test names only what
# it is about.

COLLECTED_AT = datetime(2026, 10, 9, 22, 30, tzinfo=UTC)
CREATED_AT = datetime(2026, 1, 5, 8, 0, tzinfo=UTC)


@pytest.fixture
def make_instrument() -> Callable[..., Instrument]:
    def build(**overrides: Any) -> Instrument:
        fields: dict[str, Any] = {
            "conid": 756733,
            "symbol": "SPY",
            "listing_exchange": "ARCA",
            "currency": "USD",
            "asset_class": AssetClass.ETF,
            "domicile": "US",
            "ucits": False,
            "kid_status": KidStatus.UNAVAILABLE,
            "share_class": None,
            "lot_size": 1,
            "option_exchange": "CBOE",
            "history_exchange": None,
            "roles": {InstrumentRole.DATA, InstrumentRole.OPTION_UNDERLYING},
            "superseded_by": None,
            "created_at": CREATED_AT,
            "superseded_at": None,
        }
        fields.update(overrides)
        return Instrument(**fields)

    return build


@pytest.fixture
def make_bar() -> Callable[..., Bar]:
    def build(**overrides: Any) -> Bar:
        fields: dict[str, Any] = {
            "conid": 756733,
            "session_date": date(2026, 10, 8),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1000.0,
            "source": "cp_api",
        }
        fields.update(overrides)
        return Bar(**fields)

    return build


@pytest.fixture
def make_snapshot() -> Callable[..., Snapshot]:
    def build(**overrides: Any) -> Snapshot:
        fields: dict[str, Any] = {
            "snapshot_date": date(2026, 10, 8),
            "conid": 756733,
            "last": 100.5,
            "bid": 100.4,
            "ask": 100.6,
            "volume": 1000,
            "implied_vol": 0.21,
            "implied_vol_source": "cp_api",
            "historical_vol_30d": 0.18,
            "dividend_yield": None,
            "dividend_yield_basis": None,
            "implied_vol_solved": None,
            "implied_vol_solved_source": None,
            "source": "cp_api",
            "collected_at": COLLECTED_AT,
            "market_data_status": MarketDataStatus.REALTIME,
            "market_data_raw": "REALTIME",
            "quote_delay_seconds": None,
        }
        fields.update(overrides)
        return Snapshot(**fields)

    return build


@pytest.fixture
def make_chain_row() -> Callable[..., ChainRow]:
    def build(**overrides: Any) -> ChainRow:
        fields: dict[str, Any] = {
            "snapshot_date": date(2026, 10, 8),
            "underlying_conid": 756733,
            "expiry": date(2027, 1, 15),
            "strike": 100.0,
            "right": OptionRight.CALL,
            "option_conid": None,
            "option_symbol": None,
            "option_exchange": "CBOE",
            "bid": 5.0,
            "ask": 5.2,
            "last": 5.1,
            "iv": 0.25,
            "delta": 0.5,
            "open_interest": 1000,
            "volume": 50,
            "sigma_used": None,
            "sigma_source": None,
            "source": "cp_api",
            "collected_at": COLLECTED_AT,
            "market_data_status": MarketDataStatus.REALTIME,
            "market_data_raw": "REALTIME",
            "quote_delay_seconds": None,
            "multiplier": None,
        }
        fields.update(overrides)
        return ChainRow(**fields)

    return build


@pytest.fixture
def make_curve_point() -> Callable[..., CurvePoint]:
    def build(**overrides: Any) -> CurvePoint:
        fields: dict[str, Any] = {
            "snapshot_date": date(2026, 10, 8),
            "root": "CL",
            "expiry": date(2026, 12, 21),
            "price": 70.0,
            "annualised_roll": 0.02,
            "conid": None,
            "exchange": "NYMEX",
            "currency": "USD",
            "source": "cp_api",
            "collected_at": COLLECTED_AT,
            "market_data_status": MarketDataStatus.DELAYED,
            "market_data_raw": "DELAYED",
            "quote_delay_seconds": 900,
        }
        fields.update(overrides)
        return CurvePoint(**fields)

    return build


@pytest.fixture
def make_observation() -> Callable[..., MacroObservation]:
    def build(**overrides: Any) -> MacroObservation:
        fields: dict[str, Any] = {
            "series_id": "CPI_X",
            "source": "fred",
            "reference_date": date(2026, 7, 1),
            "release_date": date(2026, 8, 12),
            "value": 2.1,
            "frequency": "monthly",
            "units": "percent",
            "collected_at": COLLECTED_AT,
        }
        fields.update(overrides)
        return MacroObservation(**fields)

    return build


# ── IBKR test support ──


class FakeClock:
    """Virtual time for the rate limiter and the session: sleeping advances the clock.

    Concurrent sleepers each resume at or after the instant they asked for, so pacing that is
    correct against this clock is correct against a real one.
    """

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        target = self.now + max(seconds, 0.0)
        await asyncio.sleep(0)
        self.now = max(self.now, target)


class FakeTransport:
    """A transport that needs no login: it counts prepares and recoveries."""

    def __init__(self, base_url: str = "https://gw.test/v1/api") -> None:
        self._base_url = base_url
        self.prepared = 0
        self.refreshed = 0
        self.recovered = 0
        self.closed = 0
        self.fail_recover: Exception | None = None

    @property
    def kind(self) -> TransportKind:
        return TransportKind.GATEWAY

    @property
    def base_url(self) -> str:
        return self._base_url

    def verify(self) -> bool:
        return True

    async def prepare(self) -> None:
        self.prepared += 1

    async def refresh(self) -> None:
        self.refreshed += 1

    async def headers(self, method: str, url: str, params: Any) -> dict[str, str]:
        return {"Accept": "application/json"}

    async def recover(self) -> None:
        self.recovered += 1
        if self.fail_recover is not None:
            raise self.fail_recover

    async def aclose(self) -> None:
        self.closed += 1


#: The base URL every IBKR test mocks.
IBKR_BASE = "https://gw.test/v1/api"


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fake_transport() -> FakeTransport:
    return FakeTransport(IBKR_BASE)


@pytest.fixture
def make_session(
    shipped_settings: ObliskConfig, fake_clock: FakeClock, fake_transport: FakeTransport
) -> Callable[..., BrokerSession]:
    """A BrokerSession over the fake transport and clock, its HTTP mocked with respx."""

    def build(**overrides: Any) -> BrokerSession:
        kwargs: dict[str, Any] = {
            "clock": fake_clock,
            "http": httpx.AsyncClient(),
        }
        ibkr = overrides.pop("ibkr", shipped_settings.ibkr)
        kwargs.update(overrides)
        transport = kwargs.pop("transport", fake_transport)
        return BrokerSession(
            transport, ibkr, RetryPolicy.from_settings(shipped_settings.http), **kwargs
        )

    return build


@pytest.fixture(scope="session")
def rig_calendars(shipped_settings: ObliskConfig) -> Calendars:
    """The venues the order rig's instruments trade on, built once. Tests must not change its
    incidents; a test that does builds its own through ``build_calendars``."""
    from order_rig import VENUES

    return Calendars.from_settings(shipped_settings, reference=CALENDAR_REFERENCE, venues=VENUES)


@pytest.fixture
async def order_rig(
    make_session: Callable[..., BrokerSession],
    shipped_settings: ObliskConfig,
    rig_calendars: Calendars,
    tmp_path: Path,
) -> Any:
    """The order path over the fake broker (``tests/support``), in paper mode, with the pre-trade
    gate over the test instruments (``order_rig.ALLOWED`` eligible)."""
    from fake_broker import FakeBroker
    from order_rig import build_rig

    with respx.mock(assert_all_called=False) as router:
        broker = FakeBroker(router)
        rig = await build_rig(
            settings=shipped_settings,
            session=make_session(),
            broker=broker,
            tmp_path=tmp_path,
            calendars=rig_calendars,
        )
        await rig.to_paper()
        yield rig
        await rig.aclose()
