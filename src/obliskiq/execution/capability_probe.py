"""``obliskiq universe capabilities --probe``: what the Web API does, per asset class and venue.

For each check of ``broker_capabilities`` that names a probe instrument it reads, in order: the
**search** (IBKR's contract search resolves the declared listing to the pinned conid, confirmed by
the contract's details), the **quote** (a snapshot with its availability flag and the account's
trading-permission field), the **contract rules** (order types, cash-quantity types, whether algos
apply, the size increment and ticks), the **algos** offered, and an **order preview**: a limit buy
of one size increment below the bid, sent to ``/whatif``. For an option check it first resolves one
contract of the underlying: the nearest option month on the declared venue, at the strike nearest
the underlying's price.

Nothing is placed: the preview is the only order endpoint touched, under the paper guard (profile
``paper`` previews only for a paper account). A step that fails is reported and the check's later
steps still run where they can; the report prints the readings to record in
``config/defaults/capabilities.yaml`` and ``docs/knowledge/ibkr-capabilities.md``, never an account
id or a balance.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from ..config.schema import ObliskConfig
from ..config.schema_universe import UniverseInstrument
from ..config.schema_vehicles import CapabilityCheck
from ..data.market.strikes import nearest_strike
from ..data.market.universe import SEARCH_SEC_TYPE
from ..domain.enums import AssetClass, MarketDataStatus
from ..domain.orders import LimitPrice, OrderSide, OrderTerms, TimeInForce
from ..errors import ObliskError
from ..ibkr.client import IbkrClient
from ..ibkr.identity import resolve_identity
from ..ibkr.models import ContractRules
from ..ibkr.orders import SMART_ROUTER, OrderClient
from ..ibkr.redact import redact
from ..util.numeric import safe_float
from .paper_test import passive_limit
from .pricing import tick_for

#: The field names the probe's snapshot reads (``ibkr.marketdata.snapshot_fields``).
QUOTE_FIELDS = ("bid", "ask", "last", "market_data_status")
OPTION_SEC_TYPE = "OPT"
CALL = "C"


@dataclass
class ProbeStep:
    check: str
    name: str
    ok: bool
    detail: str
    values: tuple[str, ...] = ()

    def line(self) -> str:
        mark = "ok  " if self.ok else "FAIL"
        listed = f" [{', '.join(self.values)}]" if self.values else ""
        return f"  [{mark}] {self.check} {self.name}: {self.detail}{listed}"


@dataclass
class ProbeReport:
    day: date
    steps: list[ProbeStep] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def add(self, check: str, name: str, ok: bool, detail: str, values: Sequence[str] = ()) -> bool:
        self.steps.append(ProbeStep(check, name, ok, detail, tuple(values)))
        return ok

    def render(self, known_ids: tuple[str, ...] = ()) -> str:
        lines = [f"IBKR capability probe (whatif only, nothing placed), {self.day}:", ""]
        lines += [redact(step.line(), known_ids) for step in self.steps]
        lines += [f"  [ -  ] {why}" for why in self.skipped]
        lines += [
            "",
            "Record each reading in config/defaults/capabilities.yaml (status verified or "
            f"refused, route CP, observed_on {self.day}) and docs/knowledge/ibkr-capabilities.md.",
        ]
        return "\n".join(lines)


def _key(check: CapabilityCheck) -> str:
    return f"{check.asset_class.value}@{check.venue}"


class CapabilityProbe:
    """Runs the probe over a started read client and the order client (previews only)."""

    def __init__(
        self, settings: ObliskConfig, client: IbkrClient, orders: OrderClient, *, day: date
    ) -> None:
        self._settings = settings
        self._client = client
        self._orders = orders
        self._day = day
        self._config = settings.broker_capabilities.probe
        fields = client.marketdata.fields(underlying=False)
        self._fields = {name: fields[name] for name in QUOTE_FIELDS if name in fields}

    async def run(self, checks: Sequence[CapabilityCheck]) -> ProbeReport:
        report = ProbeReport(self._day)
        for check in checks:
            declared = (
                None if check.probe is None else self._settings.universe.instrument(check.probe)
            )
            if declared is None or declared.conid is None:
                report.skipped.append(f"{_key(check)}: no pinned universe instrument to probe with")
                continue
            try:
                await self._probe(check, declared, report)
            except ObliskError as exc:
                report.add(_key(check), "stopped", False, str(exc))
        return report

    async def _probe(
        self, check: CapabilityCheck, declared: UniverseInstrument, report: ProbeReport
    ) -> None:
        key = _key(check)
        identity = await resolve_identity(
            self._client.secdef,
            declared.symbol,
            listing_exchange=declared.listing_exchange,
            currency=declared.currency,
            sec_type=SEARCH_SEC_TYPE[declared.asset_class],
        )
        found = identity.conid == declared.conid
        report.add(
            key,
            "search",
            found,
            f"{declared.symbol} on {declared.listing_exchange} resolved to {identity.conid}"
            + ("" if found else f", the universe pins {declared.conid}"),
        )
        if not found:
            return
        conid = identity.conid
        if check.asset_class is AssetClass.OPTION:
            option = await self._option(declared, conid, report, key)
            if option is None:
                return
            conid = option
        bid = await self._quote(conid, report, key)
        rules = await self._client.contracts.rules(conid, is_buy=True)
        report.add(
            key,
            "order types",
            rules.error is None and bool(rules.order_types),
            f"size increment {rules.size_increment}, algos apply {rules.algo_eligible}, "
            f"cash quantity {', '.join(rules.cash_quantity_order_types) or 'none'}"
            + (f"; error {rules.error}" if rules.error else ""),
            rules.order_types,
        )
        algos = await self._client.contracts.algos(conid)
        report.add(
            key, "algos", True, f"{len(algos)} offered", tuple(algo.algo_id for algo in algos)
        )
        await self._preview(conid, bid, rules, report, key)

    async def _quote(self, conid: int, report: ProbeReport, key: str) -> Decimal | None:
        permission = self._config.permission_field_id
        ids = [*self._fields.values(), permission]
        row = (await self._client.marketdata.raw_snapshot([conid], ids)).get(conid, {})
        bid = safe_float(row.get(self._fields.get("bid", "")), None)
        last = safe_float(row.get(self._fields.get("last", "")), None)
        flag = row.get(self._fields.get("market_data_status", ""))
        status = MarketDataStatus.from_route_flag(None if flag is None else str(flag))
        priced = bid is not None or last is not None
        report.add(
            key,
            "quote",
            priced,
            f"bid {bid}, last {last}, availability flag {flag} "
            f"({status.value if status else 'unread'}); trading permission field "
            f"{permission} = {row.get(permission)}",
        )
        return None if bid is None or bid <= 0 else Decimal(repr(bid))

    async def _preview(
        self,
        conid: int,
        bid: Decimal | None,
        rules: ContractRules,
        report: ProbeReport,
        key: str,
    ) -> None:
        size = rules.size_increment
        tick = None if bid is None else tick_for(rules, bid)
        limit = (
            None
            if bid is None or tick is None
            else passive_limit(bid, self._config.limit_offset_fraction, tick)
        )
        if limit is None or size is None or size <= 0:
            report.add(
                key, "order", False, f"no previewable limit (bid {bid}, tick {tick}, size {size})"
            )
            return
        terms = OrderTerms(
            conid=conid,
            side=OrderSide.BUY,
            quantity=Decimal(repr(size)),
            price=LimitPrice(limit=limit),
            tif=TimeInForce.DAY,
            venue=SMART_ROUTER,
        )
        preview = await self._orders.whatif(terms, coid=self._orders.coid_for(uuid.uuid7()))
        report.add(
            key,
            "order",
            preview.error is None,
            f"limit buy of {size} at {limit} previewed: commission {preview.commission} "
            f"{preview.currency or ''}, warning {preview.warning_code or 'none'}"
            + (f" ({preview.warning})" if preview.warning else "")
            + (f"; error {preview.error}" if preview.error else ""),
        )

    async def _option(
        self, underlying: UniverseInstrument, conid: int, report: ProbeReport, key: str
    ) -> int | None:
        """One call of the underlying's nearest option month at the strike nearest its price."""
        secdef = self._client.secdef
        matches = await secdef.search(underlying.symbol, sec_type=OPTION_SEC_TYPE)
        section = next((m.section(OPTION_SEC_TYPE) for m in matches if m.conid == conid), None)
        if section is None or not section.months:
            report.add(key, "option chain", False, f"no option months on conid {conid}")
            return None
        venue = underlying.option_exchange or (
            underlying.listing_exchange
            if underlying.listing_exchange in section.exchanges
            else section.exchanges[0]
        )
        quote = (await self._client.marketdata.snapshot([conid], underlying=True)).get(conid)
        spot = None if quote is None else (quote.last or quote.mid)
        month = section.months[0]
        strikes = await secdef.strikes(conid, month=month, exchange=venue)
        strike = None if spot is None else nearest_strike(strikes.call, spot)
        if strike is None:
            report.add(key, "option chain", False, f"no strike near {spot} in {month} on {venue}")
            return None
        contracts = await secdef.option_contracts(
            conid, month=month, exchange=venue, strike=strike, right=CALL
        )
        if not contracts:
            report.add(key, "option chain", False, f"no {month} {strike} call on {venue}")
            return None
        contract = contracts[0]
        report.add(
            key,
            "option chain",
            True,
            f"{month} {strike} call on {venue}: conid {contract.conid}, multiplier "
            f"{contract.multiplier}, maturity {contract.maturity_date}",
        )
        return contract.conid
