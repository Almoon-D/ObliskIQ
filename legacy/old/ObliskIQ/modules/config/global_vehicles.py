"""
Global Vehicles — static seed data for the global-reach layer (v6.13.0)
=======================================================================

Single, DRY home for the *static reference data* the global-reach unlock needs:

  * ``EXCHANGE_SUFFIX_MAP`` — yfinance exchange suffix -> (exchange, ISO currency).
    Drives both ticker resolution (``ticker_resolver.py``) and FX tagging.
  * ``ADR_MAP`` — foreign exchange symbol -> the LIQUID US-listed ADR line.
  * ``NAME_TO_SYMBOL`` — company name -> the canonical yfinance symbol we trade.
  * ``REGION_ETFS`` / ``REGION_ADRS`` — per-region tradable vehicles the Phase-1
    payload surfaces so the Phase-2 AI can turn a regional macro/sentiment view
    into a concrete instrument (``hybrid_formatter._format_regional_vehicles_section``).
  * ``trading_currency_for`` — stateless symbol -> trading-currency helper reused
    by the resolver, the formatter, and the thesis-ledger write site.

WHY here (not ``strategy_universe.py``): ``strategy_universe`` is a US-only
mega-cap snapshot consumed by the verdict runner + IV collector. Global ADR /
region maps are a different concern; mixing them there would muddy that module.
This file is config-layer static data — no network, no runtime state.

FREE-ENDPOINT NOTE: every symbol here comes from public listings; nothing in this
module makes a network call. The resolver's only optional live path (yfinance
search) is injected at call time, never wired here.

ADR LIQUIDITY IS A STATIC ASSERTION (verified 2026-07): membership in ``ADR_MAP``
means the mapped ADR was a hand-verified, exchange-listed (NYSE/Nasdaq) line at
authoring time — NOT a live volume measurement. OTC/pink-sheet ADRs (e.g. Nestlé
NSRGY, Tencent TCEHY, Samsung SSNLF, Roche RHHBY, Siemens SIEGY) are deliberately
EXCLUDED so those names resolve to their liquid local listing + a tag, never a
low-liquidity OTC line. A live volume probe can override this at runtime; see
``ticker_resolver.TickerResolver(volume_probe=...)``.

Region keys are the literal ``Region.value`` strings from
``modules.market.regional_sectoral_config`` (NA, Europe, China, Japan, India,
Asia, LatAm, Middle_East, Oceania, Africa). They are duplicated as plain strings
here on purpose — to keep this config module free of a market-layer import. A
unit test (``test_global_vehicles.py``) asserts the key set stays in sync with
the enum.
"""

from __future__ import annotations

from typing import Dict, FrozenSet, List, Optional, Tuple

# -----------------------------------------------------------------------------
# Exchange suffix -> (exchange name, ISO 4217 trading currency)
# -----------------------------------------------------------------------------
#: yfinance appends an exchange suffix to non-US symbols (e.g. ``005930.KS``).
#: This maps the suffix to the exchange and the currency the line trades in.
EXCHANGE_SUFFIX_MAP: Dict[str, Tuple[str, str]] = {
    # --- Asia-Pacific ---
    ".KS": ("KRX", "KRW"),  # Korea Exchange (KOSPI)
    ".KQ": ("KOSDAQ", "KRW"),  # Korea (KOSDAQ)
    ".T": ("TSE", "JPY"),  # Tokyo
    ".HK": ("HKEX", "HKD"),  # Hong Kong
    ".SS": ("SSE", "CNY"),  # Shanghai
    ".SZ": ("SZSE", "CNY"),  # Shenzhen
    ".TW": ("TWSE", "TWD"),  # Taiwan
    ".TWO": ("TPEx", "TWD"),  # Taiwan (OTC)
    ".NS": ("NSE", "INR"),  # India (National Stock Exchange)
    ".BO": ("BSE", "INR"),  # India (Bombay Stock Exchange)
    ".SI": ("SGX", "SGD"),  # Singapore
    ".BK": ("SET", "THB"),  # Thailand
    ".JK": ("IDX", "IDR"),  # Indonesia
    ".KL": ("Bursa_Malaysia", "MYR"),  # Malaysia
    ".AX": ("ASX", "AUD"),  # Australia
    ".NZ": ("NZX", "NZD"),  # New Zealand
    # --- Europe ---
    ".L": ("LSE", "GBP"),  # London
    ".PA": ("Euronext_Paris", "EUR"),
    ".AS": ("Euronext_Amsterdam", "EUR"),
    ".BR": ("Euronext_Brussels", "EUR"),
    ".LS": ("Euronext_Lisbon", "EUR"),
    ".IR": ("Euronext_Dublin", "EUR"),
    ".DE": ("XETRA", "EUR"),  # Germany
    ".F": ("FSE", "EUR"),  # Frankfurt
    ".MI": ("Borsa_Italiana", "EUR"),  # Milan
    ".MC": ("BME", "EUR"),  # Madrid
    ".VI": ("Wiener_Boerse", "EUR"),  # Vienna
    ".SW": ("SIX", "CHF"),  # Switzerland
    ".ST": ("Nasdaq_Stockholm", "SEK"),  # Sweden
    ".HE": ("Nasdaq_Helsinki", "EUR"),  # Finland
    ".CO": ("Nasdaq_Copenhagen", "DKK"),  # Denmark
    ".OL": ("Oslo_Bors", "NOK"),  # Norway
    ".AT": ("ATHEX", "EUR"),  # Athens
    # --- Americas (ex-US) ---
    ".TO": ("TSX", "CAD"),  # Toronto
    ".V": ("TSXV", "CAD"),  # TSX Venture
    ".SA": ("B3", "BRL"),  # Brazil (Sao Paulo)
    ".MX": ("BMV", "MXN"),  # Mexico
    ".BA": ("BCBA", "ARS"),  # Argentina (Buenos Aires)
    ".SN": ("BCS", "CLP"),  # Chile (Santiago)
    # --- Middle East & Africa ---
    ".SR": ("Tadawul", "SAR"),  # Saudi Arabia
    ".TA": ("TASE", "ILS"),  # Israel (Tel Aviv)
    ".QA": ("QSE", "QAR"),  # Qatar
    ".JO": ("JSE", "ZAR"),  # South Africa (Johannesburg)
    ".CA": ("EGX", "EGP"),  # Egypt (Cairo)
}

# -----------------------------------------------------------------------------
# Foreign symbol -> LIQUID US ADR (static liquidity assertion, verified 2026-07)
# -----------------------------------------------------------------------------
#: Only exchange-listed (NYSE/Nasdaq) ADRs. Names whose only US line is OTC are
#: intentionally absent so they resolve to their local listing + a tag.
ADR_MAP: Dict[str, str] = {
    # Japan
    "7203.T": "TM",  # Toyota Motor
    "6758.T": "SONY",  # Sony Group
    "8306.T": "MUFG",  # Mitsubishi UFJ Financial
    "7267.T": "HMC",  # Honda Motor
    # Europe / UK
    "BP.L": "BP",  # BP
    "SHEL.L": "SHEL",  # Shell
    "AZN.L": "AZN",  # AstraZeneca
    "HSBA.L": "HSBC",  # HSBC Holdings
    "ULVR.L": "UL",  # Unilever
    "SAP.DE": "SAP",  # SAP SE
    "NOVN.SW": "NVS",  # Novartis
    "NOVO-B.CO": "NVO",  # Novo Nordisk
    "ASML.AS": "ASML",  # ASML Holding
    # Greater China
    "9988.HK": "BABA",  # Alibaba (also HK-primary; ADR is deep-liquid)
    "2330.TW": "TSM",  # Taiwan Semiconductor
    # India
    "INFY.NS": "INFY",  # Infosys
    "WIPRO.NS": "WIT",  # Wipro
    "HDFCBANK.NS": "HDB",  # HDFC Bank
    "ICICIBANK.NS": "IBN",  # ICICI Bank
    # Latin America
    "VALE3.SA": "VALE",  # Vale
    "PETR4.SA": "PBR",  # Petrobras
    "ITUB4.SA": "ITUB",  # Itau Unibanco
    # Oceania (dual-listed miners)
    "BHP.AX": "BHP",  # BHP Group
    "RIO.AX": "RIO",  # Rio Tinto
    # Canada (dual-listed, US line deep-liquid)
    "SHOP.TO": "SHOP",  # Shopify
    "RY.TO": "RY",  # Royal Bank of Canada
    "TD.TO": "TD",  # Toronto-Dominion
    "CNQ.TO": "CNQ",  # Canadian Natural Resources
}

#: The set of US ADR symbols (used to classify a resolved US-shaped symbol as an
#: ADR rather than a native US listing). Derived from ``ADR_MAP`` — DRY.
ADR_SYMBOLS: FrozenSet[str] = frozenset(ADR_MAP.values())

# -----------------------------------------------------------------------------
# Company name -> canonical yfinance symbol (the line we actually trade)
# -----------------------------------------------------------------------------
#: Names are looked up upper-cased + whitespace-collapsed. Values are the FINAL
#: preferred symbol: an ADR where a liquid one exists (Toyota -> TM), otherwise
#: the local listing (Samsung -> 005930.KS). The resolver classifies the value.
NAME_TO_SYMBOL: Dict[str, str] = {
    # -> liquid ADR
    "TOYOTA": "TM",
    "TOYOTA MOTOR": "TM",
    "SONY": "SONY",
    "SONY GROUP": "SONY",
    "MITSUBISHI UFJ": "MUFG",
    "HONDA": "HMC",
    "HONDA MOTOR": "HMC",
    "BP": "BP",
    "SHELL": "SHEL",
    "ROYAL DUTCH SHELL": "SHEL",
    "ASTRAZENECA": "AZN",
    "HSBC": "HSBC",
    "UNILEVER": "UL",
    "SAP": "SAP",
    "NOVARTIS": "NVS",
    "NOVO NORDISK": "NVO",
    "ASML": "ASML",
    "ALIBABA": "BABA",
    "TSMC": "TSM",
    "TAIWAN SEMICONDUCTOR": "TSM",
    "INFOSYS": "INFY",
    "WIPRO": "WIT",
    "HDFC BANK": "HDB",
    "ICICI BANK": "IBN",
    "VALE": "VALE",
    "PETROBRAS": "PBR",
    "ITAU": "ITUB",
    "ITAU UNIBANCO": "ITUB",
    "BHP": "BHP",
    "BHP GROUP": "BHP",
    "RIO TINTO": "RIO",
    "SHOPIFY": "SHOP",
    "ROYAL BANK OF CANADA": "RY",
    "TORONTO-DOMINION": "TD",
    "MERCADOLIBRE": "MELI",  # US-native (Nasdaq); no foreign primary line
    # -> local listing (no liquid US ADR; keep local + tag)
    "SAMSUNG": "005930.KS",
    "SAMSUNG ELECTRONICS": "005930.KS",
    "SK HYNIX": "000660.KS",
    "TENCENT": "0700.HK",
    "MEITUAN": "3690.HK",
    "BYD": "1211.HK",
    "AIA": "1299.HK",
    "SOFTBANK": "9984.T",
    "NINTENDO": "7974.T",
    "KEYENCE": "6861.T",
    "NESTLE": "NESN.SW",
    "ROCHE": "ROG.SW",
    "NOVO NORDISK B": "NOVO-B.CO",
    "LVMH": "MC.PA",
    "TOTALENERGIES": "TTE.PA",
    "SIEMENS": "SIE.DE",
    "VOLKSWAGEN": "VOW3.DE",
    "RELIANCE": "RELIANCE.NS",
    "RELIANCE INDUSTRIES": "RELIANCE.NS",
    "TATA CONSULTANCY": "TCS.NS",
    "TATA CONSULTANCY SERVICES": "TCS.NS",
    "SAUDI ARAMCO": "2222.SR",
    "ARAMCO": "2222.SR",
    "AL RAJHI BANK": "1120.SR",
    "NASPERS": "NPN.JO",
    "PROSUS": "PRX.AS",
    "MTN GROUP": "MTN.JO",
    "COMMONWEALTH BANK": "CBA.AX",
    "BHP BILLITON": "BHP",
}

# -----------------------------------------------------------------------------
# Per-region tradable vehicles (keyed by Region.value)
# -----------------------------------------------------------------------------
#: Country/region ETFs — the liquid, US-listed way to express a regional view.
#: Invariant: every region has >= 1 ETF (enforced by test_global_vehicles.py).
REGION_ETFS: Dict[str, List[str]] = {
    "NA": ["SPY", "QQQ", "IWM", "EWC"],
    "Europe": ["VGK", "EZU", "EWG", "EWQ", "EWU", "EWL"],
    "China": ["MCHI", "FXI", "KWEB", "ASHR"],
    "Japan": ["EWJ", "DXJ"],
    "India": ["INDA", "EPI", "SMIN"],
    "Asia": ["AAXJ", "EWY", "EWT", "EWS", "EWM"],
    "LatAm": ["ILF", "EWZ", "EWW"],
    "Middle_East": ["KSA", "EIS", "TUR"],
    "Oceania": ["EWA", "ENZL"],
    "Africa": ["EZA", "AFK", "NGE"],
}

#: Liquid single-name US ADRs by region — a regional view expressed as a stock.
#: May be empty for a region (its liquid names are US-native listings).
REGION_ADRS: Dict[str, List[str]] = {
    "NA": ["SHOP", "RY", "TD"],
    "Europe": ["BP", "SHEL", "AZN", "NVS", "NVO", "SAP", "UL", "HSBC", "ASML"],
    "China": ["BABA", "JD", "PDD", "BIDU", "NIO"],
    "Japan": ["TM", "SONY", "MUFG", "HMC"],
    "India": ["INFY", "HDB", "IBN", "WIT"],
    "Asia": ["TSM", "KB", "SKM"],
    "LatAm": ["VALE", "PBR", "ITUB", "MELI"],
    "Middle_East": ["TEVA"],
    "Oceania": ["BHP", "RIO"],
    "Africa": ["GFI", "AU", "SBSW"],
}

#: Underlying-currency exposure per region. The ETFs/ADRs above trade in USD on US
#: venues, but their economic exposure is to these currencies — surfaced so the
#: Phase-2 AI sees FX risk even when the tradable vehicle is USD-denominated. This
#: is the regional counterpart to the per-position ``trading_currency`` the resolver
#: tags and the thesis ledger stores.
REGION_UNDERLYING_FX: Dict[str, List[str]] = {
    "NA": ["USD", "CAD"],
    "Europe": ["EUR", "GBP", "CHF"],
    "China": ["CNY", "HKD"],
    "Japan": ["JPY"],
    "India": ["INR"],
    "Asia": ["KRW", "TWD", "SGD"],
    "LatAm": ["BRL", "MXN"],
    "Middle_East": ["SAR", "ILS"],
    "Oceania": ["AUD", "NZD"],
    "Africa": ["ZAR"],
}


def is_foreign_listing(symbol: str) -> bool:
    """True for a symbol carrying a known non-US exchange suffix (``7203.T``, ``SAP.DE``, ``BP.L``).

    US-native tickers and US ADRs return False. Used to route fundamentals lookups: the free
    Alpha Vantage tier covers US listings only, so spending its 25 calls/day on ``7203.T`` returns
    nothing and starves the tickers it can serve.
    """
    if not symbol or "." not in symbol:
        return False
    return "." + symbol.rsplit(".", 1)[1].upper() in EXCHANGE_SUFFIX_MAP


def trading_currency_for(symbol: str) -> str:
    """Return the ISO 4217 trading currency for a yfinance symbol.

    Stateless: derives the currency from the exchange suffix. A symbol with no
    known suffix (US-native listings and US ADRs, which trade in USD) returns
    ``"USD"``. Reused by the resolver, the Phase-1 formatter, and the
    thesis-ledger write site so FX tagging has ONE source of truth.

    Examples::

        trading_currency_for("005930.KS") -> "KRW"
        trading_currency_for("AAPL")      -> "USD"
        trading_currency_for("TM")        -> "USD"   # ADR trades in USD
        trading_currency_for("VALE3.SA")  -> "BRL"
    """
    if not symbol or "." not in symbol:
        return "USD"
    suffix = "." + symbol.rsplit(".", 1)[1].upper()
    exchange_currency = EXCHANGE_SUFFIX_MAP.get(suffix)
    if exchange_currency is None:
        return "USD"
    return exchange_currency[1]


# -----------------------------------------------------------------------------
# Scoring benchmarks (AI scoreboard, v6.16.0)
# -----------------------------------------------------------------------------
#: Exchange suffix -> ``Region.value``. A foreign line is graded against ITS region's benchmark,
#: not SPY: beating the S&P 500 with a Japanese name is mostly a yen/Nikkei bet, not stock selection.
#: A unit test asserts every suffix of ``EXCHANGE_SUFFIX_MAP`` is mapped.
SUFFIX_REGION: Dict[str, str] = {
    ".TO": "NA", ".V": "NA",
    ".L": "Europe", ".PA": "Europe", ".AS": "Europe", ".BR": "Europe", ".LS": "Europe",
    ".IR": "Europe", ".DE": "Europe", ".F": "Europe", ".MI": "Europe", ".MC": "Europe",
    ".VI": "Europe", ".SW": "Europe", ".ST": "Europe", ".HE": "Europe", ".CO": "Europe",
    ".OL": "Europe", ".AT": "Europe",
    ".HK": "China", ".SS": "China", ".SZ": "China",
    ".T": "Japan",
    ".NS": "India", ".BO": "India",
    ".KS": "Asia", ".KQ": "Asia", ".TW": "Asia", ".TWO": "Asia", ".SI": "Asia", ".BK": "Asia",
    ".JK": "Asia", ".KL": "Asia",
    ".SA": "LatAm", ".MX": "LatAm", ".BA": "LatAm", ".SN": "LatAm",
    ".SR": "Middle_East", ".TA": "Middle_East", ".QA": "Middle_East",
    ".AX": "Oceania", ".NZ": "Oceania",
    ".JO": "Africa", ".CA": "Africa",
}

US_BENCHMARK = "SPY"
GLOBAL_BENCHMARK = "ACWI"

#: Currencies Yahoo quotes as "<CCY>USD=X" (USD per unit). Every other currency is quoted as
#: "USD<CCY>=X" (units per USD) and must be inverted to get USD per unit.
_USD_PER_UNIT_QUOTED = frozenset({"EUR", "GBP", "AUD", "NZD"})


def region_for_symbol(symbol: str) -> Optional[str]:
    """``Region.value`` a symbol belongs to, or None for a US-native listing we cannot place.

    Order: exchange suffix (foreign local line) -> known ADR -> known country/region ETF.
    """
    if not symbol:
        return None
    sym = symbol.upper()
    if "." in sym:
        suffix = "." + sym.rsplit(".", 1)[1]
        if suffix in SUFFIX_REGION:
            return SUFFIX_REGION[suffix]
    for region, adrs in REGION_ADRS.items():
        if sym in adrs:
            return region
    for region, etfs in REGION_ETFS.items():
        if sym in etfs:
            return region
    return None


def benchmark_for_symbol(symbol: str) -> str:
    """The benchmark a proposal on ``symbol`` is graded against (USD-denominated ETF).

    US-native names -> SPY. A foreign line, ADR or country ETF -> the first (broad) ETF of its region
    (``REGION_ETFS[region][0]``; coarse by design: e.g. Middle_East -> KSA). A symbol that IS its own
    benchmark (SPY, VGK, EWJ, ...) or the NA region is graded against ACWI/SPY so excess return is never
    identically zero.
    """
    sym = (symbol or "").upper()
    region = region_for_symbol(sym)
    if region is None or region == "NA":
        candidate = US_BENCHMARK
    else:
        candidate = REGION_ETFS[region][0]
    if sym == candidate:
        return GLOBAL_BENCHMARK
    return candidate


def fx_pair_for_currency(currency: str) -> Optional[Tuple[str, bool]]:
    """Yahoo FX symbol that converts ``currency`` to USD, as ``(symbol, invert)``.

    ``invert`` is True when the quote is "units of ``currency`` per USD", so USD per unit is the
    reciprocal. USD (or empty) needs no conversion -> None.
    """
    ccy = (currency or "").upper()
    if not ccy or ccy == "USD":
        return None
    if ccy in _USD_PER_UNIT_QUOTED:
        return f"{ccy}USD=X", False
    return f"USD{ccy}=X", True
