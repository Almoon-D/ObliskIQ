# Tax: Spain (`es`)

The rules the `es_irpf` policy (`src/obliskiq/jurisdiction/tax_es_irpf.py`) applies, the article
each comes from and the official text it was read in. The values are in
`config/jurisdictions/es.yaml`; this file says where they come from and what the engine does
with them. **Not tax advice.** The engine estimates the tax of a simulated or proposed trade so
that research and decisions treat it as a cost; it never states what an account owes.

Tag: `[BOE ✓ 2026-10-16]` = read in the consolidated text on the BOE's site on that date.

## Sources

- LIRPF: Ley 35/2006, de 28 de noviembre, del Impuesto sobre la Renta de las Personas Físicas,
  consolidated text `https://www.boe.es/buscar/act.php?id=BOE-A-2006-20764` `[BOE ✓ 2026-10-16]`
  (last amendment shown: 21 December 2024).
- RIRPF: Real Decreto 439/2007, de 30 de marzo, the regulation,
  `https://www.boe.es/buscar/act.php?id=BOE-A-2007-6820` `[BOE ✓ 2026-10-16]`.
- Orden HAC/277/2026, de 25 de marzo, the models and deadlines of the 2025 return,
  `https://www.boe.es/buscar/act.php?id=BOE-A-2026-7041` `[BOE ✓ 2026-10-16]`.

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
| Withholding at source | Spain–United States convention of 22 February 1990, art. 10.2.b, as amended by the 2013 protocol (in force 27 November 2019) | 15 % on portfolio dividends | `withholding_at_source` row US 0.15, confirmation `secondary`: the official text was not read. A country without a row cannot be simulated |

## What is not modelled

- Currency gains on foreign-currency balances (Session 7, DECISIONS D69): the simulator holds no
  currency balance.
- The personal and family minimum, regional deductions, and the part of the savings base that a
  negative general base could change.
- Spanish withholding on account (19 %) on Spanish-paid income: IBKR is not a Spanish payer.
- Foreign-asset reporting (Modelo 720) and the wealth tax.

VERIFY-LIVE(US treaty rate against the BOE text) and VERIFY-LIVE(tax-free fund switches at IBKR)
are queued in `docs/ROADMAP.md`. The rate table is re-checked every year (MASTER_PLAN §5.16).
