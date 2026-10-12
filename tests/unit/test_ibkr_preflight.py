"""``obliskiq preflight --read-only``: every step reported, the walk stops at the first failure,
the account id masked and no balance printed."""

import io
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from obliskiq.cli.app import EXIT_NO_CREDENTIALS, run
from obliskiq.env import Environment
from obliskiq.ibkr.preflight import (
    READ_STEPS,
    STEP_GUARD,
    STEP_SNAPSHOT,
    PreflightReport,
    run_read_only,
)

BASE = "https://gw.test/v1/api"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ibkr"
ACCOUNT = "DU_PAPER"
SPY = 756733
STK = [{"secType": "STK"}]


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _restore_root_logging():
    yield
    logging.getLogger().handlers.clear()


@pytest.fixture
def shipped_settings(shipped_settings):
    """The shipped settings with no wait between snapshot polls: the walk uses the real clock."""
    marketdata = shipped_settings.ibkr.marketdata.model_copy(
        update={"snapshot_settle_delay_seconds": 0.0}
    )
    ibkr = shipped_settings.ibkr.model_copy(update={"marketdata": marketdata})
    return shipped_settings.model_copy(update={"ibkr": ibkr})


@pytest.fixture
def env(monkeypatch) -> Environment:
    for name, value in {
        "OBLISK_IBKR_PROFILE": "paper",
        "OBLISK_IBKR_ACCOUNT_ID": ACCOUNT,
        "OBLISK_IBKR_TRANSPORT": "gateway",
        "OBLISK_IBKR_GATEWAY_USERNAME": "paper-user",
        "OBLISK_IBKR_GATEWAY_PASSWORD": "paper-pass",
    }.items():
        monkeypatch.setenv(name, value)
    return Environment(_env_file=None)


def _mock_read_path(router: respx.MockRouter, *, accounts: Any = None) -> None:
    router.post(f"{BASE}/iserver/auth/ssodh/init").mock(
        return_value=httpx.Response(200, json={"authenticated": True, "competing": False})
    )
    router.get(f"{BASE}/iserver/accounts").mock(
        return_value=httpx.Response(200, json={"accounts": [ACCOUNT]})
    )
    router.get(f"{BASE}/portfolio/accounts").mock(
        return_value=httpx.Response(
            200, json=accounts if accounts is not None else _fixture("accounts.json")
        )
    )
    router.get(f"{BASE}/portfolio/{ACCOUNT}/positions/0").mock(
        return_value=httpx.Response(200, json=_fixture("positions_page0.json"))
    )
    router.get(f"{BASE}/portfolio/{ACCOUNT}/positions/1").mock(
        return_value=httpx.Response(200, json=[])
    )
    router.get(f"{BASE}/portfolio/{ACCOUNT}/ledger").mock(
        return_value=httpx.Response(200, json=_fixture("ledger.json"))
    )
    router.post(f"{BASE}/iserver/secdef/search").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"conid": "999", "symbol": "SPY", "description": "MEXI", "sections": STK},
                {"conid": str(SPY), "symbol": "SPY", "description": "ARCA", "sections": STK},
            ],
        )
    )
    router.get(f"{BASE}/iserver/contract/{SPY}/info").mock(
        return_value=httpx.Response(
            200,
            json={
                "con_id": SPY,
                "symbol": "SPY",
                "currency": "USD",
                "valid_exchanges": "SMART,ARCA",
            },
        )
    )
    router.get(f"{BASE}/iserver/marketdata/snapshot").mock(
        return_value=httpx.Response(
            200,
            json=[{"conid": SPY, "31": "C671.20", "84": "671.19", "86": "671.21", "6509": "DPB"}],
        )
    )
    router.get(f"{BASE}/iserver/marketdata/history").mock(
        return_value=httpx.Response(
            200, json={"data": [{"t": 1791552600000, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 9}]}
        )
    )
    router.post(f"{BASE}/iserver/contract/rules").mock(
        return_value=httpx.Response(200, json=_fixture("rules.json"))
    )
    router.get(f"{BASE}/iserver/contract/{SPY}/algos").mock(
        return_value=httpx.Response(200, json=_fixture("algos.json"))
    )
    router.get(f"{BASE}/contract/trading-schedule").mock(
        return_value=httpx.Response(200, json=_fixture("trading_schedule.json"))
    )


@respx.mock
async def test_every_step_passes_on_a_healthy_read_path(env, shipped_settings, fake_transport):
    _mock_read_path(respx.mock)
    report = await run_read_only(env, shipped_settings, data_dir=None, transport=fake_transport)
    assert report.ok, report.render()
    assert [step.name for step in report.steps] == list(READ_STEPS)
    rendered = report.render((ACCOUNT,))
    assert ACCOUNT not in rendered, "the account id is masked"
    assert "11170" not in rendered, "no balance is printed"
    assert "10512" not in rendered, "no balance is printed"
    snapshot = next(step for step in report.steps if step.name == STEP_SNAPSHOT)
    assert "availability delayed" in snapshot.detail
    assert "documented_bound" in snapshot.detail
    assert "last is prior_close" in snapshot.detail
    assert fake_transport.closed == 1, "the transport is released"
    for path in ("iserver/account/orders", "iserver/reply", "iserver/order"):
        assert not any(path in str(call.request.url) for call in respx.mock.calls)


@respx.mock
async def test_an_account_not_listed_stops_at_the_guard(env, shipped_settings, fake_transport):
    _mock_read_path(respx.mock, accounts=[{"id": "DU_OTHER"}])
    report = await run_read_only(env, shipped_settings, data_dir=None, transport=fake_transport)
    assert not report.ok
    failed = report.failed_step
    assert failed is not None
    assert failed.name == STEP_GUARD
    assert ACCOUNT not in report.render((ACCOUNT,))
    assert not any("positions" in str(call.request.url) for call in respx.mock.calls)


async def test_a_live_account_under_paper_is_refused_before_any_call(
    monkeypatch, shipped_settings, fake_transport
):
    monkeypatch.setenv("OBLISK_IBKR_PROFILE", "paper")
    monkeypatch.setenv("OBLISK_IBKR_ACCOUNT_ID", "U_LIVE")
    monkeypatch.setenv("OBLISK_IBKR_TRANSPORT", "gateway")
    report = await run_read_only(
        Environment(_env_file=None), shipped_settings, data_dir=None, transport=fake_transport
    )
    assert report.failed_step is not None
    assert report.failed_step.name == STEP_GUARD
    assert fake_transport.prepared == 0


async def test_the_gateway_needs_a_data_directory(env, shipped_settings):
    report = await run_read_only(env, shipped_settings, data_dir=None)
    failed = report.failed_step
    assert failed is not None
    assert "OBLISK_DATA_DIR" in failed.detail


def test_an_empty_report_is_not_a_pass():
    assert not PreflightReport().ok


def _run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    status = run(list(argv), env_file=None, stdout=out, stderr=err)
    return status, out.getvalue(), err.getvalue()


def test_no_credentials_prints_so_and_exits_2(config_dir):
    status, out, _ = _run("--config-dir", str(config_dir), "preflight", "--read-only")
    assert status == EXIT_NO_CREDENTIALS == 2
    assert out.startswith("no credentials")
    assert "OBLISK_IBKR_TRANSPORT" in out


def test_the_preflight_requires_the_read_only_flag(config_dir, capsys):
    with pytest.raises(SystemExit):
        _run("--config-dir", str(config_dir), "preflight")


def test_a_transport_without_its_variables_counts_as_no_credentials(config_dir, monkeypatch):
    monkeypatch.setenv("OBLISK_IBKR_PROFILE", "paper")
    monkeypatch.setenv("OBLISK_IBKR_ACCOUNT_ID", ACCOUNT)
    monkeypatch.setenv("OBLISK_IBKR_TRANSPORT", "oauth")
    status, out, _ = _run("--config-dir", str(config_dir), "preflight", "--read-only")
    assert status == EXIT_NO_CREDENTIALS
    assert "OBLISK_IBKR_CONSUMER_KEY" in out


async def test_the_oauth_handshake_runs_first_and_stops_the_walk(monkeypatch, shipped_settings):
    monkeypatch.setenv("OBLISK_IBKR_PROFILE", "paper")
    monkeypatch.setenv("OBLISK_IBKR_ACCOUNT_ID", ACCOUNT)
    monkeypatch.setenv("OBLISK_IBKR_TRANSPORT", "oauth")
    report = await run_read_only(Environment(_env_file=None), shipped_settings, data_dir=None)
    assert report.failed_step is not None
    assert report.failed_step.name == "oauth-credentials"
    rendered = report.render((ACCOUNT,))
    assert "contract-identity (not reached)" in rendered


async def test_an_unset_profile_stops_at_the_transport(
    monkeypatch, shipped_settings, fake_transport
):
    monkeypatch.setenv("OBLISK_IBKR_ACCOUNT_ID", ACCOUNT)
    report = await run_read_only(
        Environment(_env_file=None), shipped_settings, data_dir=None, transport=fake_transport
    )
    assert report.failed_step is not None
    assert "OBLISK_IBKR_PROFILE" in report.failed_step.detail


@respx.mock
async def test_an_unauthenticated_brokerage_session_stops_the_walk(
    env, shipped_settings, fake_transport
):
    _mock_read_path(respx.mock)
    respx.post(f"{BASE}/iserver/auth/ssodh/init").mock(
        return_value=httpx.Response(200, json={"authenticated": False, "competing": True})
    )
    report = await run_read_only(env, shipped_settings, data_dir=None, transport=fake_transport)
    assert not report.ok
    assert report.failed_step is not None
    assert "authenticated" in report.failed_step.detail
    assert "Stopped at:" in report.render()


@respx.mock
async def test_a_snapshot_without_a_row_for_the_contract_fails_its_step(
    env, shipped_settings, fake_transport
):
    _mock_read_path(respx.mock)
    respx.get(f"{BASE}/iserver/marketdata/snapshot").mock(return_value=httpx.Response(200, json=[]))
    report = await run_read_only(env, shipped_settings, data_dir=None, transport=fake_transport)
    assert report.failed_step is not None
    assert report.failed_step.name == STEP_SNAPSHOT
    assert "no row" in report.failed_step.detail


@respx.mock
async def test_history_without_bars_is_reported_not_failed(env, shipped_settings, fake_transport):
    _mock_read_path(respx.mock)
    respx.get(f"{BASE}/iserver/marketdata/history").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    report = await run_read_only(env, shipped_settings, data_dir=None, transport=fake_transport)
    assert report.ok, report.render()
    assert any(step.detail == "no bars returned" for step in report.steps)
