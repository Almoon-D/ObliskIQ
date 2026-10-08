-- 015 — CFTC positioning becomes a share of open interest, so the rows written
-- under the old units go.
--
-- The value stored for a `cftc` series was a raw contract count, net
-- non-commercial, units `contracts_net_noncommercial`. Both of its consumers —
-- compute/indicators.py's `positioning` family and commodity_engine's
-- `positioning()` — rank it as a percentile of its own multi-year window.
--
-- Open interest is not constant over such a window. WTI's net non-commercial
-- count ranges 39,800 to 739,097 across ten years of the series this repository
-- collects, so a percentile of the raw count substantially ranks how large the
-- market became rather than how crowded it is — which is not what
-- `commodities.cot.extreme_percentile` or SKILL.md claim it measures.
-- `open_interest_all` arrives in the same Socrata row and was being discarded.
--
-- The fetcher now stores (long - short) / open_interest, units
-- `net_noncommercial_share_of_open_interest`. That makes the rows already in the
-- store incompatible with the rows the next run writes, and a percentile taken
-- across a units change is not a percentile of anything. So they are deleted
-- rather than migrated: CFTC is free, keyless and serves full history, so the
-- next `obliskiq-collect macro` refetches every one of them at no cost. There is
-- no arithmetic that could convert them in place — open interest was never
-- collected, so the denominator for a stored row does not exist.
--
-- macro_fetch_log goes with them. It is what
-- `macro.collection.refetch_after_seconds` reads to decide a series was pulled
-- recently enough to skip; leaving it would make the next run skip the refetch
-- and leave the series empty until the window lapsed.
--
-- Deliberately keyed on source rather than on units, so a row that somehow
-- carried neither is removed too. A CFTC row this migration left behind is a row
-- whose units nothing can establish.

PRAGMA foreign_keys = ON;

DELETE FROM macro WHERE source = 'cftc';

DELETE FROM macro_fetch_log WHERE source = 'cftc';
