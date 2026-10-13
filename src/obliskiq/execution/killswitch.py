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
  an order of unreadable side is cancelled, a sell kept). While ON the sweep repeats every
  ``sweep_interval_seconds``, so an order in flight at the switch, or listed late, is caught.
  Cancels need no clearance and run in every mode; only the paper guard applies.
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
from ..domain.orders import BrokerOrder, IntentRole, KillSwitchRecord, OrderSide, RiskEffect
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
    """Whether the file flag is set; a file that cannot be checked counts as set."""
    try:
        return kill_switch_path(data_dir).exists()
    except OSError:
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

    async def _cancel_verdict(self, order: BrokerOrder) -> tuple[bool, bool]:
        """(cancel it, classed from the broker alone) for one live order."""
        if (
            order.side is not OrderSide.SELL
            or (order.sec_type or "").upper() == CONVERSION_SEC_TYPE
        ):
            return True, True
        if order.coid is None:
            return False, True
        try:
            stored = await asyncio.wait_for(
                self._worker.run(repo.intent_by_coid, order.coid), timeout=self._timeout
            )
            if stored is None:
                return False, True
            effect = await asyncio.wait_for(
                self._worker.run(repo.spent_effect, stored.intent.intent_id),
                timeout=self._timeout,
            )
        except Exception:
            return False, True  # the store is unavailable: a sell at the broker is kept
        intent = stored.intent
        if intent.role is IntentRole.CONVERSION or intent.side is not OrderSide.SELL:
            return True, False
        if effect is None:
            return False, True
        return effect is RiskEffect.ADDS, False

    async def sweep(self, client: OrderClient) -> SweepReport:
        """Cancel every live order that adds risk; keep the sells that reduce it."""
        try:
            live = await client.live_orders()
        except IbkrError as exc:
            return SweepReport(error=f"the live orders could not be read ({type(exc).__name__})")
        cancelled: list[str] = []
        kept: list[str] = []
        failed: list[str] = []
        by_broker: list[str] = []
        working = [order for order in live.orders if is_live(order)]
        for order in working:
            cancel, broker_only = await self._cancel_verdict(order)
            if broker_only:
                by_broker.append(order.order_id)
            if not cancel:
                kept.append(order.order_id)
                continue
            try:
                answer = await client.cancel(order.order_id)
            except IbkrError as exc:
                failed.append(f"{order.order_id} ({type(exc).__name__})")
                continue
            if answer.error is not None:
                failed.append(f"{order.order_id} ({answer.error})")
            else:
                cancelled.append(order.order_id)
        report = SweepReport(
            listed=len(working),
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

    async def run(
        self,
        client: OrderClient,
        stop: asyncio.Event,
        *,
        on_report: Callable[[SweepReport], None] | None = None,
    ) -> None:
        """Sweep while the switch is on, every ``sweep_interval_seconds``, until ``stop``."""
        while not stop.is_set():
            if await self.is_on():
                report = await self.sweep(client)
                if on_report is not None:
                    on_report(report)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._interval)
