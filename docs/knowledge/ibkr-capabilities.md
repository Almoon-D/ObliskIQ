# IBKR capabilities through the Web API

What the broker does through the Client Portal Web API, per asset class and venue: whether a
contract is found by IBKR's contract search, quoted by a snapshot, and an order on it accepted by a
preview (`/iserver/account/{id}/orders/whatif`; nothing placed), with the order types and algos its
contract rules list and the trading permission an accepted preview shows. The readings the code
acts on are `config/defaults/capabilities.yaml` (`broker_capabilities`); the vehicle selector never
proposes a vehicle whose search, quote and order are not all verified there (DECISIONS D119).
Tags as in `README.md`; a check is verified only on the CP route.

## How a capability is read

`obliskiq universe capabilities --probe`, on the deployment machine in paper (SESSION_PROMPTS §A.5),
walks each configured check's probe instrument: contract search to the pinned conid, a snapshot
with the availability flag (6509) and the trading-permission field (7768, IBKR's field table,
VERIFY-LIVE(permission field values)), the contract rules, the algos, and a preview of a limit buy
of one size increment 5 % below the bid. For an option check it first resolves one call of the
underlying's nearest month at the strike nearest its price. The preview is the only order endpoint
touched, under the paper guard. Its report prints the readings to record in the configuration and
here. VERIFY-LIVE(capability probe on the paper account).

## Readings

| Asset class on venue | Search | Quote | Order (whatif) | Evidence |
|---|---|---|---|---|
| stock on XNYS | ✓ | ✓ | ✓ | FCX, conid 7089: resolved, a bid read (availability unreported), a limit buy previewed at commission 1.00 USD, no warning `[CP ✓ 2026-10-21]` (T1, paper) |
| stock on XASX | ✓ | ✓ | ✓ | BHP, conid 4036812 in AUD: resolved, quote delayed (`ZB` frozen after the close), a limit buy previewed at commission 6 AUD with IBKR's warning 4, text not recorded `[CP ✓ 2026-10-21]` (T1, paper, a config copy; BHP is not in the universe) |
| etf on XETR | ✓ | ✓ | ✓ | EXV1, conid 89005163 on `IBIS`: resolved, quote frozen after the close, a limit buy previewed at commission 3 EUR, no warning `[CP ✓ 2026-10-20, 2026-10-21]` (T1, paper). The Irish funds' Xetra lines list on `IBIS2` (below), not yet read on CP |
| etf on XNYS | ✓ | ✓ | – | SPY on ARCA, conid 756733: resolved, delayed (`DP`, 1200 s) `[CP ✓ 2026-10-21]`; never previewed (data and an option underlying under `es`) |
| stock on XAMS | ✓ | ✓ | – | ASML 117589399 on AEB with option sections EUREX and FTA; its snapshot `DPB` `[CP ✓ 2026-09-10]` (lite, live account, OAuth) |
| stock on XMAD, XPAR | ✓ | – | – | SAN 30314144 on BM, TTE 29612193 on SBF, with their option sections `[CP ✓ 2026-09-10]` (lite) |
| option on XNYS (US) | ✓ | ✓ | – | SPY's sections as a JSON body; US option quotes through the day and after the close `[CP ✓ 2026-09-10]` (lite) |
| option on XEUR (Eurex) | ✓ | ✓ | – | option sections on EUREX, FTA, MEFFRV, MONEP, strikes served on EUREX; European chains priced while open `[CP ✓ 2026-09-10..14]` (lite) |
| etf on XLON, XAMS, XMIL, XSWX, XPAR; etc on XLON, XMIL, XETR | – | – | – | listed in IBKR's contract search `[MCP ✓ 2026-10-22]` (below); not read on CP |

Order types and algos: the contract rules listed 12 order types and 10 algos for SPY and 9 order
types for BHP `[CP ✓ 2026-10-21]`, but their names were not recorded; `LMT` is the one previewed.
VERIFY-LIVE(order types and algos per class, by name).

Trading permissions: an accepted preview shows the account holds the venue's permission
(`venues.yaml: trading_permission`): Stocks: United States (XNYS), Stocks: Australia (XASX) and
Stocks: Germany (XETR) are held on the paper account `[CP ✓ 2026-10-21]`; every other venue is
unverified until a preview there is accepted.

What the selector may therefore propose today: shares on the NYSE and on ASX, and ETFs on Xetra.
Every core role's Xetra-listed UCITS ETF passes; gold (ETCs) and broad commodities (LSE listings)
have no verified capability until the probe runs.

## IBKR's contract search through the connector (2026-10-22)

Read through IBKR's MCP connector, by ISIN or symbol, for the core's candidates
(`docs/knowledge/universe-sources.md`) `[MCP ✓ 2026-10-22]`:

- **Xetra's ETF segment is `IBIS2`.** The Xetra lines of Xtrackers (XDWD, XMME, DBXQ, X57E, DBXG,
  XUTD, XEON), State Street SPDR (SPPW), Vanguard (VFEA) and iShares' Irish funds (IWLE, IBCL, IBC5,
  IBB1) answer on `IBIS2`; German-domiciled iShares funds (EXV1 and the sector series) and
  Amundi's TIUP, UBS's UIQK and Xetra-Gold answer on `IBIS`. Both are Xetra (`ibkr_exchanges`).
  VERIFY-LIVE(IBIS2 on CP).
- **The search does not return every listing.** By ISIN or ticker it returned no Xetra line of
  iShares Core MSCI World (EUNL), Core EM IMI (IS3N), € Govt 3-7yr (SXRP), $ TIPS (IUST),
  $ Treasury 7-10yr (SXRM), Physical Gold (PPFB) or Vanguard FTSE Developed World (VGVF), all of which
  justETF lists on Xetra; the universe uses the listings the search does return (another venue or
  currency) rather than guess a conid.
- **A ticker can differ from the exchange's.** Euronext Paris lists Amundi EUR Overnight Return as
  `CSH`; IBKR's symbol is `CSH1`.
- **A description can name another class.** IE00B53H0131 (UBS CMCI Composite SF, USD A-acc) answers
  on `IBIS` as `UIQK`, described "UBS ETF CMCI COMPOS. H. CHF". VERIFY-LIVE(UIQK's currency and share
  class through /iserver/contract/{conid}/info).
- The connector paces searches at 10 a second and 30 a minute ("Rate limit reached").
