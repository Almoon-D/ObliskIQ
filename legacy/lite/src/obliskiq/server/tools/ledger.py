"""The ledger writes — and the gates that stand in front of them.

These are the only tools that touch ``position_ledger``. Each one validates
against ``config.yaml`` and **rejects a gate violation here, in the server**,
before anything is written. The skill is never the only thing between a bad
trade and the ledger: if the skill's judgment fails, or a caller reaches these
tools without it, the gates still close.

What each one enforces beyond the ledger's own integrity rules:

``log_entry`` runs the full entry guard — prohibited instruments, the tenor
window and catalyst buffer, liquidity, the affordability ceiling, the premium
floor, the concurrency cap, the mandatory close date recomputed against the
trading calendar, the averaging-down rule, the payoff ratio at the named target,
the top-decile catalyst requirement, and the theta budget over the thesis's own
expected resolution window. On an option entry it then writes
the ``premium_committed`` row that makes the sleeve budget a shared pool: what
this position ties up is subtracted from what the next one may spend, until it
closes. ``contracts`` is therefore required for an option — a commitment whose
size nobody stated cannot be subtracted from anything.

``log_exit`` requires an ``exit_reason`` from ``discipline.exit_reasons_allowed``.
Price movement alone is not on that list and cannot be added by a caller. The
exit also reconciles the penalty ledger: a loss adjusts by the delta against
whatever detection already booked, so a close never double-counts; a qualifying
non-option gain reduces the penalty 1:1; an ``override`` gain never does.

``log_reunderwrite`` appends. ``thesis_text`` and ``invalidation_trigger`` are
written once at entry and no tool here can edit either — that is what makes
thesis drift detectable rather than a matter of recollection.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any

from mcp.server.fastmcp import FastMCP

from ...compute.frames import collected_dividend_yield, latest_nav
from ...compute.penalty import Conversion, PenaltyLedger
from ...compute.risk_engine import RiskEngine, holdings_from_rows
from ...config import Config
from ...logging_setup import get_logger
from ...store.db import Store
from ...store.ledger import (
    Position,
    append_reunderwrite,
    close_position,
    get_position,
    open_position,
)
from ...util.timeutil import iso_date, utc_stamp
from ..context import ServerContext
from ..encode import to_jsonable
from ..guards import EntryGuard, currency_of, parse_optional_date
from .base import LEDGER_WRITE, UPSERT, call, envelope, refuse

log = get_logger("server.tools.ledger")

#: Exit reasons whose realised loss belongs to the option sleeve's penalty.
LOSS_REASONS_NOTE = (
    "a realised loss on an OPTION position adds the penalty multiple to the balance. "
    "Where a zombie detection already booked it, the close adjusts by the delta only."
)


def register(mcp: FastMCP, context: ServerContext) -> None:
    @mcp.tool(
        name="log_entry",
        description=(
            "Open a position in the ledger. VALIDATES SERVER-SIDE against config.yaml "
            "and REJECTS gate violations: prohibited instruments (futures are never a "
            "candidate), the entry tenor window and catalyst buffer, liquidity, the "
            "affordability ceiling and premium floor, the concurrent-position cap, the "
            "mandatory close date recomputed against the trading calendar, and the "
            "averaging-down rule. The sleeve budget is a SHARED POOL: an option entry "
            "requires `contracts`, and the premium it commits is subtracted from "
            "available budget until the position closes. thesis_text and "
            "invalidation_trigger are FROZEN at "
            "entry — no tool can edit either afterwards; changes append via "
            "log_reunderwrite."
        ),
        annotations=LEDGER_WRITE,
    )
    async def log_entry(
        contract_id: int,
        entry_price: float,
        thesis_text: str,
        invalidation_trigger: str,
        opened_at: str | None = None,
        is_option: bool = True,
        target: float | None = None,
        time_window_end: str | None = None,
        mandatory_close_date: str | None = None,
        prob_target_at_entry: float | None = None,
        entry_range_pctile: float | None = None,
        expiry: str | None = None,
        catalyst_date: str | None = None,
        contracts: int | None = None,
        spread_pct_of_mid: float | None = None,
        open_interest: int | None = None,
        volume: int | None = None,
        strike: float | None = None,
        is_call: bool | None = None,
        spot: float | None = None,
        expected_resolution_days: int | None = None,
        dividend_yield: float | None = None,
        quantity: float | None = None,
        positions: list[dict[str, Any]] | None = None,
        nav: float | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        """Write an entry, once every gate has closed in its favour.

        Args:
            contract_id: The instrument, as held in the universe table.
            entry_price: Premium per share for an option, price per share for
                equity. Must be positive.
            thesis_text: Why this position exists, in the words it will be read
                back in at every review. Frozen at entry.
            invalidation_trigger: A specific observable — a price level, a data
                release, a reported figure. "If sentiment deteriorates" is not one.
            opened_at: Entry date, YYYY-MM-DD. Defaults to as_of.
            is_option: True for an option, false for a cash equity purchase.
                Decides which gates apply: PRIIPs binds on a cash purchase.
            target: The named target price the payoff ratio is measured at.
                REQUIRED for an option: option_sleeve.min_payoff_ratio is checked
                against it here, and an unnamed target cannot be checked.
            time_window_end: Date the thesis has to have resolved by, YYYY-MM-DD.
            mandatory_close_date: Required for an option. Recomputed here against
                the trading calendar and rejected if it disagrees.
            prob_target_at_entry: P0 = N(d2) at the target, from check_position_risk.
                The zombie test compares against it.
            entry_range_pctile: Where in the long-run range this entry sits, 0-1.
                REQUIRED. While discipline.entry_top_decile_requires_catalyst
                holds, a top-decile entry needs a dated catalyst, and an entry
                that does not say where in the range it sits cannot be tested.
            expiry: Option expiry, YYYY-MM-DD. Required for the tenor gate.
            catalyst_date: The dated catalyst, if the tenor is set by one.
            contracts: Number of contracts. REQUIRED for an option: it sizes the
                position-size gate and the premium this entry commits to the
                shared sleeve budget.
            spread_pct_of_mid: Bid-ask spread as a fraction of mid.
            open_interest: Open interest on the contract.
            volume: Session volume on the contract.
            strike: Option strike. REQUIRED for an option: the payoff ratio and
                the theta budget are both measured at it.
            is_call: True for a call, false for a put. REQUIRED for an option.
            spot: Underlying price at entry, for the theta budget.
            expected_resolution_days: How long the thesis is expected to take.
                Derived from time_window_end when not given. One of the two is
                REQUIRED for an option while tenor.require_theta_budget_check
                holds: the check asks what the position is worth if the thesis
                takes its FULL expected time, which is unanswerable without it.
            dividend_yield: Continuous dividend yield, decimal. Defaults to the
                value declared in universe.yaml for this instrument.
            quantity: Shares or units, for a NON-option entry. What `contracts`
                is to an option: the concentration caps cannot measure what this
                entry adds to the book without it, and report untested instead.
            positions: The current book, as get_account_positions returns it,
                folded together with any external holdings. The sector, country
                and single-currency caps are defined over the WHOLE book, so
                without it they are reported untested rather than measured
                against the part the store happens to know.
            nav: Total NAV in base currency. Defaults to the store's latest.
            as_of: Session date, YYYY-MM-DD. Defaults to today in UTC.
        """
        session = context.as_of(as_of)
        parsed_expiry = parse_optional_date(expiry, label="expiry")
        parsed_catalyst = parse_optional_date(catalyst_date, label="catalyst_date")
        parsed_close = parse_optional_date(mandatory_close_date, label="mandatory_close_date")
        parsed_window = parse_optional_date(time_window_end, label="time_window_end")
        parsed_opened = parse_optional_date(opened_at, label="opened_at") or session
        if parsed_opened > session:
            raise refuse(
                f"opened_at {iso_date(parsed_opened)} is after as_of {iso_date(session)}. "
                "An entry cannot be dated into the future: the zombie test's two-session "
                "rule, the theta budget and the mandatory close all measure elapsed time "
                "from this date, and none of them is answerable for a session that has "
                "not happened."
            )

        def work() -> dict[str, Any]:
            with context.store() as store:
                instrument = _instrument(store, contract_id)
                resolution_days = _resolution_days(
                    expected_resolution_days, opened=parsed_opened, window_end=parsed_window
                )
                if is_option:
                    _require_caller_inputs(
                        strike=strike,
                        is_call=is_call,
                        target=target,
                        entry_range_pctile=entry_range_pctile,
                        resolution_days=resolution_days,
                        expiry=parsed_expiry,
                    )
                guard = EntryGuard(store, context.config)
                verdict = guard.check_entry(
                    as_of=session,
                    instrument=instrument,
                    is_option=is_option,
                    nav=nav,
                    expiry=parsed_expiry,
                    catalyst_date=parsed_catalyst,
                    premium_per_share=entry_price,
                    contracts=contracts,
                    spread_pct_of_mid=spread_pct_of_mid,
                    open_interest=open_interest,
                    volume=volume,
                    mandatory_close_date=parsed_close,
                    exchange=(
                        str(instrument.get("exchange")) if instrument.get("exchange") else None
                    ),
                    strike=strike,
                    is_call=is_call,
                    spot=spot,
                    target=target,
                    entry_range_pctile=entry_range_pctile,
                    expected_resolution_days=resolution_days,
                    dividend_yield=_entry_dividend_yield(
                        store,
                        instrument,
                        supplied=dividend_yield,
                        contract_id=contract_id,
                        as_of=session,
                    ),
                    quantity=quantity,
                    holdings=(None if positions is None else holdings_from_rows(positions)),
                )
                if not verdict.allowed:
                    raise refuse(
                        f"log_entry rejected: {verdict.reason} "
                        "(rejected by the server, not by judgment — the gates are enforced "
                        "here so the skill is never the only thing standing between a bad "
                        "trade and the ledger)"
                    )

                # Settled BEFORE anything is written. The sleeve is denominated
                # in account.base_currency and the premium arrives in the
                # CONTRACT's currency, so a commitment that cannot be converted
                # cannot be subtracted from the pool — and booking it unconverted
                # is wrong by the exchange rate, in whichever direction that rate
                # happens to sit. The affordability GATE has always converted;
                # this write did not, and every test used the base currency, so
                # nothing saw it.
                currency = currency_of(instrument)
                committed: float | None = None
                conversion = None
                if is_option:
                    quoted = _committed_premium(
                        context.config, entry_price=entry_price, contracts=contracts
                    )
                    if quoted is None or quoted <= 0:
                        raise refuse(
                            f"an option entry commits {quoted!r} of premium, which is not "
                            "a positive amount. The sleeve budget is a shared pool, and a "
                            "commitment it cannot measure is one it cannot subtract."
                        )
                    converted = guard.convert_to_base(quoted, currency=currency, as_of=session)
                    if converted is None:
                        raise refuse(
                            f"the premium is quoted in {currency or 'an undeclared currency'} "
                            f"and the sleeve is in {guard.base_currency}, and no rate for that "
                            "pair is in the store. The commitment cannot be converted, so it "
                            "cannot be subtracted from the pool — and booking it unconverted "
                            "would overstate or understate available budget by the exchange "
                            "rate. Collect the FX series named in "
                            "account.fx_series_by_currency and retry."
                        )
                    committed, conversion = converted

                penalty = PenaltyLedger(store, context.config)
                # ONE transaction. A position that consumed a concurrency slot
                # with no commitment against it is what a failure between these
                # two writes used to leave behind.
                with store.transaction():
                    position_id = open_position(
                        store,
                        contract_id=contract_id,
                        opened_at=iso_date(parsed_opened),
                        entry_price=entry_price,
                        thesis_text=thesis_text,
                        invalidation_trigger=invalidation_trigger,
                        target=target,
                        time_window_end=(
                            None if parsed_window is None else iso_date(parsed_window)
                        ),
                        mandatory_close_date=(
                            None if parsed_close is None else iso_date(parsed_close)
                        ),
                        prob_target_at_entry=prob_target_at_entry,
                        entry_range_pctile=entry_range_pctile,
                        is_option=is_option,
                        contracts=contracts,
                        strike=strike,
                        is_call=is_call,
                        expiry=None if parsed_expiry is None else iso_date(parsed_expiry),
                        entry_currency=currency,
                    )
                    commitment = None
                    if committed is not None:
                        commitment = penalty.book_premium_committed(
                            premium_paid=committed,
                            position_id=position_id,
                            contract_id=contract_id,
                            event_at=utc_stamp(),
                            conversion=conversion,
                        )

                return {
                    "position_id": position_id,
                    "gates_checked": list(verdict.checked),
                    "gates_untested": list(verdict.untested),
                    "affordability": to_jsonable(verdict.ceiling),
                    "computed_mandatory_close": to_jsonable(verdict.computed_close),
                    "theta_budget": to_jsonable(verdict.theta_budget),
                    "open_positions_after": verdict.open_positions + 1,
                    "premium_committed": to_jsonable(commitment),
                    "premium_committed_currency": guard.base_currency,
                    "circuit_breaker": to_jsonable(verdict.circuit_breaker),
                    "caps_with_this_entry": to_jsonable(verdict.caps),
                    "sleeve_after": to_jsonable(_sleeve_after(store, penalty)),
                    "pool_note": (
                        "the premium above is now subtracted from available option budget "
                        "and stays subtracted until this position closes. Budget left "
                        "unspent stays unspent; it is not redistributed across the "
                        "remaining concurrency slots."
                    ),
                    "thesis_is_frozen": True,
                    "frozen_note": (
                        "thesis_text and invalidation_trigger are now immutable. A changed "
                        "reason for holding is a re-underwrite, not a continuation — log it "
                        "with log_reunderwrite so the drift stays visible."
                    ),
                }

        return envelope(context, as_of=session, data=await call("log_entry", work))

    @mcp.tool(
        name="log_exit",
        description=(
            "Close a position. REQUIRES an exit_reason from "
            "discipline.exit_reasons_allowed and rejects anything else server-side — "
            "price movement alone is never an exit. Reconciles the penalty ledger: an "
            "option loss adjusts by the delta against whatever a zombie detection "
            "already booked, so a close never double-counts; a realised gain on a "
            "NON-OPTION position exited for a qualifying reason reduces the penalty 1:1; "
            "an 'override' gain never does."
        ),
        annotations=LEDGER_WRITE,
    )
    async def log_exit(
        position_id: int,
        exit_reason: str,
        realised_pnl: float | None = None,
        realised_pnl_currency: str | None = None,
        closed_at: str | None = None,
        is_option: bool | None = None,
        proceeds: float | None = None,
        note: str | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        """Close a ledger row, and settle the sleeve consequences.

        Args:
            position_id: The row to close.
            exit_reason: One of discipline.exit_reasons_allowed. 'override' is
                permitted but deliberately conspicuous, and an override gain never
                reduces the penalty balance.
            realised_pnl: Realised P&L, negative for a loss, in
                realised_pnl_currency.
            realised_pnl_currency: The currency realised_pnl is stated in.
                Defaults to the currency recorded at entry. The penalty balance
                is in the account's base currency, so a figure in another one is
                converted at the stored rate before it is booked — and refused
                when no rate for that pair is held, rather than mixed.
            closed_at: Exit date, YYYY-MM-DD. Defaults to as_of.
            is_option: Which sleeve this position is in. READ FROM THE LEDGER,
                where it was recorded at entry; supply it only for a position
                opened before that was stored, and a value contradicting the
                recorded one is refused. It decides whether a loss carries the
                penalty multiple and whether a gain can qualify for recovery, so
                a caller-declared default of True used to book an option penalty
                against equity losses that never touched the sleeve.
            proceeds: Cash received on close, for the option ledger.
            note: Free text recorded against the penalty event.
            as_of: Session date, YYYY-MM-DD. Defaults to today in UTC.
        """
        session = context.as_of(as_of)
        parsed_closed = parse_optional_date(closed_at, label="closed_at") or session
        _require_finite(realised_pnl, "realised_pnl")
        _require_finite(proceeds, "proceeds")

        def work() -> dict[str, Any]:
            with context.store() as store:
                position = get_position(store, position_id)
                if position is None:
                    raise refuse(f"no position {position_id} exists in position_ledger")

                sleeve = _sleeve_of(position, declared=is_option)
                guard = EntryGuard(store, context.config)

                # The penalty balance is in base currency. A P&L stated in
                # another one is converted here, at the same rate the entry gate
                # uses, or the close is refused: booking it unconverted is wrong
                # by the exchange rate, and the penalty is not reversible.
                booked_pnl = realised_pnl
                conversion = None
                if realised_pnl is not None:
                    currency = realised_pnl_currency or position.entry_currency
                    converted = guard.convert_to_base(
                        realised_pnl, currency=currency, as_of=session
                    )
                    if converted is None:
                        raise refuse(
                            f"realised_pnl is stated in "
                            f"{currency or 'an undeclared currency'} and the sleeve is in "
                            f"{guard.base_currency}, and no rate for that pair is in the "
                            "store. State realised_pnl_currency, or pass the figure already "
                            "converted: a penalty booked on an unconverted amount is wrong "
                            "by the exchange rate and cannot be reversed."
                        )
                    booked_pnl, conversion = converted

                stamp = iso_date(parsed_closed)
                penalty = PenaltyLedger(store, context.config)
                # ONE transaction: a close whose penalty never got booked is a
                # position that escaped the sleeve consequence entirely.
                with store.transaction():
                    # close_position validates exit_reason against
                    # discipline.exit_reasons_allowed and refuses a second close.
                    close_position(
                        store,
                        context.config,
                        position_id,
                        closed_at=stamp,
                        exit_reason=exit_reason,
                        realised_pnl=booked_pnl,
                    )
                    event, settlement = _settle(
                        penalty,
                        position_id=position_id,
                        contract_id=position.contract_id,
                        exit_reason=exit_reason,
                        realised_pnl=booked_pnl,
                        is_option=sleeve,
                        proceeds=proceeds,
                        note=note,
                        stamp=utc_stamp(),
                        conversion=conversion,
                    )
                return {
                    "position_id": position_id,
                    "exit_reason": exit_reason,
                    "closed_at": stamp,
                    "is_option": sleeve,
                    "is_option_source": ("ledger" if position.is_option is not None else "caller"),
                    "realised_pnl_base": booked_pnl,
                    "conversion": to_jsonable(conversion),
                    "penalty_event": to_jsonable(event),
                    "settlement": settlement,
                    "sleeve_after": to_jsonable(_sleeve_after(store, penalty)),
                    "penalty_note": LOSS_REASONS_NOTE,
                }

        return envelope(context, as_of=session, data=await call("log_exit", work))

    @mcp.tool(
        name="log_reunderwrite",
        description=(
            "Append a dated re-underwrite to an open position. REQUIRES a NEW catalyst "
            "with a date AFTER the position was opened — falling price is not a "
            "catalyst, and an undated one is a hope with a timestamp. This is the only "
            "way a position's reasoning changes: thesis_text and invalidation_trigger "
            "are frozen at entry and no tool can edit them. A re-underwrite carrying a "
            "catalyst still ahead is also what unlocks adding to a losing position."
        ),
        annotations=LEDGER_WRITE,
    )
    async def log_reunderwrite(
        position_id: int,
        catalyst: str,
        catalyst_date: str,
        note: str | None = None,
        dated_at: str | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        """Append to the re-underwrite log. Never edits the original thesis.

        Args:
            position_id: The open position being re-underwritten.
            catalyst: The new catalyst, named specifically.
            catalyst_date: Its date, YYYY-MM-DD. Must be after opened_at.
            note: What changed, and why this is a re-underwrite rather than a
                continuation. A position held for cheap implied vol is not the
                position bought for a product story.
            dated_at: When this re-underwrite was made. Defaults to as_of.
            as_of: Session date, YYYY-MM-DD. Defaults to today in UTC.
        """
        session = context.as_of(as_of)
        parsed_dated = parse_optional_date(dated_at, label="dated_at") or session

        def work() -> dict[str, Any]:
            with context.store() as store:
                position = get_position(store, position_id)
                if position is None:
                    raise refuse(f"no position {position_id} exists in position_ledger")
                if not position.is_open:
                    raise refuse(
                        f"position {position_id} closed on {position.closed_at}. A closed "
                        "position cannot be re-underwritten."
                    )
                entries = append_reunderwrite(
                    store,
                    position_id,
                    dated_at=iso_date(parsed_dated),
                    catalyst=catalyst,
                    catalyst_date=catalyst_date,
                    note=note,
                )
                engine = RiskEngine(store, context.config)
                refreshed = get_position(store, position_id)
                verdict = (
                    engine.averaging_down(refreshed, as_of=session)
                    if refreshed is not None
                    else None
                )
                return {
                    "position_id": position_id,
                    "reunderwrite_log": to_jsonable(entries),
                    "thesis_unchanged": position.thesis_text,
                    "averaging_down": to_jsonable(verdict),
                }

        return envelope(context, as_of=session, data=await call("log_reunderwrite", work))

    @mcp.tool(
        name="upsert_external_position",
        description=(
            "Record or update a holding at a broker with no readable API — DEGIRO, "
            "Revolut, Openbank, Santander. IBKR is the source of truth for everything "
            "it can see; these rows are what make NAV and every concentration cap "
            "(position, sector, country, currency) cover the whole book rather than the "
            "part with an API. Beyond positions.external_data_stale_days since "
            "last_updated, cap utilisation is reported UNRELIABLE rather than returning "
            "a number known to be wrong. Keyed on (broker, symbol, isin)."
        ),
        annotations=UPSERT,
    )
    async def upsert_external_position(
        broker: str,
        symbol: str,
        quantity: float,
        currency: str,
        market_value_base: float,
        isin: str | None = None,
        sector: str | None = None,
        country: str | None = None,
        notes: str | None = None,
        last_updated: str | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        """Manual entry for a non-IBKR holding.

        Args:
            broker: Where it is held, e.g. "DEGIRO".
            symbol: Ticker or identifier as the broker shows it.
            quantity: Units held.
            currency: Currency of the listing.
            market_value_base: Current value in the account's base currency.
            isin: ISIN, part of the uniqueness key.
            sector: Sector, so the position counts towards the sector cap.
            country: Country, so it counts towards the country cap.
            notes: Anything worth recording about the holding.
            last_updated: When a HUMAN last confirmed this figure, YYYY-MM-DD.
                Defaults to as_of. This is what staleness is measured from, so
                back-date it honestly rather than to today.
            as_of: Session date, YYYY-MM-DD. Defaults to today in UTC.
        """
        session = context.as_of(as_of)
        confirmed = parse_optional_date(last_updated, label="last_updated") or session
        if not broker.strip() or not symbol.strip():
            raise refuse("broker and symbol are both required to key an external holding")
        _require_finite(quantity, "quantity")
        _require_finite(market_value_base, "market_value_base")

        def work() -> dict[str, Any]:
            with context.store() as store:
                _upsert_external(
                    store,
                    broker=broker.strip(),
                    symbol=symbol.strip(),
                    isin=isin,
                    quantity=quantity,
                    currency=currency,
                    market_value_base=market_value_base,
                    last_updated=iso_date(confirmed),
                    notes=notes,
                    sector=sector,
                    country=country,
                )
                engine = RiskEngine(store, context.config)
                holdings, stale = engine.external_holdings(as_of=session)
                payload: dict[str, Any] = {
                    "broker": broker.strip(),
                    "symbol": symbol.strip(),
                    "last_updated": iso_date(confirmed),
                    "external_positions": to_jsonable(holdings),
                    "external_total_base": sum(h.market_value_base for h in holdings),
                }
                if stale:
                    payload["stale"] = list(stale)
                    payload["stale_note"] = (
                        "these holdings are past positions.external_data_stale_days. Cap "
                        "utilisation over them is reported UNRELIABLE: a number known to "
                        "be wrong is worse than no number."
                    )
                return payload

        return envelope(context, as_of=session, data=await call("upsert_external_position", work))


def _upsert_external(
    store: Store,
    *,
    broker: str,
    symbol: str,
    isin: str | None,
    quantity: float,
    currency: str,
    market_value_base: float,
    last_updated: str,
    notes: str | None,
    sector: str | None,
    country: str | None,
) -> None:
    """Update in place, or insert. Matched on (broker, symbol, isin), NULLs included.

    Deliberately not ``ON CONFLICT (broker, symbol, isin)``. SQLite treats NULLs
    as distinct in a UNIQUE constraint, so a holding entered without an ISIN —
    which is the common case for these brokers — would never conflict and every
    update would append another row. The duplicate would then be counted twice in
    NAV and in every concentration cap, silently. ``IS`` compares NULL to NULL as
    equal, which is the match this key actually means.
    """
    values = (
        quantity,
        currency,
        market_value_base,
        last_updated,
        notes,
        sector,
        country,
    )
    with store.transaction() as connection:
        cursor = connection.execute(
            "UPDATE external_positions SET quantity = ?, currency = ?, "
            "market_value_base = ?, last_updated = ?, notes = ?, sector = ?, country = ? "
            "WHERE broker = ? AND symbol = ? AND isin IS ?",
            (*values, broker, symbol, isin),
        )
        if cursor.rowcount == 0:
            connection.execute(
                "INSERT INTO external_positions (broker, symbol, isin, quantity, currency, "
                "market_value_base, last_updated, notes, sector, country) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (broker, symbol, isin, *values),
            )


def _resolution_days(supplied: int | None, *, opened: date, window_end: date | None) -> int | None:
    """How long the thesis is expected to take, in calendar days.

    Derived from ``time_window_end`` when not stated outright, because the time
    window IS the expected resolution: "the date the thesis has to have resolved
    by" and "how long the thesis is expected to take" are the same commitment
    written two ways, and asking for both invites them to disagree.
    """
    if supplied is not None:
        return supplied
    if window_end is None:
        return None
    span = (window_end - opened).days
    return span if span > 0 else None


def _require_caller_inputs(
    *,
    strike: float | None,
    is_call: bool | None,
    target: float | None,
    entry_range_pctile: float | None,
    resolution_days: int | None,
    expiry: date | None,
) -> None:
    """Refuse an option entry that withheld what only the caller can supply.

    The same shape as the existing ``contracts`` rule, and for the same reason.
    These are not conveniences: without a strike and a direction there is no
    payoff to measure, without a named target there is no ratio to measure it
    against, without a range percentile the top-decile catalyst rule cannot fire,
    without an expected resolution the theta budget has no horizon, and without
    an expiry the trading calendar has nothing to count the mandatory close date
    back from. An entry that omits them is not an entry that passes those gates —
    it is an entry nobody applied them to, and recording it as logged would make
    ``option_sleeve.min_payoff_ratio``, ``discipline.entry_top_decile_requires_catalyst``,
    ``tenor.require_theta_budget_check`` and the mandatory-close rule optional in
    practice while reading as guarantees.

    ``expiry`` is the newest of them and the one that hid best: with it absent
    the close-date gate appeared in neither the checked nor the untested list, so
    an option on a US-listed ETF could be logged with no close date computed at
    all — which is how one gets held into an OCC auto-exercise.

    Distinguish this from a gate reported UNTESTED. That is the store lacking
    something — a NAV, a calendar, a collected chain — which the caller cannot
    fix and which must not block a young store. This is the caller declining to
    state its own trade.
    """
    missing: list[str] = []
    if expiry is None:
        missing.append("expiry")
    if strike is None:
        missing.append("strike")
    if is_call is None:
        missing.append("is_call")
    if target is None or target <= 0:
        missing.append("target")
    if entry_range_pctile is None:
        missing.append("entry_range_pctile")
    if resolution_days is None:
        missing.append("expected_resolution_days (or time_window_end)")
    if not missing:
        return
    raise refuse(
        "log_entry rejected: an option entry must state "
        + ", ".join(missing)
        + ". These are the inputs the "
        "server's entry gates are computed from: the payoff ratio at the named target "
        "(option_sleeve.min_payoff_ratio), the top-decile catalyst rule "
        "(discipline.entry_top_decile_requires_catalyst) and the theta budget "
        "(tenor.require_theta_budget_check). Omitted, they would not be gates that "
        "passed — they would be gates nobody ran. (rejected by the server, not by "
        "judgment — the gates are enforced here so the skill is never the only thing "
        "standing between a bad trade and the ledger)"
    )


def _require_finite(value: float | None, name: str) -> None:
    """Refuse a caller-supplied amount that is not a real number.

    ``nan`` and the infinities all parse cleanly through the MCP wire — the JSON
    grammar admits the literals ``NaN`` and ``Infinity``, and an overflowing
    literal like ``1e999`` decodes to ``inf`` rather than raising — so a caller
    can hand a ledger write a value that is not a number at all. Comparisons like
    ``realised_pnl < 0`` then fail open: ``nan`` is neither less than nor greater
    than zero, and ``-inf`` is a real loss that swamps every arithmetic check
    downstream of it. Every write this guards is either append-only
    (``option_ledger``) or awkward to unpick (``position_ledger``), so the refusal
    has to happen before anything is written, not after.
    """
    if value is not None and not math.isfinite(value):
        raise refuse(
            f"{name} is {value!r}, which is not a finite number. A ledger write is "
            "not reversible, and a NaN or an infinity here would be booked as if it "
            "were a real amount — silently locking the sleeve on an infinite loss, "
            "or recording a loss as an untracked gain. State the actual figure."
        )


def _sleeve_of(position: Position, *, declared: bool | None) -> bool:
    """Which sleeve this position is in: from the LEDGER, not from the caller.

    ``is_option`` used to be a caller argument defaulting to True, and nothing
    could check it. Closing an equity position without saying so booked an
    option-sleeve penalty of ``loss_penalty_multiple x loss`` against a loss the
    sleeve never carried; the opposite mistake let an option loss escape the
    penalty. Migration 011 records the answer at entry, so from here the ledger
    decides.

    ``position_ledger.is_option`` is NULL only on rows written before that
    migration. There the caller has to say, and the tool refuses to guess — the
    two mistakes above are not symmetric, and neither is recoverable once a
    penalty is booked.
    """
    if position.is_option is not None:
        if declared is not None and bool(declared) != position.is_option:
            raise refuse(
                f"is_option={declared!r} contradicts the ledger, which recorded this "
                f"position as {'an option' if position.is_option else 'not an option'} at "
                "entry. The ledger is the record; a close does not get to redeclare which "
                "sleeve a position was in. Omit is_option."
            )
        return position.is_option
    if declared is None:
        raise refuse(
            f"position {position.position_id} predates the migration that records which "
            "sleeve a position is in, so is_option is not stored and this tool will not "
            "guess it. Pass is_option explicitly: true books a loss against the option "
            "sleeve at option_sleeve.loss_penalty_multiple, false leaves the sleeve "
            "untouched, and a penalty booked wrongly cannot be reversed."
        )
    return bool(declared)


def _entry_dividend_yield(
    store: Store,
    instrument: dict[str, Any],
    *,
    supplied: float | None,
    contract_id: int | None,
    as_of: date | None,
) -> float | None:
    """The q this entry is priced at: caller, else collected, else declared.

    The same precedence ``check_position_risk`` reports, and it has to be the
    same or the gate that admits a position and the assessment that later judges
    it would price the identical contract differently.

    A COLLECTED figure outranks the declaration because it is dated and measured
    rather than typed into a file at some point in the past; it is read bounded at
    ``as_of`` so an entry booked for a past session cannot be priced with a yield
    collected after it.

    ``None`` rather than zero when nothing is known: the guard distinguishes them,
    and a silent zero here is the defect this whole thread exists to close."""
    if supplied is not None:
        return supplied
    collected = collected_dividend_yield(store, contract_id, as_of=as_of)
    if collected is not None:
        return collected[0]
    value = instrument.get("dividend_yield")
    return None if value is None else float(value)


def _committed_premium(
    config: Config, *, entry_price: float, contracts: int | None
) -> float | None:
    """What this entry ties up in the sleeve: premium x multiplier x contracts.

    ``None`` when the caller gave no size. The guard refuses that case before
    this is reached; returning ``None`` rather than guessing keeps the two from
    disagreeing about what an unsized entry means.
    """
    if contracts is None:
        return None
    multiplier = config.require_int("instruments.default_option_multiplier")
    return entry_price * multiplier * contracts


def _instrument(store: Store, contract_id: int) -> dict[str, Any]:
    row = store.execute("SELECT * FROM universe WHERE contract_id = ?", (contract_id,)).fetchone()
    if row is None:
        raise refuse(
            f"contract {contract_id} is not in the universe table, so no gate can be "
            "applied to it. Seed it with `obliskiq-collect universe` first: an entry "
            "whose instrument nobody can classify is an entry nobody can gate."
        )
    return dict(row)


def _settle(
    penalty: PenaltyLedger,
    *,
    position_id: int,
    contract_id: int,
    exit_reason: str,
    realised_pnl: float | None,
    is_option: bool,
    proceeds: float | None,
    note: str | None,
    stamp: str,
    conversion: Conversion | None = None,
) -> tuple[Any, str]:
    """Book the sleeve consequence of this exit, and say which one applied."""
    if realised_pnl is None:
        return None, "no realised P&L was supplied, so nothing was booked to the sleeve"

    if is_option:
        if realised_pnl < 0:
            event = penalty.reconcile_loss_closed(
                actual_loss=abs(realised_pnl),
                event_at=stamp,
                position_id=position_id,
                contract_id=contract_id,
                proceeds=proceeds,
                note=note,
                conversion=conversion,
            )
            return event, (
                "option loss reconciled against any penalty already booked at detection; "
                "adjusted by the delta only"
            )
        event = penalty.record_option_gain(
            realised_pnl=realised_pnl,
            event_at=stamp,
            position_id=position_id,
            contract_id=contract_id,
            proceeds=proceeds,
            note=note,
            conversion=conversion,
        )
        return event, (
            "option gain recorded. It does NOT reduce the penalty: recovery is measured "
            "on realised gains from non-option positions"
        )

    if realised_pnl <= 0:
        return None, ("a losing non-option exit changes nothing in the option sleeve either way")
    if exit_reason not in penalty.qualifying_exit_reasons:
        return None, (
            f"exit_reason {exit_reason!r} is not in option_sleeve.qualifying_exit_reasons "
            f"{list(penalty.qualifying_exit_reasons)}, so this gain does not reduce the "
            "penalty. Cutting a winner early to bank profit cannot unlock the sleeve."
        )
    event = penalty.book_qualifying_gain(
        realised_pnl=realised_pnl,
        exit_reason=exit_reason,
        event_at=stamp,
        position_id=position_id,
        contract_id=contract_id,
        note=note,
        conversion=conversion,
    )
    return event, "qualifying non-option gain; penalty reduced 1:1, floored at zero"


def _sleeve_after(store: Store, penalty: PenaltyLedger) -> Any:
    """The sleeve as it stands after this exit, when NAV is known.

    ``None`` rather than a status computed on a guessed NAV: the budget is a
    fraction of NAV, so an invented denominator invents the budget with it.
    """
    nav = latest_nav(store)
    return None if nav is None else penalty.status(nav)
