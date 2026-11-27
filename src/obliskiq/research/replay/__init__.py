"""The analyst replayed after its models' training cutoff (H15; DECISIONS D191, D199).

``research/preregistrations/analyst-replay.md`` fixes everything; ``plan`` reads it with the
configuration and refuses what it does not allow (a model without a documented cutoff, a date
inside the margin); ``sources`` fills the replay's own store from sources that date their own
publication; ``prices`` reads the returns and the universe at a sheet date; ``probe`` asks each
model what it remembers; ``run`` replays the segments, resumably and inside the budget; ``grade``
grades the calls with the idea ledger's grader and writes the report. Nothing here reaches
capital, a size, a slot or the gate, and nothing reads the forward record.

SPDX-License-Identifier: AGPL-3.0-or-later
"""
