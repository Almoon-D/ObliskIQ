-- 012 — a ranked row carries the state of the cell behind it.
--
-- `_rank` admits cells with status `ok` OR `stale`, which is right: a stale
-- reading is still the best available and hiding it would make the table look
-- healthier than the store is. But `grid_rankings` had nowhere to record which
-- it was, so the four ranked views — the tables a session actually reads,
-- rather than the ~5,800 raw cells — ordered stale cells beside fresh ones with
-- nothing to tell them apart.
--
-- SKILL.md requires anything frozen, delayed or stale to be labelled indicative
-- in EVERY table it appears in. This is the table that was exempt in practice.
--
-- `market_data_status` travels for the same reason: the grid cell records the
-- quote state its inputs were captured in, and a ranked row built from a FROZEN
-- quote is an indicative row.
--
-- `conditions` is the other half. SKILL.md defines *coiled* and *extended* as
-- FOUR conditions each; the ranked views order on ONE named indicator, so the
-- ordering stays auditable — a composite score nobody can reproduce is the
-- thing this design refuses. What was missing is the rest of the definition
-- travelling with the row, so `most_compressed` cannot be read as "coiled" when
-- only compression was tested. It holds a small JSON object: each condition, and
-- whether it held, did not, or could not be measured.
--
-- Columns only, so the primary key and every existing row survive.

PRAGMA foreign_keys = ON;

-- ok | stale, as the grid cell was stamped.
ALTER TABLE grid_rankings ADD COLUMN status TEXT;

-- FROZEN | DELAYED | REALTIME | NULL where the route reported nothing, carried
-- through from the cell. NULL is unreported, never "realtime".
ALTER TABLE grid_rankings ADD COLUMN market_data_status TEXT;

-- JSON: {"condition": true|false|null}. null means the store could not measure
-- it, which is not the same as false and is never reported as one.
ALTER TABLE grid_rankings ADD COLUMN conditions TEXT;
