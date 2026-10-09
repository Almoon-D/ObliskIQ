-- 006 — every snapshot row records the market-data state it was taken in.
--
-- Until now a row in chain_snapshots said WHEN it was collected and by WHICH
-- route, but not WHAT KIND OF QUOTE it held. That distinction was academic while
-- one collector ran on one schedule. It stops being academic the moment a second
-- capture route exists.
--
-- The scheduled collector runs at 22:30 UTC, deliberately after the US close, so
-- it records settled closing marks. That is the design, not a defect: the book
-- has stopped moving and the marks are final. A capture taken from a chat session
-- at some arbitrary hour is a different animal — it may be a live mid-session
-- quote, a pre-market quote that is really Friday's close, or a frozen one. All
-- three are legitimate data. What is NOT legitimate is storing them
-- indistinguishably in a series whose whole purpose is a per-strike implied-vol
-- history, because a volatility measured mid-session and one measured at the
-- close are not the same measurement, and nothing downstream could tell them
-- apart afterwards.
--
-- So the row carries IBKR's own flag, verbatim and unnormalised: whatever the
-- route reported (FROZEN, DELAYED, REALTIME, FROZEN_DELAYED, ...), or NULL where
-- it reported nothing. NULL means "the route said nothing", never "realtime" —
-- absence of a staleness flag is not evidence of freshness, and a migration that
-- back-filled 'REALTIME' onto existing rows would be inventing exactly the fact
-- this column exists to record.
--
-- Deliberately NOT a CHECK constraint. The set of values IBKR emits is not
-- known — it is being discovered — and a constraint written from today's
-- knowledge would reject tomorrow's observation, turning a new finding into a
-- collector crash on a day whose data cannot be recollected.
--
-- SPDX-License-Identifier: AGPL-3.0-or-later

ALTER TABLE chain_snapshots      ADD COLUMN market_data_status TEXT;
ALTER TABLE underlying_snapshots ADD COLUMN market_data_status TEXT;
ALTER TABLE curve_snapshots      ADD COLUMN market_data_status TEXT;
