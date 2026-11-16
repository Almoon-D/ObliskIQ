"""``obliskiq preflight --full``: the whole path a deployed bot walks, before it is deployed.

auth → accounts → guard → positions → cash → venue status → snapshot (its availability) → history
→ whatif for the minimum quantity of a core vehicle (``ibkr.preflight.whatif_conid``; previewed,
never placed) → a Telegram test message when Telegram is configured → the report. It runs on an
order path (``execution/runtime.py``), under the service lock, so it never competes with a running
service for the brokerage session. Each step reports on its own and the walk stops at the first
failure. The report masks the account id and prints no balance or position.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
import uuid
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from ..domain.orders import LimitPrice, OrderSide, OrderTerms, TimeInForce
from ..ibkr.errors import AuthError
from ..ibkr.orders import SMART_ROUTER
from ..ibkr.preflight import PreflightReport, run_step
from ..reporting.digest import venue_line
from ..reporting.notifier import Notifier
from ..util.timeutil import utc_now
from .paper_test import passive_limit
from .pricing import tick_for
from .runtime import OrderPath

STEP_AUTH = "auth"
STEP_ACCOUNTS = "accounts"
STEP_GUARD = "account-guard"
STEP_POSITIONS = "positions"
STEP_CASH = "cash"
STEP_VENUE = "venue-status"
STEP_SNAPSHOT = "snapshot"
STEP_HISTORY = "history"
STEP_WHATIF = "whatif"
STEP_TELEGRAM = "telegram"
FULL_STEPS = (
    STEP_AUTH,
    STEP_ACCOUNTS,
    STEP_GUARD,
    STEP_POSITIONS,
    STEP_CASH,
    STEP_VENUE,
    STEP_SNAPSHOT,
    STEP_HISTORY,
    STEP_WHATIF,
    STEP_TELEGRAM,
)


class FullPreflight:
    """The full walk over an opened order path; ``telegram`` is ``None`` when not configured."""

    def __init__(
        self,
        path: OrderPath,
        *,
        telegram: Notifier | None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._path = path
        self._telegram = telegram
        self._now = now
        self._conid = path.settings.ibkr.preflight.whatif_conid
        self._offset = path.settings.ibkr.preflight.paper_order.limit_offset_fraction
        self._bid: Decimal | None = None

    async def run(self) -> PreflightReport:
        path = self._path
        client = path.client
        report = PreflightReport(planned=FULL_STEPS)
        known = (client.guard.account_id,)
        instrument = path.directory.describe(self._conid)
        if instrument is None:  # pragma: no cover - the schema ties the conid to the universe
            report.add(STEP_AUTH, False, f"no universe instrument describes conid {self._conid}")
            return report
        name = f"{instrument.symbol} ({self._conid})"

        async def auth() -> str:
            status = await client.session.auth_status()
            if not status.authenticated:
                raise AuthError("the brokerage session is not authenticated")
            return "authenticated" + (" (a competing session)" if status.competing else "")

        async def accounts() -> str:
            return f"{len(await client.accounts())} account(s) listed for this login"

        async def guard() -> str:
            client.run_guard(await client.accounts())
            return f"account {client.guard.masked} under profile {client.guard.profile}"

        async def positions() -> str:
            return f"{len(await client.portfolio.positions())} position row(s)"

        async def cash() -> str:
            ledger = await client.portfolio.ledger()
            currencies = ", ".join(balance.currency for balance in ledger.balances) or "none"
            return f"cash in {currencies}"

        async def venue() -> str:
            mic = path.calendars.venue_for_ibkr(instrument.listing_exchange)
            line = venue_line(path.calendars, mic, self._now())
            return f"{line.badge} {mic} {line.text}" + (" (blocked)" if line.blocked else "")

        async def snapshot() -> str:
            read = await path.reader.quote(self._conid, option=False)
            if read is None:
                raise AuthError(f"no quote for {name}")
            evidence = read.evidence
            self._bid = evidence.bid
            status = "unreported" if evidence.status is None else evidence.status.value
            return f"{name}: availability {status}; bid {'read' if evidence.bid else 'none'}"

        async def history() -> str:
            probe = path.settings.ibkr.preflight
            found = await client.history.bars(
                self._conid, period=probe.history_period, bar=probe.history_bar
            )
            return f"{len(found.bars)} bar(s)"

        async def whatif() -> str:
            rules = await client.contracts.rules(self._conid, is_buy=True)
            size = rules.size_increment
            bid = self._bid
            tick = None if bid is None else tick_for(rules, bid)
            limit = None if bid is None or tick is None else passive_limit(bid, self._offset, tick)
            if size is None or size <= 0 or not math.isfinite(size) or limit is None:
                raise AuthError(f"no previewable order: size {size}, bid {bid}, tick {tick}")
            terms = OrderTerms(
                conid=self._conid,
                side=OrderSide.BUY,
                quantity=Decimal(repr(size)),
                price=LimitPrice(limit=limit),
                tif=TimeInForce.DAY,
                venue=SMART_ROUTER,
            )
            preview = await path.orders.whatif(terms, coid=path.orders.coid_for(uuid.uuid7()))
            if preview.error is not None:
                raise AuthError(f"the whatif refused the order: {preview.error}")
            warning = preview.warning_code or "none"
            return (
                f"buy of {size} {instrument.symbol} at {limit} previewed, not placed: commission "
                f"{preview.commission} {preview.currency or ''}, warning {warning}"
            )

        async def telegram() -> str:
            if self._telegram is None:
                return "not configured (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID unset): skipped"
            sent = await self._telegram.send(
                "ObliskIQ preflight", "A test message from `obliskiq preflight --full`."
            )
            if not sent:
                raise AuthError("the test message was not delivered")
            return "test message delivered"

        steps = (
            (STEP_AUTH, auth),
            (STEP_ACCOUNTS, accounts),
            (STEP_GUARD, guard),
            (STEP_POSITIONS, positions),
            (STEP_CASH, cash),
            (STEP_VENUE, venue),
            (STEP_SNAPSHOT, snapshot),
            (STEP_HISTORY, history),
            (STEP_WHATIF, whatif),
            (STEP_TELEGRAM, telegram),
        )
        for step, run in steps:
            if not await run_step(report, step, run, known):
                break
        return report
