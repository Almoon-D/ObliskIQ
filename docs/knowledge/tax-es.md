# Tax: Spain (`es`)

The rules the `es_irpf` policy (`src/obliskiq/jurisdiction/tax_es_irpf.py`) applies, the article
each comes from and the official text it was read in. The values are in
`config/jurisdictions/es.yaml`; this file says where they come from and what the engine does
with them. **Not tax advice.** The engine estimates the tax of a simulated or proposed trade so
that research and decisions treat it as a cost; it never states what an account owes.

Tag: `[BOE ✓ 2026-10-16]` = read in the consolidated text on the BOE's site on that date;
`[secondary]` = known only through a secondary source, the official text not read.

## Sources

- LIRPF: Ley 35/2006, de 28 de noviembre, del Impuesto sobre la Renta de las Personas Físicas,
  consolidated text `https://www.boe.es/buscar/act.php?id=BOE-A-2006-20764` `[BOE ✓ 2026-10-16]`
  (last amendment shown: 21 December 2024); arts. 14.2.e, 33.1, 33.5.e and 46.b read again for
  currency gains `[BOE ✓ 2026-10-21]`.
- RIRPF: Real Decreto 439/2007, de 30 de marzo, the regulation,
  `https://www.boe.es/buscar/act.php?id=BOE-A-2007-6820` `[BOE ✓ 2026-10-16]`.
- Orden HAC/277/2026, de 25 de marzo, the models and deadlines of the 2025 return,
  `https://www.boe.es/buscar/act.php?id=BOE-A-2026-7041` `[BOE ✓ 2026-10-16]`.
- The Spain–United States convention of 22 February 1990,
  `https://www.boe.es/buscar/act.php?id=BOE-A-1990-30940`, and its protocol of 14 January 2013,
  `https://www.boe.es/diario_boe/txt.php?id=BOE-A-2019-15166` `[BOE ✓ 2026-10-21]`.
- Consultas vinculantes of the Dirección General de Tributos on foreign-currency balances:
  V2422-20, V0463-21, V1613-25 (15 September 2025) and V0152-26 (27 January 2026) `[secondary]`:
  the DGT's search (`petete.tributos.hacienda.gob.es`) did not answer from the session's network
  on 2026-10-21 (timeouts and 502). The engine rests on the articles of the law below; the
  rulings' official texts are queued as VERIFY-LIVE in `docs/ROADMAP.md`.

## The rules

| Rule | Article | What it says | What the engine does |
|---|---|---|---|
| FIFO | LIRPF art. 37.2; art. 94.1.a for collective investments | "cuando existan valores homogéneos se considerará que los transmitidos por el contribuyente son aquéllos que adquirió en primer lugar" | lots are consumed oldest first (`lot_method: fifo`) |
| Homogeneity | RIRPF art. 8 | securities of one issuer, of one financial operation or unit of purpose, of the same nature and transmission regime, with substantially similar rights and obligations | applied conservatively: the same ISIN, or the same issuer and the same tracked index (two share lines of one company, or an accumulating and a distributing class of one ETF, are homogeneous) |
| Two-month rule | LIRPF art. 33.5.f (listed), 33.5.g (unlisted: one year) | a loss on listed securities is not computed when homogeneous securities were acquired in the two months before or after the sale; it is integrated as the securities still held are sold | a loss sale defers the loss on as many units as homogeneous units bought in the window and still held; the rest waits for a purchase until the window after the sale closes; the deferred loss rides on those units and is released, in proportion, when they are sold. A unit takes one loss |
| Acquisition and transfer value | LIRPF art. 35 | the acquisition value includes the costs and taxes inherent in the acquisition; the transfer value is net of those of the transfer | a lot's cost includes the leg's commission, transaction tax, spread and slippage; proceeds are net of them |
| Savings base | LIRPF art. 46 | capital income (dividends, interest) and gains from transfers form the savings base | the ledger keeps the two components per year |
| Offsets and carry-forward | LIRPF art. 49 | each component nets within itself; a negative balance offsets up to 25 % of the other's positive balance in the same year; what remains negative is offset in the four following years in the same order, at the maximum each year allows | the 25 % allowance is shared by the year's own and the carried negatives (the conservative reading); a balance older than four years expires |
| Savings scale | LIRPF art. 66.1 (state) and art. 76 (regional), as amended by Ley 7/2024 disp. final 7 from 1 January 2025 | 9.5 + 9.5 % to EUR 6,000; 10.5 + 10.5 % to 50,000; 11.5 + 11.5 % to 200,000; 13.5 + 13.5 % to 300,000; 15 + 15 % above | the combined scale 19 / 21 / 23 / 27 / 30 %, recorded for tax years 2025–2026 and verified 2026-10-16; the personal and family minimum is not applied (the tax errs high); a year outside the table's years is labelled |
| Foreign-tax credit | LIRPF art. 80 | the lesser of the tax paid abroad and the average effective rate (two decimals) on the income taxed abroad | applied to the year's foreign income |
| Payment | LIRPF art. 97; Orden HAC/277/2026 art. 7.1 | the 2025 return is filed and its tax paid from 8 April to 30 June 2026; a 60 / 40 split defers 40 % to 5 November | a year's tax is paid in full on 30 June of the following year (the split is not used, so the payment is never later than the law allows) |
| Fund switches | LIRPF art. 94.1.a | a switch between investment funds defers the gain, except for listed funds (ETFs) and when the proceeds are made available to the taxpayer | ETF switches are sales; a switch between unlisted funds is deferred only when `fund_switch_deferral_at_broker` is true, false until verified at IBKR |
| Withholding at source | Spain–United States convention of 22 February 1990 (BOE-A-1990-30940), art. 10.2.b as replaced by art. IV of the protocol of 14 January 2013 (BOE-A-2019-15166, in force 27 November 2019) | "15 por ciento del importe bruto de los dividendos en los demás casos" (5 % for a company holding at least 10 % of the votes) | `withholding_at_source` row US 0.15, confirmation `official` `[BOE ✓ 2026-10-21]`. A country without a row cannot be simulated |

## Currency gains

Foreign currency held in the account is a patrimonial element of its own: each time it leaves the
account's balance (converted back to euros, or paid for a security) a gain or loss arises against
what it cost in euros. **Not tax advice.**

| Rule | Article | What it says | What the engine does |
|---|---|---|---|
| A currency balance's change is a gain or loss | LIRPF art. 33.1 | "Son ganancias y pérdidas patrimoniales las variaciones en el valor del patrimonio del contribuyente que se pongan de manifiesto con ocasión de cualquier alteración en la composición de aquél" | each currency is a security of its own (`CCY:USD`); buying it opens a lot at the euros paid, spending it closes lots at the euros it is worth that day `[BOE ✓ 2026-10-21]` |
| When it is imputed | LIRPF art. 14.2.e | "Las diferencias positivas o negativas que se produzcan en las cuentas representativas de saldos en divisas o en moneda extranjera, como consecuencia de la modificación experimentada en sus cotizaciones, se imputarán en el momento del cobro o del pago respectivo." | a gain or loss arises only when the currency is paid out (a conversion back, or a purchase paid in it), never on a revaluation `[BOE ✓ 2026-10-21]` |
| Savings base | LIRPF art. 46.b | "Las ganancias y pérdidas patrimoniales que se pongan de manifiesto con ocasión de transmisiones de elementos patrimoniales" | currency results join the gains component with the securities' `[BOE ✓ 2026-10-21]` |
| Repurchase of what was sold at a loss | LIRPF art. 33.5.e | not computed are losses "derivadas de las transmisiones de elementos patrimoniales, cuando el transmitente vuelva a adquirirlos dentro del año siguiente a la fecha de dicha transmisión. Esta pérdida patrimonial se integrará cuando se produzca la posterior transmisión del elemento patrimonial." | a currency loss is deferred onto the same currency bought within the following twelve months (none before the sale; art. 33.5.f's two months apply to listed securities, not to currency) and released when those units are spent: the conservative reading, since currency is bought again almost always `[BOE ✓ 2026-10-21]` |
| Which units leave first | LIRPF art. 37.2 by analogy; the DGT's rulings above `[secondary]` | the rulings value each conversion at the rate the currency was acquired at, first in first out | currency lots are consumed oldest first (`currency_gains.lot_method: fifo`) |
| A sale in dollars, then a conversion | V0152-26 `[secondary]` | the sale of the security and the later conversion of its dollars are two results | a security sold in dollars realises its result in euros at the trade date's rate and opens a dollar lot at that value; converting the dollars later realises the currency's own result |

The values are `tax.currency_gains` in `config/jurisdictions/es.yaml` (`kept_as_lots`,
`lot_method`, `loss_reacquisition_months`, `source`). The rate of a fill's day is the ledger's,
recorded by the cycle's NAV step and the heartbeat (`fx_rates`); a fill whose day has no
recorded rate is taken once the day's rate is recorded, or marked incomplete when its day passed
without one. Currency the account held before the bot's first fill has no lots: spending it
is noted and its result not computed.

## What is not modelled

- Dividends, interest and withholding on the live path: the live lots take fills; income enters
  the ledger once Session 7's cash classification feeds it (a follow-up).
- Currency held before the bot's first fill and positions bought before it (no opening lots yet).
- The personal and family minimum, regional deductions, and the part of the savings base that a
  negative general base could change.
- Spanish withholding on account (19 %) on Spanish-paid income: IBKR is not a Spanish payer.
- Foreign-asset reporting (Modelo 720) and the wealth tax.

VERIFY-LIVE(tax-free fund switches at IBKR) and VERIFY-LIVE(the DGT's currency rulings in their
official text) are queued in `docs/ROADMAP.md`. The rate table is re-checked every year
(MASTER_PLAN §5.16).
