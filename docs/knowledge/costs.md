# Costs

What the cost model in `quant/costs.py` is built from, and how far each number has been
checked. The values are in `config/defaults/costs.yaml`, each with its source and
`verify_live: true`; this file says what was and was not confirmed and what the estimator
does with a gap. **Not tax advice.** A rate here is an estimate for sizing and for refusing
trades that cost too much, never a statement of what an account owes.

Tag: `[<source> ✓ 2026-10-09]` = read from that source on that date. `VERIFY-LIVE(<what>)` =
not seen in an order's executions or on the broker's published schedule.

## Commission schedules

Chosen by (venue, asset class, currency); a combination with no row has **no schedule**, and
the estimator returns `no_schedule` instead of a number.

| Row | Schedule | Origin | Status |
|---|---|---|---|
| `us_stock` | USD 0.005 per share, minimum USD 1.00, at most 1 % of trade value | legacy `transaction_costs.py` @68280e1; IBKR fixed schedule | `VERIFY-LIVE(US stock commission)` |
| `us_option` | USD 0.65 per contract, minimum USD 1.00 | legacy `transaction_costs.py` @68280e1 (rate); lite `config.yaml` @b1ebd49 (minimum) | `VERIFY-LIVE(US option commission)`; exchange and regulatory fees of a few cents per contract are not included |
| `eurex_option` | EUR 1.50 per contract, no minimum | lite `config.yaml` @b1ebd49, documentation-derived | `VERIFY-LIVE(Eurex option commission)` |
| `us_treasury` | 0.002 % of face, minimum USD 5 | `docs/MASTER_PLAN.md` §5.3 | `VERIFY-LIVE(US Treasury commission)` |
| `eur_bond` | 0.1 % of face up to EUR 8,500, minimum EUR 1 | `docs/MASTER_PLAN.md` §5.3 | `VERIFY-LIVE(EUR bond tier)` |
| `europe_eur` | 0.05 % of value, minimum EUR 3.00, on Xetra, the LSE, Euronext Amsterdam and Paris, Borsa Italiana and SIX | IBKR's published page "Stocks, ETFs and Warrants – Europe", Fixed – IB SmartRouting `[IBKR ✓ 2026-10-20]` | `VERIFY-LIVE(European commissions)` |
| `europe_usd` | 0.05 % of value, minimum USD 4.00, on the LSE, Euronext Amsterdam and SIX | the same page | `VERIFY-LIVE(European commissions)` |
| `lse_gbp` | 0.05 % of value, minimum GBP 3.00, on the LSE | the same page (United Kingdom) | `VERIFY-LIVE(European commissions)` |
| `six_gbp`, `six_chf` | 0.05 % of value, minimum GBP 3.00 and CHF 5.00, on SIX | the same page (Switzerland) | `VERIFY-LIVE(European commissions)` |
| `spot_fx` | 0.20 basis point (0.002 %) of the trade value in USD, minimum USD 2.00 an order; charged in the base currency | IBKR's published page "Spot Currencies" (IDEALPRO), Fixed, Tier I up to USD 1 bn a month `[IBKR ✓ 2026-10-20]` | `VERIFY-LIVE(conversion commission)` |

- The European rows price **IB SmartRouting**, the way the order path routes every order
  (DECISIONS D97). The same page lists direct routing at 0.10 % of value with higher minimums
  (EUR 4.00, USD 6.00, GBP 4.00 on the LSE and 6.00 on SIX, CHF 6.00) and the tiered plan at
  0.05 % with a cap (EUR 29.00); fixed pricing includes the exchange, clearing and regulatory
  fees. The page's worked examples agree: 75 shares at EUR 80 cost EUR 3.00 smart-routed, 100
  shares at EUR 40 cost EUR 4.00 direct-routed `[IBKR ✓ 2026-10-16]`.
- A **conversion** is priced by `spot_fx` on its value in US dollars; the spot-currency page
  says the commission is converted to and charged in the base currency, and that a modified
  order counts as a cancellation and a replacement for the minimum, so each reprice of a
  conversion can cost another minimum `[IBKR ✓ 2026-10-20]`. Conversions are batched for that
  minimum (`execution.currency.min_conversion_base`). The research simulator does not price
  conversions yet (a follow-up in the ROADMAP).
- The schedule is chosen by the instrument's **listing venue**, never `SMART` (DECISIONS D67).

- The EUR bond row is defined only up to `up_to_face`; an order above it falls into a tier the
  file does not hold, and the estimator returns `beyond_tier` rather than extrapolating.
- Option rows apply to the venue the contract trades on. IBKR exchange codes map to venues in
  `venues.yaml`; US option exchanges such as CBOE are mapped to the US row by the venue
  profile (`VERIFY-LIVE(option-exchange → venue mapping)`).
- Money and rates are `Decimal` end to end; YAML holds them as quoted strings because a bare
  `0.005` is a float.

## Transaction taxes (by the issuer's country)

Transaction taxes follow the company, not the venue. Each row says what was read and how
well it was confirmed. A rate that could not be stated with confidence is `null`, and the
estimator returns `tax_unknown` — never zero. An estimate uses the rate as a conservative
upper bound (reliefs and exemptions are ignored) and carries the label `unverified` until
the row's `verify_live` is cleared.

| Country | Tax | Rate | From | Read |
|---|---|---|---|---|
| GB | Stamp duty reserve tax, Finance Act 1986 s.87 | 0.5 %, purchases | — | official: HMRC Stamp Taxes on Shares Manual STSM031010 `[HMRC ✓ 2026-10-09]`. Newly listed companies are relieved for three years from listing (HM Treasury, 2025-11-26); the estimate ignores the relief and errs high. |
| HK | Stamp duty, Cap. 117 | 0.1 % from each of buyer and seller | 2023-11-17 | official: IRD press release 2023-11-15 and HKEX circular CT 158/2023 `[IRD ✓ 2026-10-09]`. ETFs are exempt; the estimate ignores that and errs high. |
| ES | Impuesto sobre las Transacciones Financieras, Ley 5/2020 | 0.2 %, purchases of shares of listed Spanish companies above EUR 1,000 million market capitalisation | 2021-01-16 | official: Ley 5/2020 (BOE-A-2020-12356) arts. 2.1 and 7 `[BOE ✓ 2026-10-21]`; the AEAT list of taxable issuers is published yearly, so the caller must say whether the issuer is on it (`needs_issuer_list`) |
| FR | Taxe sur les transactions financieres, CGI art. 235 ter ZD | 0.4 %, purchases, above EUR 1,000 million | 2025-04-01 | secondary sources `[secondary ✓ 2026-10-09]`; amendments to raise the rate again for 2026 were tabled and whether any passed was not established, so the rate may be higher. BOFiP BOI-ANNX-000467 lists issuers |
| IT | Financial transaction tax, Legge 228/2012 art. 1 c. 491–500 | **unknown** (`null`) | — | Legge 199/2025 art. 1 c. 29 raises comma 491's first-sentence rate from 0.2 % to 0.4 % for transfers from 2026-01-01 (c. 31) `[Normattiva ✓ 2026-10-22]`; the rate on regulated markets and MTFs (until 2025 half the standard rate, per secondary sources) and the side that owes it are in Legge 228/2012's own text, which Normattiva did not serve that day, so no rate is entered |

- A country with no row is **not modelled**: the estimate says `applies: false` and the leg
  carries the label `tax_not_modelled`. That is not the same as untaxed, and it is not a gap
  that blocks a total; the label is what tells a reader the figure leaves a tax out. Countries
  and instrument classes (ETFs, ADRs, derivatives) with a tax this file does not hold are in
  that position.
- A share whose issuer country the caller does not give is `tax_unknown` and has no total;
  for any other asset class a missing country is labelled `tax_not_modelled`.
- `VERIFY-LIVE(taxes)`: the FR row needs its official text (Légifrance refused the container on
  2026-10-22) and the IT row Legge 228/2012's commi 491 and 494; the ES row was read in Ley 5/2020's
  BOE text (2026-10-21); the GB and HK rows are read from official sources but not from an executed
  trade's confirmation. None applies to the core's UCITS ETFs and ETCs: each taxes shares.

## Friction tiers (assumptions)

The legacy friction tiers — spread, slippage and square-root market impact for equities;
half-spread for options by liquidity tier — are kept as **labelled assumptions** in
`costs.friction`, not measurements. Session 18's transaction-cost analysis of the account's own
fills replaces them. The market-capitalisation threshold for "liquid" and the list of
penny-wide underlyings are inputs the legacy code held in its settings; an unknown market
capitalisation is treated as illiquid.

## Behaviour that differs from the legacy module

Each is a decision in `docs/DECISIONS.md` (D31).

- The legacy equity path booked a flat 0.0 for commission; the port charges the per-share
  schedule.
- A missing row, a missing tax rate and an unknown issuer country for a share are explicit
  statuses. A missing average daily value gives zero market impact without a label (a known
  gap, Session 18's cost analysis replaces it); an unknown market capitalisation takes the
  illiquid tier and is labelled `market_cap_unknown`.
- Lite's open-interest gate is not part of costs: gates own it, and passing on a missing
  figure was dishonest degradation.
