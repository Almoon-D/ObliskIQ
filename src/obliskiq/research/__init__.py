"""The research harness: point-in-time research history, the walk-forward simulator with costs
and tax, the statistics and decision rule of a pre-registered test, the trial registry and the
pre-registration lock, and the report (MASTER_PLAN §5.9; DECISIONS D60 to D64).

Its output can move capital, so it lives under every rule of ``src/``. The live decision path
never imports it, and it never imports execution or the broker (import contracts).

SPDX-License-Identifier: AGPL-3.0-or-later
"""
