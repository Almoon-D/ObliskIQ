"""The XBRL frames the stock screen reads, stored as read (``stock_fundamentals``).

A frame (``data.sec.gov/api/xbrl/frames/<taxonomy>/<tag>/<unit>/<period>.json``) holds one value
per company: the last one filed for the calendar period that best fits it, so a restatement
replaces the original at the SEC. Here each value is stored with the instant it was read
(``known_at``) and a value that later differs is a new row: the store is point in time from its
first run, never before it. Each run reads, for every input of ``stocks.fundamentals.inputs``,
the annual frames of the current calendar year and the ``annual_years`` - 1 before it, or the
instants of the last ``instant_quarters`` completed quarter ends. A frame the SEC has not built
yet answers HTTP 404 (``NoSuchKey``) and is no failure.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from datetime import date, datetime
from typing import Any, Final

import httpx

from ...config.schema import ObliskConfig
from ...config.schema_sources import FrameInputConfig, StockFundamentalsConfig
from ...domain.stocks import StockFact
from ...errors import UpstreamError
from ...store import stocks as stocks_repo
from ...store.worker import StoreWorker
from ...util.http import RetryPolicy
from ..insiders.edgar import absent, sec_get
from ..market.plumbing import Collection, Sleep, failure_text

STEP: Final = "stock_fundamentals"
HTTP_OK: Final = 200
MONTHS_PER_QUARTER: Final = 3
QUARTERS: Final = 4


def frame_periods(
    config: FrameInputConfig, today: date, settings: StockFundamentalsConfig
) -> list[str]:
    """The frames of one input a run on ``today`` reads, newest first."""
    if config.kind == "annual":
        return [f"CY{today.year - back}" for back in range(settings.annual_years)]
    quarter = (today.month - 1) // MONTHS_PER_QUARTER  # completed quarters this year
    found = []
    year = today.year
    for _ in range(settings.instant_quarters):
        if quarter == 0:
            year, quarter = year - 1, QUARTERS
        found.append(f"CY{year}Q{quarter}I")
        quarter -= 1
    return found


def parse_frame(body: Any, *, input_name: str, period: str, known_at: datetime) -> list[StockFact]:
    """The values a frame holds, one per company."""
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise UpstreamError(f"the {period} frame of {input_name} is not {{..., data}}")
    found: list[StockFact] = []
    for row in body["data"]:
        try:
            found.append(
                StockFact(
                    cik=int(row["cik"]),
                    input=input_name,
                    period=period,
                    period_end=date.fromisoformat(str(row["end"])),
                    value=float(row["val"]),
                    accession=str(row["accn"]),
                    known_at=known_at,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise UpstreamError(
                f"the {period} frame of {input_name} holds a row of another shape: {exc}"
            ) from None
    return found


class FundamentalsCollector:
    """Reads the screen's frames and keeps every value that is new or changed."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        client: httpx.AsyncClient,
        policy: RetryPolicy,
        worker: StoreWorker,
        user_agent: str | None,
        sleep: Sleep,
        now: Callable[[], datetime],
    ) -> None:
        self._config = settings.sources.stocks.fundamentals
        self._spacing = settings.sources.insiders.request_spacing_seconds
        self._client = client
        self._policy = policy
        self._worker = worker
        self._user_agent = user_agent
        self._sleep = sleep
        self._now = now

    async def run(self, *, dry_run: bool = False) -> Collection:
        collection = Collection(STEP)
        now = self._now()
        for name, frame in sorted(self._config.inputs.items()):
            for period in frame_periods(frame, now.date(), self._config):
                target = f"{name} {period}"
                try:
                    facts = await self._frame(name, frame, period, now)
                except Exception as exc:  # one frame never stops the others
                    collection.failed(target, failure_text(exc))
                    continue
                if facts is None:
                    collection.succeeded(target, 0, built=False)
                    continue
                saved = 0 if dry_run else await self._worker.run(stocks_repo.save_facts, facts)
                collection.succeeded(target, saved, companies=len(facts))
        return collection

    async def _frame(
        self, name: str, frame: FrameInputConfig, period: str, now: datetime
    ) -> list[StockFact] | None:
        url = self._config.frames_url_template.format(
            taxonomy=frame.taxonomy, tag=frame.tag, unit=frame.unit, period=period
        )
        response = await sec_get(
            self._client,
            url,
            policy=self._policy,
            user_agent=self._user_agent,
            spacing=self._spacing,
            sleep=self._sleep,
        )
        if absent(response):
            return None
        if response.status_code != HTTP_OK:
            raise UpstreamError(f"answered HTTP {response.status_code}")
        return parse_frame(response.json(), input_name=name, period=period, known_at=now)
