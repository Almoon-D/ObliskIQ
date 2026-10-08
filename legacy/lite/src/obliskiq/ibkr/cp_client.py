"""Client Portal Web API endpoint layer.

Ported from IBKR-Portmanteau, with the auth layer beneath it replaced (see
``oauth.py``). The endpoint methods themselves keep the behaviour that was worth
keeping — in particular the exact-ticker-match-before-fallback rule in
:meth:`search_contracts`, which is the difference between resolving ``BP`` to BP
plc and to whatever the API happens to rank first.

Ported unchanged in substance
-----------------------------
* ``/iserver/secdef/search`` with exact-ticker-match-before-fallback.
* ``/iserver/marketdata/snapshot``.
* ``/portfolio/accounts``, ``/portfolio/{account}/ledger``,
  ``/portfolio/{account}/positions/{page}``, ``/iserver/contract/{conid}/info``.

Written new, because the original had nothing to port
-----------------------------------------------------
* ``/iserver/marketdata/history`` — named in the port brief but absent from
  IBKR-Portmanteau. All of its historical bars came from Yahoo Finance's v8 chart
  endpoint, not from IBKR.
* Options chain retrieval — likewise. The original pulled chains from Yahoo's
  crumb-gated v7 options endpoint. Chains here come from
  ``/iserver/secdef/strikes`` and ``/iserver/secdef/info``, which is the only route
  that yields IBKR conids, and conids are what the snapshot and history endpoints
  take.

Deliberately not ported
-----------------------
* ``query_ibkr_endpoint``, the arbitrary-endpoint passthrough. It guarded writes
  with a substring blocklist (``"order"``, ``"trade"``, ``"cancel"`` …), which
  fails open: ``/iserver/reply/{id}``, the endpoint that confirms a live order,
  contains none of those words. An LLM-reachable tool that can reach any endpoint
  on a funded brokerage account needs an allowlist, not a blocklist, and this
  project stages orders through named tools with explicit confirmation instead.
* Yahoo, Binance and CoinGecko fallbacks. Out of scope, and unusable unattended.

.. note::
   **RUN LIVE, and this module carries its own receipts.** It was built against
   fixtures in a sandbox and this header went on saying so afterwards. Every path
   here has since answered the live API — the measurements are inline, dated, and
   tagged: the semicolon-separated exchange list and the ``secdef/search`` JSON
   body ``[CP ✓ 2026-09-10]``, the ``/accounts`` precondition repair and the 401
   re-arm the same day, the price-settle predicate timed 2026-09-15, the
   abbreviated numeric on SPY. That first live run found four defects that were
   green in the test suite, which is why **a fixture is evidence about our parsing
   and never about their format**.

   What is still unverified is named where it lives rather than blanketed here:
   `ibkr.cp_api.marketdata.*` field ids marked ``UNVERIFIED`` in ``config.yaml``,
   and the ids CP serves nothing for at all — the dividend yield and the IV
   percentile — which are ``null`` by measurement. Corrections belong in
   ``references/ibkr-playbook.md``.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import Config
from ..errors import (
    ERROR_EXCERPT_CHARS,
    AuthError,
    ObliskError,
    PermanentUpstreamError,
    UpstreamError,
)
from ..logging_setup import get_logger
from ..util.http import RetryPolicy, request_with_retry
from ..util.numeric import safe_float, safe_int
from .oauth import OAuthSession

log = get_logger("ibkr.cp")

#: Rows carrying data from this client are tagged with this source, so collection
#: can be migrated wholesale if the MCP and OAuth sessions turn out to conflict.
SOURCE = "cp_api"

#: HTTP status codes referenced by name rather than as bare literals.
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403

#: How much of an error body to quote back in an exception message.

#: A percent figure divided by this is a decimal. Unit fact, not a threshold.
PERCENT_TO_DECIMAL = 100.0

#: Snapshot fields whose value is a count, not a price.
_COUNT_FIELDS = ("volume", "open_interest")

#: Fields quoted as a volatility, and so subject to the same percent-or-decimal
#: normalisation. A percentile is NOT one of them: it is already a fraction, and
#: dividing it by a hundred would turn a 90th percentile into a 0.9th.
_IMPLIED_VOL_FIELDS = ("implied_vol", "historical_vol_30d")

#: Fields carrying a dividend yield, unit-normalised on the same rule as implied
#: vol and for the same reason: the route serves a percent under a name that does
#: not say so. See _decode_dividend_yield.
_DIVIDEND_YIELD_FIELDS = ("dividend_yield",)

#: Fields that carry TEXT, not a number. IBKR's market-data availability flag
#: is a code ("R", "D", "Z", ...), and putting it through safe_float would
#: silently discard every value it can take.
_TEXT_FIELDS = ("market_data_status",)

#: The fields whose arrival means a snapshot row has actually QUOTED, and so the
#: only ones that end :meth:`CpApiClient.market_snapshot`'s settle poll.
#:
#: Named by what they ARE rather than by excluding what arrives early, because
#: the measurement says an exclusion list cannot be kept right. Timed on a cold
#: option subscription, 2026-09-15 on cp_api: the first response carries no
#: requested field at all, `market_data_status` AND all four Greeks arrive
#: together at about 1.3s, and the prices arrive at about 7s. Excluding only the
#: status — the obvious reading of the bug — would leave delta ending the poll
#: five seconds before any price existed.
QUOTE_FIELD_NAMES = ("last", "bid", "ask")


#: The body IBKR returns when the ``/accounts`` precondition has not been met, or
#: has been lost. Matched on the message rather than the status because the status
#: is a bare 500, which every other upstream fault also uses.
_ACCOUNTS_PRECONDITION = "please query /accounts first"

#: Bodies this route uses to say "no such data", all of them under HTTP 500.
#: Observed rather than documented; ``references/ibkr-playbook.md`` owns the
#: readings and is where a new one gets appended before it is added here.
_NO_DATA_BODIES = (
    "chart data unavailable",
    "contract details are not available",
)


def _needs_account_arming(response: httpx.Response) -> bool:
    """Is this the ``/accounts`` precondition rather than a genuine server fault?"""
    if response.status_code < HTTP_BAD_REQUEST:
        return False
    try:
        return _ACCOUNTS_PRECONDITION in response.text.lower()
    except Exception:  # pragma: no cover - a body that cannot be decoded is not this
        return False


def _needs_rearming(response: httpx.Response) -> bool:
    """Is this response the brokerage session having been lost mid-run?

    Two shapes, one condition. The documented one is ``HTTP 500 {"error":"Please
    query /accounts first"}``, matched on the body because the status alone is
    every upstream fault. The second is a bare **401**, measured 2026-09-10 on a
    single chain target 31 minutes into a run whose live session token had roughly
    22 hours left and whose every later target collected normally — an expired
    token does not recover by itself, and a dropped ``/iserver`` session does.

    The repair is identical either way, so the recognition lives in one place
    rather than being duplicated at the two call sites it would otherwise need.
    """
    return _needs_account_arming(response) or response.status_code == HTTP_UNAUTHORIZED


def _is_permanent_no_data(response: httpx.Response) -> bool:
    """Is this a PERMANENT "there is no such data" wearing a RETRYABLE status?

    Measured 2026-09-10: ``/iserver/marketdata/history`` answers ``HTTP 500
    {"error":"Chart data unavailable"}`` for an instrument it holds no bars for,
    and ``{"error":"Contract details are not available"}`` for a conid it cannot
    resolve on the exchange asked for. Both are final answers, and ``500`` is in
    ``runtime.http.retry_status_codes`` — so fifteen of the 167 backfill targets
    each cost three HTTP attempts and then two more per-target attempts, nine
    calls apiece, to be told the same thing nine times.

    Matched on the body rather than the status for the same reason
    ``_ACCOUNTS_PRECONDITION`` is: the status is a bare 500, which every genuine
    upstream fault also uses, and reading THAT as permanent would turn a real
    outage into a target the collector gives up on after one try.
    """
    if response.status_code < HTTP_BAD_REQUEST:
        return False
    try:
        body = response.text.lower()
    except Exception:  # pragma: no cover - a body that cannot be decoded is not this
        return False
    return any(marker in body for marker in _NO_DATA_BODIES)


def _raw_or_formatted(row: Mapping[str, Any], field_id: str) -> Any:
    """Prefer IBKR's own ``<id>_raw`` value over the display string beside it.

    **The display string is abbreviated and the abbreviation is lossy.** Measured
    on SPY, 2026-09-10: field ``87`` (volume) came back as the string ``"53.4K"``
    with ``"87_raw": 53400.0` alongside it. ``safe_float`` cannot parse a ``K``
    suffix, so it returned None and the volume column was silently NULL for every
    heavily traded name — while BG, whose volume was small enough to be printed
    as a bare ``"219"``, stored fine. A NULL that appears only on liquid
    instruments is the worst shape a gap can take, because the sample that
    survives is biased.

    Reading ``_raw`` fixes the parse AND the precision: ``"53.4K"`` rounds to
    three significant figures, ``53400.0`` does not.
    """
    raw = row.get(f"{field_id}_raw")
    return row.get(field_id) if raw is None else raw


def _split_list(raw: Any) -> tuple[str, ...]:
    """Split one of IBKR's delimited section lists on either separator it uses."""
    return tuple(stripped for part in re.split(r"[;,]", str(raw)) if (stripped := part.strip()))


def _declared_field_ids(declared: Mapping[str, Any] | None) -> dict[str, str]:
    """Config's field-name -> field-id map, minus the ids that are not known yet.

    ``null`` in config means "no verified id for this field". Such a field is
    neither requested nor decoded, so its column stays NULL. Guessing an id is
    specifically forbidden: 7633 is Implied Vol./Historical Vol %, a ratio, and
    in a percentile column it would look entirely plausible.
    """
    return {
        str(name): str(field_id)
        for name, field_id in dict(declared or {}).items()
        if field_id is not None
    }


@dataclass(frozen=True)
class ContractMatch:
    """One row from ``/iserver/secdef/search``."""

    conid: int
    symbol: str
    company_name: str | None
    description: str | None
    sections: tuple[dict[str, Any], ...]

    @property
    def has_options(self) -> bool:
        return any(s.get("secType") == "OPT" for s in self.sections)

    def option_months(self) -> tuple[str, ...]:
        for section in self.sections:
            if section.get("secType") == "OPT" and section.get("months"):
                return _split_list(section["months"])
        return ()

    def option_exchanges(self) -> tuple[str, ...]:
        """The option exchanges, however IBKR chose to punctuate the list.

        **Live CP separates BOTH lists with semicolons**, not commas: measured on
        SPY, 2026-09-10, the OPT section carried
        ``"SMART;AMEX;BATS;BOX;CBOE;..."``. Splitting that on a comma yields one
        element containing the whole string, which then travels to
        ``/iserver/secdef/strikes`` as ``exchange=SMART;AMEX;BATS;...`` and comes
        back **HTTP 410** — the failure that survived every other fix in the first
        live run. ``months`` was already split on ``;`` and so was never wrong;
        the fixtures that pinned this used commas for both, which is why no test
        caught it.
        """
        for section in self.sections:
            if section.get("secType") == "OPT" and section.get("exchange"):
                return _split_list(section["exchange"])
        return ()


class CpApiClient:
    """Async Client Portal Web API client.

    TLS verification is always on. A custom CA bundle may be supplied, but there
    is no path through this class that disables verification — the original's
    ``verify=False`` fallback is not reproduced.
    """

    def __init__(
        self,
        base_url: str,
        oauth: OAuthSession,
        config: Config,
        *,
        client: httpx.AsyncClient | None = None,
        ca_bundle: str | None = None,
    ) -> None:
        cp_cfg = config.section("ibkr.cp_api")
        self._base_url = base_url.rstrip("/") + cp_cfg.require_str("base_path")
        self._oauth = oauth
        self._config = config
        self._policy = RetryPolicy.from_config(config)
        self._timeout = cp_cfg.require_float("request_timeout_seconds")

        md_cfg = cp_cfg.section("marketdata")
        self._max_conids = md_cfg.require_int("max_conids_per_snapshot")
        self._settle_retries = md_cfg.require_int("snapshot_settle_retries")
        self._settle_delay = md_cfg.require_float("snapshot_settle_delay_seconds")
        # A field declared null is one with no verified id yet. It is dropped from
        # BOTH maps, so the column it would have filled stays NULL rather than
        # holding a guess, and no request ever carries a null in its field list.
        #
        # Both maps get the same treatment deliberately. Only the underlying map
        # used to drop nulls, because only it had ever held one; the option map
        # would have put a bare None into the `fields` query parameter the first
        # time anything unverified was declared there.
        self._snapshot_fields = _declared_field_ids(md_cfg.get("snapshot_fields"))
        # The UNDERLYING field set is separate from the per-option one: asking for
        # historical vol and an IV percentile on every strike would spend market
        # data lines on fields that only mean anything for the underlying.
        self._underlying_fields = _declared_field_ids(md_cfg.get("underlying_snapshot_fields", {}))
        self._iv_units = md_cfg.require_choice("implied_vol_units", ["auto", "decimal", "percent"])
        self._iv_units_fallback = md_cfg.require_choice(
            "implied_vol_units_fallback", ["decimal", "percent"]
        )
        self._dividend_yield_units = md_cfg.require_choice(
            "dividend_yield_units", ["auto", "decimal", "percent"]
        )
        self._dividend_yield_units_fallback = md_cfg.require_choice(
            "dividend_yield_units_fallback", ["decimal", "percent"]
        )
        self._dividend_yield_max_plausible = md_cfg.require_float("dividend_yield_max_plausible")
        self._iv_max_plausible = md_cfg.require_float("implied_vol_max_plausible")
        self._iv_min_plausible = md_cfg.require_float("implied_vol_min_plausible")
        self._delta_max_plausible = md_cfg.require_float("delta_max_plausible")

        self._max_position_pages = cp_cfg.require_int("portfolio.max_position_pages")

        secdef_cfg = cp_cfg.section("secdef")
        self._exact_match_first = secdef_cfg.require_bool("exact_ticker_match_first")
        self._accept_first_fallback = secdef_cfg.require_bool("accept_first_on_no_exact_match")

        self._session_cfg = cp_cfg.section("session")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            verify=ca_bundle if ca_bundle else True,
            timeout=self._timeout,
        )
        self._tickle_task: asyncio.Task[None] | None = None
        self._brokerage_session_open = False
        #: Guard against recursion: _rearm() issues requests of its own.
        self._arming = False

    # ── lifecycle ──

    async def aclose(self) -> None:
        await self.stop_keepalive()
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> CpApiClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def brokerage_session_open(self) -> bool:
        return self._brokerage_session_open

    # ── signed transport ──

    def _url(self, path: str) -> str:
        return f"{self._base_url}/{path.lstrip('/')}"

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        """Issue a signed request and return the decoded JSON body.

        ``json_body`` sends a JSON request body. It deliberately does NOT
        participate in the OAuth 1.0a signature base string: only form-encoded
        parameters do, so a JSON body is signed with the query mapping alone.
        """
        url = self._url(path)
        query = {k: v for k, v in (params or {}).items() if v is not None}

        response = await self._send(method, url, query, json_body)
        if _needs_rearming(response) and not self._arming:
            # The precondition is not once-per-session: it is lost mid-run. Re-arm
            # and repeat the call rather than failing the target. See _rearm().
            #
            # A bare 401 is the SAME loss wearing a different status. Measured
            # 2026-09-10: a chain run 31 minutes in — far inside the live session
            # token's TTL — had one target answer `HTTP 401` on
            # /iserver/marketdata/snapshot while every target after it collected
            # normally. A token that had genuinely expired would have taken the
            # rest of the run with it. Re-arming costs one ssodh/init on a
            # credential fault, which then fails again below and raises; not
            # re-arming costs a target on every session drop.
            await self._rearm()
            response = await self._send(method, url, query, json_body)

        if response.status_code in (HTTP_UNAUTHORIZED, HTTP_FORBIDDEN):
            raise AuthError(
                f"{method} {path} rejected with HTTP {response.status_code}. The live session "
                "token may have expired, or the brokerage session may not be initialised."
            )
        if response.status_code >= HTTP_BAD_REQUEST:
            excerpt = response.text[:ERROR_EXCERPT_CHARS]
            if _is_permanent_no_data(response):
                # PermanentUpstreamError, so with_target_retry gives up at once
                # instead of asking twice more for an answer that cannot change.
                raise PermanentUpstreamError(
                    f"{method} {path} returned HTTP {response.status_code}: {excerpt}. "
                    "This route reports 'no such data' with a 500; it is a final "
                    "answer rather than an outage."
                )
            raise UpstreamError(f"{method} {path} returned HTTP {response.status_code}: {excerpt}")
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise UpstreamError(
                f"{method} {path} returned a non-JSON body: {response.text[:ERROR_EXCERPT_CHARS]}"
            ) from exc

    async def _send(
        self, method: str, url: str, query: Mapping[str, Any], json_body: Any
    ) -> httpx.Response:
        """Sign and issue one request. A fresh signature per attempt: the OAuth
        nonce and timestamp are single-use, so a retry may not reuse the header."""
        headers = self._oauth.sign_request(method, url, params=dict(query))
        return await request_with_retry(
            self._client,
            method,
            url,
            policy=self._policy,
            params=query,
            headers=headers,
            json_body=json_body,
            is_permanent=_is_permanent_no_data,
        )

    async def _rearm(self) -> None:
        """Re-satisfy the ``/accounts`` precondition after it has been lost.

        ``/iserver/accounts`` is documented as a once-per-session precondition for
        the ``/iserver`` market-data routes, and this build calls it on connect.
        **That is not enough.** Measured on 2026-09-10: a run armed at connect
        (``iserver/accounts`` returning one account) still had every subsequent
        ``/iserver/marketdata/snapshot`` answer ``HTTP 500 {"error":"Please query
        /accounts first"}`` once an unrelated call had failed mid-run, and no
        amount of ordinary retrying cleared it — the same snapshot succeeded
        immediately from a fresh process. The precondition is therefore session
        state IBKR can drop, not a one-time handshake.

        Both endpoints are called because they are not interchangeable:
        ``/iserver/accounts`` is the documented one and ``/portfolio/accounts`` is
        the one that reliably returns the account list on this account. Failures
        are swallowed — the caller is about to repeat the real request, and that
        request's own error says far more than a guess made here.
        """
        self._arming = True
        try:
            # ssodh/init FIRST, and it is the part that actually works. Measured
            # 2026-09-10: calling the two /accounts endpoints alone cleared
            # nothing across nine attempts in one run, while a fresh process
            # served the same snapshot immediately — and re-opening the brokerage
            # session is the only thing a fresh process does that this did not.
            with contextlib.suppress(Exception):
                body = await self.request(
                    "POST",
                    "iserver/auth/ssodh/init",
                    params={"publish": "true", "compete": "true"},
                )
                self._brokerage_session_open = bool((body or {}).get("authenticated", False))
            for path in ("iserver/accounts", "portfolio/accounts"):
                with contextlib.suppress(Exception):
                    await self.request("GET", path)
            log.info(
                "re-established the brokerage session and the /accounts precondition",
                extra={"authenticated": self._brokerage_session_open},
            )
        finally:
            self._arming = False

    async def get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params=params)

    # ── brokerage session ──

    async def init_brokerage_session(self) -> bool:
        """Open the brokerage session via ``/iserver/auth/ssodh/init``.

        Required before any ``/iserver/*`` call. Replaces the original's reliance
        on a human having logged into a local gateway.
        """
        body = await self.request(
            "POST",
            "iserver/auth/ssodh/init",
            params={"publish": "true", "compete": "true"},
        )
        authenticated = bool((body or {}).get("authenticated", False))
        self._brokerage_session_open = authenticated
        log.info("brokerage session init", extra={"authenticated": authenticated})
        if authenticated:
            await self.iserver_accounts()
        return authenticated

    async def iserver_accounts(self) -> list[str]:
        """``GET /iserver/accounts``, which market data will not serve without.

        **This is a precondition, not a portfolio read.** Until it is called once
        per brokerage session, ``/iserver/marketdata/snapshot`` answers
        ``HTTP 500 {"error":"Please query /accounts first"}`` — measured on
        2026-09-10, when it failed every chain target after ``ssodh/init`` had
        already reported authenticated. ``/portfolio/accounts`` does NOT satisfy
        it: they are different endpoints and only the ``/iserver`` one arms the
        ``/iserver`` market-data routes. ``references/ibkr-playbook.md``.

        Failure is logged rather than raised: the caller has an authenticated
        session either way, and the market-data call that follows reports the
        precondition itself far more precisely than a guess made here would.
        """
        try:
            body = await self.get("iserver/accounts")
        except ObliskError as exc:  # pragma: no cover - live-only failure path
            log.warning(
                "iserver/accounts failed; market data may refuse", extra={"error": repr(exc)}
            )
            return []
        accounts = [str(a) for a in (body or {}).get("accounts", [])]
        log.info("iserver accounts armed", extra={"accounts": len(accounts)})
        return accounts

    async def auth_status(self) -> dict[str, Any]:
        return await self.request("POST", "iserver/auth/status") or {}

    async def tickle(self) -> dict[str, Any]:
        """Keep the brokerage session alive. Same purpose as the original's tickle."""
        return await self.request("POST", "tickle") or {}

    async def start_keepalive(self) -> None:
        """Run the tickle loop in the background.

        This is the one piece of the original's ``_maintenance_loop`` worth keeping:
        the Web API really does drop an idle brokerage session. What changed is that
        a failure now re-initialises the session rather than merely flipping a
        ``healthy`` flag that callers had to consult.
        """
        if self._tickle_task is not None:
            return
        interval = self._session_cfg.require_float("tickle_interval_seconds")
        grace = self._session_cfg.require_int("tickle_failure_grace")
        self._tickle_task = asyncio.create_task(self._keepalive_loop(interval, grace))

    async def stop_keepalive(self) -> None:
        task, self._tickle_task = self._tickle_task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _keepalive_loop(self, interval: float, grace: int) -> None:
        consecutive_failures = 0
        while True:
            try:
                await asyncio.sleep(interval)
                await self.tickle()
                consecutive_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                consecutive_failures += 1
                log.warning(
                    "tickle failed",
                    extra={"consecutive_failures": consecutive_failures, "error": repr(exc)},
                )
                self._brokerage_session_open = False
                if consecutive_failures >= grace:
                    try:
                        await self.init_brokerage_session()
                        consecutive_failures = 0
                    except Exception as reinit_exc:  # pragma: no cover - live-only path
                        log.error(
                            "brokerage session re-init failed",
                            extra={"error": repr(reinit_exc)},
                        )

    # ── /iserver/secdef/search ──

    async def search_contracts(
        self, symbol: str, *, sec_type: str = "STK", name: bool = False
    ) -> list[ContractMatch]:
        """Search the security definitions for ``symbol``.

        Results are returned in the API's own order; :meth:`resolve_conid` applies
        the exact-match rule.

        **The search terms go in a JSON BODY, not the query string.** Sent as query
        parameters the endpoint still answers HTTP 200 and still matches the symbol
        — it just returns every row with ``sections: []``. That is indistinguishable
        from an instrument with no listed options, and it is what made the first
        live run report "no option months" for all 24 chain targets while looking
        like a market-data entitlement problem. Measured against SPY on
        2026-09-10: query parameters gave 55 matches and 0 sections; the same
        search as a JSON body gave 3 matches, 6 sections, and the ``OPT`` months
        the chain collector needs. ``references/ibkr-playbook.md``.
        """
        body = await self.request(
            "POST",
            "iserver/secdef/search",
            json_body={"symbol": symbol, "secType": sec_type, "name": bool(name)},
        )
        # A symbol this route does not know answers HTTP 200 with a JSON OBJECT —
        # `{"error": "No symbol found"}` — where a match answers with a LIST.
        # Iterating the object yields its KEYS, so the loop below then called
        # .get() on the string "error" and the whole target died of an
        # AttributeError that named nothing an operator could act on, after two
        # retries of a condition that could not change. Measured on TOPIX,
        # [CP ✓ 2026-09-10]; no match is a legitimate answer and reads as one.
        if not isinstance(body, list):
            if body:
                log.warning(
                    "the security search returned no list of matches",
                    extra={
                        "symbol": symbol,
                        "sec_type": sec_type,
                        "body": str(body)[:ERROR_EXCERPT_CHARS],
                    },
                )
            return []
        matches: list[ContractMatch] = []
        for row in body:
            if not isinstance(row, Mapping):
                continue
            conid = safe_int(row.get("conid"), None)
            if conid is None:
                continue
            matches.append(
                ContractMatch(
                    conid=conid,
                    symbol=str(row.get("symbol") or row.get("ticker") or "").upper(),
                    company_name=row.get("companyName") or row.get("companyHeader"),
                    description=row.get("description"),
                    sections=tuple(row.get("sections") or ()),
                )
            )
        return matches

    async def resolve_conid(
        self, ticker: str, *, sec_type: str = "STK", exchange: str | None = None
    ) -> int | None:
        """Resolve a ticker to a conid: declared venue first, then exact ticker.

        **Ported behaviour, and the reason this method exists rather than a bare
        search call.** ``/iserver/secdef/search`` ranks by its own relevance, which
        is not the same as correctness: searching a short ticker routinely returns
        a foreign listing or a similarly-named instrument ahead of the one asked
        for. Scan for an exact match first; fall back to the first row only when
        ``ibkr.cp_api.secdef.accept_first_on_no_exact_match`` allows it.

        **The exact-ticker rule alone cannot separate the listings of one
        company**, which is the half of this that was missing. Measured
        [CP ✓ 2026-09-10]: every row of the ASML, SAN and TTE searches carries the
        bare ticker as ``symbol`` — NASDAQ, TSE and AEB all answer ``"ASML"`` —
        so the rule matches all of them, returns the first, and the first is the
        US listing for all three. That is how a row declaring ``AEB`` and ``EUR``
        came to hold a NASDAQ conid and collect New York's chain for a session.

        ``exchange`` is the LISTING VENUE the caller declared, and it is what
        actually discriminates: this route puts the venue in ``description``
        (``"AEB"``, ``"BM"``, ``"SBF"``, ``"NYSE"``). It is a TIEBREAK and not a
        filter — a declared venue matching nothing falls through to the behaviour
        below rather than failing, so this can only ever sharpen a resolution and
        never refuse one that used to work. An explicit ``conid:`` in
        ``universe.yaml`` still wins outright and is still the answer for anything
        genuinely ambiguous; this is what stops the NEXT dual-listed row being
        silently wrong before anyone thinks to pin it.
        """
        matches = await self.search_contracts(ticker, sec_type=sec_type)
        if not matches:
            return None

        wanted = ticker.upper()
        if exchange:
            venue = exchange.upper()
            for match in matches:
                if (match.description or "").upper() == venue and (
                    not self._exact_match_first or match.symbol == wanted
                ):
                    return match.conid

        if self._exact_match_first:
            for match in matches:
                if match.symbol == wanted:
                    return match.conid

        if self._accept_first_fallback:
            log.warning(
                "no exact ticker match; falling back to the first search result",
                extra={
                    "ticker": ticker,
                    "fallback_conid": matches[0].conid,
                    "fallback_symbol": matches[0].symbol,
                },
            )
            return matches[0].conid

        return None

    async def contract_info(self, conid: int) -> dict[str, Any]:
        return await self.get(f"iserver/contract/{int(conid)}/info") or {}

    # ── /iserver/marketdata/snapshot ──

    def _settling_field_ids(self, wanted: Sequence[str]) -> frozenset[str]:
        """Which of ``wanted`` mean a quote has actually ARRIVED.

        Defined by INCLUSION — the price fields — rather than by excluding the
        fields known to arrive early, and the measurement is why. On a cold
        option subscription, timed 2026-09-15 on ``cp_api``, the first response
        carries nothing at all; ``6509`` **and all four Greeks** (``7308``
        ``7309`` ``7310`` ``7311``) arrive together about 1.3s in; the prices
        arrive about 7s in. An exclusion list would have had to name five fields
        to be right today and would break again the first time IBKR served a
        sixth one early. A price is what the caller came for, so a price is what
        settles the poll. See ``references/ibkr-playbook.md``.

        The degenerate case is real and is handled rather than assumed away: a
        caller asking ONLY for metadata — a status-field probe — has no price to
        wait for, so every requested field settles it and the poll terminates on
        the first response instead of spending the whole budget.
        """
        quote_ids = {
            field_id
            for source in (self._snapshot_fields, self._underlying_fields)
            for name, field_id in source.items()
            if name in QUOTE_FIELD_NAMES
        }
        settling = frozenset(quote_ids & set(wanted))
        return settling or frozenset(wanted)

    async def market_snapshot(
        self, conids: Sequence[int], *, fields: Sequence[str] | None = None
    ) -> dict[int, dict[str, Any]]:
        """Snapshot quotes for ``conids``, keyed by conid.

        The first call for a conid the session is not yet subscribed to returns a
        skeleton row; the fields populate on a later call. This polls up to
        ``snapshot_settle_retries`` times rather than treating the first response
        as data — the original returned whatever came back first, which on a cold
        subscription is an empty quote indistinguishable from a halted one.

        **Polling on its own was not enough, and a whole phase of chain rows paid
        for it.** The poll used to break as soon as ANY requested field appeared,
        and what appears first on a cold subscription is never a price: the
        collector therefore wrote its first pass over every new option conid with
        no bid, no ask and no last. Measured 2026-09-14, each name re-run with the
        venue equally closed on both passes: XIU 264 rows 0 priced then 264,
        FCX 170 rows 0 then 170. :meth:`_settling_field_ids` is that fix.

        Batches are PRIMED before any of them is polled. A subscription starts
        warming when it is first asked for, so polling batch one to completion
        before touching batch two used to serialise the warm-ups; priming them all
        first overlaps them. Timed on the same run: a batch polled cold settled in
        7.03s, the same basket primed first settled in 4.78s.
        """
        if not conids:
            return {}

        wanted = list(fields) if fields else list(self._snapshot_fields.values())
        settling = self._settling_field_ids(wanted)
        field_param = ",".join(wanted)
        batches = [
            [int(c) for c in conids[start : start + self._max_conids]]
            for start in range(0, len(conids), self._max_conids)
        ]

        def params_for(batch: Sequence[int]) -> dict[str, Any]:
            return {"conids": ",".join(str(c) for c in batch), "fields": field_param}

        # One batch needs no priming: its first poll IS the priming request, and a
        # second one would spend a call to learn nothing.
        if len(batches) > 1:
            for batch in batches:
                await self.request("GET", "iserver/marketdata/snapshot", params=params_for(batch))

        collected: dict[int, dict[str, Any]] = {}
        for batch in batches:
            params = params_for(batch)
            settled: set[int] = set()
            for attempt in range(self._settle_retries):
                body = await self.request("GET", "iserver/marketdata/snapshot", params=params)
                for row in body or []:
                    conid = safe_int(row.get("conid"), None)
                    if conid is None:
                        continue
                    # MERGED, not replaced. Each retry re-reads the whole batch, so
                    # assigning the row outright let a conid that had already
                    # quoted be clobbered by a later skeleton while the loop waited
                    # on a different conid in the same batch.
                    merged = dict(collected.get(conid) or {})
                    merged.update(row)
                    collected[conid] = merged
                    if any(field in row for field in settling):
                        settled.add(conid)
                # Remembered per conid rather than recounted per attempt, for the
                # same reason: settlement is a fact about a conid, not about the
                # response that happened to carry it.
                if len(settled) >= len(batch):
                    break
                if attempt < self._settle_retries - 1:
                    await asyncio.sleep(self._settle_delay)

        return collected

    @property
    def underlying_field_ids(self) -> list[str]:
        """The field ids to request for an underlying, in declaration order."""
        return list(self._underlying_fields.values())

    def decode_underlying_snapshot(
        self, row: Mapping[str, Any]
    ) -> dict[str, float | int | str | None]:
        """Decode a row against the UNDERLYING field map rather than the option one."""
        return self.decode_snapshot(row, fields=self._underlying_fields)

    def decode_snapshot(
        self, row: Mapping[str, Any], *, fields: Mapping[str, str] | None = None
    ) -> dict[str, float | int | str | None]:
        """Map the numeric field ids in a snapshot row onto the config's field names.

        ``default=None`` throughout: an absent bid is not a bid of zero.

        Implied vol is unit-normalised here, once, rather than at each call site.
        IBKR has been observed serving it both as a bare decimal and as a
        percent-suffixed string; a value that survives normalisation but lands
        beyond ``implied_vol_max_plausible`` is a unit error rather than a quote
        and is dropped to ``None`` with a warning. Guessing units from magnitude is
        deliberately not attempted — it turns a real 200% IV into 2% silently.
        """
        decoded: dict[str, float | int | str | None] = {}
        for name, field_id in (fields or self._snapshot_fields).items():
            raw = _raw_or_formatted(row, field_id)
            if name in _TEXT_FIELDS:
                decoded[name] = None if raw is None else str(raw).strip() or None
            elif name in _COUNT_FIELDS:
                decoded[name] = safe_int(raw, None)
            elif name in _IMPLIED_VOL_FIELDS:
                decoded[name] = self._decode_implied_vol(raw, row)
            elif name in _DIVIDEND_YIELD_FIELDS:
                decoded[name] = self._decode_dividend_yield(raw, row)
            else:
                decoded[name] = safe_float(raw, None)
        decoded["delta"] = self._reject_implausible(
            decoded.get("delta"), self._delta_max_plausible, "delta", row
        )
        return decoded

    def _decode_implied_vol(self, raw: Any, row: Mapping[str, Any]) -> float | None:
        value = safe_float(raw, None)
        if value is None:
            return None
        units = self._iv_units
        if units == "auto":
            looks_percent = isinstance(raw, str) and raw.strip().endswith("%")
            units = "percent" if looks_percent else self._iv_units_fallback
        if units == "percent":
            value /= PERCENT_TO_DECIMAL
        if value < self._iv_min_plausible:
            # A SENTINEL, not a quote. IBKR serves -1.0 for "no implied vol here",
            # and -1.0 sits well inside any plausible magnitude, so the ceiling
            # below never sees it. Volatility has no negative branch: there is no
            # unit convention under which this becomes a real number.
            log.warning(
                "implied vol is below its floor; dropping it rather than storing a "
                "sentinel as a quote",
                extra={
                    "field": "implied_vol",
                    "value": value,
                    "floor": self._iv_min_plausible,
                    "conid": row.get("conid"),
                },
            )
            return None
        return self._reject_implausible(value, self._iv_max_plausible, "implied_vol", row)

    def _decode_dividend_yield(self, raw: Any, row: Mapping[str, Any]) -> float | None:
        """Normalise a served yield to a DECIMAL, the unit the whole schema uses.

        Same rule as implied vol, and necessary for the same reason: the field
        name does not state the unit. Measured on the MCP connector 2026-09-09,
        one SPY response carried dividend-yield {yield_pct: 0.98} — a PERCENT —
        beside historical-vol {annual_pct: 0.0956}, a FRACTION. cp_api's own
        convention is UNVERIFIED, which is why the units are configured rather
        than assumed, and why the fallback is `percent` rather than the decimal
        one implied vol defaults to.

        A NEGATIVE yield is a sentinel, never a payout: an instrument cannot pay
        out less than nothing, and IBKR is known to serve -1.0 for "no value here"
        on other fields. Zero is left alone — a non-payer is a real reading, and
        the column NULL already means "nobody collected one", which is the
        distinction 009 exists to preserve.
        """
        value = safe_float(raw, None)
        if value is None:
            return None
        units = self._dividend_yield_units
        if units == "auto":
            looks_percent = isinstance(raw, str) and raw.strip().endswith("%")
            units = "percent" if looks_percent else self._dividend_yield_units_fallback
        if units == "percent":
            value /= PERCENT_TO_DECIMAL
        if value < 0.0:
            log.warning(
                "dividend yield is negative; dropping it rather than storing a "
                "sentinel as a payout",
                extra={
                    "field": "dividend_yield",
                    "value": value,
                    "conid": row.get("conid"),
                },
            )
            return None
        return self._reject_implausible(
            value, self._dividend_yield_max_plausible, "dividend_yield", row
        )

    def _reject_implausible(
        self, value: float | None, ceiling: float, field: str, row: Mapping[str, Any]
    ) -> float | None:
        """A value beyond its plausible MAGNITUDE is a unit error, not data.

        Magnitude only, deliberately: delta is legitimately signed, so -0.4 is a
        quote and not a fault. Sign is therefore a per-field question, handled by
        the caller — see the floor in :meth:`_decode_implied_vol`.
        """
        if value is None or abs(value) <= ceiling:
            return value
        log.warning(
            "snapshot field is beyond its plausible range; dropping it rather than "
            "storing a value that is almost certainly a unit error",
            extra={"field": field, "value": value, "ceiling": ceiling, "conid": row.get("conid")},
        )
        return None

    # ── /iserver/marketdata/history ──

    async def market_history(
        self,
        conid: int,
        *,
        period: str,
        bar: str,
        exchange: str | None = None,
        outside_regular_trading_hours: bool = False,
        start_time: str | None = None,
    ) -> dict[str, Any]:
        """Historical bars for one conid.

        Written new — IBKR-Portmanteau had no such call; its bars came from Yahoo.

        ``period`` is IBKR's total-span notation (``5y``, ``2y``, ``6m``) and
        ``bar`` the bar size (``1w``, ``1d``, ``1h``). Index contracts require the
        NATIVE exchange: SMART returns empty, per the playbook.

        Returns the raw body. Bars carry ``t`` (epoch ms), ``o``, ``h``, ``l``,
        ``c``, ``v``.
        """
        params: dict[str, Any] = {
            "conid": int(conid),
            "period": period,
            "bar": bar,
            "outsideRth": str(bool(outside_regular_trading_hours)).lower(),
        }
        if exchange:
            params["exchange"] = exchange
        if start_time:
            params["startTime"] = start_time
        return await self.request("GET", "iserver/marketdata/history", params=params) or {}

    # ── options chains ──

    async def option_strikes(
        self, underlying_conid: int, *, month: str, exchange: str, sec_type: str = "OPT"
    ) -> dict[str, list[float]]:
        """Strike ladder for one expiry month: ``{"call": [...], "put": [...]}``.

        Written new. ``month`` is IBKR's ``MMMYY`` form, e.g. ``JAN26``. Strikes
        come back as strings and are cast here — the playbook's rule.
        """
        body = (
            await self.request(
                "GET",
                "iserver/secdef/strikes",
                params={
                    "conid": int(underlying_conid),
                    "sectype": sec_type,
                    "month": month,
                    "exchange": exchange,
                },
            )
            or {}
        )
        out: dict[str, list[float]] = {}
        for side in ("call", "put"):
            values = [safe_float(v, None) for v in body.get(side, []) or []]
            out[side] = [v for v in values if v is not None]
        return out

    async def option_contracts(
        self,
        underlying_conid: int,
        *,
        month: str,
        exchange: str,
        strike: float,
        right: str,
        sec_type: str = "OPT",
    ) -> list[dict[str, Any]]:
        """Resolve one strike and right to its option contract(s) via ``/secdef/info``.

        Written new. Returns one row per expiry within the month; each carries the
        option's own ``conid``, which is what snapshot and history calls take.
        """
        if right.upper() not in ("C", "P"):
            raise ValueError(f"right must be 'C' or 'P', got {right!r}")
        body = await self.request(
            "GET",
            "iserver/secdef/info",
            params={
                "conid": int(underlying_conid),
                "sectype": sec_type,
                "month": month,
                "exchange": exchange,
                "strike": strike,
                "right": right.upper(),
            },
        )
        return list(body or [])

    # ── /trsrv/futures ──

    async def futures_ladder(self, roots: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
        """Front-to-back futures ladder per root, e.g. ``{"CL": [...], "NG": [...]}``.

        Written new. This is the Client Portal equivalent of the MCP
        ``search_futures`` tool, and it is a **data call**: a futures contract is a
        prohibited instrument and is never returned as a tradeable candidate. The
        curve drives wrapper selection, so the term structure is required — see
        ``instruments.futures_data_use`` in config.yaml.

        Each row carries ``conid``, ``symbol`` and ``expirationDate`` (IBKR's
        ``YYYYMMDD`` integer). Rows without a conid or an expiry are dropped: a
        curve point that cannot be priced or dated is not a curve point.

        .. warning:: UNVERIFIED. Path and response shape are documentation-derived.
        """
        if not roots:
            return {}
        body = (
            await self.request(
                "GET", "trsrv/futures", params={"symbols": ",".join(str(r) for r in roots)}
            )
            or {}
        )
        out: dict[str, list[dict[str, Any]]] = {}
        for root in roots:
            rows = [
                row
                for row in (body.get(root) or [])
                if safe_int(row.get("conid"), None) is not None and row.get("expirationDate")
            ]
            rows.sort(key=lambda row: str(row.get("expirationDate")))
            out[str(root)] = rows
        return out

    # ── /portfolio ──

    async def accounts(self) -> list[dict[str, Any]]:
        body = await self.get("portfolio/accounts")
        return list(body or [])

    async def primary_account_id(self) -> str:
        accounts = await self.accounts()
        if not accounts:
            raise UpstreamError("/portfolio/accounts returned no accounts")
        return str(accounts[0].get("id") or accounts[0].get("accountId"))

    async def ledger(self, account_id: str) -> dict[str, Any]:
        """Per-currency cash ledger, including the BASE row and FX rates.

        Ported: the base-currency consolidation in IBKR-Portmanteau's
        ``PortfolioEngine`` read net liquidation value and per-currency
        ``exchangeRate`` from here, which is exactly what an EUR-base NAV needs.
        """
        return await self.get(f"portfolio/{account_id}/ledger") or {}

    async def positions(self, account_id: str, *, page: int = 0) -> list[dict[str, Any]]:
        body = await self.get(f"portfolio/{account_id}/positions/{int(page)}")
        return list(body or [])

    async def all_positions(
        self, account_id: str, *, max_pages: int | None = None
    ) -> list[dict[str, Any]]:
        """Every page of positions. The endpoint pages and stops on an empty page.

        The page ceiling is a runaway guard from
        ``ibkr.cp_api.portfolio.max_position_pages``, not a limit on the portfolio.
        """
        out: list[dict[str, Any]] = []
        for page in range(max_pages if max_pages is not None else self._max_position_pages):
            rows = await self.positions(account_id, page=page)
            if not rows:
                break
            out.extend(rows)
        return out
