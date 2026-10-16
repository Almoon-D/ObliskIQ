"""The pre-trade gate's market checks on the fake broker: the quote policy per asset class, the
collar's widening, and the calendars' act-now view (DST, incidents, the date-critical window of
an option), with the alert an option close refused for want of a real-time quote raises.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from order_rig import EUR_ETF, OPEN, OPTION, STOCK, VENUES, whatif_answer

from obliskiq.calendars.incidents import IncidentBook
from obliskiq.calendars.service import Calendars
from obliskiq.domain.calendar import Incident, IncidentScope, IncidentSource, SessionObservation
from obliskiq.domain.orders import OrderSide, PretradeClearance
from obliskiq.execution.boundary import OrderAlertKind
from obliskiq.execution.pretrade import PretradeRefusal


@pytest.fixture
def rig(order_rig: Any) -> Any:
    order_rig.ready()
    return order_rig


async def codes(rig: Any, stored: Any) -> tuple[str, ...]:
    answer = await rig.gate.clear(stored.intent)
    return () if isinstance(answer, PretradeClearance) else answer.codes


def millis(at: datetime) -> int:
    return int(at.timestamp() * 1000)


def with_quote_policy(settings: Any, **changes: Any) -> Any:
    rules = [
        rule.model_copy(update=changes) if rule.policy == "realtime_preferred" else rule
        for rule in settings.execution.quote_policy
    ]
    execution = settings.execution.model_copy(update={"quote_policy": rules})
    return settings.model_copy(update={"execution": execution})


# ── the quote policy ──


async def test_a_stale_quote_refuses_and_a_fresh_one_clears(rig: Any) -> None:
    stored = await rig.staged()
    rig.broker.quote(STOCK, updated_ms=millis(OPEN - timedelta(seconds=61)))
    assert await codes(rig, stored) == ("quote_policy",)
    rig.broker.quote(STOCK, updated_ms=millis(OPEN - timedelta(seconds=59)))
    assert await codes(rig, stored) == ()


async def test_a_quote_stamped_ahead_of_the_clock_is_read_at_the_clock(rig: Any) -> None:
    rig.broker.quote(STOCK, updated_ms=millis(OPEN + timedelta(seconds=5)))
    clearance = await rig.gate.clear((await rig.staged()).intent)
    assert isinstance(clearance, PretradeClearance)
    assert clearance.quote is not None
    assert clearance.quote.observed_at == OPEN


async def test_a_delayed_quote_beyond_the_maximum_delay_refuses(rig: Any) -> None:
    stored = await rig.staged()
    rig.broker.quote(STOCK, availability="DP")  # delayed: IBKR's documented 1200 s bound
    clearance = await rig.gate.clear(stored.intent)
    assert isinstance(clearance, PretradeClearance)  # within lite's 1200 s maximum
    assert clearance.quote is not None
    assert clearance.quote.delay_seconds == 1200
    rig.make_gate(settings=with_quote_policy(rig.settings, max_delay_seconds=600))
    answer = await rig.gate.clear(stored.intent)
    assert isinstance(answer, PretradeRefusal)
    assert answer.codes == ("quote_policy",)
    assert "beyond 600 s" in answer.reason


async def test_a_delayed_quote_widens_the_collar_with_its_sigma(rig: Any) -> None:
    reaching = await rig.staged(limit="100.8")  # past the real-time 0.5 %, inside the widened one
    rig.broker.quote(STOCK, availability="DP")
    assert await codes(rig, reaching) == ()
    rig.broker.quote(STOCK, availability="DP", sigma=None)
    answer = await rig.gate.clear(reaching.intent)
    assert isinstance(answer, PretradeRefusal)
    assert answer.codes == ("collar",)
    assert "needs a volatility" in answer.reason
    rig.broker.quote(STOCK)
    assert await codes(rig, reaching) == ("collar",)  # real-time: the base collar alone


async def test_a_frozen_quote_refuses(rig: Any) -> None:
    rig.broker.quote(STOCK, availability="ZP")
    assert await codes(rig, await rig.staged()) == ("quote_policy",)


async def test_an_option_without_a_real_time_quote_on_the_option_refuses(rig: Any) -> None:
    rig.broker.whatif_answer = whatif_answer("501.00", "USD")
    buy = await rig.staged(OPTION, quantity="1", limit="5")
    rig.broker.quote(OPTION, bid="4.9", ask="5.1", last="5", option=True, availability="DP")
    answer = await rig.gate.clear(buy.intent)
    assert isinstance(answer, PretradeRefusal)
    assert answer.codes == ("quote_policy",)
    assert "requires a real-time quote" in answer.reason
    rig.broker.quote(OPTION, bid="4.9", ask="5.1", last="5", option=True)
    clearance = await rig.gate.clear(buy.intent)
    assert isinstance(clearance, PretradeClearance)
    assert clearance.underlying_quote is not None
    assert clearance.underlying_quote.conid == STOCK
    assert rig.alerts == []  # a refused buy is not an option close


async def test_an_option_without_a_real_time_quote_on_its_underlying_refuses(rig: Any) -> None:
    rig.broker.whatif_answer = whatif_answer("501.00", "USD")
    buy = await rig.staged(OPTION, quantity="1", limit="5")
    rig.broker.quote(STOCK, availability="DP")
    answer = await rig.gate.clear(buy.intent)
    assert isinstance(answer, PretradeRefusal)
    assert answer.codes == ("quote_policy",)
    assert "underlying" in answer.reason
    del rig.broker.quotes[STOCK]
    answer = await rig.gate.clear(buy.intent)
    assert isinstance(answer, PretradeRefusal)
    assert "no quote for the underlying" in answer.reason


async def test_an_option_close_without_a_real_time_quote_is_refused_and_alerts(rig: Any) -> None:
    await rig.hold(OPTION, "1")
    close = await rig.staged(OPTION, side=OrderSide.SELL, quantity="1", limit="4.98")
    rig.broker.quote(OPTION, bid="4.9", ask="5.1", last="5", option=True, availability="DP")
    answer = await rig.gate.clear(close.intent)
    assert isinstance(answer, PretradeRefusal)
    assert answer.codes == ("quote_policy",)
    (alert,) = rig.alerts
    assert alert.kind is OrderAlertKind.OPTION_CLOSE_BLOCKED
    assert alert.intent_id == close.intent.intent_id
    assert "may be exercised" in alert.detail
    rig.broker.unavailable.add("snapshot")
    await rig.gate.clear(close.intent)
    assert len(rig.alerts) == 2  # an unreadable quote is no real-time quote either
    rig.broker.unavailable.clear()
    rig.broker.quote(OPTION, bid="4.9", ask="5.1", last="5", option=True)
    assert isinstance(await rig.gate.clear(close.intent), PretradeClearance)
    assert len(rig.alerts) == 2


async def test_a_failing_alert_hook_does_not_change_the_refusal(rig: Any) -> None:
    def broken(alert: Any) -> None:
        raise RuntimeError("telegram down")

    rig.gate._alert = broken
    await rig.hold(OPTION, "1")
    close = await rig.staged(OPTION, side=OrderSide.SELL, quantity="1", limit="4.98")
    rig.broker.quote(OPTION, bid="4.9", ask="5.1", last="5", option=True, availability="DP")
    answer = await rig.gate.clear(close.intent)
    assert isinstance(answer, PretradeRefusal)
    assert answer.codes == ("quote_policy",)


async def test_an_option_close_alerts_without_a_hook_too(rig: Any) -> None:
    rig.gate._alert = None
    await rig.hold(OPTION, "1")
    close = await rig.staged(OPTION, side=OrderSide.SELL, quantity="1", limit="4.98")
    rig.broker.quote(OPTION, bid="4.9", ask="5.1", last="5", option=True, availability="DP")
    assert isinstance(await rig.gate.clear(close.intent), PretradeRefusal)


# ── the calendars ──


async def test_the_dst_change_moves_the_xetra_window_in_utc(rig: Any) -> None:
    """Europe leaves summer time on 2026-10-25: the Xetra window opens at 07:30 UTC on the
    Friday before and at 08:30 UTC on the Monday after (09:30 local both days)."""
    rig.broker.whatif_answer = whatif_answer("101.00", "EUR")
    friday = datetime(2026, 10, 23, 7, 45, tzinfo=UTC)
    monday = datetime(2026, 10, 26, 7, 45, tzinfo=UTC)
    rig.clock.now = friday
    assert await codes(rig, await rig.staged(EUR_ETF, quantity="1")) == ()
    rig.clock.now = monday
    stored = await rig.staged(EUR_ETF, quantity="1")
    answer = await rig.gate.clear(stored.intent)
    assert isinstance(answer, PretradeRefusal)
    assert answer.codes == ("venue_not_tradable",)
    assert "XETR" in answer.reason
    assert "outside_window" in answer.reason
    rig.clock.now = monday + timedelta(hours=1)
    assert await codes(rig, stored) == ()


async def test_the_window_edges_are_half_open(rig: Any) -> None:
    rig.broker.whatif_answer = whatif_answer("101.00", "EUR")
    opens = datetime(2026, 10, 26, 8, 30, tzinfo=UTC)
    rig.clock.now = opens
    stored = await rig.staged(EUR_ETF, quantity="1")
    assert await codes(rig, stored) == ()
    rig.clock.now = opens - timedelta(seconds=1)
    assert await codes(rig, stored) == ("venue_not_tradable",)


async def test_a_venue_blocked_by_an_incident_refuses(rig: Any, build_calendars: Any) -> None:
    halt = Incident(
        incident_id="nyse-halt",
        origin="manual-incidents.yaml",
        source=IncidentSource.MANUAL,
        scope=IncidentScope.VENUE,
        venue="XNYS",
        conid=None,
        start=OPEN - timedelta(minutes=10),
        expected_end=OPEN + timedelta(hours=1),
        reason="exchange-wide halt",
    )
    rig.make_gate(calendars=build_calendars(VENUES, incidents=IncidentBook((halt,))))
    answer = await rig.gate.clear((await rig.staged()).intent)
    assert isinstance(answer, PretradeRefusal)
    assert answer.codes == ("venue_not_tradable",)
    assert "incident_overlap" in answer.reason
    assert "exchange-wide halt" in answer.reason
    rig.broker.whatif_answer = whatif_answer("101.00", "EUR")
    assert await codes(rig, await rig.staged(EUR_ETF, quantity="1")) == ()  # Xetra trades


class Disagreeing:
    """A schedule source that says New York is closed on the order's day."""

    name = "ibkr"

    def __init__(self, day: date) -> None:
        self.day = day

    def observe(self, venue: str, day: date, conid: int | None) -> SessionObservation | None:
        if venue == "XNYS" and day == self.day:
            return SessionObservation(
                venue=venue, day=day, source=self.name, is_session=False, open=None, close=None
            )
        return None


async def test_a_disagreement_blocks_an_option_near_its_last_session_not_a_stock(
    rig: Any, shipped_settings: Any
) -> None:
    calendars = Calendars.from_settings(
        shipped_settings,
        reference=date(2026, 10, 9),
        venues=VENUES,
        sources=[Disagreeing(date(2026, 10, 13))],
    )
    rig.make_gate(calendars=calendars)
    assert await codes(rig, await rig.staged()) == ()  # a stock order is not date-critical
    rig.broker.whatif_answer = whatif_answer("501.00", "USD")
    far = await rig.staged(OPTION, quantity="1", limit="5")
    assert await codes(rig, far) == ()  # far from its last session: not date-critical either
    near = rig.directory.instruments[OPTION].model_copy(update={"expiry": date(2026, 10, 16)})
    rig.directory.instruments[OPTION] = near
    allowlist = [
        near if i.conid == OPTION else i for i in rig.settings.eligibility.skeleton_allowlist
    ]
    eligibility = rig.settings.eligibility.model_copy(update={"skeleton_allowlist": allowlist})
    rig.make_gate(
        calendars=calendars, settings=rig.settings.model_copy(update={"eligibility": eligibility})
    )
    answer = await rig.gate.clear(far.intent)
    assert isinstance(answer, PretradeRefusal)
    assert set(answer.codes) == {"venue_not_tradable"}
    assert "source_disagreement" in answer.reason  # on the option's venue and the underlying's


async def test_an_option_outside_the_windows_its_venues_share_refuses(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.broker.whatif_answer = whatif_answer("501.00", "USD")
    buy = await rig.staged(OPTION, quantity="1", limit="5")
    monkeypatch.setattr(rig.calendars, "common_windows", lambda _day, *_legs: ())
    answer = await rig.gate.clear(buy.intent)
    assert isinstance(answer, PretradeRefusal)
    assert answer.codes == ("venue_not_tradable",)
    assert "share" in answer.reason
    monkeypatch.undo()
    assert await codes(rig, buy) == ()


async def test_an_unmapped_exchange_code_refuses(rig: Any) -> None:
    rig.directory.instruments[STOCK] = rig.directory.instruments[STOCK].model_copy(
        update={"listing_exchange": "NOWHERE"}
    )
    answer = await rig.gate.clear((await rig.staged()).intent)
    assert isinstance(answer, PretradeRefusal)
    assert "venue_not_tradable" in answer.codes
    assert "cannot judge" in answer.reason
