-- 011 — the ledger records what a position IS, and what currency its money is in.
--
-- Two defects, one schema gap behind both.
--
-- 1. WHICH SLEEVE A POSITION BELONGS TO WAS NOT STORED.
--
--    position_ledger held no is_option, so log_exit took it from the caller and
--    defaulted it to true. Closing an EQUITY position without saying so booked
--    an option-sleeve penalty of loss_penalty_multiple x loss against a loss
--    that never touched the sleeve; the opposite mistake let an option loss
--    escape the penalty entirely. Nothing in the store could detect either
--    afterwards, because nothing in the store knew which the position was.
--
--    NULL is a third state and not a default: it means the row was written
--    before this migration, when the fact was not recorded. log_exit refuses to
--    guess on such a row and asks the caller, rather than assuming the value
--    that happens to be more common. Every row written from now on states it at
--    entry, where the guard already knows the answer.
--
--    The option terms travel with it — contracts, strike, right, expiry — for the
--    same reason. check_position_risk's docstring already promised to default
--    the strike "from the ledger's contract terms", and there were none to
--    default from.
--
-- 2. MONEY WAS MIXED ACROSS CURRENCIES.
--
--    The affordability GATE converts a premium into base currency before
--    comparing it against the sleeve (compute/gates.py::_cost_in_base). The
--    WRITE did not: log_entry booked premium x multiplier x contracts in the
--    CONTRACT's currency, and PenaltyLedger.committed_premium summed those raw
--    figures straight out of a budget denominated in account.base_currency. A
--    non-base option therefore overstated or understated available budget by
--    the exchange rate, in the one direction the gate exists to refuse. Every
--    test used the base currency, so nothing saw it.
--
--    From here the money columns on option_ledger are base currency, always, and
--    the quoted figure is kept beside them so the conversion can be audited
--    rather than reconstructed: what was stated, in which currency, at which
--    rate. A row with amount_currency NULL is one written before this migration
--    or one already in base.
--
-- Both tables gain columns only — no rebuild — so trg_position_thesis_frozen and
-- every existing index survive untouched.

PRAGMA foreign_keys = ON;

-- ── which sleeve, and on what terms ──────────────────────────────────────────

-- 1 = option, 0 = cash equity/fund, NULL = written before this migration and
-- therefore unknown. Never defaulted: see the header.
ALTER TABLE position_ledger ADD COLUMN is_option INTEGER
    CHECK (is_option IS NULL OR is_option IN (0, 1));

-- Contract count at entry. What the position committed to the shared sleeve is
-- entry_price x multiplier x contracts, and until now that arithmetic lived
-- only in the event note.
ALTER TABLE position_ledger ADD COLUMN contracts INTEGER
    CHECK (contracts IS NULL OR contracts > 0);

ALTER TABLE position_ledger ADD COLUMN strike REAL
    CHECK (strike IS NULL OR strike > 0);

-- 'C' or 'P'. Named option_right because RIGHT is a join keyword in SQLite 3.39+.
ALTER TABLE position_ledger ADD COLUMN option_right TEXT
    CHECK (option_right IS NULL OR option_right IN ('C', 'P'));

ALTER TABLE position_ledger ADD COLUMN expiry TEXT;

-- The currency entry_price is quoted in, from the instrument's universe row.
ALTER TABLE position_ledger ADD COLUMN entry_currency TEXT;

-- ── the money, and the conversion behind it ──────────────────────────────────

-- premium_paid, proceeds, realised_pnl, penalty_added and penalty_balance_after
-- are ALL in account.base_currency. These three record what the figure was
-- before conversion, so a booked amount can be checked against the rate that
-- produced it years later.
ALTER TABLE option_ledger ADD COLUMN amount_currency TEXT;
ALTER TABLE option_ledger ADD COLUMN amount_quote REAL;
ALTER TABLE option_ledger ADD COLUMN fx_to_base REAL
    CHECK (fx_to_base IS NULL OR fx_to_base > 0);
