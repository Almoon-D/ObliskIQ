"""``check_position_risk`` — greeks, the zombie test, stops, caps, the breaker.

**This tool writes, deliberately.** The zombie test requires the four conditions
to hold across ``zombie_test.consecutive_sessions_required`` consecutive review
sessions before it fires, and it books the penalty at *detection* rather than at
close. Neither is possible without recording each review, so a purely read-only
version of this tool would be a zombie test that could never accumulate a second
session and never fire. ``record`` is exposed so a caller can explicitly ask for
a dry read, and defaults to true because the recording is the mechanism.

N(d₂) is computed on the target strike's own smile implied volatility, read out
of the collected chain. Constant-vol Black-Scholes understates the terminal
probability on an out-of-the-money strike, and that understatement is exactly
what produces a false zombie positive and a premature exit. If the chain carries
no usable implied vol, the position is reported as unmeasurable — never
discounted at a substituted constant.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from datetime import date
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ...compute.frames import (
    QUALITY_LIVE,
    bar_series,
    chain_rows,
    collected_dividend_yield,
    latest_chain_date,
    latest_nav,
    universe_row,
)
from ...compute.risk_engine import RiskEngine, holdings_from_rows
from ...compute.smile import Smile, SmileFilters
from ...logging_setup import get_logger
from ...macro.rates import cached_risk_free_rate
from ...store.db import Store
from ...store.ledger import Position, get_position
from ...util.timeutil import parse_date
from ..context import ServerContext
from ..encode import to_jsonable
from .base import call, envelope, refuse

log = get_logger("server.tools.risk")

#: Not read-only: the review is recorded so the two-session confirmation can
#: accumulate, and detection books the penalty. Not destructive either — every
#: write is an append.
RECORDS_OBSERVATIONS = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False
)

#: Where the dividend yield came from. Reported on every option assessment,
#: because "no yield was declared" and "this instrument pays nothing" are
#: different claims and only the second is evidence.
DIVIDEND_FROM_CALLER = "caller"
#: A figure a collector wrote, dated, outranking the declaration because it was
#: MEASURED on a session rather than typed into a file at some point in the past.
DIVIDEND_FROM_COLLECTED = "collected"
DIVIDEND_FROM_UNIVERSE = "universe.yaml"
DIVIDEND_NONE_DECLARED = "none_declared"


def register(mcp: FastMCP, context: ServerContext) -> None:
    @mcp.tool(
        name="check_position_risk",
        description=(
            "Greeks, N(d2) at the target on the strike's OWN smile vol, the four-condition "
            "zombie test with two-session confirmation, theta budget, mandatory close date, "
            "volatility-scaled stops, cap utilisation and the portfolio drawdown breaker. "
            "WRITES: each review is recorded so the consecutive-session confirmation can "
            "accumulate, and a detection books the penalty at detection rather than at "
            "close. Percentage drawdown is not a trigger and cannot be made one."
        ),
        annotations=RECORDS_OBSERVATIONS,
    )
    async def check_position_risk(
        position_id: int | None = None,
        contract_id: int | None = None,
        as_of: str | None = None,
        spot: float | None = None,
        current_bid: float | None = None,
        strike: float | None = None,
        expiry: str | None = None,
        is_call: bool | None = None,
        premium_paid: float | None = None,
        rate: float | None = None,
        dividend_yield: float | None = None,
        expected_resolution_days: int | None = None,
        nav: float | None = None,
        positions: list[dict[str, Any]] | None = None,
        record: bool = True,
    ) -> dict[str, Any]:
        """Everything the risk engine knows about one position, or about the book.

        Args:
            position_id: Ledger row to assess. Omit for the book-level view
                (caps and the circuit breaker only).
            contract_id: The UNDERLYING, when assessing an option that is not yet
                in the ledger — which is how prob_target_at_entry is computed
                before calling log_entry. Defaults to the position's contract.
                One of the two is required for the option assessment: the smile
                must be built from one underlying's chain.
            as_of: Session date, YYYY-MM-DD. Defaults to today in UTC.
            spot: Live underlying price. Required for the option assessment;
                without it only stops, caps and the breaker are returned.
            current_bid: Live bid on the option. Condition four of the zombie
                test asks whether there is capital worth rescuing.
            strike: Option strike. Defaults to the ledger's contract terms where
                the position is an option entry that recorded them.
            expiry: Option expiry, YYYY-MM-DD.
            is_call: True for a call, false for a put.
            premium_paid: Premium paid per share at entry. Defaults to the
                ledger's entry_price.
            rate: Annualised risk-free rate as a decimal (0.0325 = 3.25%).
                Defaults to the last rate the collectors stored for the base
                currency. Never substituted with a constant.
            dividend_yield: Continuous dividend yield as a decimal (0.05 = 5%).
                Defaults to the value declared for this instrument in
                universe.yaml, and to zero only when none is declared — which
                the response says outright, because a payer priced at zero
                returns an N(d2) and a delta that are both too high, and the
                zombie test compares today's N(d2) against entry's.
            expected_resolution_days: How long the thesis is expected to take.
                Supplying it returns the theta budget check.
            nav: Total NAV in base currency, for caps and the breaker.
            positions: IBKR holdings, for cap utilisation over the whole book.
            record: Write this review to zombie_observations. Leave true unless
                you explicitly want a dry read: the two-session confirmation
                cannot accumulate without it.
        """
        session = context.as_of(as_of)
        parsed_expiry = _optional_date(expiry, label="expiry")

        def work() -> dict[str, Any]:
            with context.store() as store:
                engine = RiskEngine(store, context.config)
                payload: dict[str, Any] = {}
                position = _position(store, position_id)

                if position is not None:
                    payload["position"] = to_jsonable(
                        {
                            "position_id": position.position_id,
                            "contract_id": position.contract_id,
                            "opened_at": position.opened_at,
                            "thesis_text": position.thesis_text,
                            "invalidation_trigger": position.invalidation_trigger,
                            "target": position.target,
                            "mandatory_close_date": position.mandatory_close_date,
                            "time_window_end": position.time_window_end,
                        }
                    )
                    payload["equity"] = _equity_view(store, engine, position)
                    payload["averaging_down"] = to_jsonable(
                        engine.averaging_down(position, as_of=session)
                    )

                option = _option_view(
                    store,
                    engine,
                    context,
                    position=position,
                    contract_id=contract_id,
                    session=session,
                    spot=spot,
                    strike=strike,
                    expiry=parsed_expiry,
                    is_call=is_call,
                    premium_paid=premium_paid,
                    current_bid=current_bid,
                    rate=rate,
                    dividend_yield=dividend_yield,
                    expected_resolution_days=expected_resolution_days,
                    record=record,
                )
                payload.update(option)

                resolved_nav = nav if nav is not None else latest_nav(store)
                if resolved_nav is not None:
                    payload["breaker"] = to_jsonable(
                        engine.circuit_breaker(as_of=session, nav=resolved_nav)
                    )
                    holdings = holdings_from_rows(positions or [])
                    external, _ = engine.external_holdings(as_of=session)
                    if holdings or external:
                        payload["caps"] = to_jsonable(
                            engine.cap_utilisation(holdings, nav=resolved_nav, as_of=session)
                        )
                else:
                    payload["caps_note"] = (
                        "no NAV was supplied and none is recorded, so caps and the "
                        "circuit breaker cannot be evaluated."
                    )

                payload["drawdown_is_not_a_trigger"] = (
                    "Percentage drawdown is not one of the four zombie conditions. A long "
                    "option down on a short move with months of tenor left is noise; never "
                    "recommend closing because a position is red."
                )
                return payload

        return envelope(context, as_of=session, data=await call("check_position_risk", work))


def _position(store: Store, position_id: int | None) -> Position | None:
    if position_id is None:
        return None
    position = get_position(store, position_id)
    if position is None:
        raise refuse(f"no position {position_id} exists in position_ledger")
    return position


def _equity_view(store: Store, engine: RiskEngine, position: Position) -> dict[str, Any]:
    bars = bar_series(store, position.contract_id)
    if not bars.close:
        return {
            "stops": None,
            "note": (
                "no daily bars are held for this contract, so ATR cannot be computed. "
                "A fixed-percentage stop is not offered as a substitute."
            ),
        }
    stops = engine.equity_stops(
        bars,
        entry_price=position.entry_price,
        trailing_high=bars.trailing_high,
    )
    return {"stops": to_jsonable(stops)}


def _option_view(
    store: Store,
    engine: RiskEngine,
    context: ServerContext,
    *,
    position: Position | None,
    contract_id: int | None,
    session: date,
    spot: float | None,
    strike: float | None,
    expiry: date | None,
    is_call: bool | None,
    premium_paid: float | None,
    current_bid: float | None,
    rate: float | None,
    dividend_yield: float | None,
    expected_resolution_days: int | None,
    record: bool,
) -> dict[str, Any]:
    """The option assessment, when enough of the contract terms were supplied."""
    if spot is None or strike is None or expiry is None or is_call is None:
        return {
            "option": None,
            "option_note": (
                "spot, strike, expiry and is_call are all required for the greeks and the "
                "zombie test. Supply them from the live chain: the store holds collected "
                "snapshots, not this session's quotes."
            ),
        }

    premium = (
        premium_paid
        if premium_paid is not None
        else (position.entry_price if position is not None else None)
    )
    if premium is None or premium <= 0:
        raise refuse(
            "premium_paid is required and must be positive. Without the premium there is "
            "no payoff ratio, no target level and no recoverable percentage."
        )

    underlying_id = (
        contract_id
        if contract_id is not None
        else (position.contract_id if position is not None else None)
    )
    if underlying_id is None:
        raise refuse(
            "the smile must be built from ONE underlying's chain, so either position_id "
            "or contract_id is required for the option assessment. Without it the "
            "implied vols of unrelated instruments would be pooled into a single "
            "surface, and N(d2) read off the result would be a number about nothing."
        )
    smile = _smile(store, context, underlying_id=underlying_id, spot=spot, expiry=expiry)
    if smile is None:
        return {
            "option": None,
            "option_note": (
                "no collected chain row for this expiry survives smile hygiene with a "
                "usable implied volatility, so N(d2) cannot be computed on the strike's "
                "own smile. Rows are dropped for a spread wider than "
                "liquidity.max_spread_pct, a book thinner than "
                "liquidity.min_open_interest, no session volume, a delta outside the "
                "smile band, or an implied vol off the chord of its neighbours by more "
                "than smile.butterfly_max_iv_deviation. The position is unmeasurable "
                "today rather than measured at a constant or at an unfiltered second "
                "reading: constant-vol Black-Scholes understates the terminal "
                "probability on an out-of-the-money strike and produces false zombie "
                "positives, and one bad print on an illiquid strike does the same."
            ),
        }

    resolved_rate, rate_note = _rate(store, context, rate)
    universe = universe_row(store, underlying_id)
    exchange = str(universe.get("exchange")) if universe and universe.get("exchange") else None
    resolved_q, q_source, q_note = _dividend_yield(
        universe, dividend_yield, store=store, contract_id=underlying_id, as_of=session
    )

    risk = engine.assess_option(
        as_of=session,
        spot=spot,
        strike=strike,
        is_call=is_call,
        expiry=expiry,
        premium_paid=premium,
        current_bid=current_bid,
        smile=smile,
        rate=resolved_rate,
        dividend_yield=resolved_q,
        prob_target_at_entry=(position.prob_target_at_entry if position else None),
        stated_target=(position.target if position else None),
        exchange=exchange,
    )
    assessment = engine.zombie(
        risk,
        position_id=(position.position_id if position else None),
        record=record,
        book_penalty=record,
    )

    out: dict[str, Any] = {
        "option": to_jsonable(risk),
        "zombie": to_jsonable(assessment),
        "zombie_summary": assessment.summary(),
        "recorded": record,
        "prob_vol_source": engine.prob_vol_source,
        # TWO reads off one smile, at two different points, because they price
        # two different things: N(d2) asks about the TARGET, the greeks price
        # THIS strike. Both provenances are reported — whether each was measured
        # at a quoted strike, interpolated, or clamped to the outermost surviving
        # one. A clamped read is still the smile rather than ATM vol, and a
        # caller quoting the probability or the delta should say which it got.
        "smile_provenance": risk.vol_quote.provenance(),
        "smile_provenance_at_strike": (
            None if risk.strike_vol_quote is None else risk.strike_vol_quote.provenance()
        ),
        "vol_is_clamped": {
            "at_target": risk.vol_quote.is_clamped,
            "at_strike": (
                None if risk.strike_vol_quote is None else risk.strike_vol_quote.is_clamped
            ),
        },
        # Hygiene, not decoration. A probability read off three surviving strikes
        # out of forty is a different claim from one read off a full book, and
        # this is what says which it was.
        "smile_hygiene": to_jsonable(smile.hygiene),
        "smile_hygiene_summary": smile.hygiene.summary(),
        "data_quality": _data_quality(smile),
        "dividend_yield": resolved_q,
        "dividend_yield_source": q_source,
    }
    if q_note:
        out["dividend_yield_note"] = q_note
    if rate_note:
        out["rate_note"] = rate_note
    if expected_resolution_days is not None:
        out["theta_budget"] = to_jsonable(
            engine.theta_budget(
                spot=spot,
                strike=strike,
                is_call=is_call,
                # The theta budget reprices THIS contract, so it takes the
                # strike's vol, like the greeks — not the target's.
                sigma=risk.strike_vol if risk.strike_vol is not None else risk.vol_used,
                rate=resolved_rate,
                premium_paid=premium,
                entry_dte=risk.dte,
                expected_resolution_days=expected_resolution_days,
                dividend_yield=resolved_q,
            )
        )
    return out


def _smile(
    store: Store, context: ServerContext, *, underlying_id: int, spot: float, expiry: date
) -> Smile | None:
    """The strike's own smile, filtered.

    The hygiene pass is not optional here and there is no unfiltered fallback:
    ``zombie_test.prob_vol_source`` is ``strike_smile_iv``, so a single bad
    implied vol on an illiquid strike propagates into N(d2) and out into an exit
    recommendation on a position that was still alive. Unmeasurable is the right
    answer when the chain cannot support the measurement.
    """
    snapshot = latest_chain_date(store, underlying_id)
    if snapshot is None:
        return None
    rows = chain_rows(store, underlying_id=underlying_id, snapshot_date=snapshot, expiry=expiry)
    return Smile.from_rows(
        rows,
        spot=spot,
        underlying_id=underlying_id,
        expiry=expiry,
        filters=SmileFilters.from_config(context.config),
    )


def _data_quality(smile: Smile) -> dict[str, Any]:
    """The market-data state behind every number derived from this smile.

    SKILL.md: "Check data status on every pull. Label anything FROZEN or DELAYED
    as indicative in every table it appears in. Never mix frozen and live quotes
    unmarked." The store has recorded this per row since migration 006 and
    nothing read it until now, so the skill was being asked to apply a label
    that no tool gave it.
    """
    indicative = smile.market_data_quality != QUALITY_LIVE
    return {
        "market_data_quality": smile.market_data_quality,
        "market_data_statuses": dict(smile.market_data_statuses),
        "sources": dict(smile.sources),
        "snapshot_date": smile.snapshot_date.isoformat(),
        "indicative": indicative,
        "note": (
            "every figure derived from this chain is INDICATIVE, not live: the quotes "
            "behind it are not on data.market_data_status_live. Label it as such in "
            "every table it appears in."
            if indicative
            else "the quotes behind this chain are live per data.market_data_status_live."
        ),
    }


def _dividend_yield(
    universe: dict[str, Any] | None,
    supplied: float | None,
    *,
    store: Store | None = None,
    contract_id: int | None = None,
    as_of: date | None = None,
) -> tuple[float, str, str | None]:
    """The continuous dividend yield q, and where it came from.

    Shaped like :func:`_rate`, with one deliberate difference: a missing rate is
    refused and a missing dividend yield is not. A risk-free rate is always
    knowable and always material. A dividend yield is legitimately zero for most
    of this universe, and refusing every instrument that has not declared one
    would make the tool unusable on the names that genuinely pay nothing.

    So the fallback is zero WITH A NOTE. What is not acceptable is the previous
    behaviour, where zero was the silent default for everything: q enters d1 as
    ``(r - q + sigma^2/2) * tau``, so a payer priced at zero gets an N(d2) and a
    delta that are both too high, and ``zombie_test.max_prob_ratio_vs_entry``
    compares today's N(d2) against entry's. An undeclared yield must never read
    as a measured zero.
    """
    if supplied is not None:
        return supplied, DIVIDEND_FROM_CALLER, None
    collected = None if store is None else collected_dividend_yield(store, contract_id, as_of=as_of)
    if collected is not None:
        value, session, basis = collected
        return (
            value,
            DIVIDEND_FROM_COLLECTED,
            f"discounted at the dividend yield collected on {session.isoformat()} "
            f"({basis or 'basis unrecorded'}). IBKR serves a TRAILING yield and "
            "Black-Scholes-Merton wants a continuous one, so this is an approximation "
            "of q rather than q itself.",
        )
    declared = None if universe is None else universe.get("dividend_yield")
    if declared is not None:
        as_of = None if universe is None else universe.get("dividend_yield_as_of")
        stamp = f", sourced {as_of}" if as_of else " (undated)"
        return (
            float(declared),
            DIVIDEND_FROM_UNIVERSE,
            f"discounted at the dividend yield declared in universe.yaml{stamp}.",
        )
    return (
        0.0,
        DIVIDEND_NONE_DECLARED,
        "universe.yaml declares no dividend yield for this instrument, so this is the "
        "ZERO-DIVIDEND case rather than a measured zero. If the underlying pays, N(d2) "
        "and delta are both overstated here — on a 5% payer at 150 days that is about "
        "15% on N(d2) and 0.048 on delta — and the zombie test compares this N(d2) "
        "against the one recorded at entry. Declare the yield, or pass dividend_yield.",
    )


def _rate(store: Store, context: ServerContext, supplied: float | None) -> tuple[float, str | None]:
    if supplied is not None:
        return supplied, None
    cached = cached_risk_free_rate(store, context.config)
    if cached is None:
        raise refuse(
            "no risk-free rate was supplied and none has been collected, so d2 cannot be "
            "computed. Pass `rate`, or run the macro collector. A substituted rate flows "
            "straight into prob_target_at_entry and the zombie test."
        )
    return cached.rate, (
        f"discounted at the stored {cached.currency} rate {cached.series_id} "
        f"({cached.rate:.4f}) as of {cached.reference_date}, from the last collector run "
        "rather than a live quote."
    )


def _optional_date(raw: str | None, *, label: str) -> date | None:
    if raw is None or not str(raw).strip():
        return None
    parsed = parse_date(str(raw))
    if parsed is None:
        raise refuse(f"{label} {raw!r} is not a date in YYYY-MM-DD form")
    return parsed
