"""The universe's schema refuses every malformed declaration, and the configuration around it the
references it cannot resolve: each refusal below mutates the shipped file once.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from obliskiq.config import ObliskConfig
from obliskiq.config.loader import ConfigInvalid, load_config
from obliskiq.config.schema_collectors import CollectorsConfig
from obliskiq.config.schema_instruments import TenorConfig
from obliskiq.config.schema_market_data import MarketDataConfig
from obliskiq.config.schema_universe import UniverseConfig

type Data = dict[str, Any]


@pytest.fixture(scope="module")
def shipped(shipped_settings: ObliskConfig) -> Data:
    return shipped_settings.universe.model_dump(mode="json")


def instrument(data: Data, symbol: str) -> Data:
    return next(i for i in data["instruments"] if i["symbol"] == symbol)


def refused(shipped: Data, change: Callable[[Data], None], match: str) -> None:
    data = copy.deepcopy(shipped)
    change(data)
    with pytest.raises(ValidationError, match=match):
        UniverseConfig.model_validate(data)


def test_the_shipped_universe_round_trips(shipped: Data) -> None:
    assert UniverseConfig.model_validate(shipped).model_dump(mode="json") == shipped


# ── axes ──


def test_the_axes_refuse_repeats_and_an_unknown_broad_sector(shipped: Data) -> None:
    refused(shipped, lambda d: d["grid"]["sectors"].append("energy"), "lists a sector twice")
    refused(shipped, lambda d: d["grid"].update(broad_sector="all"), "broad_sector all is not in")
    refused(
        shipped,
        lambda d: d["macro_board"]["asset_classes"].append("credit"),
        "lists an asset class twice",
    )

    def drop_cell(d: Data) -> None:
        d["macro_board"]["regions"]["us"]["cells"].pop("credit")

    refused(shipped, drop_cell, r"us\.credit is not declared")

    def extra_cell(d: Data) -> None:
        d["macro_board"]["regions"]["us"]["cells"]["weather"] = {
            "series": [], "roots": [], "label": None, "unmapped": "none",
        }  # fmt: skip

    refused(shipped, extra_cell, r"us\.weather is not an asset class of the board")


# ── instruments ──


@pytest.mark.parametrize(
    ("symbol", "update", "match"),
    [
        ("IWDA", {"symbol": "iwda"}, "upper-case symbol"),
        ("IWDA", {"asset_class": "option"}, "is not declared in the universe"),
        ("IWDA", {"roles": ["tradable_candidate", "tradable_candidate"]}, "a role is listed twice"),
        ("IWDA", {"data_only_reason": "why not"}, "set exactly when it is not"),
        ("IWDA", {"collect_bars": False}, "bars_off_reason is set exactly"),
        ("SPY", {"options_unavailable_reason": "none"}, "chain cannot also be unreachable"),
        ("IWDA", {"option_exchange": "LSE"}, "no chain is collected"),
        ("SPX", {"roles": ["tradable_candidate"], "data_only_reason": None}, "an index is a level"),
    ],
)
def test_an_incoherent_instrument_is_refused(
    shipped: Data, symbol: str, update: Data, match: str
) -> None:
    refused(shipped, lambda d: instrument(d, symbol).update(update), match)


def test_a_cell_measured_without_a_chain_must_say_why(shipped: Data) -> None:
    def no_reason(d: Data) -> None:
        exv1 = instrument(d, "EXV1")
        assert exv1["exposures"]["grid"]["measures"]
        exv1["options_unavailable_reason"] = None

    refused(shipped, no_reason, "collects no chain and gives no")


def test_attributes_name_a_hedge_and_a_futures_root_exactly_when_they_have_one(
    shipped: Data,
) -> None:
    refused(
        shipped,
        lambda d: instrument(d, "IWDA")["attributes"].update(hedged=True),
        "names hedge_currency",
    )
    refused(
        shipped,
        lambda d: instrument(d, "IWDA")["attributes"].update(replication="futures"),
        "names its futures_root",
    )


def test_a_proxy_names_a_reference_exactly_when_it_has_one(shipped: Data) -> None:
    refused(
        shipped,
        lambda d: d["commodity_proxies"][0].update(reference_kind="none"),
        "a reference is named exactly when",
    )


def test_two_declarations_of_one_symbol_or_conid_are_refused(shipped: Data) -> None:
    def twice(d: Data) -> None:
        d["instruments"].append(copy.deepcopy(instrument(d, "IWDA")))

    refused(shipped, twice, "instruments declared twice: IWDA")

    def same_conid(d: Data) -> None:
        instrument(d, "EMIM")["conid"] = instrument(d, "IWDA")["conid"]

    refused(shipped, same_conid, "conids pinned by two instruments")


# ── the grid matrix ──


def test_the_grid_refuses_cells_off_the_axes_measured_twice_or_left_undeclared(
    shipped: Data,
) -> None:
    refused(
        shipped,
        lambda d: instrument(d, "EXV1")["exposures"]["grid"].update(market="atlantis"),
        "not on the axes",
    )

    def measured_twice(d: Data) -> None:
        instrument(d, "EXV3")["exposures"]["grid"].update(sector="financials")

    refused(shipped, measured_twice, "both measure eurozone/financials")

    def unmeasured(d: Data) -> None:
        instrument(d, "EXV1")["exposures"]["grid"]["measures"] = False

    refused(shipped, unmeasured, "cells neither measured by an instrument nor declared")


def test_an_unmapped_block_must_claim_real_unmeasured_cells_once(shipped: Data) -> None:
    def market(d: Data) -> None:
        d["grid"]["unmapped_cells"][0]["market"] = "atlantis"

    refused(shipped, market, "names market atlantis, not on the axis")

    def nothing_left(d: Data) -> None:
        d["grid"]["unmapped_cells"].append(
            {"market": "japan", "sectors": "remaining", "reason": "none"}
        )

    refused(shipped, nothing_left, r"\(japan\) claims no cell")

    def sector(d: Data) -> None:
        d["grid"]["unmapped_cells"].append(
            {"market": "japan", "sectors": ["weather"], "reason": "none"}
        )

    refused(shipped, sector, "names sector weather, not on the axis")

    def measured(d: Data) -> None:
        d["grid"]["unmapped_cells"].append(
            {"market": "japan", "sectors": ["energy"], "reason": "none"}
        )

    refused(shipped, measured, "japan/energy is declared unmapped and measured by 1618")

    def twice(d: Data) -> None:
        block = next(b for b in d["grid"]["unmapped_cells"] if b["sectors"] != "remaining")
        d["grid"]["unmapped_cells"].append(copy.deepcopy(block))

    refused(shipped, twice, "is declared unmapped twice")


def test_a_native_index_measures_its_broad_cell_in_its_markets_currency(shipped: Data) -> None:
    refused(
        shipped,
        lambda d: d["grid"]["markets"]["japan"].update(native_index="EWJ"),
        "native_index EWJ does not measure japan/broad",
    )
    refused(
        shipped,
        lambda d: d["grid"]["markets"]["japan"].update(currency="USD"),
        "measure another currency's return",
    )


# ── the macro board and references ──


def test_the_macro_board_refuses_unknown_double_and_contradictory_claims(shipped: Data) -> None:
    claimant = next(i for i in shipped["instruments"] if i["exposures"]["macro_board"])
    ref = claimant["exposures"]["macro_board"][0]
    refused(
        shipped,
        lambda d: instrument(d, claimant["symbol"])["exposures"]["macro_board"].append("mars.gdp"),
        "claims mars.gdp, not a cell of the board",
    )

    def double(d: Data) -> None:
        other = next(i for i in d["instruments"] if i["symbol"] != claimant["symbol"])
        other["exposures"]["macro_board"].append(ref)

    refused(shipped, double, f"both claim {ref}")
    region, _, asset_class = ref.partition(".")

    def contradiction(d: Data) -> None:
        d["macro_board"]["regions"][region]["cells"][asset_class]["unmapped"] = "no input"

    refused(shipped, contradiction, f"{ref} must either name its inputs")

    def unknown_root(d: Data) -> None:
        cell = d["macro_board"]["regions"]["world"]["cells"]["commodities"]
        cell["roots"] = [*cell["roots"], "ZZ"]

    refused(shipped, unknown_root, "names root ZZ, not a declared futures root")


def test_every_reference_resolves(shipped: Data) -> None:
    root = shipped["futures_roots"][0]["root"]
    refused(
        shipped,
        lambda d: d["futures_roots"].append(copy.deepcopy(d["futures_roots"][0])),
        "a futures root is declared twice",
    )
    refused(
        shipped,
        lambda d: d["commodity_buckets"].append(d["commodity_buckets"][0]),
        "lists a bucket twice",
    )
    refused(
        shipped,
        lambda d: d["futures_roots"][0].update(bucket="weather"),
        f"futures root {root} names bucket weather",
    )
    refused(
        shipped,
        lambda d: instrument(d, "FCX")["exposures"].update(commodity_bucket="weather"),
        "FCX names bucket weather",
    )

    def rolls(d: Data) -> None:
        instrument(d, "IWDA")["attributes"].update(replication="futures", futures_root="ZZ")

    refused(shipped, rolls, "IWDA rolls ZZ, not a declared futures root")
    refused(
        shipped,
        lambda d: instrument(d, "EWJ")["holdings"].update(candidates_for="atlantis"),
        "EWJ supplies candidates for atlantis",
    )


def test_every_proxy_reference_resolves(shipped: Data) -> None:
    proxy = shipped["commodity_proxies"][0]["symbol"]
    refused(
        shipped,
        lambda d: d["commodity_proxies"].append(copy.deepcopy(d["commodity_proxies"][0])),
        "a commodity proxy is declared twice",
    )
    refused(
        shipped,
        lambda d: d["commodity_proxies"][0].update(symbol="NOPE"),
        "commodity proxy NOPE is not a declared instrument",
    )
    refused(
        shipped,
        lambda d: d["commodity_proxies"][0].update(root="ZZ"),
        f"commodity proxy {proxy} names root ZZ",
    )
    refused(
        shipped,
        lambda d: d["commodity_proxies"][0].update(reference_kind="curve", reference="ZZ"),
        "references curve ZZ",
    )


def test_every_market_has_a_candidate_source_or_declares_its_gaps(shipped: Data) -> None:
    refused(
        shipped,
        lambda d: d["candidates"]["scanners"][0].update(market="atlantis"),
        "scanners names market atlantis",
    )
    refused(
        shipped,
        lambda d: d["candidates"]["gaps"][0].update(market="atlantis"),
        "gaps names market atlantis",
    )
    refused(
        shipped,
        lambda d: d["candidates"]["gaps"][0].update(sectors=["weather"]),
        "gaps names sector weather",
    )

    def unsourced(d: Data) -> None:
        for declared in d["instruments"]:
            holdings = declared.get("holdings")
            if holdings and holdings["candidates_for"] == "canada":
                holdings["candidates_for"] = None

    refused(shipped, unsourced, "market canada has no candidate source")


# ── the configuration around the universe ──


def _with(settings: ObliskConfig, section: str, update: Callable[[Data], None]) -> Data:
    data = settings.model_dump(mode="json")
    update(data[section] if section else data)
    return data


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (
            lambda d: instrument(d["universe"], "IWDA").update(listing_exchange="NOWHERE"),
            "universe IWDA.listing_exchange: NOWHERE is not in ibkr_exchanges",
        ),
        (
            lambda d: instrument(d["universe"], "IWDA").update(currency="JPY"),
            "universe IWDA: JPY is not one of venues.XLON.currencies",
        ),
        (
            lambda d: d["collectors"]["candidates"]["exchanges"].update(Nowhere="NOWHERE"),
            "collectors.candidates.exchanges: NOWHERE is not in ibkr_exchanges",
        ),
        (
            lambda d: d["collectors"]["candidates"]["sectors"].update(Weather="weather"),
            "weather is not a sector of the grid",
        ),
        (
            lambda d: d["research"]["data"]["yahoo"]["digits"].update(NOWHERE=4),
            "research.data.yahoo.digits.NOWHERE: not in ibkr_exchanges",
        ),
        (
            lambda d: d["market_data"]["feeds"][0].update(venues=["XXXX"]),
            "market_data.feeds.us_complimentary: XXXX is not a venue",
        ),
        (
            lambda d: d["collectors"]["schedule"].update(fallback_venue="XXXX"),
            "collectors.schedule.fallback.venue: XXXX is not a venue",
        ),
    ],
)
def test_the_configuration_refuses_what_the_universe_cannot_resolve(
    shipped_settings: ObliskConfig, change: Callable[[Data], None], match: str
) -> None:
    data = shipped_settings.model_dump(mode="json")
    change(data)
    with pytest.raises(ValidationError, match=match):
        ObliskConfig.model_validate(data)


def test_the_collectors_market_data_and_tenor_sections_refuse_their_own_contradictions(
    shipped_settings: ObliskConfig,
) -> None:
    collectors = shipped_settings.collectors.model_dump(mode="json")
    slow = {**collectors, "retry": {**collectors["retry"], "backoff_max_seconds": 1.0}}
    with pytest.raises(ValidationError, match="must not be below backoff_seconds"):
        CollectorsConfig.model_validate(slow)
    budgets = dict(collectors["step_deadline_seconds"])
    budgets.pop("chains")
    with pytest.raises(ValidationError, match=r"no budget for \['chains'\]"):
        CollectorsConfig.model_validate({**collectors, "step_deadline_seconds": budgets})
    plan = shipped_settings.market_data.model_dump(mode="json")
    for mutated, match in (
        ({**plan, "feeds": [*plan["feeds"], plan["feeds"][0]]}, "lists a feed id twice"),
        ({**plan, "plan": [*plan["plan"], plan["plan"][0]]}, "lists a feed twice"),
        ({**plan, "plan": ["nope"]}, r"not offered: \['nope'\]"),
    ):
        with pytest.raises(ValidationError, match=match):
            MarketDataConfig.model_validate(mutated)
    tenor = shipped_settings.tenor.model_dump(mode="json")
    with pytest.raises(ValidationError, match="entry_dte_min must be below"):
        TenorConfig.model_validate({**tenor, "entry_dte_min": tenor["entry_dte_max"]})


def test_the_universe_file_holds_one_section_and_no_default_defines_it(config_dir: Path) -> None:
    universe = config_dir / "universe.yaml"
    text = universe.read_text(encoding="utf-8")
    universe.write_text(text + "\nstray: 1\n", encoding="utf-8")
    with pytest.raises(
        ConfigInvalid, match="is not a section of this file; it holds only universe"
    ):
        load_config(config_dir)
    universe.write_text(text, encoding="utf-8")
    extra = config_dir / "defaults" / "zz_universe.yaml"
    extra.write_text(yaml.safe_dump({"universe": {"grid": {}}}), encoding="utf-8")
    with pytest.raises(ConfigInvalid, match="cannot also be a default"):
        load_config(config_dir)
    extra.unlink()
    universe.write_text("[not, a, mapping]\n", encoding="utf-8")
    with pytest.raises(ConfigInvalid):
        load_config(config_dir)
