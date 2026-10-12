"""The typed IBKR read client, by endpoint group.

:class:`IbkrClient` opens the brokerage session, runs the account guard before any other read,
and exposes the groups:

* ``portfolio`` — accounts, paginated positions, the ledger (NAV in base currency only, cash per
  currency);
* ``marketdata`` — snapshots with the settle loop, decoded with declared units and plausibility
  guards, availability as realtime | delayed | frozen | none plus delay;
* ``history`` — :class:`~obliskiq.ibkr.history.HistoryReader`;
* ``secdef`` — search (JSON body, exact ticker, declared-venue tie-break), contract info,
  strikes, option contracts, futures ladder (data only), bond issuers and filters, funds;
* ``contracts`` — trading rules, algos and the trading schedule;
* ``scanner`` — parameters and runs.

Every method is a read; nothing here places, modifies or confirms an order.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..config.schema import ObliskConfig
from ..config.schema_ibkr import IbkrConfig
from ..logging_setup import get_logger
from ..util.numeric import safe_int
from .decode import (
    QUOTE_FIELD_NAMES,
    decode_accounts,
    decode_algos,
    decode_bond_filters,
    decode_contract_info,
    decode_futures,
    decode_ledger,
    decode_positions,
    decode_quote,
    decode_rules,
    decode_scanner_params,
    decode_scanner_run,
    decode_search,
    decode_strikes,
    decode_trading_schedule,
    field_map,
)
from .errors import AccountGuardError, DecodeError
from .guard import AccountGuard
from .history import HistoryReader
from .models import (
    Account,
    Algo,
    BondFilter,
    CashBalance,
    ContractInfo,
    ContractMatch,
    ContractRules,
    FuturesRung,
    Ledger,
    NavReading,
    OptionStrikes,
    Position,
    Quote,
    ScannerContract,
    ScannerParams,
    TradingSchedule,
)
from .session import AuthStatus, BrokerSession

log = get_logger("ibkr.client")

SNAPSHOT_PATH = "iserver/marketdata/snapshot"
SEARCH_PATH = "iserver/secdef/search"
STRIKES_PATH = "iserver/secdef/strikes"
SECDEF_INFO_PATH = "iserver/secdef/info"
BOND_FILTERS_PATH = "iserver/secdef/bond-filters"
FUTURES_PATH = "trsrv/futures"
RULES_PATH = "iserver/contract/rules"
SCHEDULE_PATH = "contract/trading-schedule"
SCANNER_PARAMS_PATH = "iserver/scanner/params"
SCANNER_RUN_PATH = "iserver/scanner/run"
ACCOUNTS_PATH = "portfolio/accounts"

#: The bond search IBKR documents: ``symbol`` is always ``BOND`` for the filter endpoint.
BOND_SYMBOL = "BOND"


class Portfolio:
    """``/portfolio``: accounts, positions and the ledger of the guarded account."""

    def __init__(self, session: BrokerSession, ibkr: IbkrConfig, account_id: str, base: str):
        self._session = session
        self._ibkr = ibkr
        self._account_id = account_id
        self._base_currency = base
        self._accounts: list[Account] | None = None

    async def accounts(self, *, refresh: bool = False) -> list[Account]:
        """``/portfolio/accounts``, cached: IBKR allows one call per five seconds."""
        if self._accounts is None or refresh:
            self._accounts = decode_accounts(await self._session.request("GET", ACCOUNTS_PATH))
            self._session.add_known_ids(account.account_id for account in self._accounts)
        return self._accounts

    async def positions_page(self, page: int) -> list[Position]:
        body = await self._session.request(
            "GET",
            f"portfolio/{self._account_id}/positions/{int(page)}",
            label="portfolio/{account}/positions/{page}",
        )
        return decode_positions(body)

    async def positions(self) -> list[Position]:
        """Every page of positions; the pager stops on an empty page.

        ``portfolio.max_position_pages`` is a runaway guard, not a limit on the portfolio: a
        pager that reaches it without an empty page raises rather than return a short list.
        """
        out: list[Position] = []
        pages = self._ibkr.portfolio.max_position_pages
        for page in range(pages):
            rows = await self.positions_page(page)
            if not rows:
                return out
            out.extend(rows)
        raise DecodeError(
            f"positions still arriving after {pages} pages: the positions read is incomplete"
        )

    async def ledger(self) -> Ledger:
        body = await self._session.request(
            "GET", f"portfolio/{self._account_id}/ledger", label="portfolio/{account}/ledger"
        )
        return decode_ledger(body, base_currency=self._base_currency)

    async def nav(self) -> NavReading | None:
        return (await self.ledger()).nav

    async def cash_balances(self) -> tuple[CashBalance, ...]:
        """Cash per currency; raises when any currency's balance could not be read."""
        ledger = await self.ledger()
        if ledger.unreadable_currencies:
            raise DecodeError(
                "the ledger carries no readable cash balance for "
                f"{', '.join(ledger.unreadable_currencies)}: the cash read is incomplete"
            )
        return ledger.balances


class MarketData:
    """``/iserver/marketdata/snapshot`` with priming, settling and decoding."""

    def __init__(self, session: BrokerSession, ibkr: IbkrConfig) -> None:
        self._session = session
        self._ibkr = ibkr
        self._fields = field_map(ibkr.marketdata.snapshot_fields)
        self._underlying_fields = field_map(ibkr.marketdata.underlying_snapshot_fields)

    def fields(self, *, underlying: bool) -> dict[str, str]:
        return dict(self._underlying_fields if underlying else self._fields)

    async def raw_snapshot(
        self, conids: Sequence[int], field_ids: Sequence[str]
    ) -> dict[int, dict[str, Any]]:
        """Snapshot rows keyed by conid, merged across polls until a price arrives.

        The first call for an unsubscribed conid answers a skeleton; the status and Greeks arrive
        before any price [CP ✓ 2026-09-15], so a row is settled only when one of the PRICE fields
        it was asked for arrives (or, asking for no price, on the first answer). Rows are merged,
        never replaced, so a later skeleton cannot clobber a row that already quoted. With more
        than one batch every batch is primed before any is polled, so warm-ups overlap.
        """
        if not conids:
            return {}
        cap = self._ibkr.marketdata.max_fields_per_snapshot
        if len(field_ids) > cap:
            raise ValueError(f"{len(field_ids)} fields asked for; IBKR serves at most {cap}")
        names = {**self._fields, **self._underlying_fields}
        price_ids = {names[name] for name in QUOTE_FIELD_NAMES if name in names}
        settling = frozenset(price_ids & set(field_ids)) or frozenset(field_ids)
        size = self._ibkr.marketdata.max_conids_per_snapshot
        batches = [
            [int(c) for c in conids[start : start + size]] for start in range(0, len(conids), size)
        ]
        field_param = ",".join(field_ids)

        def params(batch: Sequence[int]) -> dict[str, str]:
            return {"conids": ",".join(str(c) for c in batch), "fields": field_param}

        if len(batches) > 1:
            for batch in batches:
                await self._session.request("GET", SNAPSHOT_PATH, params=params(batch))
        collected: dict[int, dict[str, Any]] = {}
        retries = self._ibkr.marketdata.snapshot_settle_retries
        for batch in batches:
            settled: set[int] = set()
            for attempt in range(retries):
                body = await self._session.request("GET", SNAPSHOT_PATH, params=params(batch))
                for row in body if isinstance(body, list) else []:
                    if not isinstance(row, Mapping):
                        continue
                    conid = safe_int(row.get("conid"), None)
                    if conid is None:
                        continue
                    merged = dict(collected.get(conid) or {})
                    merged.update(row)
                    collected[conid] = merged
                    if any(field in row for field in settling):
                        settled.add(conid)
                if len(settled) >= len(batch):
                    break
                if attempt < retries - 1:
                    await self._session.clock.sleep(
                        self._ibkr.marketdata.snapshot_settle_delay_seconds
                    )
        return collected

    async def snapshot(
        self, conids: Sequence[int], *, underlying: bool = False
    ) -> dict[int, Quote]:
        """Decoded quotes for ``conids`` with the option (or underlying) field set."""
        fields = self.fields(underlying=underlying)
        rows = await self.raw_snapshot(conids, list(fields.values()))
        return {conid: decode_quote(conid, row, fields, self._ibkr) for conid, row in rows.items()}


class Secdef:
    """Security definitions: search, info, chains, futures, bonds and funds."""

    def __init__(self, session: BrokerSession, ibkr: IbkrConfig) -> None:
        self._session = session
        self._ibkr = ibkr

    async def search(
        self, symbol: str, *, sec_type: str | None = None, name: bool = False
    ) -> list[ContractMatch]:
        """``/iserver/secdef/search`` with its terms in a JSON BODY.

        Sent as query parameters the endpoint answers 200, matches the symbol and returns every
        row with ``sections: []`` [CP ✓ 2026-09-10]. ``secType`` was removed from the endpoint
        (changelog 2026-09-09), so a wanted type filters the rows' sections instead.
        """
        body: dict[str, Any] = {"symbol": symbol, "name": bool(name)}
        if sec_type is not None and self._ibkr.secdef.send_sec_type:
            body["secType"] = sec_type
        matches = decode_search(await self._session.request("POST", SEARCH_PATH, json_body=body))
        if sec_type is None:
            return matches
        wanted = sec_type.upper()
        return [match for match in matches if match.section(wanted) is not None]

    def pick(
        self, matches: Sequence[ContractMatch], ticker: str, *, exchange: str | None
    ) -> list[ContractMatch]:
        """The rows the resolution rules leave, best first.

        Exact ticker first; among those, the declared listing venue (``description``) breaks the
        tie — every listing of one company answers the same bare ticker [CP ✓ 2026-09-10], and the
        search's ranking is time-varying. A declared venue matching nothing leaves the
        exact-ticker rows; a venue never overrides the ticker rule.
        """
        wanted = ticker.strip().upper()
        exact = (
            [m for m in matches if m.symbol == wanted]
            if self._ibkr.secdef.exact_ticker_match_first
            else list(matches)
        )
        if exchange:
            venue = exchange.strip().upper()
            on_venue = [m for m in exact if (m.description or "").upper() == venue]
            if on_venue:
                return on_venue
        if exact:
            return exact
        return list(matches[:1]) if self._ibkr.secdef.accept_first_on_no_exact_match else []

    async def info(self, conid: int) -> ContractInfo:
        body = await self._session.request("GET", f"iserver/contract/{int(conid)}/info")
        return decode_contract_info(int(conid), body)

    async def strikes(
        self, underlying_conid: int, *, month: str, exchange: str, sec_type: str = "OPT"
    ) -> OptionStrikes:
        body = await self._session.request(
            "GET",
            STRIKES_PATH,
            params={
                "conid": int(underlying_conid),
                "sectype": sec_type,
                "month": month,
                "exchange": exchange,
            },
        )
        return decode_strikes(body)

    async def option_contracts(
        self,
        underlying_conid: int,
        *,
        month: str,
        exchange: str,
        strike: float,
        right: str,
        sec_type: str = "OPT",
    ) -> list[ContractInfo]:
        """``/iserver/secdef/info`` for one strike and right: a row per expiry in the month."""
        if right.upper() not in ("C", "P"):
            raise ValueError(f"right must be 'C' or 'P', got {right!r}")
        body = await self._session.request(
            "GET",
            SECDEF_INFO_PATH,
            params={
                "conid": int(underlying_conid),
                "sectype": sec_type,
                "month": month,
                "exchange": exchange,
                "strike": strike,
                "right": right.upper(),
            },
        )
        rows = body if isinstance(body, list) else []
        return [
            decode_contract_info(conid, row)
            for row in rows
            if isinstance(row, Mapping)
            and (conid := safe_int(row.get("conid"), None)) is not None
            and conid > 0
        ]

    async def futures(self, roots: Sequence[str]) -> dict[str, tuple[FuturesRung, ...]]:
        """``/trsrv/futures``: the ladder per root, sorted by expiry. A data call only."""
        if not roots:
            return {}
        body = await self._session.request("GET", FUTURES_PATH, params={"symbols": ",".join(roots)})
        return decode_futures(body, roots)

    async def bond_issuers(self, query: str) -> list[ContractMatch]:
        """Search rows describing bond issuers (``sections`` carry ``BOND``)."""
        return await self.search(query, sec_type="BOND", name=True)

    async def bond_filters(self, issuer_id: str) -> tuple[BondFilter, ...]:
        body = await self._session.request(
            "GET", BOND_FILTERS_PATH, params={"symbol": BOND_SYMBOL, "issuerId": issuer_id}
        )
        return decode_bond_filters(body)

    async def funds(self, query: str) -> list[ContractMatch]:
        """Mutual funds matching ``query`` (rows with a ``FUND`` section)."""
        return await self.search(query, sec_type="FUND", name=True)


class Contracts:
    """Trading rules, algos and the trading schedule of one contract."""

    def __init__(self, session: BrokerSession) -> None:
        self._session = session

    async def rules(
        self, conid: int, *, exchange: str | None = None, is_buy: bool = True
    ) -> ContractRules:
        body: dict[str, Any] = {"conid": int(conid), "isBuy": is_buy}
        if exchange:
            body["exchange"] = exchange
        answer = await self._session.request("POST", RULES_PATH, json_body=body)
        return decode_rules(int(conid), is_buy, answer)

    async def algos(self, conid: int) -> tuple[Algo, ...]:
        body = await self._session.request(
            "GET",
            f"iserver/contract/{int(conid)}/algos",
            params={"addDescription": "1", "addParams": "1"},
        )
        return decode_algos(body)

    async def trading_schedule(self, conid: int, *, exchange: str | None = None) -> TradingSchedule:
        body = await self._session.request(
            "GET", SCHEDULE_PATH, params={"conid": int(conid), "exchange": exchange}
        )
        return decode_trading_schedule(int(conid), exchange, body)


class Scanner:
    """The market scanner (``/iserver/scanner``; ``/hmds/scanner`` is deprecated)."""

    def __init__(self, session: BrokerSession) -> None:
        self._session = session

    async def params(self) -> ScannerParams:
        return decode_scanner_params(await self._session.request("GET", SCANNER_PARAMS_PATH))

    async def run(
        self,
        *,
        instrument: str,
        scan_type: str,
        location: str,
        filters: Sequence[Mapping[str, Any]] = (),
    ) -> tuple[ScannerContract, ...]:
        body = {
            "instrument": instrument,
            "type": scan_type,
            "location": location,
            "filter": [dict(item) for item in filters],
        }
        return decode_scanner_run(
            await self._session.request("POST", SCANNER_RUN_PATH, json_body=body)
        )


class IbkrClient:
    """The read client: session, guard and endpoint groups.

    The endpoint groups are reachable only once the account guard has passed (``start``, or
    ``accounts`` then ``run_guard``): before that every one of them raises, so nothing reads
    the broker for an account the guard has not accepted.
    """

    def __init__(self, session: BrokerSession, settings: ObliskConfig, guard: AccountGuard):
        ibkr = settings.ibkr
        self.session = session
        self.guard = guard
        session.add_known_ids([guard.account_id])
        self._portfolio = Portfolio(session, ibkr, guard.account_id, settings.account.base_currency)
        self._marketdata = MarketData(session, ibkr)
        self._history = HistoryReader(
            session, ibkr, calendar_days_per_year=settings.numerics.bsm.day_count_basis
        )
        self._secdef = Secdef(session, ibkr)
        self._contracts = Contracts(session)
        self._scanner = Scanner(session)
        self._guarded = False

    def _require_guard(self) -> None:
        if not self._guarded:
            raise AccountGuardError(
                f"the account guard has not passed for {self.guard.masked}; nothing is read "
                "before it"
            )

    @property
    def portfolio(self) -> Portfolio:
        self._require_guard()
        return self._portfolio

    @property
    def marketdata(self) -> MarketData:
        self._require_guard()
        return self._marketdata

    @property
    def history(self) -> HistoryReader:
        self._require_guard()
        return self._history

    @property
    def secdef(self) -> Secdef:
        self._require_guard()
        return self._secdef

    @property
    def contracts(self) -> Contracts:
        self._require_guard()
        return self._contracts

    @property
    def scanner(self) -> Scanner:
        self._require_guard()
        return self._scanner

    async def accounts(self, *, refresh: bool = False) -> list[Account]:
        """The accounts this login may use: the one read the guard needs before it passes."""
        return await self._portfolio.accounts(refresh=refresh)

    def run_guard(self, accounts: Iterable[Account]) -> None:
        """Check the configured account against ``accounts``; on success the groups open."""
        self.guard.check(account.account_id for account in accounts)
        self._guarded = True

    async def start(self) -> AuthStatus | None:
        """Open the session and run the account guard; refuses to start when it fails, and
        releases the session it opened."""
        self.guard.check_prefix()
        try:
            status = await self.session.open()
            self.run_guard(await self.accounts())
        except BaseException:
            await self.aclose()
            raise
        log.info("IBKR client started", extra={"account": self.guard.masked})
        return status

    async def aclose(self) -> None:
        await self.session.aclose()

    async def __aenter__(self) -> IbkrClient:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()
