"""Broker capabilities: what the configuration may claim, the verdicts the selector asks for, the
permissions an accepted preview shows, and the probe (whatif only) on the fake broker (Session 9).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from typing import Any

import pytest
from order_rig import EUR_ETF, whatif_answer
from pydantic import ValidationError
from vehicle_rig import unverified

from obliskiq.cli.vehicles import capability_lines
from obliskiq.config import ObliskConfig
from obliskiq.config.schema_vehicles import (
    BrokerCapabilitiesConfig,
    CapabilityCheck,
    CapabilityReading,
    ListReading,
)
from obliskiq.decision.capabilities import REQUIRED_CHECKS, BrokerCapabilities
from obliskiq.domain.enums import AssetClass
from obliskiq.execution.capability_probe import CapabilityProbe
from obliskiq.ibkr.models import ContractInfo, ContractMatch, ContractSection, OptionStrikes

DAY = date(2026, 10, 22)


# ── what the configuration may claim ──


def test_a_check_is_verified_only_on_the_client_portal_route() -> None:
    with pytest.raises(ValidationError, match="Client Portal route"):
        CapabilityReading(status="verified", route="MCP", observed_on=DAY, evidence="connector")
    with pytest.raises(ValidationError, match="Client Portal route"):
        CapabilityReading(status="refused", route="CP", observed_on=None, evidence="x")
    with pytest.raises(ValidationError, match="route and its date together"):
        CapabilityReading(status="unverified", route="MCP", observed_on=None, evidence="x")
    evidence = CapabilityReading(status="unverified", route="MCP", observed_on=DAY, evidence="x")
    assert not evidence.verified


def test_a_verified_list_names_what_it_listed() -> None:
    with pytest.raises(ValidationError, match="names what it listed"):
        ListReading(status="verified", route="CP", observed_on=DAY, evidence="x", values=[])
    with pytest.raises(ValidationError, match="listed twice"):
        ListReading(
            status="verified", route="CP", observed_on=DAY, evidence="x", values=["LMT", "LMT"]
        )


def test_a_check_is_listed_once(shipped_settings: ObliskConfig) -> None:
    check = shipped_settings.broker_capabilities.checks[0]
    with pytest.raises(ValidationError, match="lists twice"):
        BrokerCapabilitiesConfig(
            checks=[check, check], probe=shipped_settings.broker_capabilities.probe
        )


def test_a_probe_is_an_instrument_of_its_class_on_its_venue(
    shipped_settings: ObliskConfig,
) -> None:
    def config_with(check: CapabilityCheck) -> dict[str, Any]:
        data = shipped_settings.model_dump(mode="json")
        data["broker_capabilities"]["checks"] = [check.model_dump(mode="json")]
        return data

    base = shipped_settings.broker_capabilities.checks[0]
    wrong_venue = base.model_copy(update={"asset_class": AssetClass.ETF, "venue": "XLON"})
    for check, message in (
        (base.model_copy(update={"probe": "NOWHERE"}), "not a universe instrument"),
        (
            wrong_venue.model_copy(update={"probe": "XDWD"}),
            "is not an instrument of class etf on XLON",
        ),
        (base.model_copy(update={"venue": "XXXX"}), "not a venue declared"),
        (
            base.model_copy(update={"asset_class": AssetClass.OPTION, "probe": "XDWD"}),
            "is not an option chain on XNYS",
        ),
    ):
        with pytest.raises(ValidationError, match=message):
            ObliskConfig.model_validate(config_with(check))


# ── verdicts ──


def test_a_verdict_names_what_is_missing(shipped_settings: ObliskConfig) -> None:
    capabilities = BrokerCapabilities(shipped_settings)
    xetra = capabilities.assess(AssetClass.ETF, "XETR")
    assert xetra.verified
    assert xetra.missing == ()
    assert "search 2026-10-21" in xetra.reason
    amsterdam = capabilities.assess(AssetClass.STOCK, "XAMS")
    assert not amsterdam.verified
    assert amsterdam.missing == ("order",)
    absent = capabilities.assess(AssetClass.BOND, "XETR")
    assert absent.missing == REQUIRED_CHECKS
    assert "nothing is known of bond on XETR" in absent.reason


def test_a_permission_is_held_where_a_preview_was_accepted(
    shipped_settings: ObliskConfig,
) -> None:
    permissions = {p.venue: p for p in BrokerCapabilities(shipped_settings).permissions()}
    assert permissions["XETR"].held
    assert "etf preview accepted 2026-10-21" in permissions["XETR"].evidence
    assert permissions["XETR"].permission == "Stocks: Germany"
    assert not permissions["XLON"].held
    assert permissions["XLON"].evidence == "no accepted preview yet"
    assert set(permissions) == set(shipped_settings.venues)


def test_the_capability_lines_show_every_check(shipped_settings: ObliskConfig) -> None:
    lines = "\n".join(capability_lines(shipped_settings))
    assert "etf on XETR (proposable, probe XDWD)" in lines
    assert "stock on XASX (proposable, no probe instrument)" in lines
    assert "search unverified MCP 2026-10-22" in lines
    assert "any other asset class and venue: unverified" in lines
    assert "XETR Stocks: Germany: held" in lines


# ── the probe (whatif only) ──


def probe_settings(settings: ObliskConfig, *checks: CapabilityCheck) -> ObliskConfig:
    base = settings.broker_capabilities
    return settings.model_copy(
        update={"broker_capabilities": base.model_copy(update={"checks": list(checks)})}
    )


def check(kind: AssetClass, venue: str, probe: str | None) -> CapabilityCheck:
    lists = ListReading(status="unverified", route=None, observed_on=None, evidence="x", values=[])
    return CapabilityCheck(
        asset_class=kind,
        venue=venue,
        probe=probe,
        search=unverified(),
        quote=unverified(),
        order=unverified(),
        order_types=lists,
        algos=lists,
    )


@pytest.fixture
def rig(order_rig: Any) -> Any:
    order_rig.ready()
    order_rig.broker.contracts = {EUR_ETF: ("EXV1", "IBIS", "EUR")}
    order_rig.broker.whatif_for = lambda _order: whatif_answer("96.00", "EUR")
    return order_rig


def steps(report: Any) -> dict[str, Any]:
    return {step.name: step for step in report.steps}


async def test_the_probe_reads_each_capability_and_only_previews(rig: Any) -> None:
    settings = probe_settings(
        rig.settings,
        check(AssetClass.ETF, "XETR", "EXV1"),
        check(AssetClass.STOCK, "XASX", None),
    )
    report = await CapabilityProbe(settings, rig.ibkr, rig.client, day=DAY).run(
        settings.broker_capabilities.checks
    )
    found = steps(report)
    assert found["search"].ok
    assert "resolved to 89005163" in found["search"].detail
    assert found["quote"].ok
    assert "permission field 7768" in found["quote"].detail
    assert found["order types"].ok
    assert found["algos"].values == ("Adaptive",)
    assert found["order"].ok
    assert "previewed: commission" in found["order"].detail
    assert rig.broker.orders == {}  # nothing was placed
    assert report.skipped == ["stock@XASX: no pinned universe instrument to probe with"]
    rendered = report.render()
    assert "whatif only, nothing placed" in rendered
    assert "observed_on 2026-10-22" in rendered


async def test_a_contract_the_search_resolves_elsewhere_stops_its_check(rig: Any) -> None:
    rig.broker.contracts = {EUR_ETF + 1: ("EXV1", "IBIS", "EUR")}
    settings = probe_settings(rig.settings, check(AssetClass.ETF, "XETR", "EXV1"))
    report = await CapabilityProbe(settings, rig.ibkr, rig.client, day=DAY).run(
        settings.broker_capabilities.checks
    )
    found = steps(report)
    assert not found["search"].ok
    assert "the universe pins 89005163" in found["search"].detail
    assert "quote" not in found


async def test_without_a_bid_nothing_is_previewed(rig: Any) -> None:
    rig.broker.quote(EUR_ETF, bid="", ask="", last="")
    settings = probe_settings(rig.settings, check(AssetClass.ETF, "XETR", "EXV1"))
    report = await CapabilityProbe(settings, rig.ibkr, rig.client, day=DAY).run(
        settings.broker_capabilities.checks
    )
    found = steps(report)
    assert not found["quote"].ok
    assert not found["order"].ok
    assert "no previewable limit" in found["order"].detail


async def test_a_refused_preview_is_reported(rig: Any) -> None:
    rig.broker.whatif_for = lambda _order: {"error": "no trading permission"}
    settings = probe_settings(rig.settings, check(AssetClass.ETF, "XETR", "EXV1"))
    report = await CapabilityProbe(settings, rig.ibkr, rig.client, day=DAY).run(
        settings.broker_capabilities.checks
    )
    order = steps(report)["order"]
    assert not order.ok
    assert "no trading permission" in order.detail


async def test_an_error_stops_one_check_only(rig: Any) -> None:
    rig.broker.contracts = {}
    settings = probe_settings(
        rig.settings, check(AssetClass.ETF, "XETR", "EXV1"), check(AssetClass.ETF, "XLON", "IWDA")
    )
    report = await CapabilityProbe(settings, rig.ibkr, rig.client, day=DAY).run(
        settings.broker_capabilities.checks
    )
    stopped = [step for step in report.steps if step.name == "stopped"]
    assert [step.check for step in stopped] == ["etf@XETR", "etf@XLON"]


class OptionSecdef:
    """The secdef calls the option probe makes, answered for one underlying."""

    def __init__(
        self,
        inner: Any,
        conid: int,
        *,
        months: tuple[str, ...],
        strikes: Any,
        listed: bool = True,
    ) -> None:
        self._inner = inner
        self._conid = conid
        self._months = months
        self._strikes = strikes
        self._listed = listed

    async def search(self, symbol: str, *, sec_type: str | None = None, name: bool = False) -> Any:
        if sec_type == "OPT":
            section = ContractSection(sec_type="OPT", months=self._months, exchanges=("EUREX",))
            return [
                ContractMatch(
                    conid=self._conid, symbol=symbol, company_name=None, description="BM",
                    sections=(section,), issuers=(),
                )
            ]  # fmt: skip
        return await self._inner.search(symbol, sec_type=sec_type, name=name)

    def pick(self, *args: Any, **kwargs: Any) -> Any:
        return self._inner.pick(*args, **kwargs)

    async def info(self, conid: int) -> Any:
        return await self._inner.info(conid)

    async def strikes(self, conid: int, *, month: str, exchange: str) -> Any:
        return self._strikes

    async def option_contracts(self, conid: int, **kw: Any) -> list[ContractInfo]:
        if not self._months or not self._listed:
            return []
        return [
            ContractInfo(
                conid=EUR_ETF, symbol="SAN", sec_type="OPT", exchange=kw["exchange"],
                listing_exchange=None, valid_exchanges=(), currency="EUR", company_name=None,
                trading_class=None, local_symbol=None, multiplier=100.0, maturity_date="20261218",
                underlying_conid=self._conid,
            )
        ]  # fmt: skip


def option_rig(rig: Any, monkeypatch: pytest.MonkeyPatch, **kw: Any) -> ObliskConfig:
    san = rig.settings.universe.instrument("SAN")
    rig.broker.contracts = {san.conid: ("SAN", "BM", "EUR")}
    rig.broker.quote(san.conid, bid="5", ask="5.2", last="5.1")
    secdef = OptionSecdef(rig.ibkr.secdef, san.conid, **kw)
    monkeypatch.setattr(type(rig.ibkr), "secdef", property(lambda _self: secdef))
    return probe_settings(rig.settings, check(AssetClass.OPTION, "XEUR", "SAN"))


async def test_an_option_check_previews_one_call_of_the_underlying(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    strikes = OptionStrikes(call=(4.5, 5.0, 5.5), put=(4.5, 5.0, 5.5))
    settings = option_rig(rig, monkeypatch, months=("DEC26",), strikes=strikes)
    report = await CapabilityProbe(settings, rig.ibkr, rig.client, day=DAY).run(
        settings.broker_capabilities.checks
    )
    found = steps(report)
    assert found["option chain"].ok
    assert "DEC26 5.0 call on EUREX" in found["option chain"].detail
    assert found["order"].ok


async def test_an_option_without_months_or_strikes_is_reported(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = option_rig(rig, monkeypatch, months=(), strikes=OptionStrikes(call=(), put=()))
    report = await CapabilityProbe(settings, rig.ibkr, rig.client, day=DAY).run(
        settings.broker_capabilities.checks
    )
    chain = steps(report)["option chain"]
    assert not chain.ok
    assert "no option months" in chain.detail
    settings = option_rig(
        rig, monkeypatch, months=("DEC26",), strikes=OptionStrikes(call=(), put=())
    )
    report = await CapabilityProbe(settings, rig.ibkr, rig.client, day=DAY).run(
        settings.broker_capabilities.checks
    )
    assert "no strike near" in steps(report)["option chain"].detail
    settings = option_rig(rig, monkeypatch, months=("DEC26",), strikes=strikes_near(), listed=False)
    report = await CapabilityProbe(settings, rig.ibkr, rig.client, day=DAY).run(
        settings.broker_capabilities.checks
    )
    assert "no DEC26 5.0 call on EUREX" in steps(report)["option chain"].detail


def strikes_near() -> OptionStrikes:
    return OptionStrikes(call=(4.5, 5.0, 5.5), put=(4.5, 5.0, 5.5))
