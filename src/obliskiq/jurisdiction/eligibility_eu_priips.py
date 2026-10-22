"""The ``eu_priips`` eligibility policy: what an EU retail investor may buy (MASTER_PLAN §2).

Computed from the universe's attributes (``config/universe.yaml``), with the values the profile
states (``eligibility`` in ``config/jurisdictions/<id>.yaml``):

* **Shares** are not packaged products: eligible on any venue, whatever their domicile.
* **ETFs, ETCs and funds** are packaged products (PRIIPs): bought only with a key information
  document available, and an ETF or fund only in the UCITS form. A US-domiciled ETF has neither,
  so it is data and an option underlying, never a purchase; the verdict says which facts refuse
  it. An unknown fact refuses too: an unknown KID is not an available one.
* **Futures-tracking ETCs** roll a futures curve: eligible only while lite's contango rule passes
  on the curve they roll, through a :class:`ContangoCheck`. The commodity engine computes it
  (Session 12); until then :class:`UnavailableContangoCheck` fails closed.
* **Bonds**: government bonds by the profile's switch, corporate bonds off by default.
* **Options**: eligible when their underlying is; on an ETF or ETC the policy refuses, held only
  under the mandatory close (or refused, by the profile's choice); on anything else refused.
* An index is a level and a future is prohibited: never eligible. An instrument the universe
  does not describe is never eligible: its attributes are unknown.

Leveraged and inverse products are rejected at ingestion (``data/market/universe.py``) and by the
gate's prohibited categories, before eligibility is asked.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from dataclasses import dataclass
from typing import Final, Protocol

from ..config.schema_universe import UniverseConfig, UniverseInstrument
from ..domain.enums import AssetClass, KidStatus
from ..domain.instrument import TradedInstrument
from .eligibility import EXCHANGE_TRADED_PRODUCTS, EligibilityProfile, EligibilityVerdict

EU_PRIIPS_POLICY_ID: Final = "eu_priips"


@dataclass(frozen=True, slots=True)
class ContangoVerdict:
    """Whether a futures-tracking product's curve passes the contango rule, and why."""

    passes: bool
    reason: str


class ContangoCheck(Protocol):
    """Lite's contango rule on a futures curve (``commodities.term_structure``)."""

    def assess(self, root: str) -> ContangoVerdict: ...


class UnavailableContangoCheck:
    """The contango rule before the commodity engine exists: every curve fails it."""

    def assess(self, root: str) -> ContangoVerdict:
        return ContangoVerdict(
            False,
            f"the contango rule on {root}'s curve is computed by the commodity engine (Session "
            "12); until then a futures-tracking product is refused",
        )


def _described(value: object) -> str:
    return "unknown" if value is None else str(value)


class EuPriipsEligibility:
    """Buy eligibility for an EU retail account, computed from the universe's attributes."""

    def __init__(
        self, universe: UniverseConfig, eligibility: EligibilityProfile, contango: ContangoCheck
    ) -> None:
        self._universe = universe
        self._eligibility = eligibility
        self._contango = contango

    @property
    def policy_id(self) -> str:
        return EU_PRIIPS_POLICY_ID

    @property
    def source(self) -> str:
        """Where the profile's rules come from."""
        return self._eligibility.source

    def _verdict(
        self, eligible: bool, reason: str, *, mandatory_close: bool = False
    ) -> EligibilityVerdict:
        return EligibilityVerdict(
            eligible=eligible,
            reason=reason,
            policy=self.policy_id,
            mandatory_close=mandatory_close,
        )

    def assess(
        self, instrument: TradedInstrument, *, underlying: TradedInstrument | None
    ) -> EligibilityVerdict:
        if instrument.asset_class is AssetClass.OPTION:
            return self._option(instrument, underlying)
        declared = self._universe.pinned(instrument.conid)
        if declared is None:
            return self._verdict(
                False,
                f"conid {instrument.conid} is not an instrument the universe pins, so its "
                "attributes are unknown",
            )
        if declared.asset_class is not instrument.asset_class:
            return self._verdict(
                False,
                f"conid {instrument.conid} is described as {instrument.asset_class.value} but the "
                f"universe declares {declared.symbol} a {declared.asset_class.value}",
            )
        return self.assess_declared(declared)

    def assess_declared(self, declared: UniverseInstrument) -> EligibilityVerdict:
        """The verdict on a declared instrument (``obliskiq universe validate`` prints it)."""
        kind = declared.asset_class
        symbol = declared.symbol
        if kind is AssetClass.STOCK:
            return self._verdict(
                True, f"{symbol} is a share: not a packaged product, so PRIIPs does not apply"
            )
        if kind is AssetClass.BOND:
            return self._bond(declared)
        if kind in self._eligibility.kid_required:
            return self._packaged(declared)
        if kind is AssetClass.INDEX:
            return self._verdict(False, f"{symbol} is an index: a level, not something to buy")
        return self._verdict(
            False, f"{symbol} is a {kind.value}, which this policy never lets the account buy"
        )

    def _packaged(self, declared: UniverseInstrument) -> EligibilityVerdict:
        attrs = declared.attributes
        kind = declared.asset_class.value
        kid = _described(attrs.kid and attrs.kid.value)
        facts = (
            f"{declared.symbol} is an {kind} domiciled in {_described(attrs.domicile)}, UCITS "
            f"{_described(attrs.ucits)}, with its PRIIPs KID {kid}"
        )
        refusals: list[str] = []
        if attrs.kid is not KidStatus.AVAILABLE:
            refusals.append(
                "a packaged product is offered to an EU retail investor only with a key "
                "information document available"
            )
        if declared.asset_class in self._eligibility.ucits_required and attrs.ucits is not True:
            refusals.append(f"an {kind} is bought only in the UCITS form")
        if attrs.replication == "futures":
            root = attrs.futures_root or "its curve"
            contango = self._contango.assess(root)
            if not contango.passes:
                refusals.append(contango.reason)
        if refusals:
            return self._verdict(False, f"{facts}: " + "; ".join(refusals))
        return self._verdict(True, facts)

    def _bond(self, declared: UniverseInstrument) -> EligibilityVerdict:
        issuer = declared.attributes.bond_issuer
        symbol = declared.symbol
        if issuer == "government":
            if self._eligibility.government_bonds:
                return self._verdict(True, f"{symbol} is a government bond")
            return self._verdict(
                False, f"{symbol} is a government bond, which the profile turns off"
            )
        if issuer == "corporate":
            if self._eligibility.corporate_bonds:
                return self._verdict(True, f"{symbol} is a corporate bond")
            return self._verdict(
                False, f"{symbol} is a corporate bond: corporate bonds are off by the profile"
            )
        return self._verdict(False, f"{symbol} is a bond whose issuer kind is not declared")

    def _option(
        self, option: TradedInstrument, underlying: TradedInstrument | None
    ) -> EligibilityVerdict:
        if underlying is None or underlying.conid != option.underlying_conid:
            return self._verdict(False, f"the underlying of {option.symbol} is not described")
        on_underlying = self.assess(underlying, underlying=None)
        if on_underlying.eligible:
            return self._verdict(
                True,
                f"{option.symbol} is a listed option on {underlying.symbol}, which is eligible",
            )
        if underlying.asset_class not in EXCHANGE_TRADED_PRODUCTS:
            return self._verdict(
                False,
                f"{option.symbol} is an option on {underlying.symbol}, which is not eligible: "
                f"{on_underlying.reason}",
            )
        if self._eligibility.options_on_ineligible_etps == "refused":
            return self._verdict(
                False,
                f"{option.symbol} is an option on the ineligible ETP {underlying.symbol}, which "
                "the profile refuses",
            )
        return self._verdict(
            True,
            f"{option.symbol} is an option on {underlying.symbol}, an ETP the policy does not let "
            "the account buy: held only if closed before expiry",
            mandatory_close=True,
        )
