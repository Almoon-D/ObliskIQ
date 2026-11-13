# Hunter feasibility (Session 23)

The scripts behind `research/reports/hunter-feasibility.md`. They are exploration (DECISIONS
D64): they count stocks, filings and filers and compute power before data; none reads a return
of any candidate. Run them with the same output directory, outside the repository, with
`SEC_USER_AGENT` set for the SEC's fair-access rule:

1. `universe.py <out dir>`: today's listed US common stocks (Nasdaq's screener) and their median
   daily traded value over twenty sessions (Yahoo's bars).
2. `floors.py <out dir>/universe.json <eurusd> <repo>`: the floors computed from the account's
   configuration and the stocks passing them, by size tier.
3. `xbrl_listed.py <out dir>/universe.json <out dir>`: the share of those stocks whose periodic
   reports of the last four quarters carry XBRL.
4. `edgar_index.py <out dir> 2001 2026`: by year, periodic filers, those with XBRL, 8-K filers
   and Form 10 registrations, from EDGAR's full indexes.
5. `earnings_dates.py <out dir>`: in a seeded sample of filers, the share of periodic reports
   preceded by an 8-K earnings release (item 2.02).
6. `power.py <repo>` (with the project's environment): the supported effects and the
   probability of a sufficient outcome for the fixed sequence.

`results/` holds the summaries the report cites. Yahoo and the screener serve today's listings,
so a later run counts a different universe.
