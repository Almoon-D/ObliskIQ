# Universe sources: the core's vehicles

Where each core vehicle's identifiers come from and how well it tracks its role, as read on
2026-10-22. The values the code uses are `config/universe.yaml` (each instrument's `attributes` and
`source`) and `config/defaults/core.yaml` (`core.roles`); the choices behind them are DECISIONS
D121 and D122. Tags as in `README.md`; not investment or tax advice.

## How a vehicle was confirmed

- **Two sources per identifier.** The ISIN, issuer, tracked index, fee, share class (accumulating
  or distributing), currency hedge, domicile, UCITS status and launch date were read on the
  issuer's product page or factsheet and on justETF's profile of the same ISIN; the PRIIPs KID was
  found among the issuer's documents (`kid: available`) or recorded as unknown when the issuer's
  site could not be read. Each instrument's `source` names both pages and the date.
- **Conids from IBKR's contract search.** The conid, IBKR's symbol, listing exchange and currency
  were read by ISIN through IBKR's MCP connector `[MCP ✓ 2026-10-22]`; a listing the search did not
  return is not used (`ibkr-capabilities.md`). VERIFY-LIVE(conids on CP).
- **Preference.** Accumulating classes first (D121; `vehicles.ranking.weights.tax`), then a Xetra
  line in EUR, then another venue. The owner's market-data plan (`market_data.plan`) covers no
  European cash venue; the ETF and ETC quote policy accepts delayed quotes, so a vehicle on any of
  these venues needs no subscription.
- **Hedged classes are separate vehicles** (D69): IWLE, IBC5 and IBB1 are the EUR-hedged classes of
  IWDA's, TPSA's and CSBGU0's funds, with their own ISINs and conids. They are refused until the
  owner rules on `vehicles.hedged_share_classes` (D122).

## Tracking measurement

Monthly log returns of each vehicle's adjusted close (`universe_<symbol>.adj_close`, Yahoo) against
its role's `exposure_series`, both converted to EUR at the ECB's reference rates, over the five
years to 2026-10-22, point in time (`vehicles.correlation`, D121). Each reading below rests on 60
monthly returns unless noted `[Yahoo ✓ 2026-10-22]`. Weekly returns read lower for reasons that are
not tracking: closes in different time zones (XMME against MSCI EM: 0.704 weekly over three years,
0.977 monthly over five) and isolated bad ticks (XMME's Yahoo history, 2025-10-23 to 31).

Selector: the verdict of `obliskiq universe vehicles <role>` on that date with the research store
loaded and today's capabilities (`ibkr-capabilities.md`).

### world_equity — `msci_world_eur.level`

| Symbol | ISIN | Issuer | Index | Class | IBKR listing | Conid | ρ | Selector |
|---|---|---|---|---|---|---|---|---|
| IWDA | IE00B4L5Y983 | iShares | MSCI World | acc, 0.20 % | LSEETF, USD | 78999785 | 0.964 | capability unverified (XLON) |
| XDWD | IE00BJ0KDQ92 | Xtrackers | MSCI World | acc, 0.12 % | IBIS2, EUR | 163606916 | 0.972 | proposed |
| SPPW | IE00BFY0GT14 | State Street SPDR | MSCI World | acc, 0.12 % | IBIS2, EUR | 355720826 | 0.972 | proposed |
| VHVE | IE00BK5BQV03 | Vanguard | FTSE Developed | acc, 0.12 % | BVME.ETF, EUR | 399364016 | 0.966 | capability unverified (XMIL) |
| IWLE | IE00BKBF6H24 | iShares | MSCI World, EUR-hedged | dist, 0.30 % | IBIS2, EUR | 393119102 | 0.908 | hedged class |

### em_equity — `msci_em_eur.level`

| Symbol | ISIN | Issuer | Index | Class | IBKR listing | Conid | ρ | Selector |
|---|---|---|---|---|---|---|---|---|
| EMIM | IE00BKM4GZ66 | iShares | MSCI EM IMI | acc, 0.18 % | AEB, EUR | 153454120 | 0.973 | capability unverified (XAMS) |
| XMME | IE00BTJRMP35 | Xtrackers | MSCI Emerging Markets | acc, 0.18 % | IBIS2, EUR | 280737629 | 0.977 | proposed |
| IEMA | IE00B4L5YC18 | iShares | MSCI Emerging Markets | acc, 0.18 % | AEB, EUR | 100292766 | 0.979 | capability unverified (XAMS) |
| VFEA | IE00BK5BR733 | Vanguard | FTSE Emerging | acc, 0.17 % | IBIS2, EUR | 385047109 | 0.932 | proposed |

### euro_govt_intermediate — `bund_zero_05y.level`

| Symbol | ISIN | Issuer | Index | Class | IBKR listing | Conid | ρ | Selector |
|---|---|---|---|---|---|---|---|---|
| IBGM | IE00B1FZS806 | iShares | Bloomberg Euro Govt 7-10 Year Term | dist, 0.15 % | AEB, EUR | 43614383 | 0.941 | capability unverified (XAMS) |
| CSBGE7 | IE00B3VTML14 | iShares | Bloomberg Euro Govt 3-7 Year Term | acc, 0.15 % | EBS, EUR | 79000241 | 0.967 | capability unverified (XSWX) |
| DBXQ | LU0290356954 | Xtrackers | iBoxx EUR Eurozone 3-5 | acc, 0.15 % | IBIS2, EUR | 53507356 | 0.964 | proposed |
| X57E | LU0290357176 | Xtrackers | iBoxx EUR Eurozone 5-7 | acc, 0.15 % | IBIS2, EUR | 53507360 | 0.961 | proposed |
| MTB | LU1650488494 | Amundi | Bloomberg Euro Treasury 50bn 3-5 Year | acc, 0.15 % | SBF, EUR | 297475417 | 0.965 | capability unverified (XPAR) |

### euro_govt_long — `bund_zero_17y.level`

| Symbol | ISIN | Issuer | Index | Class | IBKR listing | Conid | ρ | Selector |
|---|---|---|---|---|---|---|---|---|
| IBCL | IE00B1FZS913 | iShares | Bloomberg Euro Govt 30 Year Term | dist, 0.15 % | IBIS2, EUR | 43614368 | 0.949 | proposed |
| DBXG | LU0290357846 | Xtrackers | iBoxx EUR Eurozone 25+ | acc, 0.15 % | IBIS2, EUR | 48701963 | 0.944 | proposed |

### inflation_linked — `tip.adj_close`

| Symbol | ISIN | Issuer | Index | Class | IBKR listing | Conid | ρ | Selector |
|---|---|---|---|---|---|---|---|---|
| TPSA | IE00B1FZSC47 | iShares | Bloomberg US Govt Inflation-Linked | acc, 0.10 % | AEB, EUR | 100292445 | 0.974 | capability unverified (XAMS) |
| IBC5 | IE00BDZVH966 | iShares | the same, EUR-hedged | acc, 0.12 % | IBIS2, EUR | 309787188 | 0.385 | hedged class |
| TIUP | LU1452600270 | Amundi | Bloomberg US Govt Inflation-Linked | dist, 0.09 % | IBIS, EUR | 255605037 | 0.971 | proposed |
| TIP1D | LU1459801434 | UBS | Bloomberg US Govt 1-10 Year Inflation-Linked | dist, 0.08 % | BVME.ETF, EUR | 255179957 | 0.932 | ineligible (KID unknown) |

### us_treasuries — `ief.adj_close`

| Symbol | ISIN | Issuer | Index | Class | IBKR listing | Conid | ρ | Selector |
|---|---|---|---|---|---|---|---|---|
| CSBGU0 | IE00B3VWN518 | iShares | ICE US Treasury 7-10 Year | acc, 0.07 % | EBS, USD | 79000139 | 0.984 | capability unverified (XSWX) |
| IBB1 | IE00BGPP6697 | iShares | the same, EUR-hedged | dist, 0.10 % | IBIS2, EUR | 394126588 | 0.523 | hedged class |
| VDTA | IE00BGYWFS63 | Vanguard | Bloomberg Global Agg US Treasury Float Adjusted | acc, 0.05 % | BVME.ETF, EUR | 399364034 | 0.921 | capability unverified (XMIL) |
| XUTD | LU0429459356 | Xtrackers | iBoxx USD Treasuries | dist, 0.06 % | IBIS2, EUR | 220580484 | 0.931 | proposed |

### gold — `gld.close`

No UCITS fund holds only gold (a UCITS fund must diversify), so the role is served by ETCs: debt
securities backed by allocated bullion, eligible under `es` with a KID and taxed as debt (`tax-es.md`).

| Symbol | ISIN | Issuer | Index | Class | IBKR listing | Conid | ρ | Selector |
|---|---|---|---|---|---|---|---|---|
| IGLN | IE00B4ND3602 | iShares | LBMA Gold Price | ETC, 0.12 % | LSEETF, USD | 86656182 | 0.980 | capability unverified (XLON) |
| SGLD | IE00B579F325 | Invesco | LBMA Gold Price | ETC, 0.12 % | BVME.ETF, EUR | 175394979 | 0.974 | capability unverified (XMIL) |
| 4GLD | DE000A0S9GB0 | Deutsche Börse Commodities | none (delivery right on 1 g) | ETC, custody at the depositary | IBIS, EUR | 50784405 | 0.974 | ineligible (KID unknown) |

### broad_commodities — `crsox.adj_close`

| Symbol | ISIN | Issuer | Index | Class | IBKR listing | Conid | ρ | Selector |
|---|---|---|---|---|---|---|---|---|
| CMOD | IE00BD6FTQ80 | Invesco | Bloomberg Commodity | acc, 0.19 % + swap fee | LSEETF, USD | 262067279 | 0.984 | capability unverified (XLON) |
| ICOM | IE00BDFL4P12 | iShares | Bloomberg Commodity | acc, 0.19 % | LSEETF, USD | 282849246 | 0.983 | capability unverified (XLON) |
| UIQK | IE00B53H0131 | UBS | UBS Bloomberg CMCI Composite | acc, 0.34 % | IBIS, EUR | 128562052 | 0.929 | ineligible (KID unknown) |

### cash — `euro_cash.level`

| Symbol | ISIN | Issuer | Index | Class | IBKR listing | Conid | ρ | Selector |
|---|---|---|---|---|---|---|---|---|
| XEON | LU0290358497 | Xtrackers | Solactive €STR +8.5 Daily | acc, 0.10 % | IBIS2, EUR | 46041702 | 0.993 | proposed |
| CSH2 | LU1190417599 | Amundi | €STR Compounded | acc, 0.10 % | SBF, EUR | 185829298 | unmeasured (19 monthly returns) | capability unverified (XPAR) |
| CSH1 | FR0010510800 | Amundi | Solactive Euro Overnight Return | acc, 0.10 % | SBF, EUR | 46501995 | unmeasured (no Yahoo series) | capability unverified (XPAR) |

## Partner pairs

Correlation of the two vehicles' monthly returns in EUR, measured as above. A partner must come
from another issuer (`es`, RIRPF art. 8; `tax-es.md`); the owner allows it to track the same index
(`config/owner.yaml`, D123); the floor is `min_correlation`.

| Role | Pair | ρ | Different issuer and index |
|---|---|---|---|
| world_equity | XDWD / VHVE, SPPW / VHVE, IWDA / VHVE | 0.996, 0.996, 0.992 | yes |
| world_equity | XDWD / SPPW | 1.000 | issuer only (both MSCI World): a partner since D123 |
| em_equity | XMME / VFEA, IEMA / VFEA, EMIM / VFEA | 0.943, 0.939, 0.932 | yes |
| em_equity | EMIM / XMME | 0.993 | yes |
| euro_govt_intermediate | DBXQ / MTB, X57E / MTB, CSBGE7 / MTB, CSBGE7 / DBXQ | 0.997, 0.991, 0.990, 0.986 | yes |
| euro_govt_long | DBXG / IBCL | 0.996 | yes |
| inflation_linked | TPSA / TIUP, TPSA / TIP1D, TIUP / TIP1D | 0.994, 0.958, 0.956 | TPSA / TIUP issuer only; the rest yes |
| us_treasuries | XUTD / VDTA, CSBGU0 / XUTD, CSBGU0 / VDTA | 0.996, 0.953, 0.942 | yes |
| gold | SGLD / 4GLD, IGLN / SGLD, IGLN / 4GLD | 0.998, 0.989, 0.986 | issuer only, except against 4GLD |
| broad_commodities | CMOD / UIQK, ICOM / UIQK | 0.932, 0.931 | yes |
| broad_commodities | CMOD / ICOM | 1.000 | issuer only (both Bloomberg Commodity) |
| cash | XEON / CSH2 | unmeasured | yes |

A hedged class and its unhedged sibling are the same issuer and correlate far less once converted
to EUR (IWDA / IWLE 0.918, TPSA / IBC5 0.385, CSBGU0 / IBB1 0.492): the hedge removes the dollar.

## Data notes

- **Xetra's segments.** Most of the Irish and Luxembourg funds' Xetra lines are `IBIS2` at IBKR;
  Amundi's TIUP, UBS's UIQK and Xetra-Gold are `IBIS` (`ibkr-capabilities.md`). Yahoo serves both
  under `.DE` (`research.yaml`).
- **Short or missing history.** The research loader asks Yahoo for IBKR's symbol with the venue's
  suffix; Yahoo has no `CSH1.PA` (Euronext's ticker is `CSH`), so CSH1 has no series. CSH2's Yahoo
  history starts on 2025-03-17 although the fund launched in 2015, too short for a reading.
- **KIDs not read.** ubs.com refuses this container, so TIP1D's and UIQK's KIDs are unknown and
  both are ineligible under `es` until a KID is confirmed. Xetra-Gold's KID and its fee at IBKR
  (custody is charged by the depositary bank, Clearstream's 0.30 % a year plus VAT passed on at the
  bank's discretion) were not read either. VERIFY-LIVE(TIP1D, UIQK and 4GLD KIDs).
- **UIQK's class.** IBKR describes conid 128562052 as "UBS ETF CMCI COMPOS. H. CHF" although its
  ISIN is the USD A-acc class. VERIFY-LIVE(UIQK's currency and share class).
- **The selection's ADR map was not ported** from 4c: the core's roles map to UCITS vehicles only.
