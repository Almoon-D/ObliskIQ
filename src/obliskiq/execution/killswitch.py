"""The kill switch: a file, a database flag and ``obliskiq kill on|off`` (MASTER_PLAN §5.12).

It must work when everything else fails, so (DECISIONS D48):

* **Two flags, either one turns it on.** The file ``KILL_SWITCH`` in the data directory and the
  latest row of the ``killswitch`` table. A file that cannot be checked, a database that cannot
  be read in ``execution.kill_switch.store_timeout_seconds`` or any other failure reads as ON.
  The file's name is fixed in code, not configuration, so ``obliskiq kill on`` works when the
  configuration does not load; it then sets the file alone.
* **What ON does (I4).** It blocks every placement or modify that adds risk, checked at the
  write boundary immediately before the send. Its sweep cancels every working order that adds
  risk, whatever its role, and keeps the sells that reduce it: a buy or a conversion is
  cancelled; a sell is kept unless the store knows it as a conversion, a buy or an order the gate
  cleared as adding risk. An order the store cannot classify — the store unreadable, or an order
  the bot did not place — is classed from the broker's live order alone (a buy, a conversion or
  an order of unreadable side is cancelled, a sell kept). What the broker's order alone shows
  adds risk is cancelled before the store is read, and after one failed store read the
  remaining sells are classed from the broker, so a hung store delays no cancel of a buy. A
  live-order row that cannot be read at all is cancelled by its id, and one without an id is
  reported; one bad row never stops the others. While ON the sweep repeats every
  ``sweep_interval_seconds``, so an order in flight at the switch, or listed late, is caught; no
  failure, a failing report hook included, ends it. Cancels need no clearance and run in every
  mode; only the paper guard applies.
* **Turning it off** clears both flags and fails loudly if either stays set.

``flatten`` (closing the managed positions) is a separate command (Session 4a).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from ..config.schema import ObliskConfig
from ..domain.orders import BrokerOrder, KillSwitchRecord, OrderSide, RiskEffect
from ..errors import ExecutionError
from ..ibkr.errors import IbkrError
from ..ibkr.orders import OrderClient, is_live
from ..logging_setup import get_logger
from ..store import orders as repo
from ..store.worker import StoreWorker
from ..util.timeutil import utc_now, utc_stamp

log = get_logger("execution.killswitch")

#: The kill switch's file in the data directory. Fixed, so it can be set without configuration.
KILL_SWITCH_FILE: Final = "KILL_SWITCH"

#: IBKR's security type for a currency conversion.
CONVERSION_SEC_TYPE = "CASH"


def kill_switch_path(data_dir: Path) -> Path:
    return data_dir / KILL_SWITCH_FILE


def file_flag_on(data_dir: Path) -> bool:
    """Whether the file flag is set; a file that cannot be checked counts as set.

    ``Path.exists`` would read an unreadable entry (a permission refusal, a symlink loop) as
    absent, and ``stat`` a dangling symlink, so the entry itself is ``lstat``-ed: only a missing
    entry reads as off.
    """
    try:
        kill_switch_path(data_dir).lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def set_file_flag(data_dir: Path, *, actor: str, reason: str, at: datetime) -> None:
    """Write the file flag (atomically: a partial file is never the only record)."""
    path = kill_switch_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{KILL_SWITCH_FILE}.tmp")
    staging.write_text(
        json.dumps({"at": utc_stamp(at), "actor": actor, "reason": reason}) + "\n",
        encoding="utf-8",
    )
    staging.replace(path)


def clear_file_flag(data_dir: Path) -> None:
    kill_switch_path(data_dir).unlink(missing_ok=True)


def adds_risk_at_broker(order: BrokerOrder) -> bool:
    """What the broker's live order alone shows: anything but a sell of a security adds risk."""
    return order.side is not OrderSide.SELL or (order.sec_type or "").upper() == CONVERSION_SEC_TYPE


@dataclass(frozen=True, slots=True)
class SweepReport:
    """One pass of the sweep over the broker's live orders."""

    listed: int = 0
    cancelled: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    classed_by_broker: tuple[str, ...] = ()
    error: str | None = None


class KillSwitch:
    """The switch's two flags, the check the write boundary makes, and the sweep."""

    def __init__(
        self,
        data_dir: Path,
        worker: StoreWorker,
        settings: ObliskConfig,
        *,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._data_dir = data_dir
        self._worker = worker
        config = settings.execution.kill_switch
        self._timeout = config.store_timeout_seconds
        self._interval = config.sweep_interval_seconds
        self._now = now

    async def _stored(self) -> KillSwitchRecord | None:
        return await asyncio.wait_for(
            self._worker.run(repo.latest_kill_switch), timeout=self._timeout
        )

    async def is_on(self) -> bool:
        """ON when either flag says so or either cannot be read."""
        try:
            if await asyncio.to_thread(file_flag_on, self._data_dir):
                return True
            record = await self._stored()
        except Exception as exc:
            log.warning(
                "the kill switch could not be read; it counts as on", extra={"error": str(exc)}
            )
            return True
        return record is not None and record.on

    async def turn_on(self, *, actor: str, reason: str) -> tuple[str, ...]:
        """Set both flags. Returns what could not be set; the switch is on while either holds."""
        at = self._now()
        problems: list[str] = []
        file_set = database_set = True
        try:
            await asyncio.to_thread(
                set_file_flag, self._data_dir, actor=actor, reason=reason, at=at
            )
        except OSError as exc:
            file_set = False
            problems.append(f"the file flag could not be written ({exc})")
        try:
            await asyncio.wait_for(
                self._worker.run(
                    repo.record_kill_switch,
                    KillSwitchRecord(at=at, on=True, actor=actor, reason=reason),
                ),
                timeout=self._timeout,
            )
        except Exception as exc:
            database_set = False
            problems.append(f"the database flag could not be written ({type(exc).__name__})")
        if not (file_set or database_set):
            raise ExecutionError("the kill switch could not be set: " + "; ".join(problems))
        return tuple(problems)

    async def turn_off(self, *, actor: str, reason: str) -> None:
        """Clear both flags; raises while either stays set."""
        await self._worker.run(
            repo.record_kill_switch,
            KillSwitchRecord(at=self._now(), on=False, actor=actor, reason=reason),
        )
        await asyncio.to_thread(clear_file_flag, self._data_dir)
        if await self.is_on():
            raise ExecutionError("the kill switch is still on after turning it off")

    async def _store_verdict(self, order: BrokerOrder) -> bool | None:
        """Whether a sell at the broker adds risk by the store's record of its intent; ``None``
        when the store holds no record that says. Raises when the store cannot be read."""
        if order.coid is None:
            return None
        stored = await asyncio.wait_for(
            self._worker.run(repo.intent_by_coid, order.coid), timeout=self._timeout
        )
        if stored is None:
            return None
        intent = stored.intent
        if intent.terms.conversion or intent.side is not OrderSide.SELL:
            return True
        effect = await asyncio.wait_for(
            self._worker.run(repo.spent_effect, intent.intent_id), timeout=self._timeout
        )
        return None if effect is None else effect is RiskEffect.ADDS

    async def _cancel(self, client: OrderClient, order_id: str) -> str | None:
        """Cancel one order; the reason it failed, or ``None``."""
        try:
            answer = await client.cancel(order_id)
        except Exception as exc:  # any failure is reported; the next sweep tries again
            return f"{order_id} ({type(exc).__name__})"
        return None if answer.error is None else f"{order_id} ({answer.error})"

    async def _cancel_each(
        self, client: OrderClient, order_ids: list[str], cancelled: list[str], failed: list[str]
    ) -> None:
        for order_id in order_ids:
            failure = await self._cancel(client, order_id)
            if failure is None:
                cancelled.append(order_id)
            else:
                failed.append(failure)

    async def sweep(self, client: OrderClient) -> SweepReport:
        """Cancel every live order that adds risk; keep the sells that reduce it."""
        try:
            live = await client.live_order_rows()
        except IbkrError as exc:
            return SweepReport(error=f"the live orders could not be read ({type(exc).__name__})")
        cancelled: list[str] = []
        kept: list[str] = []
        failed: list[str] = []
        working = [order for order in live.orders if is_live(order)]
        at_broker = [order.order_id for order in working if adds_risk_at_broker(order)]
        for order_id in live.unreadable:
            if order_id is None:
                failed.append("a live order whose id cannot be read")
            else:
                at_broker.append(order_id)  # nothing proves it reduces risk
        by_broker = list(at_broker)
        # What the broker alone shows adds risk is cancelled before the store is read, so a
        # hung store delays none of it.
        await self._cancel_each(client, at_broker, cancelled, failed)
        by_store: list[str] = []
        store_up = True
        for order in working:
            if adds_risk_at_broker(order):
                continue
            verdict = None
            if store_up:
                try:
                    verdict = await self._store_verdict(order)
                except Exception:
                    store_up = False  # the remaining sells are classed from the broker: kept
            if verdict is None:
                by_broker.append(order.order_id)
            (by_store if verdict else kept).append(order.order_id)
        await self._cancel_each(client, by_store, cancelled, failed)
        report = SweepReport(
            listed=len(working) + len(live.unreadable),
            cancelled=tuple(cancelled),
            kept=tuple(kept),
            failed=tuple(failed),
            classed_by_broker=tuple(by_broker),
        )
        log.warning(
            "kill switch sweep",
            extra={"cancelled": len(cancelled), "kept": len(kept), "failed": len(failed)},
        )
        return report

    async def _pass(
        self, client: OrderClient, on_report: Callable[[SweepReport], None] | None
    ) -> None:
        if not await self.is_on():
            return
        report = await self.sweep(client)
        if on_report is None:
            return
        try:
            on_report(report)
        except Exception:
            log.exception("the kill switch's report hook failed; the sweep goes on")

    async def run(
        self,
        client: OrderClient,
        stop: asyncio.Event,
        *,
        on_report: Callable[[SweepReport], None] | None = None,
    ) -> None:
        """Sweep while the switch is on, every ``sweep_interval_seconds``, until ``stop``.

        A pass that fails for any reason is logged and the next one runs on schedule.
        """
        while not stop.is_set():
            try:
                await self._pass(client, on_report)
            except Exception:
                log.exception("a kill switch pass failed; the next one runs on schedule")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._interval)
