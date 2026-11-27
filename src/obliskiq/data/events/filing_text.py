"""A listed candidate's own filings, read for the analyst's dossier (MASTER_PLAN §5.5; DECISIONS
D189, D200).

For a candidate the store knows as a listed company (its ticker in a listing snapshot by the
sheet's date), the dossier reads the sections of its latest annual report (business, risk factors,
management's discussion), of its latest quarterly report (management's discussion, risk factors)
and its current reports of the last ``dossier_text.lookback_days`` (at most
``max_current_reports``, newest first), each cut to ``max_chars``. The SEC's submissions file
names them; each document is fetched once and kept as read, by accession and section
(``filing_texts``). A heading a report does not hold is ``not_found``; a document that cannot be
fetched or read is a ``failed`` line, never an empty section, and is asked again by the next
dossier.

Point in time: documents are fetched only on the sheet's own day (the submissions file is today's);
a dossier of an earlier date reads what was kept by its end, and what was read after it changes
nothing.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any, Final

import httpx

from ...config.schema import ObliskConfig
from ...config.schema_llm import DossierTextConfig
from ...domain.filings import FilingText
from ...errors import UpstreamError
from ...store import filings as filings_repo
from ...store import stocks as stocks_repo
from ...store.db import Store
from ...store.worker import StoreWorker
from ...util.http import RetryPolicy
from ..market.plumbing import failure_text
from ..sec import sec_get
from .filing_parse import folder, html_text, report_sections
from .filings import SOURCE

HTTP_OK: Final = 200
#: The whole text of a current report is its one section.
BODY: Final = "body"
#: The headings of an annual and of a quarterly report's sections: the SEC's form structure.
ANNUAL_SECTIONS: Final[Mapping[str, tuple[str, str]]] = {
    "business": ("1", "Business"),
    "risk_factors": ("1A", "Risk Factors"),
    "management_discussion": ("7", "Management"),
}
QUARTERLY_SECTIONS: Final[Mapping[str, tuple[str, str]]] = {
    "management_discussion": ("2", "Management"),
    "risk_factors": ("1A", "Risk Factors"),
}


def _recent(body: Any) -> list[dict[str, str]]:
    """The submissions file's recent filings as rows (form, accession, day, document)."""
    try:
        recent = body["filings"]["recent"]
        columns = ("form", "accessionNumber", "filingDate", "primaryDocument")
        values = [recent[name] for name in columns]
    except KeyError, TypeError:
        raise UpstreamError("the submissions file lists no recent filings") from None
    return [dict(zip(columns, row, strict=True)) for row in zip(*values, strict=True)]


def chosen_filings(
    rows: Sequence[Mapping[str, str]],
    *,
    as_of: date,
    annual: str,
    quarterly: str,
    current: str,
    since: date,
    most: int,
) -> list[Mapping[str, str]]:
    """The latest annual and quarterly reports filed by ``as_of`` and the current reports filed
    from ``since``, newest first, at most ``most``."""
    dated = sorted(
        (row for row in rows if date.fromisoformat(row["filingDate"]) <= as_of),
        key=lambda row: row["filingDate"],
        reverse=True,
    )
    found: list[Mapping[str, str]] = []
    for form in (annual, quarterly):
        latest = next((row for row in dated if row["form"] == form), None)
        if latest is not None:
            found.append(latest)
    found += [
        row
        for row in dated
        if row["form"] == current and date.fromisoformat(row["filingDate"]) >= since
    ][:most]
    return found


class CompanyTexts:
    """Reads and keeps a listed candidate's report sections; the analyst's dossier calls it."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        client: httpx.AsyncClient,
        policy: RetryPolicy,
        worker: StoreWorker,
        user_agent: str | None,
        now: Callable[[], datetime],
        sleep: Callable[[float], Any],
    ) -> None:
        self._config = settings.llm.analyst.dossier_text
        self._submissions = settings.sources.stocks.listings.submissions_url_template
        self._document = settings.sources.filings.document_url_template
        self._max_bytes = settings.sources.filings.max_document_bytes
        self._spacing = settings.sources.insiders.request_spacing_seconds
        self._client = client
        self._policy = policy
        self._worker = worker
        self._user_agent = user_agent
        self._now = now
        self._sleep = sleep

    async def _get(self, url: str) -> httpx.Response:
        return await sec_get(
            self._client,
            url,
            policy=self._policy,
            user_agent=self._user_agent,
            spacing=self._spacing,
            sleep=self._sleep,
        )

    def _wanted(self, form: str) -> Mapping[str, tuple[str, str]] | None:
        if form == self._config.annual_form:
            return ANNUAL_SECTIONS
        if form == self._config.quarterly_form:
            return QUARTERLY_SECTIONS
        return None

    async def __call__(self, symbol: str, as_of: date) -> list[str]:
        """The dossier's lines of ``symbol``'s filings as of ``as_of``: one per section read, or
        one status line saying why there are none."""
        cik = await self._worker.run(_cik_of, symbol, as_of=as_of)
        if cik is None:
            return [f"{symbol}: no filings read, it is not a listed company the SEC's file names"]
        failed: list[str] = []
        if as_of == self._now().date():
            failed = await self._fetch(cik, as_of)
        held = await self._worker.run(_held_texts, cik, as_of=as_of, config=self._config)
        lines = [_render(text) for text in held] + failed
        return lines or [f"{symbol}: no report of the company was read by {as_of.isoformat()}"]

    async def _fetch(self, cik: int, as_of: date) -> list[str]:
        """Read the chosen documents not kept yet; returns a line for each that failed."""
        config = self._config
        try:
            response = await self._get(self._submissions.format(cik=cik))
            if response.status_code != HTTP_OK:
                raise UpstreamError(f"answered HTTP {response.status_code}")
            rows = _recent(response.json())
        except Exception as exc:  # the dossier stands; its line says why
            return [f"the SEC's submissions file of CIK {cik}: failed ({failure_text(exc)})"]
        chosen = chosen_filings(
            rows,
            as_of=as_of,
            annual=config.annual_form,
            quarterly=config.quarterly_form,
            current=config.current_form,
            since=as_of - timedelta(days=config.lookback_days),
            most=config.max_current_reports,
        )
        failed: list[str] = []
        for row in chosen:
            accession = row["accessionNumber"]
            if await self._worker.run(filings_repo.texts, accession, as_of=as_of):
                continue
            try:
                texts = await self._read(cik, row)
            except Exception as exc:  # one document never stops the others; asked again
                failed.append(
                    f"{row['form']} filed {row['filingDate']} ({accession}): failed "
                    f"({failure_text(exc)})"
                )
                continue
            await self._worker.run(filings_repo.save_texts, texts)
        return failed

    async def _read(self, cik: int, row: Mapping[str, str]) -> list[FilingText]:
        accession, form, name = row["accessionNumber"], row["form"], row["primaryDocument"]
        url = self._document.format(cik=cik, folder=folder(accession), name=name)
        response = await self._get(url)
        if response.status_code != HTTP_OK:
            raise UpstreamError(f"{name} answered HTTP {response.status_code}")
        if len(response.content) > self._max_bytes:
            raise UpstreamError(f"{name} is larger than {self._max_bytes} bytes")
        text = html_text(response.text)
        wanted = self._wanted(form)
        sections: Mapping[str, str | None] = (
            {BODY: text or None} if wanted is None else report_sections(text, wanted)
        )
        filed = date.fromisoformat(row["filingDate"])
        now = self._now()
        return [
            FilingText(
                accession=accession,
                cik=cik,
                form=form,
                filed=filed,
                section=section,
                status="ok" if found else "not_found",
                text=found[: self._config.max_chars] if found else None,
                detail=None if found else f"{name} holds no heading of it",
                known_at=now,
            )
            for section, found in sections.items()
        ]


def _cik_of(store: Store, symbol: str, *, as_of: date) -> int | None:
    return stocks_repo.symbol_ciks(store, as_of=as_of).get(symbol)


def _held_texts(
    store: Store, cik: int, *, as_of: date, config: DossierTextConfig
) -> list[FilingText]:
    """The kept sections of ``cik``'s latest annual and quarterly reports and of its current
    reports in the window, as of ``as_of``."""
    held = filings_repo.company_texts(store, cik, as_of=as_of)
    by_filing: dict[str, list[FilingText]] = {}
    for text in held:
        by_filing.setdefault(text.accession, []).append(text)
    rows = [
        {
            "form": texts[0].form,
            "accessionNumber": accession,
            "filingDate": texts[0].filed.isoformat(),
            "primaryDocument": "",
        }
        for accession, texts in by_filing.items()
    ]
    chosen = chosen_filings(
        rows,
        as_of=as_of,
        annual=config.annual_form,
        quarterly=config.quarterly_form,
        current=config.current_form,
        since=as_of - timedelta(days=config.lookback_days),
        most=config.max_current_reports,
    )
    return [text for row in chosen for text in by_filing[row["accessionNumber"]]]


def _render(text: FilingText) -> str:
    head = f"{text.form} filed {text.filed.isoformat()} ({text.accession}), {text.section}"
    if text.status == "ok":
        return f"{head}: {text.text}"
    return f"{head}: {text.status} ({text.detail})"


def company_texts(
    settings: ObliskConfig,
    *,
    client: httpx.AsyncClient,
    worker: StoreWorker,
    secrets: Callable[[str], str | None],
    now: Callable[[], datetime],
    sleep: Callable[[float], Any],
) -> CompanyTexts:
    """The dossier's reader, with the events source's credential (``SEC_USER_AGENT``)."""
    credential = settings.sources.provider(SOURCE).credential
    return CompanyTexts(
        settings,
        client=client,
        policy=RetryPolicy.from_settings(settings.http),
        worker=worker,
        user_agent=None if credential is None else secrets(credential),
        now=now,
        sleep=sleep,
    )
