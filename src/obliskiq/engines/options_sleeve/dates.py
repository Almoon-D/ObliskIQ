"""The sleeve's date rules: the session an option is sold by, and the tenor window a thesis's
catalyst sets.

**Every option date rule counts from the last tradable session** of the option and its
underlying (``Calendars.last_tradable_session``: a holiday expiry, or a Monday expiry after a
Friday holiday, lands earlier), minus ``tenor.mandatory_close_trading_days`` common sessions
(``Calendars.session_before``), the rule the pre-trade gate applies. Lite counted the days back
from the expiry itself; the anchor here is one session more conservative on a holiday expiry
(docs/knowledge/calendars.md, "Expiry and the last tradable session"). A date the calendars cannot
judge is no date: the caller's gate is untested with the reason, never a guessed session.

**The catalyst sets the tenor.** A dated thesis's expiry clears its catalyst by
``tenor.catalyst_buffer_days``; the entry window (``tenor.entry_dte_min`` to ``entry_dte_max``)
bounds every entry, so a catalyst too far out for the window has no expiry the sleeve may buy.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from dataclasses import dataclass
from datetime import date, timedelta

from ...calendars.service import Calendars
from ...config.schema import ObliskConfig
from ...errors import CalendarError


@dataclass(frozen=True, slots=True)
class CloseBy:
    """The session an option is sold by, or why it cannot be computed (``session`` ``None``)."""

    session: date | None
    last_tradable: date | None
    reason: str


@dataclass(frozen=True, slots=True)
class TenorWindow:
    """The expiries an entry on ``as_of`` may buy: from ``earliest`` to ``latest``, both
    inclusive; empty when the catalyst's buffer lands beyond the window's end."""

    as_of: date
    earliest: date
    latest: date
    catalyst_date: date | None

    @property
    def is_empty(self) -> bool:
        return self.earliest > self.latest

    def admits(self, expiry: date) -> bool:
        return self.earliest <= expiry <= self.latest


class SleeveDates:
    """The calendar rules of the sleeve, bound to configuration once."""

    def __init__(self, settings: ObliskConfig, calendars: Calendars) -> None:
        self._calendars = calendars
        tenor = settings.tenor
        self._close_sessions = tenor.mandatory_close_trading_days
        self._entry_dte_min = tenor.entry_dte_min
        self._entry_dte_max = tenor.entry_dte_max
        self._buffer_days = tenor.catalyst_buffer_days

    def close_by(
        self,
        *,
        option_exchange: str,
        underlying_exchange: str,
        expiry: date,
        option_conid: int | None = None,
        underlying_conid: int | None = None,
    ) -> CloseBy:
        """The session the option is sold by: its last tradable session less the buffer."""
        calendars = self._calendars
        try:
            venue = calendars.venue_for_ibkr(option_exchange)
            underlying_venue = calendars.venue_for_ibkr(underlying_exchange)
            last = calendars.last_tradable_session(
                venue,
                underlying_venue,
                expiry,
                instrument_conid=option_conid,
                underlying_conid=underlying_conid,
            ).session
            session = calendars.session_before(
                venue,
                underlying_venue,
                last,
                self._close_sessions,
                instrument_conid=option_conid,
                underlying_conid=underlying_conid,
            )
        except CalendarError as exc:
            return CloseBy(None, None, f"the calendars cannot place the {expiry} expiry: {exc}")
        return CloseBy(
            session,
            last,
            f"sold by {session}: {self._close_sessions} sessions before its last tradable session "
            f"{last} on {venue} and {underlying_venue}",
        )

    def window(self, *, as_of: date, catalyst_date: date | None) -> TenorWindow:
        """The expiries an entry on ``as_of`` may buy, the catalyst's buffer included."""
        earliest = as_of + timedelta(days=self._entry_dte_min)
        if catalyst_date is not None:
            earliest = max(earliest, catalyst_date + timedelta(days=self._buffer_days))
        return TenorWindow(
            as_of=as_of,
            earliest=earliest,
            latest=as_of + timedelta(days=self._entry_dte_max),
            catalyst_date=catalyst_date,
        )
