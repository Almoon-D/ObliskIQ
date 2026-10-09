-- 014 — a ranked view carries the denominator it ranked over.
--
-- The four views are written and read the same way whatever they ordered. That
-- is fine for `most_extended`, which ranks every cell with bars, and misleading
-- for `widest_iv_hv`, which ranks the cells with a reachable OPTION CHAIN — two
-- of a hundred and fifty-six before this session mapped the US sector SPDRs.
-- A table of two rows and a table of a hundred rows looked identical, and the
-- only way to tell them apart was to read universe.yaml and work out which rows
-- set `collect_chain`.
--
-- Migration 012 fixed the same shape of problem for `status`: the fact existed
-- in the builder and had nowhere to live on the row. These two columns are the
-- family's reach, carried beside every row of the view it produced.
--
--   reachable    market x sector cells where this view's family is not
--                structurally inapplicable — what the view COULD rank.
--   ranked_over  cells that actually held a usable value — what it DID rank.
--
-- Reported, never used to suppress: a view below grid.min_cells_for_ranked_view
-- is labelled with its count rather than deleted, because deleting it would
-- throw away the only implied-vol signal the store holds while a label costs
-- nothing and says exactly what the reader is looking at.
--
-- Columns only, so the primary key and every existing row survive.

PRAGMA foreign_keys = ON;

-- Cells this view's family can reach at all. NULL on rows written before this
-- migration: unrecorded, which is not the same as zero.
ALTER TABLE grid_rankings ADD COLUMN reachable INTEGER;

-- Cells that held a usable value on this session, so ranked_over <= reachable.
ALTER TABLE grid_rankings ADD COLUMN ranked_over INTEGER;
