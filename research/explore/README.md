# Exploration

Scripts here are exploration: they try ideas, plot data and size effects before anything is
pre-registered. They are outside the coverage floor and are **never evidence** (DECISIONS D64):
only a run of the harness (`src/obliskiq/research/`) on a merged, locked pre-registration counts.

- An exploratory run of a candidate specification goes through
  `obliskiq research run <file> --explore`, so the trial registry counts it; the deflated Sharpe
  of the confirmation run is taken across every trial it holds. Exploration that bypasses the
  harness is not counted, which is why it cannot be cited.
- Read data from the research store (`obliskiq research data load`), never from an ad-hoc
  download: a figure without a series hash cannot be reproduced.
- A result found here is a reason to write a pre-registration
  (`research/preregistrations/TEMPLATE.md`), not a result.
