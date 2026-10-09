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
| ES | Impuesto sobre las Transacciones Financieras, Ley 5/2020 | 0.2 %, purchases of shares of listed Spanish companies above EUR 1,000 million market capitalisation | 2021-01-16 | secondary sources citing the provision `[secondary ✓ 2026-10-09]`; the AEAT list of taxable issuers is published yearly, so the caller must say whether the issuer is on it (`needs_issuer_list`) |
| FR | Taxe sur les transactions financieres, CGI art. 235 ter ZD | 0.4 %, purchases, above EUR 1,000 million | 2025-04-01 | secondary sources `[secondary ✓ 2026-10-09]`; amendments to raise the rate again for 2026 were tabled and whether any passed was not established, so the rate may be higher. BOFiP BOI-ANNX-000467 lists issuers |
| IT | Financial transaction tax, Legge 228/2012 art. 1 c. 491–500 | **unknown** (`null`) | — | sources disagree: 0.1 % regulated / 0.2 % otherwise until 2025, and one secondary source reports 0.2 % / 0.4 % from 2026-01-01 (Legge 199/2025 c. 29); no official text was read |

- A country with no row is **not modelled**: the estimate says `applies: false` and the leg
  carries the label `tax_not_modelled`. That is not the same as untaxed, and it is not a gap
  that blocks a total; the label is what tells a reader the figure leaves a tax out. Countries
  and instrument classes (ETFs, ADRs, derivatives) with a tax this file does not hold are in
  that position.
- An issuer country the caller does not give is also reported, as `issuer country not given`.
- `VERIFY-LIVE(taxes)`: the ES, FR and IT rows need the official texts; the GB and HK rows are
  read from official sources but not from an executed trade's confirmation.

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
- A missing row, a missing tax rate and an unknown liquidity input are explicit statuses,
  never a silent default.
- Lite's open-interest gate is not part of costs: gates own it, and passing on a missing
  figure was dishonest degradation.
